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

from stls_core import HEADER_SESSION_ID
from stls_core.stls_core_crypto import StlsTransport

# perform_client_handshake(), perform_pqc_client_handshake() and
# StlsHandshakeFailed moved to stls-core (stls_core_client_handshake.py)
# as part of issue #5 ("Restrict stls-core's public API surface") —
# establish_session() there needs to own resolve+handshake end to end so
# it never has to hand raw key material back to its caller. Re-exported
# here unchanged: stls-mcp's own public API (names, signatures, README
# samples) is unaffected.
from stls_core import (  # noqa: F401 — re-export
    StlsHandshakeFailed,
    perform_client_handshake,
    perform_pqc_client_handshake,
)

logger = logging.getLogger(__name__)


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
