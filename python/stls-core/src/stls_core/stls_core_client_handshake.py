"""
stls_core_client_handshake.py
-------------------------------
Client-side STLS handshake: generic over the STLS protocol itself
(base/arqit_py pre-shared-key handshake, and the PQC KEM handshake),
with no knowledge of any particular application protocol (MCP, A2A,
...) built on top of it — that's why this lives in stls-core rather
than stls-mcp, where it used to live (moved here as part of issue #5,
"Restrict stls-core's public API surface": establish_session(), in
stls_core_session_establish.py, needs to own resolve+handshake end to
end so it never has to hand raw key material back to its caller).

python/stls-mcp's stls_mcp_transport.py now just re-exports
perform_client_handshake()/perform_pqc_client_handshake()/
StlsHandshakeFailed for backward compatibility — its own public API is
unchanged.

httpx moves from stls-core's `[dev]`-only extras to a real runtime
dependency as a consequence (pyproject.toml).

Port of node/stls-core/src/client-handshake.ts.
"""

from __future__ import annotations

import base64
import logging

import httpx

from .stls_core_constants import HANDSHAKE_PATH, HANDSHAKE_PQC_PATH
from .stls_core_crypto import StlsTransport
from .stls_core_handshake import HandshakeRequest, HandshakeResponse, verify_handshake_proof

logger = logging.getLogger(__name__)


class StlsHandshakeFailed(Exception):
    """Raised if the client-side handshake fails or the proof is invalid."""


# ---------------------------------------------------------------------------
# perform_client_handshake() — standard (base / arqit_py)
# ---------------------------------------------------------------------------

async def perform_client_handshake(
    base_url: str,
    session_key: bytes,
    *,
    client_id: str,
    kid: str | None = None,
    handshake_path: str = HANDSHAKE_PATH,
    httpx_client: httpx.AsyncClient | None = None,
) -> tuple[str, StlsTransport]:
    """
    Performs the STLS handshake against the server and returns a ready
    session. The session key is never transmitted: the handshake only
    proves mutual possession via an HMAC proof.

    Low-level primitive: the caller must already hold `session_key`
    (e.g. resolved from its own KeySessionProvider). Most callers
    should use establish_session() instead, which resolves the key AND
    performs this handshake without ever handing the raw key back to
    ITS caller.

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

    logger.info("STLS client handshake completed: session_id=%s", response.session_id)

    transport = StlsTransport(session_key=session_key, session_id=response.session_id)
    return response.session_id, transport


# ---------------------------------------------------------------------------
# perform_pqc_client_handshake() — X25519 + ML-KEM-768
# ---------------------------------------------------------------------------

async def perform_pqc_client_handshake(
    base_url: str,
    *,
    client_id: str,
    handshake_path: str = HANDSHAKE_PQC_PATH,
    httpx_client: httpx.AsyncClient | None = None,
) -> tuple[str, StlsTransport]:
    """
    Performs the PQC STLS handshake against the server (X25519 + ML-KEM-768).

    Unlike perform_client_handshake(), this doesn't require a
    pre-shared session_key: it generates the keypairs client-side, sends
    them to the server, and derives the session_key locally via the KEM
    exchange — entirely inside this function via
    KeySessionProviderPQC.derive_client_key(); the ephemeral KEM private
    key material never leaves that class. Requires liboqs-python
    installed (uv pip install -e '.[pqc]').

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
        from .stls_core_handshake_pqc import PQCHandshakeRequest, PQCHandshakeResponse, verify_pqc_handshake_proof
        from .providers.stls_core_providers_pqc import KeyProviderClientPQC
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

    try:
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

        # 4. Derive session_key via KEM — entirely inside KeySessionProviderPQC,
        #    never touching its private key attributes directly.
        session = pqc_client.createProviderSession()
        try:
            session_key = session.derive_client_key(
                base64.b64decode(response.server_x25519_public_key),
                base64.b64decode(response.mlkem_ciphertext),
                base64.b64decode(nonce),
            )
        except Exception as exc:
            raise StlsHandshakeFailed(f"PQC session_key derivation failed: {exc}") from exc

        # 5. Verify the server's proof — mutual proof of possession
        if not verify_pqc_handshake_proof(session_key, nonce, response):
            raise StlsHandshakeFailed(
                "Invalid PQC proof of possession — the server did not derive "
                "the same session_key. Possible attack or misconfiguration. "
                "Handshake rejected for safety."
            )
    finally:
        pqc_client.stop()  # zero out private keys (best-effort)

    logger.info(
        "PQC STLS client handshake completed: session_id=%s kid=%s",
        response.session_id, response.kid,
    )

    transport = StlsTransport(session_key=session_key, session_id=response.session_id)
    return response.session_id, transport
