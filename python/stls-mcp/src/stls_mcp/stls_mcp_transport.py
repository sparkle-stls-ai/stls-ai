"""
stls_mcp_transport.py
----------------------
Client-side: custom httpx transport that transparently encrypts/decrypts
requests to MCP_STLS_PATH, for use with
mcp.client.streamable_http.streamablehttp_client(httpx_client_factory=...).

Usage flow:

    1. One-time, synchronous STLS handshake against /stls/handshake, to
       obtain session_id + session_key. See perform_client_handshake().
    2. Build the encrypted transport with that session.
    3. streamablehttp_client(url=".../mcp-stls", httpx_client_factory=...)
       uses the transport for every request — completely transparently
       to the rest of the MCP client stack (ClientSession, etc.), which
       keeps seeing plaintext JSON-RPC.

Interception happens at the httpx.AsyncBaseTransport level, below the
MCP application layer: StreamableHTTPTransport (the SDK layer handling
sessions, SSE, reconnection) has no idea it's talking to an encrypted
endpoint — it sees "normal" HTTP requests/responses, simply routed
through a transport that encrypts their body.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from stls_core import HEADER_SESSION_ID, HandshakeRequest, verify_handshake_proof
from stls_core.stls_core_crypto import StlsTransport
from stls_core.stls_core_handshake import HandshakeError, HandshakeResponse

logger = logging.getLogger(__name__)


class StlsHandshakeFailed(Exception):
    """Raised if the client-side handshake fails or the proof is invalid."""


async def perform_client_handshake(
    base_url: str,
    session_key: bytes,
    *,
    client_id: str,
    kid: str | None = None,
    handshake_path: str = "/stls/handshake",
    httpx_client: httpx.AsyncClient | None = None,
) -> tuple[str, StlsTransport]:
    """
    Performs the STLS handshake against the server and returns a ready
    session.

    Args:
        base_url:       base URL of the server (e.g. "https://my-server.example.com").
        session_key:     session key the client already possesses (resolved
                        from its own KeySessionProvider — base or arqit_py —
                        exactly like the server side). Never transmitted:
                        the handshake only proves mutual possession via an
                        HMAC proof.
        client_id:       client identifier, passed in the handshake request
                        (for server-side audit/logging purposes only).
        kid:             optional kid the caller already resolved alongside
                        session_key (e.g. via KeySessionProvider.getSessionKeyIdentifier()
                        for arqit_py) — passed as a hint so the server
                        resolves THIS exact key directly instead of a plain
                        "next available" lookup, which is not safe once more
                        than one handshake can be in flight concurrently
                        against the same long-running server-side provider
                        instance (see HandshakeRequest.kid's own docstring
                        in stls_core_handshake.py). Leave unset for base
                        (nothing to disambiguate) or when unavailable.
        handshake_path:  path of the handshake endpoint (default "/stls/handshake").
        httpx_client:    optional httpx client to reuse; if None, a
                        temporary one is created just for the handshake.

    Returns:
        (session_id, transport): session_id to use in the
        X-STLS-Session-ID header of every subsequent request, and an
        StlsTransport already ready to encrypt/decrypt with that session.

    Raises:
        StlsHandshakeFailed: if the HTTP handshake fails or the proof
                             returned by the server doesn't match the
                             session_key held by the client (meaning the
                             server doesn't hold the same key — possible
                             attack or misconfiguration).
    """
    request = HandshakeRequest.generate(client_id=client_id, kid=kid)
    url = base_url.rstrip("/") + handshake_path

    owns_client = httpx_client is None
    client = httpx_client or httpx.AsyncClient()
    try:
        resp = await client.post(url, json=request.model_dump())
    except httpx.HTTPError as exc:
        raise StlsHandshakeFailed(f"HTTP handshake failed against {url}: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()

    if resp.status_code != 200:
        raise StlsHandshakeFailed(
            f"Handshake rejected by server (status={resp.status_code}): {resp.text}"
        )

    try:
        response = HandshakeResponse.model_validate(resp.json())
    except Exception as exc:
        raise StlsHandshakeFailed(f"Invalid handshake response: {exc}") from exc

    if not verify_handshake_proof(session_key=session_key, nonce=request.nonce, response=response):
        raise StlsHandshakeFailed(
            "Invalid proof of possession — the server does not appear to hold "
            "the same session key as the client. Handshake rejected for safety."
        )

    logger.info("STLS-MCP handshake completed: session_id=%s", response.session_id)

    transport = StlsTransport(session_key=session_key, session_id=response.session_id)
    return response.session_id, transport


class StlsHttpxTransport(httpx.AsyncBaseTransport):
    """
    httpx.AsyncBaseTransport that transparently encrypts/decrypts
    requests to a specific STLS path, letting everything else pass
    through unchanged (including GET requests used for the SSE
    long-poll, where body encryption doesn't apply).

    Args:
        session_id:    STLS session already established via perform_client_handshake().
        transport:      StlsTransport already ready for that session.
        stls_path:      path to intercept and encrypt (default "/mcp-stls").
                       Requests to other paths pass through unmodified.
        inner_transport: the real underlying httpx transport, used to
                       actually perform the HTTP request after
                       (de)encryption. If None, a default
                       httpx.AsyncHTTPTransport is created.
    """

    def __init__(
        self,
        session_id: str,
        transport: StlsTransport,
        *,
        stls_path: str = "/mcp-stls",
        inner_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._session_id = session_id
        self._transport = transport
        self._stls_path = stls_path
        self._inner = inner_transport or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != self._stls_path or request.method != "POST":
            # Pass-through for any request not aimed at the STLS path
            # (e.g. GET for an SSE stream, requests to other endpoints).
            return await self._inner.handle_async_request(request)

        # ── Encrypt the outgoing body ───────────────────────────────────────────
        try:
            plaintext_payload = json.loads(request.content)
            envelope = self._transport.encrypt_json(plaintext_payload)
        except Exception as exc:
            logger.exception("StlsHttpxTransport: error encrypting request")
            raise httpx.TransportError(f"STLS encryption error: {exc}") from exc

        encrypted_body = json.dumps(envelope).encode("utf-8")

        # Native MCP headers (mcp-session-id, accept, etc.) are preserved;
        # we only add the STLS session header and fix up content-length.
        headers = httpx.Headers(request.headers)
        headers[HEADER_SESSION_ID] = self._session_id
        headers["content-length"] = str(len(encrypted_body))

        outgoing = httpx.Request(
            method=request.method,
            url=request.url,
            headers=headers,
            content=encrypted_body,
        )

        response = await self._inner.handle_async_request(outgoing)

        # HTTP error responses (4xx/5xx) are always sent in plaintext
        # server-side (see StlsMcpMiddleware._send_json_error) — never try
        # to decrypt them.
        if response.status_code >= 400:
            return response

        # ── Decrypt the inbound body ───────────────────────────────────────
        await response.aread()
        try:
            response_envelope = json.loads(response.content)
            plaintext_response = self._transport.decrypt_json(response_envelope)
        except Exception as exc:
            logger.exception("StlsHttpxTransport: error decrypting response")
            raise httpx.TransportError(f"STLS decryption error: {exc}") from exc

        plaintext_bytes = json.dumps(plaintext_response).encode("utf-8")

        return httpx.Response(
            status_code=response.status_code,
            headers=[
                (k, v) for k, v in response.headers.items()
                if k.lower() not in ("content-length", "content-encoding")
            ] + [("content-length", str(len(plaintext_bytes)))],
            content=plaintext_bytes,
            request=request,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


async def perform_pqc_client_handshake(
    base_url: str,
    *,
    client_id: str,
    handshake_path: str = "/stls/pqc/handshake",
    httpx_client: httpx.AsyncClient | None = None,
) -> tuple[str, StlsTransport]:
    """
    Performs the PQC STLS handshake against the server (X25519 + ML-KEM-768).

    Unlike perform_client_handshake(), this doesn't require a
    pre-shared session_key: it generates the keypairs client-side, sends
    them to the server, and derives the session_key locally via the KEM
    exchange (X25519 decap + ML-KEM). Requires liboqs-python installed
    (uv pip install -e '.[pqc]').

    Flow:
        1. Generate X25519 + ML-KEM-768 keypairs (client-side).
        2. POST /stls/pqc/handshake with the public keys.
        3. Receive server_x25519_pub + mlkem_ciphertext + proof.
        4. Derive session_key via HKDF(X25519_secret ‖ ML-KEM_secret, salt=nonce).
        5. Verify proof = HMAC(session_key, nonce) — proof of possession.
        6. Zero out the client's private keys (best-effort).

    Args:
        base_url:       base URL of the server (e.g. "https://my-server.example.com").
        client_id:      client identifier (server-side audit/logging).
        handshake_path: path of the PQC endpoint (default "/stls/pqc/handshake").
        httpx_client:   optional httpx client to reuse; if None, a
                        temporary one is created.

    Returns:
        (session_id, transport): session_id to use in X-STLS-Session-ID,
        an StlsTransport already ready to encrypt/decrypt.

    Raises:
        StlsHandshakeFailed: if the HTTP handshake fails, the proof is
                             invalid, or liboqs is not installed/compatible.
    """
    try:
        from stls_core import (
            PQCHandshakeRequest,
            PQCHandshakeResponse,
            KeyProviderClientPQC,
            derive_session_key_client,
            verify_pqc_handshake_proof,
        )
    except ImportError as exc:
        raise StlsHandshakeFailed(
            f"perform_pqc_client_handshake requires liboqs-python: {exc}"
        ) from exc

    # 1. Generate X25519 + ML-KEM-768 keypairs client-side
    pqc_client = KeyProviderClientPQC()
    try:
        pqc_client.start()
    except Exception as exc:
        raise StlsHandshakeFailed(
            f"PQC keypair generation failed — is liboqs installed and compatible? {exc}"
        ) from exc

    # 2. Prepare the request with the client's public keys
    request = PQCHandshakeRequest.generate(
        client_id,
        pqc_client.x25519_public_key_bytes,
        pqc_client.mlkem_public_key_bytes,
    )
    nonce = request.nonce

    # 3. POST to the server
    url = base_url.rstrip("/") + handshake_path
    owns_client = httpx_client is None
    client = httpx_client or httpx.AsyncClient()
    try:
        resp = await client.post(url, json=request.model_dump())
    except httpx.HTTPError as exc:
        raise StlsHandshakeFailed(
            f"PQC HTTP handshake failed against {url}: {exc}"
        ) from exc
    finally:
        if owns_client:
            await client.aclose()

    if resp.status_code != 200:
        raise StlsHandshakeFailed(
            f"PQC handshake rejected by server "
            f"(status={resp.status_code}): {resp.text}"
        )

    try:
        response = PQCHandshakeResponse.model_validate(resp.json())
    except Exception as exc:
        raise StlsHandshakeFailed(f"Invalid PQCHandshake response: {exc}") from exc

    # 4. Derive session_key via KEM (X25519 + ML-KEM decap, then HKDF)
    session = pqc_client.createProviderSession()
    try:
        session_key = derive_session_key_client(
            session.x25519_private_key_bytes,
            session.mlkem_secret_key_bytes,
            response,
            nonce,
        )
    except Exception as exc:
        raise StlsHandshakeFailed(f"PQC session_key derivation failed: {exc}") from exc
    finally:
        pqc_client.stop()  # zero out private keys (best-effort)

    # 5. Verify the server's proof — mutual proof of possession
    if not verify_pqc_handshake_proof(session_key, nonce, response):
        raise StlsHandshakeFailed(
            "Invalid PQC proof of possession — the server did not derive "
            "the same session_key. Possible attack or misconfiguration. "
            "Handshake rejected for safety."
        )

    logger.info(
        "PQC STLS-MCP handshake completed: session_id=%s kid=%s",
        response.session_id, response.kid,
    )

    transport = StlsTransport(session_key=session_key, session_id=response.session_id)
    return response.session_id, transport


def stls_httpx_factory(session_id: str, transport: StlsTransport, *, stls_path: str = "/mcp-stls"):
    """
    Builds a factory compatible with
    mcp.client.streamable_http.streamablehttp_client(httpx_client_factory=...).

    Typical usage:

        session_id, transport = await perform_client_handshake(
            base_url, session_key, client_id="my-client",
        )

        factory = stls_httpx_factory(session_id, transport)

        async with streamablehttp_client(
            url=f"{base_url}/mcp-stls",
            httpx_client_factory=factory,
        ) as (read_stream, write_stream, get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                ...
    """

    def _factory(
        headers: dict[str, str] | None = None,
        timeout: Any = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        stls_transport = StlsHttpxTransport(session_id, transport, stls_path=stls_path)
        kwargs: dict[str, Any] = {"transport": stls_transport, "follow_redirects": True}
        if headers is not None:
            kwargs["headers"] = headers
        if timeout is not None:
            kwargs["timeout"] = timeout
        if auth is not None:
            kwargs["auth"] = auth
        return httpx.AsyncClient(**kwargs)

    return _factory
