"""
stls_mcp_auth.py
-----------------
API key protection for the MCP endpoints (plain and encrypted).

Same two-level scheme already used by stls-a2a — now genuinely shared,
not just mirrored: AuthConfig lives in stls_core.auth (moved there from
stls-a2a, where it was a duplicated local class), same for ApiKeyManager:

    1. ApiKeyManager (enterprise, Redis/File/Postgres, stls_core.auth) —
       if configured, takes priority.
    2. AuthConfig (static, in-memory, timing-safe, stls_core.auth) —
       backward-compatible fallback.
    3. DID/VC identity presentation (issue #15 gap #1, brought here in
       #46) — optional, ADDITIONAL check, strictly after 1/2 already
       passed. See _check_identity_async() below.
    4. None of the above configured → no auth, open endpoints (default
       behavior, backward-compatible with vault_server.py and anyone
       using create_secure_mcp_app() without auth/api_key_manager/
       identity_verifier).

Structural difference vs. stls-a2a
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
In stls-a2a every endpoint is a hand-written Starlette handler
(secure_a2a_handler, plain_a2a_handler, ...), so the check
(_check_auth_async) is an explicit call at the start of each one.

Here the surface to protect includes MCP_PLAIN_PATH — the app generated
ENTIRELY by the official SDK (mcp.streamable_http_app()), whose internal
handlers we don't own — in addition to MCP_STLS_PATH (StlsMcpMiddleware,
which is ours). The check can't live inside the handlers, so
_AuthGateASGI is a reusable ASGI wrapper, applied in front of BOTH apps,
that intercepts the request before it reaches the wrapped app (STLS or
plain, indifferently).

Protects both MCP_PLAIN_PATH and MCP_STLS_PATH — not just the encrypted
channel, exactly like stls-a2a (where AuthConfig protects both /a2a and
/secure-a2a). The identity check specifically only ever engages on
MCP_STLS_PATH in practice: it keys off the STLS session header, which
the plain path never carries — see _check_identity_async()'s own
docstring, same posture as stls-a2a's identical check.

The handshake endpoints (/stls/handshake, /stls/pqc/handshake) stay
ALWAYS open, by design — same behavior as stls-a2a: a client has no
provable credentials yet before the handshake itself, and the encrypted
channel's security comes from the handshake's proof-of-possession, not
an additional API key on that endpoint.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any, Awaitable, Callable, Dict, MutableMapping, Optional, Tuple

from stls_core import HEADER_SESSION_ID

from .stls_mcp_constants import ERR_IDENTITY_CHALLENGE_REQUIRED, HEADER_IDENTITY_PRESENTATION

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# Same value and reasoning as stls-a2a's _IDENTITY_CHALLENGE_TTL_SECONDS
# (stls_a2a_integration.py): long enough for a client to sign and retry
# within a normal request round trip, short enough that a captured,
# unused challenge doesn't stay exploitable indefinitely.
_IDENTITY_CHALLENGE_TTL_SECONDS = 120

__all__ = ["AuthConfig", "wrap_with_auth"]

# AuthConfig now lives in stls_core.auth — shared with stls-a2a, no longer
# duplicated. Same try/except pattern already used for ApiKeyManager: its
# absence (redis/yaml/asyncpg not installed) must not prevent importing
# this module, even though in practice AuthConfig itself requires no
# external dependency — see the comment on its definition in
# stls_core_auth_models.py.
try:
    from stls_core.auth.stls_core_auth_models import AuthConfig
except ImportError:
    AuthConfig = None  # type: ignore[assignment,misc]


class _Denial:
    """Internal result of _AuthGateASGI._check() — carries everything
    _send_json_error() needs to build the exact response shape,
    including the identity check's extra `code`/`challenge` fields that
    the plain api-key/auth denials never need."""

    __slots__ = ("status_code", "message", "www_authenticate", "code", "extra")

    def __init__(
        self,
        status_code: int,
        message: str,
        www_authenticate: Optional[str] = None,
        *,
        code: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.www_authenticate = www_authenticate
        self.code = code
        self.extra = extra


class _AuthGateASGI:
    """
    ASGI wrapper that protects an app (plain or encrypted) with
    ApiKeyManager (priority 1), AuthConfig (priority 2, fallback), and
    DID/VC identity presentation (priority 3, additional) — see the
    module docstring for the full logic.

    Never consumes the request's body/receive: it only reads
    scope["headers"]/scope["query_string"] (via a starlette.requests.Request
    object built on the fly, which populates .headers/.query_params/.url
    without touching I/O) — the wrapped app receives receive() fully
    intact, so StlsMcpMiddleware can still read the encrypted body
    normally downstream of this gate.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        auth: Optional[AuthConfig] = None,
        api_key_manager: Optional[Any] = None,
        identity_verifier: Optional[Any] = None,
    ) -> None:
        """
        Args:
            identity_verifier: optional DID/VC identity check (issue #15
                gap #1, brought here in #46), duck-typed like
                api_key_manager — stls-mcp never imports stls_identity,
                it only calls `await identity_verifier.verify_request(
                request, challenge=...)` expecting an object with
                `.ok: bool` / `.error: str | None`. Runs as an
                ADDITIONAL check, strictly after api_key_manager/auth
                have already passed — never a substitute for them.
                None (default) = disabled: zero header parsing, zero
                challenge-cache use, zero behavior change for every
                existing caller.
        """
        self._app = app
        self._auth = auth
        self._api_key_manager = api_key_manager
        self._identity_verifier = identity_verifier
        # {session_id: (challenge, minted_at_monotonic)} — only ever
        # populated when identity_verifier is configured; see
        # _check_identity_async(). Same shape as stls-a2a's
        # SecureA2AHTTPMiddleware._identity_challenges.
        self._identity_challenges: Dict[str, Tuple[str, float]] = {}

    def _gate_enabled(self) -> bool:
        return (
            self._api_key_manager is not None
            or (self._auth is not None and self._auth.enabled)
            or self._identity_verifier is not None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not self._gate_enabled():
            await self._app(scope, receive, send)
            return

        denial = await self._check(scope, receive)
        if denial is not None:
            await _send_json_error(
                send, denial.status_code, denial.message,
                denial.www_authenticate, code=denial.code, extra=denial.extra,
            )
            return

        await self._app(scope, receive, send)

    async def _check(self, scope: Scope, receive: Receive) -> Optional[_Denial]:
        """
        Returns:
            None     → authorized
            _Denial  → denied
        """
        from starlette.requests import Request

        request = Request(scope, receive=receive)

        # ── 1. Enterprise ApiKeyManager ──────────────────────────────────
        if self._api_key_manager is not None:
            try:
                result = await self._api_key_manager.validate_request(request)
                if not result.ok:
                    logger.warning(
                        "ApiKeyManager auth denied: %s %s — %s",
                        request.method, request.url.path, result.result.value,
                    )
                    return _Denial(
                        401,
                        "Unauthorized: missing or invalid credentials",
                        result.www_authenticate_header(),
                    )
            except Exception as exc:
                logger.error("ApiKeyManager.validate_request() error: %s", exc)
                return _Denial(503, "Auth service unavailable")
        else:
            # ── 2. Static AuthConfig (backward-compatible fallback) ────
            if self._auth is not None and self._auth.enabled:
                authorization = request.headers.get("authorization")
                api_key = request.headers.get("x-api-key") or request.query_params.get("api_key")
                if not self._auth.check(authorization, api_key):
                    return _Denial(
                        401,
                        "Unauthorized: missing or invalid credentials",
                        f'ApiKey realm="{self._auth.realm}"',
                    )

        # ── 3. Identity presentation (issue #15 gap #1, #46) ─────────────
        return await self._check_identity_async(request)

    async def _check_identity_async(self, request: Any) -> Optional[_Denial]:
        """
        DID/VC identity check (issue #15 gap #1, brought here in #46) —
        runs only when identity_verifier is configured, strictly AFTER
        api_key_manager/auth have already passed. Mirrors stls-a2a's
        SecureA2AHTTPMiddleware._check_identity_async() exactly.

        With identity_verifier=None (the default), this is a no-op:
        returns None immediately, no header parsing, no challenge-cache
        use — zero behavior change for every existing caller.

        Challenge/response shape: the first request on a session_id
        without a (still-fresh) cached challenge gets back
        ERR_IDENTITY_CHALLENGE_REQUIRED plus a freshly minted challenge;
        the client is expected to sign a VerifiablePresentation over it
        and retry exactly once, same shape as ERR_SESSION_EXPIRED.

        Keys off stls-core's own HEADER_SESSION_ID (X-STLS-Session-ID),
        NOT a custom stls-mcp header — unlike stls-a2a, which has its
        own X-A2A-Session-ID, stls-mcp already uses the generic
        stls-core session header directly for the STLS crypto binding
        (see stls_mcp_middleware.py), so there's no second header to
        introduce here. Scoped to the encrypted transport by design:
        MCP_PLAIN_PATH requests never carry this header, so this check
        is naturally a no-op there even when identity_verifier is
        configured — same posture as stls-a2a's identical check.

        Returns:
            None     → authorized (or identity not configured)
            _Denial  → denied
        """
        if self._identity_verifier is None:
            return None

        session_id = request.headers.get(HEADER_SESSION_ID, "").strip()
        if not session_id:
            return None

        presentation_header = request.headers.get(HEADER_IDENTITY_PRESENTATION, "").strip()
        cached = self._identity_challenges.get(session_id)

        if not presentation_header or cached is None or (
            time.monotonic() - cached[1] > _IDENTITY_CHALLENGE_TTL_SECONDS
        ):
            challenge = secrets.token_urlsafe(32)
            self._identity_challenges[session_id] = (challenge, time.monotonic())
            logger.info("Identity challenge issued for session_id=%s", session_id)
            return _Denial(
                401, "Identity presentation required",
                code=ERR_IDENTITY_CHALLENGE_REQUIRED, extra={"challenge": challenge},
            )

        challenge, _minted_at = cached
        try:
            result = await self._identity_verifier.verify_request(request, challenge=challenge)
        except Exception as exc:
            logger.error("identity_verifier.verify_request() error: %s", exc)
            return _Denial(503, "Identity verification service unavailable")

        if result.ok:
            self._identity_challenges.pop(session_id, None)
            return None

        logger.warning("Identity presentation rejected for session_id=%s: %s", session_id, result.error)
        return _Denial(401, "Unauthorized: invalid identity presentation")


async def _send_json_error(
    send: Send,
    status_code: int,
    message: str,
    www_authenticate: Optional[str] = None,
    *,
    code: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Sends a plaintext JSON-RPC error (never encrypted — consistent with StlsMcpMiddleware)."""
    error: Dict[str, Any] = {
        "code": code if code is not None else (-32600 if status_code < 500 else -32603),
        "message": message,
    }
    if extra:
        error.update(extra)
    body = json.dumps({
        "jsonrpc": "2.0",
        "error": error,
        "id": None,
    }).encode("utf-8")
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if www_authenticate:
        headers.append((b"www-authenticate", www_authenticate.encode("utf-8")))
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


def wrap_with_auth(
    app: ASGIApp,
    *,
    auth: Optional[AuthConfig] = None,
    api_key_manager: Optional[Any] = None,
    identity_verifier: Optional[Any] = None,
) -> ASGIApp:
    """
    Wraps an ASGI app with _AuthGateASGI if auth, api_key_manager, or
    identity_verifier are configured, otherwise returns it unchanged —
    no overhead, no behavior change from the pre-existing default.
    """
    if auth is None and api_key_manager is None and identity_verifier is None:
        return app
    return _AuthGateASGI(
        app, auth=auth, api_key_manager=api_key_manager, identity_verifier=identity_verifier,
    )
