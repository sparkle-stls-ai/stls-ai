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
import inspect
from typing import Any, Awaitable, Callable, Optional, Union

import httpx

from stls_core import HEADER_SESSION_ID
from stls_core.stls_core_crypto import StlsTransport

from .stls_mcp_constants import ERR_IDENTITY_CHALLENGE_REQUIRED, ERR_SESSION_EXPIRED

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

IdentityChallengeHandler = Callable[[str], Union[dict[str, str], Awaitable[dict[str, str]]]]
"""
Signature stls-mcp expects for identity_challenge_handler (issue #15
gap #1, brought here in #46): given the server-issued challenge string,
returns (sync or async) the headers to merge into the retried request —
typically stls_identity.IdentityPresenter.on_challenge, passed directly.
stls-mcp never imports stls_identity; this is a duck type, same posture
as identity_verifier on the server side (_AuthGateASGI). Same shape as
stls-a2a's own IdentityChallengeHandler (stls_a2a_secure_remote.py).
"""


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


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
        rehandshake:    optional zero-argument async callable that performs
                       a brand new handshake and returns (session_id,
                       transport), used to recover from session expiry
                       (idle-TTL or the absolute max-age added in issue
                       #6) with a single automatic retry — same
                       retry-once contract as stls-a2a's
                       STLSHttpxTransport. None (default): no change from
                       before this existed — an expired session is
                       returned to the caller as a plain 401, same as any
                       other error. The caller builds this closure because
                       only it knows which handshake function and
                       credentials apply (perform_client_handshake() needs
                       a pre-shared session_key; perform_pqc_client_handshake()
                       needs none) — this class stays provider-agnostic.
        identity_challenge_handler: optional (issue #15 gap #1, #46) —
                       see IdentityChallengeHandler's own docstring. None
                       here (the default) means every
                       ERR_IDENTITY_CHALLENGE_REQUIRED simply propagates
                       as a normal httpx.HTTPStatusError, same as before
                       this hook existed. Independent retry budget from
                       `rehandshake` — a single request can legitimately
                       retry once for session expiry AND once for an
                       identity challenge (two different causes, same
                       "retry exactly once per cause" contract as
                       stls-a2a's STLSHttpxTransport).
    """

    def __init__(
        self,
        session_id: str,
        transport: StlsTransport,
        *,
        stls_path: str = "/mcp-stls",
        inner_transport: httpx.AsyncBaseTransport | None = None,
        rehandshake: Optional[Callable[[], Awaitable[tuple[str, StlsTransport]]]] = None,
        identity_challenge_handler: Optional[IdentityChallengeHandler] = None,
    ) -> None:
        self._session_id = session_id
        self._transport = transport
        self._stls_path = stls_path
        self._inner = inner_transport or httpx.AsyncHTTPTransport()
        self._rehandshake = rehandshake
        self._identity_challenge_handler = identity_challenge_handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != self._stls_path or request.method != "POST":
            # Pass-through for any request not aimed at the STLS path
            # (e.g. GET for an SSE stream, requests to other endpoints).
            return await self._inner.handle_async_request(request)
        return await self._handle_stls_request(request, _retried=False, _identity_retried=False)

    async def _handle_stls_request(
        self, request: httpx.Request, *, _retried: bool, _identity_retried: bool = False
    ) -> httpx.Response:
        # ── Encrypt the outgoing body ───────────────────────────────────────────
        try:
            plaintext_payload = json.loads(request.content)
            envelope = self._transport.encrypt_json(plaintext_payload)
        except Exception as exc:
            logger.exception("StlsHttpxTransport: error encrypting request")
            raise httpx.TransportError(f"STLS encryption error: {exc}") from exc

        encrypted_body = json.dumps(envelope).encode("utf-8")

        # Native MCP headers (mcp-session-id, accept, etc.) are preserved;
        # we only add the STLS session header (plus, on an identity retry,
        # the presentation header merged in by the caller) and fix up
        # content-length.
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
            if response.status_code == 401:
                await response.aread()

                if not _retried and self._rehandshake is not None and _looks_like_session_expired(response):
                    logger.info(
                        "StlsHttpxTransport: session expired, re-handshaking and retrying"
                    )
                    # Not caught here: a rehandshake failure propagates
                    # immediately, with no retry — same fail-fast contract
                    # as stls-a2a's STLSHttpxTransport.
                    self._session_id, self._transport = await self._rehandshake()
                    return await self._handle_stls_request(
                        request, _retried=True, _identity_retried=_identity_retried,
                    )

                if not _identity_retried and self._identity_challenge_handler is not None:
                    challenge = _looks_like_identity_challenge_required(response)
                    if challenge is not None:
                        logger.info(
                            "StlsHttpxTransport: identity presentation required, "
                            "signing and retrying"
                        )
                        identity_headers = await _maybe_await(
                            self._identity_challenge_handler(challenge)
                        )
                        retried_request = httpx.Request(
                            method=request.method,
                            url=request.url,
                            headers={**dict(request.headers), **identity_headers},
                            content=request.content,
                        )
                        return await self._handle_stls_request(
                            retried_request, _retried=_retried, _identity_retried=True,
                        )
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


def _looks_like_session_expired(response: httpx.Response) -> bool:
    """
    Recognizes a 401 response as ERR_SESSION_EXPIRED (-32004) rather than
    an authentication 401 (e.g. an invalid API key) — only the former
    justifies an automatic retry after re-handshake. Mirrors stls-a2a's
    helper of the same name (stls_a2a_secure_remote.py).
    """
    try:
        body = response.json()
    except Exception:
        return False
    error = body.get("error") if isinstance(body, dict) else None
    return isinstance(error, dict) and error.get("code") == ERR_SESSION_EXPIRED


def _looks_like_identity_challenge_required(response: httpx.Response) -> Optional[str]:
    """
    Recognizes a 401 response as ERR_IDENTITY_CHALLENGE_REQUIRED (-32005)
    and extracts the challenge to sign over — distinct from
    ERR_SESSION_EXPIRED's own 401 (that one justifies a re-handshake;
    this one justifies calling identity_challenge_handler and retrying
    with a signed VerifiablePresentation header instead). Mirrors
    stls-a2a's helper of the same name (stls_a2a_secure_remote.py).

    Returns the challenge string, or None if this isn't that error.
    """
    try:
        body = response.json()
    except Exception:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and error.get("code") == ERR_IDENTITY_CHALLENGE_REQUIRED:
        return error.get("challenge")
    return None


def stls_httpx_factory(
    session_id: str,
    transport: StlsTransport,
    *,
    stls_path: str = "/mcp-stls",
    rehandshake: Optional[Callable[[], Awaitable[tuple[str, StlsTransport]]]] = None,
    identity_challenge_handler: Optional[IdentityChallengeHandler] = None,
):
    """
    Builds a factory compatible with
    mcp.client.streamable_http.streamablehttp_client(httpx_client_factory=...).

    Args:
        rehandshake: see StlsHttpxTransport's docstring. Typical closure,
                     for the base/arqit_py provider (needs the pre-shared
                     session_key the caller already resolved):

                         async def _rehandshake():
                             return await perform_client_handshake(
                                 base_url, session_key, client_id="my-client",
                             )

                     or, for pqc (no pre-shared key needed):

                         async def _rehandshake():
                             return await perform_pqc_client_handshake(
                                 base_url, client_id="my-client",
                             )
        identity_challenge_handler: see StlsHttpxTransport's docstring —
                     typically stls_identity.load_identity_presenter(...).on_challenge
                     or stls_identity.identity_challenge_handler_from_env().

    Typical usage:

        session_id, transport = await perform_client_handshake(
            base_url, session_key, client_id="my-client",
        )

        factory = stls_httpx_factory(
            session_id, transport,
            rehandshake=lambda: perform_client_handshake(
                base_url, session_key, client_id="my-client",
            ),
        )

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
        stls_transport = StlsHttpxTransport(
            session_id, transport, stls_path=stls_path, rehandshake=rehandshake,
            identity_challenge_handler=identity_challenge_handler,
        )
        kwargs: dict[str, Any] = {"transport": stls_transport, "follow_redirects": True}
        if headers is not None:
            kwargs["headers"] = headers
        if timeout is not None:
            kwargs["timeout"] = timeout
        if auth is not None:
            kwargs["auth"] = auth
        return httpx.AsyncClient(**kwargs)

    return _factory
