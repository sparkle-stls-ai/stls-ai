"""
stls_mcp_middleware.py
-----------------------
Server-side ASGI middleware for the encrypted MCP endpoint (MCP_STLS_PATH).

Design: "same session manager, byte-level proxy" — there is no second
FastMCP instance. This middleware:

    1. Receives the HTTP request on MCP_STLS_PATH.
    2. Resolves the STLS session (handshake already happened, transport
       cached).
    3. Decrypts the JSON-RPC body.
    4. Builds a synthetic ASGI scope pointing at MCP_PLAIN_PATH (the real
       internal path handled by the official SDK).
    5. Invokes the standard Starlette app with that scope, capturing the
       entire response via a custom `send`.
    6. Encrypts the response body and returns it to the client on
       MCP_STLS_PATH.

The standard Starlette app (and its MCP session manager) has no idea
it's being called from here instead of directly by the client: it
receives a scope and a plaintext body identical to what it would receive
on MCP_PLAIN_PATH. This guarantees the tool/resource/prompt surface is
always the same on both paths, with no risk of drift.

v0.1 limitations (documented, not silent):
    - Does not yet support chunk-by-chunk encrypted multi-chunk SSE
      responses: the response is buffered entirely before being
      re-encrypted. For Streamable HTTP with stateless_http=True and
      json_response=True (the configuration recommended for production)
      this isn't an issue, since every response is a single JSON block
      anyway. Chunk-by-chunk encrypted SSE streaming is planned for a
      future version.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, MutableMapping

from stls_core import HEADER_SESSION_ID, SessionCache, SessionCacheBase
from stls_core.stls_core_crypto import StlsTransport

from .stls_mcp_constants import ERR_SESSION_EXPIRED, MCP_PLAIN_PATH, MCP_STLS_PATH

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class StlsMcpError(Exception):
    """
    Application-level error of the STLS-MCP middleware.

    Attributes:
        status_code: HTTP status code to return to the client.
        message:     human-readable message, never contains key material.
        code:        optional JSON-RPC error code override. None (default)
                     keeps _send_json_error()'s generic status-based code
                     (-32600/-32603) — only the expired-session case below
                     sets this explicitly, to ERR_SESSION_EXPIRED.
    """
    def __init__(self, message: str, status_code: int = 400, code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class StlsMcpMiddleware:
    """
    ASGI app mounted on MCP_STLS_PATH (or the custom path passed via
    stls_path — see below).

    Args:
        plain_app:     the standard Starlette app (mcp.streamable_http_app()),
                       the same one mounted on MCP_PLAIN_PATH. It is not
                       modified or wrapped: it's simply invoked again with
                       a synthetic scope.
        session_cache: SessionCacheBase shared with the STLS handshake
                       endpoint, from which this middleware retrieves the
                       StlsTransport associated with each session.
        stls_path:     path on which THIS middleware is actually mounted
                       (default: the MCP_STLS_PATH constant). Must match
                       the stls_path argument passed to
                       create_secure_mcp_app() — otherwise _rewrite_path()
                       doesn't recognize the incoming prefix, the
                       decrypted body gets forwarded to the WRONG internal
                       path, and the client sees a generic 500 (a real bug
                       observed in the wild: a non-JSON internal response
                       — e.g. a Starlette 404 page — makes json.loads()
                       fail in the response-encryption block, raising an
                       exception caught as a 500 error instead of a
                       routing 404).
        plain_path:    path on which the STANDARD app is actually mounted
                       (default: the MCP_PLAIN_PATH constant). Must match
                       the FastMCP constructor's streamable_http_path, for
                       the same reason as above.
    """

    def __init__(
        self,
        plain_app: ASGIApp,
        session_cache: SessionCacheBase,
        *,
        stls_path: str = MCP_STLS_PATH,
        plain_path: str = MCP_PLAIN_PATH,
    ) -> None:
        self._plain_app = plain_app
        self._session_cache = session_cache
        self._stls_path = stls_path
        self._plain_path = plain_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # WebSocket/lifespan aren't expected on this path — direct
            # pass-through, nothing to encrypt.
            await self._plain_app(scope, receive, send)
            return

        try:
            await self._handle_http(scope, receive, send)
        except StlsMcpError as exc:
            logger.warning("StlsMcpMiddleware: %s (status=%d)", exc.message, exc.status_code)
            await self._send_json_error(send, exc.status_code, exc.message, code=exc.code)
        except Exception as exc:
            logger.exception("StlsMcpMiddleware: unhandled error")
            await self._send_json_error(send, 500, f"Internal STLS-MCP error: {exc}")

    # ------------------------------------------------------------------
    # Handling HTTP
    # ------------------------------------------------------------------

    async def _handle_http(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = dict(scope.get("headers") or [])
        session_id = self._get_header(headers, HEADER_SESSION_ID)

        if not session_id:
            raise StlsMcpError(
                f"Missing header {HEADER_SESSION_ID} — STLS session not established. "
                f"Perform the handshake against /stls/handshake first.",
                status_code=401,
            )

        transport = self._session_cache.get(session_id)
        if transport is None:
            raise StlsMcpError(
                "Unknown or expired STLS session — repeat the handshake.",
                status_code=401,
                code=ERR_SESSION_EXPIRED,
            )

        # ── 1. Read and decrypt the inbound body ────────────────────────────
        raw_body = await self._read_body(receive)
        try:
            import json
            envelope = json.loads(raw_body)
            plaintext_payload = transport.decrypt_json(envelope)
        except ValueError as exc:
            raise StlsMcpError(f"Decrypt failed: {exc}", status_code=400) from exc
        except Exception as exc:
            raise StlsMcpError(f"Invalid payload: {exc}", status_code=400) from exc

        plaintext_bytes = json.dumps(plaintext_payload).encode("utf-8")

        # ── 2. Build a synthetic scope pointing at MCP_PLAIN_PATH ─────────
        inner_path = self._rewrite_path(scope["path"])
        inner_scope = dict(scope)
        inner_scope["path"] = inner_path
        inner_scope["raw_path"] = inner_path.encode("utf-8")
        # Content-Length must reflect the new plaintext body, not the
        # original encrypted one — otherwise the inner app reads a
        # truncated body or tries to read past the real size.
        inner_scope["headers"] = self._rewrite_content_length(
            scope.get("headers") or [], len(plaintext_bytes)
        )

        async def inner_receive() -> MutableMapping[str, Any]:
            return {"type": "http.request", "body": plaintext_bytes, "more_body": False}

        # ── 3. Invoke the standard app, capturing the entire response ───────────
        captured: dict[str, Any] = {"status": 200, "headers": [], "body": b""}

        async def capturing_send(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
                captured["headers"] = list(message.get("headers") or [])
            elif message["type"] == "http.response.body":
                captured["body"] += message.get("body", b"")

        await self._plain_app(inner_scope, inner_receive, capturing_send)

        # ── 4. Encrypt the response and return it on the STLS path ───────────
        # HTTP errors (e.g. 400/406/415 raised directly by the SDK, not
        # raised as StlsMcpError) are returned as-is, in plaintext — the
        # client (StlsHttpxTransport) assumes exactly this for any status
        # >= 400 and skips decryption. Only a genuine application response
        # (status < 400; a JSON-RPC error INSIDE a 200 is still encrypted
        # normally) goes through the encryption step below.
        if captured["status"] >= 400:
            await send({
                "type": "http.response.start",
                "status": captured["status"],
                "headers": captured["headers"],
            })
            await send({
                "type": "http.response.body",
                "body": captured["body"],
                "more_body": False,
            })
            return

        try:
            response_payload = json.loads(captured["body"].decode("utf-8")) if captured["body"] else {}
            response_envelope = transport.encrypt_json(response_payload)
        except Exception as exc:
            raise StlsMcpError(
                f"Error encrypting the response: {exc}", status_code=500
            ) from exc

        response_bytes = json.dumps(response_envelope).encode("utf-8")

        await send({
            "type": "http.response.start",
            "status": captured["status"],
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(response_bytes)).encode("ascii")),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": response_bytes,
            "more_body": False,
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_header(headers: dict, name: str) -> str | None:
        """Reads an ASGI header (lowercase byte string) by case-insensitive name."""
        target = name.lower().encode("ascii")
        value = headers.get(target)
        return value.decode("utf-8") if value is not None else None

    @staticmethod
    async def _read_body(receive: Receive) -> bytes:
        """Reads the entire request body, handling any multiple chunks."""
        body = b""
        more_body = True
        while more_body:
            message = await receive()
            body += message.get("body", b"")
            more_body = message.get("more_body", False)
        return body

    def _rewrite_path(self, original_path: str) -> str:
        """
        Rewrites self._stls_path (and its sub-paths) into the internal
        self._plain_path expected by the standard app.

        Uses the paths actually configured on THIS instance (passed to
        create_secure_mcp_app as stls_path/plain_path), not the module
        constants — historical bug: they used to be hardcoded to
        MCP_STLS_PATH/MCP_PLAIN_PATH, so with a custom path the condition
        below never matched, the decrypted body got forwarded to the
        WRONG internal path, and the client saw a generic 500
        (json.loads() failed on a non-JSON Starlette 404 response,
        caught as an exception indistinguishable from a real internal
        error).

        Note on Starlette's behavior: when StlsMcpMiddleware is mounted
        with Mount(self._stls_path, app=...), the received scope does NOT
        have the prefix stripped the way an application router would —
        the path always arrives as the original prefix with a trailing
        slash added (e.g. a client request to "/mcp-stls" or
        "/mcp-stls/" produces original_path == "/mcp-stls/" in both
        cases).

        Without normalization, "/mcp-stls/" → "/mcp/" (suffix="/"), which
        doesn't match the exact route "/mcp" registered by FastMCP:
        Starlette would respond with a 307 redirect to "/mcp" instead of
        processing the request — a redirect our capturing_send would
        intercept as if it were the tool's real response.

        Example (default paths): "/mcp-stls/" → "/mcp", "/mcp-stls/foo" → "/mcp/foo"
        Example (custom path "/vault/mcp-stls" → "/vault/mcp"):
                 "/vault/mcp-stls/" → "/vault/mcp"
        """
        if original_path.startswith(self._stls_path):
            suffix = original_path[len(self._stls_path):]
            if suffix == "/":
                suffix = ""
            return self._plain_path + suffix
        return original_path

    @staticmethod
    def _rewrite_content_length(
        headers: list[tuple[bytes, bytes]], new_length: int
    ) -> list[tuple[bytes, bytes]]:
        """Replaces the content-length header with the plaintext size."""
        rewritten = [
            (k, v) for k, v in headers if k.lower() != b"content-length"
        ]
        rewritten.append((b"content-length", str(new_length).encode("ascii")))
        return rewritten

    @staticmethod
    async def _send_json_error(
        send: Send, status_code: int, message: str, *, code: int | None = None,
    ) -> None:
        """Sends a plaintext JSON-RPC error (never encrypted — consistent with stls-a2a)."""
        import json
        body = json.dumps({
            "jsonrpc": "2.0",
            "error": {
                "code": code if code is not None else (-32600 if status_code < 500 else -32603),
                "message": message,
            },
            "id": None,
        }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body, "more_body": False})
