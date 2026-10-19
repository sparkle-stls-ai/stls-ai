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
    3. Neither configured → no auth, open endpoints (default behavior,
       backward-compatible with vault_server.py and anyone using
       create_secure_mcp_app() without auth/api_key_manager).

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
/secure-a2a).

The handshake endpoints (/stls/handshake, /stls/pqc/handshake) stay
ALWAYS open, by design — same behavior as stls-a2a: a client has no
provable credentials yet before the handshake itself, and the encrypted
channel's security comes from the handshake's proof-of-possession, not
an additional API key on that endpoint.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, MutableMapping, Optional

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

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


class _AuthGateASGI:
    """
    ASGI wrapper that protects an app (plain or encrypted) with
    ApiKeyManager (priority 1) and/or AuthConfig (priority 2, fallback) —
    see the module docstring for the full logic.

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
    ) -> None:
        self._app = app
        self._auth = auth
        self._api_key_manager = api_key_manager

    def _gate_enabled(self) -> bool:
        return self._api_key_manager is not None or (self._auth is not None and self._auth.enabled)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not self._gate_enabled():
            await self._app(scope, receive, send)
            return

        denial = await self._check(scope, receive)
        if denial is not None:
            status_code, message, www_authenticate = denial
            await _send_json_error(send, status_code, message, www_authenticate)
            return

        await self._app(scope, receive, send)

    async def _check(
        self, scope: Scope, receive: Receive
    ) -> Optional[tuple[int, str, Optional[str]]]:
        """
        Returns:
            None                                      → authorized
            (status_code, message, www_authenticate)  → denied
        """
        from starlette.requests import Request

        request = Request(scope, receive=receive)

        # ── 1. Enterprise ApiKeyManager ──────────────────────────────────
        if self._api_key_manager is not None:
            try:
                result = await self._api_key_manager.validate_request(request)
                if result.ok:
                    return None
                logger.warning(
                    "ApiKeyManager auth denied: %s %s — %s",
                    request.method, request.url.path, result.result.value,
                )
                return (
                    401,
                    "Unauthorized: missing or invalid credentials",
                    result.www_authenticate_header(),
                )
            except Exception as exc:
                logger.error("ApiKeyManager.validate_request() error: %s", exc)
                return 503, "Auth service unavailable", None

        # ── 2. Static AuthConfig (backward-compatible fallback) ────────────
        if self._auth is not None and self._auth.enabled:
            authorization = request.headers.get("authorization")
            api_key = request.headers.get("x-api-key") or request.query_params.get("api_key")
            if self._auth.check(authorization, api_key):
                return None
            return (
                401,
                "Unauthorized: missing or invalid credentials",
                f'ApiKey realm="{self._auth.realm}"',
            )

        return None


async def _send_json_error(
    send: Send, status_code: int, message: str, www_authenticate: Optional[str] = None
) -> None:
    """Sends a plaintext JSON-RPC error (never encrypted — consistent with StlsMcpMiddleware)."""
    body = json.dumps({
        "jsonrpc": "2.0",
        "error": {"code": -32600 if status_code < 500 else -32603, "message": message},
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
) -> ASGIApp:
    """
    Wraps an ASGI app with _AuthGateASGI if auth or api_key_manager are
    configured, otherwise returns it unchanged (no overhead, no behavior
    change from the pre-existing default).
    """
    if auth is None and api_key_manager is None:
        return app
    return _AuthGateASGI(app, auth=auth, api_key_manager=api_key_manager)
