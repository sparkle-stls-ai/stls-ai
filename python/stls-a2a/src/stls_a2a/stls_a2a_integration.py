"""
STLS A2A Transport Integration with Agent Frameworks
=====================================================

This module provides integration of the A2A encryption layer
with different AI frameworks via the adapter pattern.

Supports:
- Google ADK
- LangChain
- Semantic Kernel
- Custom agents

"""

import asyncio
import inspect
import os
import uuid as _uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, Any, Optional, AsyncIterator, Callable
import concurrent
import httpx
import json
from .stls_a2a_transport import SecureA2ATransport, A2AMessage
from .stls_a2a_utils import to_a2a
from .stls_a2a_adapters import (
    AgentAdapter,
    create_adapter,
    a2a_to_agent_request,
    agent_response_to_a2a
)
from stls_core import KeySessionProvider, SessionCacheBase, SessionCacheFactory
from .stls_a2a_task import task_store, TaskState


import logging
logger = logging.getLogger(__name__)

# ── Agent Registry (optional — active only if AUTH_PG_DSN is configured) ────
try:
    from .stls_a2a_agent_registry import init_registry, make_agent_id, get_registry
    _REGISTRY_AVAILABLE = True
except ImportError:
    init_registry = lambda **_: None   # type: ignore
    make_agent_id = lambda n, u: n     # type: ignore
    get_registry  = lambda: None       # type: ignore
    _REGISTRY_AVAILABLE = False

# ── Auth system (optional — doesn't break if stls_core[redis] isn't installed) ──
try:
    from stls_core import ApiKeyManager
    from stls_core.auth.stls_core_auth_models import AuthConfig, AuthResult
    _AUTH_MANAGER_AVAILABLE = True
except ImportError:
    ApiKeyManager = None          # type: ignore[assignment,misc]
    AuthConfig = None             # type: ignore[assignment,misc]
    AuthResult = None             # type: ignore[assignment]
    _AUTH_MANAGER_AVAILABLE = False


# ── SSE constants ─────────────────────────────────────────────────────────────
# Defined only once at module level; used in send_secure_message,
# process_plain_request, process_plain_stream, and process_secure_stream.

SSE_STREAM_METHODS: frozenset[str] = frozenset({
    "messages/sendStream",
    "message/sendStream",
    "message/stream",
    "tasks/subscribe",
    "task/subscribe",
})
"""All A2A methods that produce an SSE response (not a single JSON)."""

SSE_SEND_METHODS: frozenset[str] = frozenset({
    "messages/sendStream",
    "message/sendStream",
    "message/stream",
})
"""Subset: methods that create a new Task and invoke the adapter."""

SSE_SUBSCRIBE_METHODS: frozenset[str] = frozenset({
    "tasks/subscribe",
    "task/subscribe",
})
"""Subset: methods that attach to an existing Task in the TaskStore."""

CANONICAL_SEND_METHOD = "messages/sendStream"
"""Canonical name that SSE_SEND_METHODS aliases are normalized to."""


class SessionExpiredError(Exception):
    """
    Unknown STLS session on the server side (cache-miss) and not
    regenerable.

    Raised by SecureA2AMiddleware.get_transport() when session_id isn't
    in the cache AND the configured key_session_provider fails to
    regenerate the key (normal case for the PQC provider: the session_key
    is born from an ephemeral KEM at handshake time, and isn't derivable
    from the session_id alone — unlike base/arqit_py, which support a
    lazy deterministic derivation).

    Mapped by all HTTP/SSE handlers onto ERR_SESSION_EXPIRED (-32004),
    so the client can distinguish "redo the handshake and retry"
    from a permanent application error (ERR_INTERNAL_ERROR).
    """
    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f"Expired or unknown STLS session: session_id={session_id}")


# ── SSE Heartbeat (keep-alive) ────────────────────────────────────────────────
# When the adapter stays for a long time without producing a chunk (e.g. a
# blocking delegation to a non-streaming sub-agent via RemoteA2aAgent), the
# SSE stream stays silent and any read/idle timeout along the path (httpx
# read timeout, nginx proxy_read_timeout, LB idle timeout, Node
# server.timeout) closes the connection. The heartbeat periodically emits
# an SSE comment line (": keep-alive") — spec-compliant, ignored by all
# SSE parsers — that keeps the socket alive and resets clients' read timeouts.
#
# On every beat, task_store.touch(task_id) is also run to renew the
# Task's updatedAt/TTL: a job longer than the TTL is no longer
# removed by cleanup while it's still running.
#
# Configuration: STLS_SSE_HEARTBEAT_SECONDS (default 15; 0 = disabled).

SSE_HEARTBEAT_SECONDS: float = float(os.getenv("STLS_SSE_HEARTBEAT_SECONDS", "15"))
"""Interval in seconds between SSE keep-alives. 0 disables the heartbeat."""

SSE_KEEPALIVE_LINE: str = ": keep-alive\n\n"
"""SSE comment line emitted as heartbeat (spec §9.2.6 — ignored by clients)."""


async def _iter_with_heartbeat(
    source: AsyncIterator,
    heartbeat_seconds: float = SSE_HEARTBEAT_SECONDS,
    on_beat: Optional[Callable] = None,
) -> AsyncIterator:
    """
    Wraps an async iterator, adding heartbeat beats.

    If `source` doesn't produce an item within `heartbeat_seconds`, yields
    ``None`` (a ping marker for the caller) and keeps waiting for the
    SAME item — the pending wait is never canceled, so no adapter
    chunk is ever lost or interrupted.

    Args:
        source:            async iterator to wrap (e.g. adapter.stream(...))
        heartbeat_seconds: beat interval; <=0 → pure passthrough
        on_beat:           optional coroutine factory invoked on every beat
                           (e.g. lambda: task_store.touch(task_id)); exceptions
                           are logged and don't interrupt the stream.

    Yields:
        `source`'s items interleaved with ``None`` on every beat.
    """
    if heartbeat_seconds <= 0:
        async for item in source:
            yield item
        return

    it = source.__aiter__()
    next_task: Optional[asyncio.Task] = None
    try:
        while True:
            next_task = asyncio.ensure_future(it.__anext__())
            while True:
                done, _ = await asyncio.wait({next_task}, timeout=heartbeat_seconds)
                if next_task in done:
                    break
                # Beat: no chunk within the interval → ping to the caller
                yield None
                if on_beat is not None:
                    try:
                        await on_beat()
                    except Exception as beat_exc:  # the touch must never break the stream
                        logger.debug("SSE heartbeat on_beat error (ignored): %s", beat_exc)
            try:
                item = next_task.result()
            except StopAsyncIteration:
                next_task = None
                return
            next_task = None
            yield item
    finally:
        # Early generator closure (client disconnected):
        # cancels the pending wait so as not to leave orphan tasks.
        if next_task is not None and not next_task.done():
            next_task.cancel()


# ── Timestamp and SSE payload helpers ───────────────────────────────────────────
# Extracted at module level: previously redefined as local functions
# in _stream_task_subscribe(), process_plain_stream(), and process_secure_stream().

def _now_iso() -> str:
    """Current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _make_status_payload(
    task_id: str,
    context_id: str,
    status: str,
    *,
    final: bool = False,
) -> dict:
    """
    Builds the payload for a TaskStatusUpdateEvent (A2A spec §4.2).

    Args:
        task_id:    Task UUID.
        context_id: Context UUID.
        status:     String or TaskState enum (e.g. "working", "completed").
        final:      True if this is the task's last event.
    """
    state_val = status if isinstance(status, str) else getattr(status, "value", str(status))
    return {
        "kind":      "status-update",
        "taskId":    task_id,
        "contextId": context_id,
        "status":    {"state": state_val},
        "final":     final,
        "timestamp": _now_iso(),
    }


def _make_artifact_payload(
    task_id: str,
    context_id: str,
    parts: list,
    *,
    append: bool,
    last_chunk: bool,
    artifact_id: str | None = None,
) -> dict:
    """
    Builds the payload for a TaskArtifactUpdateEvent (A2A spec §4.2).

    Args:
        task_id:     Task UUID.
        context_id:  Context UUID.
        parts:       List of A2A Parts already serialized as dicts.
        append:      True = the client appends the parts to the current artifact.
        last_chunk:  True = last chunk of the artifact.
        artifact_id: Artifact UUID; auto-generated if None.
    """
    return {
        "kind":      "artifact-update",
        "taskId":    task_id,
        "contextId": context_id,
        "artifact":  {
            "artifactId": artifact_id or str(_uuid.uuid4()),
            "parts": parts,
        },
        "append":    append,
        "lastChunk": last_chunk,
    }


class _SseMethodError(Exception):
    """
    Raised by process_plain_request when the method requires SSE.
    plain_a2a_handler intercepts it and responds with a StreamingResponse.
    """
    def __init__(self, plain_data: dict):
        self.plain_data = plain_data
        super().__init__(f"SSE method: {plain_data.get('method')}")


def _sse_event(event_type: str, payload: dict) -> str:
    """
    Formats a single SSE event per the spec (RFC 8895).

    Output:
        event: TaskStatusUpdateEvent\n
        data: {...}\n
        \n
    """
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"


class SecureA2ATransportAdapter:
    """
    Adapter that adds encryption to the HTTP transport for A2A

    Sits between the A2A client and the server, encrypting/decrypting
    the exchanged JSON-RPC messages.
    """

    def __init__(self, session_key: bytes, session_id: str, base_url: str):
        """
        Initializes the secure transport adapter

        Args:
            session_key: Shared session key for encryption
            session_id: Unique session ID
            base_url: Base URL of the remote A2A server
        """
        self.transport = SecureA2ATransport(session_key=session_key, session_id=session_id)
        self.base_url = base_url.rstrip('/')
        self.session_id = str(session_id)
        self.client = httpx.AsyncClient(timeout=30.0)
        # handles the case where secure-a2a is already present in the url
        _b = self.base_url
        self._secure_url = _b if _b.endswith("/secure-a2a") else f"{_b}/secure-a2a"
        self._stream_url  = (f"{_b}/stream" if _b.endswith("/secure-a2a") else f"{_b}/secure-a2a/stream")
    
    def _encrypt_message(self, message: Dict[str, Any], effective_method: str) -> Dict[str, Any]:
        """
        Internal helper: builds and encrypts an A2AMessage.
        Extracted to be shared by send_secure_message and stream_secure_message.
        """
        a2a_msg = A2AMessage(
            jsonrpc=message.get("jsonrpc", "2.0"),
            method=effective_method,
            params=message.get("params"),
            id=message.get("id"),
        )
        return self.transport.encrypt_message(a2a_msg)

    def _sse_effective_method(self, message: Dict[str, Any]) -> str:
        """
        Determines the effective method for an SSE request.
        Preserves native SSE methods (tasks/subscribe), normalizes the others
        to the canonical method messages/sendStream.
        """
        provided = message.get("method")
        return provided if provided in SSE_STREAM_METHODS else CANONICAL_SEND_METHOD

    async def stream_secure_message(
        self,
        message: Dict[str, Any],
        stream_timeout: float = 300.0,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        AsyncGenerator that yields each encrypted SSE event as it
        arrives from the server — true incremental chunk-by-chunk streaming.

        Unlike send_secure_message(stream=True), it doesn't internally
        collect all events before returning: each chunk is decrypted
        and yielded immediately, letting the caller process incoming
        tokens without waiting for completion.

        Each yielded chunk is a dict with the structure:

            # Artifact chunk (partial text/file/data)
            {
                "event":      "TaskArtifactUpdateEvent",
                "taskId":     str,
                "contextId":  str,
                "parts":      list[dict],   # A2A Part (TextPart, FilePart, DataPart)
                "append":     bool,         # True = appends to the previous chunk
                "last_chunk": bool,         # True = last chunk of the artifact
            }

            # Status update
            {
                "event":     "TaskStatusUpdateEvent",
                "taskId":    str,
                "contextId": str,
                "status":    str,           # "submitted"|"working"|"completed"|"failed"
                "final":     bool,          # True = terminal state
            }

            # Error from the server (unencrypted)
            {
                "event":  "error",
                "code":   int,
                "message": str,
            }

        Typical usage:

            async for chunk in adapter.stream_secure_message(message):
                if chunk["event"] == "TaskArtifactUpdateEvent":
                    for part in chunk["parts"]:
                        if part.get("type") == "text":
                            print(part["text"], end="", flush=True)
                elif chunk["event"] == "TaskStatusUpdateEvent" and chunk.get("final"):
                    break

        Args:
            message:        JSON-RPC dict with method, params, id.
            stream_timeout: read timeout in seconds between one SSE event
                            and the next (default 300s). With server v0.1.4+
                            emitting keep-alive every STLS_SSE_HEARTBEAT_SECONDS,
                            this timeout only fires if the server is really
                            dead. connect/write/pool stay at short values.
        """
        effective_method = self._sse_effective_method(message)
        encrypted = self._encrypt_message(message, effective_method)

        # Merges the base headers with the optional ones (e.g. Authorization, X-API-Key).
        # extra_headers overwrites on conflict.
        _headers = {
            "Content-Type":     "application/json",
            "Accept":           "text/event-stream",
            "X-A2A-Session-ID": self.session_id,
            **(extra_headers or {}),
        }
        # Structured timeout: the high value only applies to the read between chunks
        # (potentially long stream); connect/write/pool fail quickly
        # if the server is unreachable.
        _sse_timeout = httpx.Timeout(
            connect=10.0, read=stream_timeout, write=30.0, pool=10.0,
        )
        async with httpx.AsyncClient(timeout=_sse_timeout) as sse_client:
            async with sse_client.stream(
                "POST",
                self._stream_url,
                json=encrypted,
                headers=_headers,
            ) as resp:
                resp.raise_for_status()
                event_type = ""
                async for raw_line in resp.aiter_lines():
                    line = raw_line.strip()
                    if not line:
                        event_type = ""
                        continue
                    if line.startswith("event:"):
                        event_type = line[len("event:"):].strip()
                        continue
                    if not line.startswith("data:"):
                        continue

                    raw_data = line[len("data:"):].strip()

                    # ── error event: arrives UNENCRYPTED ──────────────────────
                    if event_type == "error":
                        try:
                            err_payload = json.loads(raw_data)
                        except Exception:
                            err_payload = {}
                        yield {
                            "event":   "error",
                            "code":    err_payload.get("code",    -32603),
                            "message": err_payload.get("message", "stream error"),
                        }
                        return

                    # ── All other events are AES-256-GCM encrypted ───────
                    try:
                        encrypted_chunk = json.loads(raw_data)
                        decrypted = self.transport.decrypt_message(encrypted_chunk)
                        payload = decrypted.params or {}
                    except Exception as e:
                        logger.warning("[stream] undecryptable chunk: %s", e)
                        continue

                    if event_type == "TaskArtifactUpdateEvent":
                        artifact = payload.get("artifact", {})
                        parts = [p for p in artifact.get("parts", []) if p]
                        if parts:
                            yield {
                                "event":      "TaskArtifactUpdateEvent",
                                "taskId":     payload.get("taskId",    ""),
                                "contextId":  payload.get("contextId", ""),
                                "parts":      parts,
                                "append":     payload.get("append",     True),
                                "last_chunk": payload.get("lastChunk",  False),
                            }

                    elif event_type == "TaskStatusUpdateEvent":
                        status = payload.get("status", {})
                        state = status.get("state", status) if isinstance(status, dict) else status
                        yield {
                            "event":     "TaskStatusUpdateEvent",
                            "taskId":    payload.get("taskId",    ""),
                            "contextId": payload.get("contextId", ""),
                            "status":    state,
                            "final":     payload.get("final", False),
                        }

    async def send_secure_message(
        self,
        message: Dict[str, Any],
        stream: bool = False,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Sends a secure message and returns a single dict with the result.

        Non-stream mode (default): synchronous POST /secure-a2a call.
        Stream mode (stream=True):  collects all SSE events and
            returns the aggregated result — backward-compatible with
            existing code. For true incremental streaming use stream_secure_message().

        extra_headers: additional headers (e.g. Authorization, X-API-Key) injected
            into all HTTP requests, both sync and SSE.
        """
        if not stream:
            # ── Standard path (not SSE-encrypted) ───────────────────────────
            encrypted = self._encrypt_message(message, message.get("method"))
            _sync_headers = {
                "Content-Type":     "application/json",
                "X-A2A-Session-ID": self.session_id,
                **(extra_headers or {}),
            }
            response = await self.client.post(
                self._secure_url,
                json=encrypted,
                headers=_sync_headers,
            )
            response.raise_for_status()
            decrypted_msg = self.transport.decrypt_message(response.json())
            return decrypted_msg.model_dump(exclude_none=True)

        # ── SSE path: collects via stream_secure_message() ──────────────
        # Maintains backward compatibility: returns a single aggregated dict
        # with all received Parts, exactly as before.
        # Whoever wants true streaming uses stream_secure_message() directly.
        collected_parts: list[dict] = []
        last_status = ""
        task_id     = ""
        context_id  = ""

        async for chunk in self.stream_secure_message(message, extra_headers=extra_headers):
            event = chunk["event"]

            if event == "error":
                return {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {"code": chunk["code"], "message": chunk["message"]},
                }
            elif event == "TaskArtifactUpdateEvent":
                collected_parts.extend(chunk["parts"])
            elif event == "TaskStatusUpdateEvent":
                last_status = chunk["status"]
                task_id     = chunk.get("taskId",    task_id)
                context_id  = chunk.get("contextId", context_id)

        full_text = "".join(
            p["text"] for p in collected_parts
            if p.get("type") == "text" and p.get("text")
        )
        return {
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "result": {
                "taskId":    task_id,
                "contextId": context_id,
                "status":    last_status,
                "artifacts": [{"name": "response", "parts": collected_parts}],
                "history":   [{"role": "agent",    "parts": collected_parts}],
                **({"text": full_text} if full_text else {}),
            },
        }

    async def close(self):
        """Closes the HTTP client"""
        await self.client.aclose()


class SecureA2AHTTPMiddleware:
    """
    HTTP middleware for A2A servers that handles encryption/decryption

    Integrates with FastAPI/Starlette to process encrypted A2A requests.
    Uses AgentAdapter to support different AI frameworks.
    """

    def __init__(
        self,
        key_session_provider: KeySessionProvider,
        agent_adapter: Optional[AgentAdapter] = None,
        auth: Optional[AuthConfig] = None,
        api_key_manager: Optional[Any] = None,
    ):
        """
        Args:
            key_session_provider: Provider for STLS session keys
            agent_adapter:        Optional adapter (if None, must be passed
                                  to process_secure_request)
            auth:                 Legacy authorization configuration (AuthConfig).
                                  None = no validation (backward-compatible).
            api_key_manager:      Enterprise ApiKeyManager (optional).
                                  If present, takes priority over ``auth``.
                                  Supports Redis, File, Postgres as backends.
        """
        self.key_session_provider = key_session_provider
        # LRU+TTL SessionCache shared with stls-mcp (stls_core).
        # Env vars read internally by SessionCache:
        #   STLS_SESSION_CACHE_MAX  — max number of sessions (default 256)
        #   STLS_SESSION_TTL        — TTL in seconds for an inactive session (default 3600)
        self.sessions: SessionCacheBase = SessionCacheFactory.build()
        self.agent_adapter = agent_adapter
        self.auth = auth
        self.api_key_manager = api_key_manager
        self._closed = False

    def _check_auth(self, request: Any) -> Optional[Any]:
        """
        Verifies the Starlette/FastAPI request's credentials.

        Reads in priority order:
          1. Authorization: Bearer <token>  (header)
          2. X-API-Key: <key>               (header)
          3. ?api_key=<key>                 (query param — useful for browser-based SSE)

        Returns:
            None          → authorized (or auth not configured)
            JSONResponse  → HTTP 401 with JSON-RPC body to return to the client
        """
        if not self.auth or not self.auth.enabled:
            return None

        authorization = request.headers.get("Authorization")
        api_key = (
            request.headers.get("X-API-Key")
            or request.query_params.get("api_key")
        )

        if self.auth.check(authorization, api_key):
            return None

        # Builds the WWW-Authenticate header appropriate to the enabled schemes
        schemes = []
        if self.auth.bearer_tokens:
            schemes.append(f'Bearer realm="{self.auth.realm}"')
        if self.auth.api_keys:
            schemes.append(f'ApiKey realm="{self.auth.realm}"')

        from starlette.responses import JSONResponse as _JSONResponse
        logger.warning(
            "Auth failed: %s %s — missing or invalid credentials",
            request.method, request.url.path,
        )
        return _JSONResponse(
            status_code=401,
            content={
                "jsonrpc": "2.0",
                "error": {
                    "code": -32600,
                    "message": "Unauthorized: missing or invalid credentials",
                },
                "id": None,
            },
            headers={"WWW-Authenticate": ", ".join(schemes)},
        )
    
    async def _check_auth_async(self, request: Any) -> Optional[Any]:
        """
        Async version of _check_auth.

        Priority:
          1. ApiKeyManager (enterprise, Redis/File/Postgres) — if configured.
          2. Legacy AuthConfig (in-memory, timing-safe) — backward-compatible fallback.
          3. No auth configured → None (open access).

        Returns:
            None          → authorized
            JSONResponse  → HTTP 401 with JSON-RPC body
        """
        from starlette.responses import JSONResponse as _JSONResponse

        # ── 1. Enterprise ApiKeyManager ───────────────────────────────────────
        if self.api_key_manager is not None:
            try:
                result = await self.api_key_manager.validate_request(request)
                if result.ok:
                    return None
                logger.warning(
                    "ApiKeyManager auth denied: %s %s — %s",
                    request.method, request.url.path, result.result.value,
                )
                return _JSONResponse(
                    status_code=401,
                    content={
                        "jsonrpc": "2.0",
                        "error": {
                            "code": -32600,
                            "message": "Unauthorized: missing or invalid credentials",
                        },
                        "id": None,
                    },
                    headers={"WWW-Authenticate": result.www_authenticate_header()},
                )
            except Exception as exc:
                logger.error("ApiKeyManager.validate_request() error: %s", exc)
                return _JSONResponse(
                    status_code=503,
                    content={
                        "jsonrpc": "2.0",
                        "error": {"code": -32603, "message": "Auth service unavailable"},
                        "id": None,
                    },
                )

        # ── 2. Legacy AuthConfig (sync, no await needed) ──────────────
        return self._check_auth(request)

    def _extract_http_context(self, request: Any) -> Dict[str, Any]:
        """
        Extracts the user context from the request's HTTP headers.

        Reads the proprietary STLS headers and normalizes them into a dict
        compatible with request.context — the same structure used
        by message.metadata and params.metadata.

        Supported headers:
            X-STLS-User-Id   → context["user_id"]   (string)
            X-STLS-Profile   → context["profile"]    (JSON string → dict)

        Works independently of ApiKeyManager and AuthConfig:
        the headers are always read, even on unauthenticated endpoints.
        If the headers are absent or malformed, returns an empty dict
        (no exception, no side effect).

        Priority in the framework (unchanged, backward-compatible):
            1. message.metadata   — direct STLS client
            2. params.metadata    — ContextualRemoteA2aAgent / ADK
            3. X-STLS-* headers   ← THIS METHOD
            4. "default_user"     — final fallback
        """
        ctx: Dict[str, Any] = {}

        user_id = request.headers.get("X-STLS-User-Id", "").strip()
        if user_id:
            ctx["user_id"] = user_id

        profile_raw = request.headers.get("X-STLS-Profile", "").strip()
        if profile_raw:
            try:
                import json as _json
                profile = _json.loads(profile_raw)
                if isinstance(profile, dict):
                    ctx["profile"] = profile
                else:
                    logger.warning(
                        "_extract_http_context: X-STLS-Profile isn't a JSON object "
                        "(type=%s) — ignored", type(profile).__name__
                    )
            except Exception:
                logger.warning(
                    "_extract_http_context: X-STLS-Profile isn't valid JSON — ignored. "
                    "Received value: %.80s", profile_raw
                )

        if ctx:
            logger.debug(
                "_extract_http_context: context extracted from headers → keys=%s",
                list(ctx.keys()),
            )
        return ctx

    async def close(self):
        """Closes the middleware and releases resources"""
        if self._closed:
            return

        try:
            # Stops ApiKeyManager (BackgroundRefresher + Redis/PG connections)
            if self.api_key_manager is not None:
                try:
                    await self.api_key_manager.stop()
                except Exception as e:
                    import warnings
                    warnings.warn(f"Error stopping ApiKeyManager: {e}", RuntimeWarning)

            # Closes the adapter if it has a close method
            if self.agent_adapter and hasattr(self.agent_adapter, 'close'):
                await self.agent_adapter.close()
        except Exception as e:
            import warnings
            warnings.warn(f"Error closing agent adapter: {e}", RuntimeWarning)
        finally:
            self._closed = True
    
    def get_transport(self, session_id: str) -> SecureA2ATransport:
        """
        Gets or creates a transport for the session

        Args:
            session_id: Session ID

        Returns:
            Secure transport for the session
        """
        if not session_id:
            logger.error("Invalid session_id provided to get_transport: %r", session_id)
            raise ValueError("session_id invalid or missing")

        # SessionCache.get() internally manages TTL and LRU refresh.
        # Note: the cache holds stls_core.StlsTransport (pure crypto);
        # here we wrap it in SecureA2ATransport (A2AMessage-aware) to
        # expose the same interface as before to process_secure_request.
        cached = self.sessions.get(session_id)
        if cached is not None:
            logger.debug("Reusing cached transport for session_id=%s", session_id)
            wrapper = SecureA2ATransport.__new__(SecureA2ATransport)
            wrapper.session_id = session_id
            wrapper.session_key = None  # not kept on the cache side, only in the transport
            wrapper._transport = cached
            return wrapper

        logger.info("Creating secure transport for session_id=%s", session_id)

        try:
            def _resolve_session_key_data(provider_result: Any) -> Any:
                """Normalizes the provider's result (coroutine/Java Future/direct value)."""
                if inspect.isawaitable(provider_result):
                    logger.debug("Session key provider result is awaitable; resolving with asyncio.run")
                    return asyncio.run(provider_result)

                # JPype compatibility: a Java CompletableFuture typically exposes get().
                if hasattr(provider_result, 'get') and callable(getattr(provider_result, 'get')):
                    logger.debug("Session key provider result exposes get(); resolving as Java future-like object")
                    return provider_result.get()

                logger.debug(
                    "Session key provider returned direct value (type=%s)",
                    type(provider_result).__name__,
                )
                return provider_result

            # Runs the async coroutine in a separate thread with its own loop
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    _resolve_session_key_data,
                    self.key_session_provider.getSessionKeyData(session_id),
                )
                raw_session_key = future.result()  # blocks until the result

            logger.info(
                "Session key data resolved for session_id=%s (raw_type=%s)",
                session_id,
                type(raw_session_key).__name__,
            )

            if raw_session_key is None:
                logger.error("Session key provider returned None for session_id=%s", session_id)
                raise RuntimeError("Session key not available")

            try:
                session_key = bytes(raw_session_key)
            except Exception as e:
                logger.exception(
                    "Failed converting session key to bytes for session_id=%s (type=%s)",
                    session_id,
                    type(raw_session_key).__name__,
                )
                raise RuntimeError("Invalid session key format") from e

            if not session_key:
                logger.error("Session key is empty for session_id=%s", session_id)
                raise RuntimeError("Empty session key")

            transport = SecureA2ATransport(session_key, session_id)
            # Registers the session's raw data in the shared SessionCache.
            # On this lazy path the Arqit kid isn't known (getSessionKeyData was
            # called with the STLS session_id as a fallback), so we use
            # session_id as the kid too — consistent with stls-a2a's
            # explicit pre-handshake flow, documented in CHANGELOG 0.1.1.
            self.sessions.put(session_id, session_id, session_key)

            logger.info(
                "Secure transport created for session_id=%s (session_key_len=%d, cache=%d)",
                session_id, len(session_key), len(self.sessions),
            )
            return transport

        except SessionExpiredError:
            raise
        except Exception as e:
            logger.exception("Unable to create secure transport for session_id=%s", session_id)
            # Non-regenerable cache-miss (typical for PQC: no deterministic
            # derivation possible from session_id alone). Normalizes into
            # SessionExpiredError so the caller can distinguish this
            # case — for which the only sensible action is to re-handshake — from a
            # generic internal failure.
            raise SessionExpiredError(session_id) from e
            raise

    async def process_secure_request(
        self,
        encrypted_data: Dict[str, Any],
        agent_adapter: Optional[AgentAdapter] = None,
        http_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Processes an encrypted A2A request using AgentAdapter

        Args:
            encrypted_data: Received encrypted data
            agent_adapter: Agent's adapter (optional if passed to __init__)

        Returns:
            Encrypted response
        """
        logger.debug(f"Processing secure request with encrypted data: {encrypted_data}")
        session_id = encrypted_data.get('session_id')
        logger.debug(f"Extracted session_id: {session_id}")
        if not session_id:
            raise ValueError("Session ID missing from the request")

        # Uses the passed adapter or the configured one
        adapter = agent_adapter or self.agent_adapter
        if not adapter:
            raise ValueError("No AgentAdapter configured")

        # Gets the transport for this session
        transport = self.get_transport(session_id)
        logger.debug(f"Transport obtained for session {session_id}")
        logger.debug(f"Encrypted data received: {encrypted_data}")
        logger.debug(f"Transport session ID: {transport.session_id}")
        logger.debug(f"Transport session key: {transport.session_key}")

        # Decrypts the message
        decrypted_msg = transport.decrypt_message(encrypted_data)
        logger.debug(f"Decrypted message: {decrypted_msg}")

        # Converts the A2A message into an AgentRequest
        agent_request = a2a_to_agent_request({
            'jsonrpc': decrypted_msg.jsonrpc,
            'method': decrypted_msg.method,
            'params': decrypted_msg.params,
            'id': decrypted_msg.id
        })
        if http_context:
            agent_request = agent_request.model_copy(
                update={"context": {**(agent_request.context or {}), **http_context}}
            )

        # Invokes the agent via the adapter
        agent_response = await adapter.handle_request(agent_request)
        logger.debug(f"Agent response: {agent_response}")

        # Converts the AgentResponse into an A2A message
        response_dict = agent_response_to_a2a(agent_response)

        # Converts into an A2AMessage
        response_msg = A2AMessage(
            jsonrpc=response_dict.get('jsonrpc', '2.0'),
            result=response_dict.get('result'),
            **({'error': response_dict['error']} if response_dict.get('error') else {}),
            id=response_dict.get('id')
        )
        logger.debug(f"Response message before encryption: {response_msg}")

        # Encrypts the response
        encrypted_response = transport.encrypt_message(response_msg.model_dump(exclude_none=True))
        
        return encrypted_response

    async def _stream_task_subscribe(
        self,
        params: Dict[str, Any],
        serialize_event: "Callable[[dict], str]",
        poll_interval: float = 0.5,
        max_poll_seconds: float = 300.0,
    ) -> AsyncIterator[str]:
        """
        Implements tasks/subscribe (spec §3.1.6).

        Attaches to an **existing** Task in the TaskStore via taskId
        and streams SSE updates until it completes.

        Args:
            params:           JSON-RPC message params (must contain 'taskId')
            serialize_event:  callable that serializes a dict payload into an SSE data string.
                              For the encrypted channel: encrypts with AES-256-GCM.
                              For the plain channel:     direct json.dumps.
            poll_interval:    seconds between one poll and the next (default 0.5s)
            max_poll_seconds: maximum timeout (default 300s)
        """
        TERMINAL = {"completed", "failed", "canceled"}

        # ── Params validation ────────────────────────────────────────────────
        task_id = params.get("taskId")
        if not task_id:
            yield _sse_event("error", {
                "code": -32602,
                "message": "tasks/subscribe requires 'taskId' in params"
            })
            return

        task = await task_store.get(task_id)
        if not task:
            yield _sse_event("error", {
                "code": -32001,
                "message": f"Task '{task_id}' not found in the TaskStore"
            })
            return

        context_id = task.contextId

        def _emit_artifacts(t) -> list[str]:
            lines = []
            for artifact in t.artifacts:
                parts = [
                    p.model_dump(by_alias=True) if hasattr(p, "model_dump") else p
                    for p in artifact.parts
                ]
                parts = self._maybe_process_csv_key(parts)
                ap = _make_artifact_payload(
                    task_id, context_id, parts,
                    append=False, last_chunk=True,
                )
                lines.append(
                    f"event: TaskArtifactUpdateEvent\ndata: {serialize_event(ap)}\n\n"
                )
            return lines

        # ── Task already terminal: immediate response ────────────────────────────
        if task.status.state in TERMINAL:
            if task.status.state == TaskState.COMPLETED:
                for line in _emit_artifacts(task):
                    yield line
            yield (
                f"event: TaskStatusUpdateEvent\n"
                f"data: {serialize_event(_make_status_payload(task_id, context_id, task.status.state, final=True))}\n\n"
            )
            return

        # ── Task still active: polling with timeout ───────────────────────────
        elapsed = 0.0
        while elapsed < max_poll_seconds:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            task = await task_store.get(task_id)
            if not task:
                yield _sse_event("error", {
                    "code": -32001,
                    "message": f"Task '{task_id}' expired or removed during subscribe"
                })
                return

            if task.status.state in TERMINAL:
                if task.status.state == TaskState.COMPLETED:
                    for line in _emit_artifacts(task):
                        yield line
                yield (
                    f"event: TaskStatusUpdateEvent\n"
                    f"data: {serialize_event(_make_status_payload(task_id, context_id, task.status.state, final=True))}\n\n"
                )
                return

            yield (
                f"event: TaskStatusUpdateEvent\n"
                f"data: {serialize_event(_make_status_payload(task_id, context_id, 'working'))}\n\n"
            )

        yield _sse_event("error", {
            "code": -32603,
            "message": f"tasks/subscribe timeout: task '{task_id}' not completed within {max_poll_seconds}s"
        })

    async def process_plain_request(
        self,
        plain_data: Dict[str, Any],
        agent_adapter: Optional[AgentAdapter] = None,
        http_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Handles a standard (unencrypted) JSON-RPC request.

        Used by the /a2a endpoint for interoperability with standard
        A2A clients (a2a-python SDK) that don't implement the STLS layer.

        Args:
            plain_data:    already-deserialized JSON-RPC body
            agent_adapter: optional adapter (uses self.agent_adapter if None)
            http_context:  user context extracted from HTTP headers
                           (X-STLS-User-Id, X-STLS-Profile).
                           Priority 3 — overwritten by message.metadata
                           and params.metadata if present.

        Returns:
            JSON-RPC response dict (unencrypted)
        """
        from .stls_a2a_adapters_model import ERR_INVALID_REQUEST, ERR_INTERNAL_ERROR

        adapter = agent_adapter or self.agent_adapter
        rpc_id  = plain_data.get("id")

        def _error(code: int, msg: str) -> Dict[str, Any]:
            return {"jsonrpc": "2.0", "error": {"code": code, "message": msg}, "id": rpc_id}

        if not adapter:
            return _error(ERR_INVALID_REQUEST, "No AgentAdapter configured")

        method = plain_data.get("method")
        if not method:
            return _error(ERR_INVALID_REQUEST, "Invalid request: 'method' field is required")

        # ── Intercepts SSE methods before reaching handle_request ────────
        # The a2a-python SDK can send messages/sendStream (alias "message/stream")
        # to the base /a2a endpoint. process_plain_request can't return SSE:
        # it raises _SseMethodError, which plain_a2a_handler intercepts and
        # converts into a text/event-stream StreamingResponse.
        if method in SSE_STREAM_METHODS:
            raise _SseMethodError(plain_data)

        agent_request = a2a_to_agent_request({
            "jsonrpc": plain_data.get("jsonrpc", "2.0"),
            "method":  method,
            "params":  plain_data.get("params"),
            "id":      rpc_id,
        })
        # Injects http_context as the AgentRequest's base context.
        # _handle_messages_send/stream will overwrite it with message.metadata
        # and params.metadata if present (higher priority).
        if http_context:
            agent_request = agent_request.model_copy(
                update={"context": {**(agent_request.context or {}), **http_context}}
            )

        try:
            agent_response = await adapter.handle_request(agent_request)
            return agent_response_to_a2a(agent_response)
        except Exception as e:
            logger.exception("Error in process_plain_request")
            return _error(ERR_INTERNAL_ERROR, str(e))

    async def process_plain_stream(
        self,
        plain_data: Dict[str, Any],
        agent_adapter: Optional[AgentAdapter] = None,
        http_context: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[str]:
        """
        Handles a standard (unencrypted) SSE request.

        Used by the /a2a/stream endpoint for interoperability with
        standard A2A clients. SSE events are emitted as plain JSON —
        identical in structure to the encrypted ones, without the STLS wrapper.

        Args:
            http_context: user context from X-STLS-* headers (priority 3).

        Yields:
            str — spec-compliant SSE lines (event: / data: / blank line)
        """
        from .stls_a2a_adapters_model import ERR_INVALID_REQUEST, ERR_INTERNAL_ERROR

        adapter = agent_adapter or self.agent_adapter
        if not adapter:
            yield _sse_event("error", {"code": ERR_INVALID_REQUEST,
                                       "message": "No AgentAdapter configured"})
            return

        method = plain_data.get("method")
        rpc_id = plain_data.get("id")

        if not method:
            yield _sse_event("error", {"code": ERR_INVALID_REQUEST,
                                       "message": "Invalid request: 'method' field is required"})
            return

        if method not in SSE_STREAM_METHODS:
            yield _sse_event("error", {
                "code": -32601,
                "message": f"Method '{method}' not supported on stream endpoint. "
                           "Use messages/sendStream or tasks/subscribe.",
            })
            return

        if method in SSE_SEND_METHODS:
            method = CANONICAL_SEND_METHOD

        params = plain_data.get("params") or {}

        # Plain serializer: wraps in a JSON-RPC 2.0 envelope
        # required by the a2a-python SDK to validate SendStreamingMessageResponse.
        def _plain_event(payload: dict) -> str:
            return json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": payload})

        # Builds the AgentRequest's base context from http_context.
        # _handle_messages_send_stream will overwrite it with message.metadata
        # and params.metadata if present (higher priority).
        _stream_base_context: Dict[str, Any] = dict(http_context or {})

        # ── tasks/subscribe ───────────────────────────────────────────────────
        if method in SSE_SUBSCRIBE_METHODS:
            async for sse_line in self._stream_task_subscribe(params, _plain_event):
                yield sse_line
            return

        # ── messages/sendStream ───────────────────────────────────────────────
        # Looks for contextId in all spec-compliant positions:
        #   1. params.contextId          (top-level, some implementations)
        #   2. params.message.contextId  (canonical position, A2A spec §4.1.2)
        context_id = (
            params.get("contextId")
            or params.get("message", {}).get("contextId")
            or None
        )
        pre_task   = await task_store.create(context_id=context_id)
        task_id    = pre_task.taskId
        context_id = pre_task.contextId

        yield f"event: TaskStatusUpdateEvent\ndata: {_plain_event(_make_status_payload(task_id, context_id, 'submitted'))}\n\n"
        yield f"event: TaskStatusUpdateEvent\ndata: {_plain_event(_make_status_payload(task_id, context_id, 'working'))}\n\n"

        try:
            if hasattr(adapter, "stream") and callable(adapter.stream):
                params_for_adapter = {**params, "_stls_task_id": task_id, "contextId": context_id}
                if http_context:
                    # Passes http_context as _stls_http_context in params:
                    # _handle_messages_send_stream reads it as the base context
                    # (priority 3, overwritten by message.metadata/params.metadata).
                    params_for_adapter["_stls_http_context"] = http_context
                async for chunk in _iter_with_heartbeat(
                    adapter.stream(params_for_adapter),
                    SSE_HEARTBEAT_SECONDS,
                    on_beat=lambda: task_store.touch(task_id),
                ):
                    if chunk is None:
                        # Beat: no chunk from the adapter within the interval
                        yield SSE_KEEPALIVE_LINE
                        continue
                    chunk_parts = [
                        p.model_dump(by_alias=True) if hasattr(p, "model_dump") else p
                        for p in chunk.get("parts", [])
                    ]
                    if not chunk_parts:
                        continue
                    ap = _make_artifact_payload(
                        task_id, context_id, chunk_parts,
                        append=chunk.get("append", True),
                        last_chunk=chunk.get("last_chunk", False),
                    )
                    yield f"event: TaskArtifactUpdateEvent\ndata: {_plain_event(ap)}\n\n"
                    if not chunk.get("last_chunk"):
                        yield f"event: TaskStatusUpdateEvent\ndata: {_plain_event(_make_status_payload(task_id, context_id, 'working'))}\n\n"
            else:
                # Synchronous fallback
                agent_request = a2a_to_agent_request({
                    "jsonrpc": plain_data.get("jsonrpc", "2.0"),
                    "method":  "messages/send",
                    "params":  params,
                    "id":      rpc_id,
                })
                if _stream_base_context:
                    agent_request = agent_request.model_copy(
                        update={"context": {**(agent_request.context or {}), **_stream_base_context}}
                    )
                # Heartbeat on the synchronous path too: the whole handle_request()
                # can block for minutes (adapter without stream()).
                _pending = asyncio.ensure_future(adapter.handle_request(agent_request))
                try:
                    while True:
                        done, _ = await asyncio.wait(
                            {_pending},
                            timeout=SSE_HEARTBEAT_SECONDS if SSE_HEARTBEAT_SECONDS > 0 else None,
                        )
                        if _pending in done:
                            break
                        yield SSE_KEEPALIVE_LINE
                        try:
                            await task_store.touch(task_id)
                        except Exception as beat_exc:
                            logger.debug("SSE heartbeat touch error (ignored): %s", beat_exc)
                except BaseException:
                    if not _pending.done():
                        _pending.cancel()
                    raise
                agent_response = _pending.result()
                response_dict  = agent_response_to_a2a(agent_response)
                fallback_parts: list = []
                r = response_dict.get("result") or {}
                if isinstance(r, dict):
                    if r.get("parts"):
                        fallback_parts = [
                            p.model_dump(by_alias=True) if hasattr(p, "model_dump") else p
                            for p in r["parts"]
                        ]
                    else:
                        text_result = r.get("text") or r.get("output") or json.dumps(r)
                        fallback_parts = [{"type": "text", "text": text_result,
                                           "mimeType": "text/plain"}]
                yield f"event: TaskArtifactUpdateEvent\ndata: {_plain_event(_make_artifact_payload(task_id, context_id, fallback_parts, append=False, last_chunk=True))}\n\n"

            yield f"event: TaskStatusUpdateEvent\ndata: {_plain_event(_make_status_payload(task_id, context_id, 'completed', final=True))}\n\n"

        except Exception as e:
            logger.exception("Plain SSE: error during the adapter's stream")
            yield f"event: TaskStatusUpdateEvent\ndata: {_plain_event(_make_status_payload(task_id, context_id, 'failed', final=True))}\n\n"
            yield _sse_event("error", {"code": ERR_INTERNAL_ERROR, "message": str(e)})

    # ── CSV_KEY post-processing ──────────────────────────────────────────────
    # Intercepts the [CSV_KEY: key=... file_name=...] tag in chunks sent to Sofia.
    # The tag is inserted by imsi_sparkle_query.export_sim_csv: the CSV is saved
    # in Redis with a 600s TTL, and the key is passed in the response text.
    # This method reads the CSV from Redis, registers it in BDA_T_FILES_DOWN (Oracle),
    # and replaces the tag with the webrepository download link.
    #
    # Activates ONLY if AG_DOWNLOAD_BASE_URL and DB_HOST are configured
    # (coordinator container) → no-op for all other agents.
    #
    # Called by process_secure_stream before sending each artifact to Sofia.
    # ALWAYS runs, regardless of any cancel from the ADK runner.
    @staticmethod
    def _maybe_process_csv_key(chunk_parts: list) -> list:
        import os as _os
        import re as _re_check
        # Diagnostic log — ALWAYS written, even if no-op
        _has_csv_key = any(
            _re_check.search(r"\[CSV_KEY:", p.get("text", ""))
            for p in chunk_parts if isinstance(p, dict) and p.get("type") == "text"
        )
        if _has_csv_key:
            logger.info(f"[csv_key] _maybe_process_csv_key: found CSV_KEY tag in {len(chunk_parts)} parts")
        _base_url = _os.getenv("AG_DOWNLOAD_BASE_URL", "").rstrip("/")
        _db_host  = _os.getenv("DB_HOST")
        logger.info(f"[csv_key] AG_DOWNLOAD_BASE_URL={'set' if _base_url else 'NOT SET'} DB_HOST={'set' if _db_host else 'NOT SET'}")
        if not _base_url or not _db_host:
            if _has_csv_key:
                logger.warning("[csv_key] tag found but AG_DOWNLOAD_BASE_URL or DB_HOST not configured — no-op")
            return chunk_parts

        import re as _re
        _CSV_KEY_RE = _re.compile(r"\[CSV_KEY:\s*key=(\S+)\s+file_name=(\S+)\]")

        result = []
        for part in chunk_parts:
            if part.get("type") != "text":
                result.append(part)
                continue
            text = part.get("text", "")
            m = _CSV_KEY_RE.search(text)
            if not m:
                result.append(part)
                continue

            redis_key = m.group(1)
            file_name = m.group(2)
            logger.info(f"[csv_key] tag intercepted: key={redis_key} file={file_name}")

            try:
                import redis as _redis_mod
                import uuid as _uuid_mod
                import oracledb as _oracledb

                # 1. Reads the CSV from Redis
                _redis_url = _os.getenv("REDIS_URL", "redis://localhost:6379")
                _r = _redis_mod.Redis.from_url(_redis_url, socket_timeout=5)
                csv_bytes = _r.get(redis_key)
                if not csv_bytes:
                    logger.error(f"[csv_key] Redis key not found or expired: {redis_key}")
                    result.append({"type": "text", "text": _CSV_KEY_RE.sub(
                        "⚠️ CSV not available (Redis key expired). Please repeat the request.", text
                    )})
                    continue
                _r.delete(redis_key)
                logger.info(f"[csv_key] CSV read from Redis: {len(csv_bytes)} bytes")

                # 2. Registers in Oracle BDA_T_FILES_DOWN
                from sparkleagcommon import decrypt_password as _dec
                _u_id = _uuid_mod.uuid4().hex
                _dsn  = _oracledb.makedsn(
                    _db_host,
                    _os.getenv("DB_PORT"),
                    service_name=_os.getenv("DB_SERVICE"),
                )
                _orcl = _oracledb.connect(
                    user=_os.getenv("DB_USER"),
                    password=_dec(_os.getenv("DB_PSW")),
                    dsn=_dsn,
                )
                _cur = _orcl.cursor()
                _cur.setinputsizes(None, None, None, _oracledb.DB_TYPE_BLOB, None)
                _cur.execute(
                    "INSERT INTO BDA_T_FILES_DOWN "
                    "(u_id, content_type, file_name, blob_file, duration) "
                    "VALUES (:1, :2, :3, :4, :5)",
                    [_u_id, "text/csv", file_name, csv_bytes, 300],
                )
                _orcl.commit()
                _cur.close()
                _orcl.close()

                _download_url  = f"{_base_url}/{_u_id}/downloadFile"
                _download_link = f"[{file_name}]({_download_url})"
                _prefix = _CSV_KEY_RE.sub("", text).strip()
                _final  = (
                    f"{_prefix}\n\n📥 **Download CSV:** {_download_link}\n"
                    f"_(link valid for 5 minutes)_"
                ).strip()
                logger.info(f"[csv_key] CSV registered in BDA_T_FILES_DOWN: {_u_id}")
                result.append({"type": "text", "text": _final})

            except Exception as _exc:
                import traceback as _tb
                logger.error(f"[csv_key] Error:\n{_tb.format_exc()}")
                result.append({"type": "text", "text": _CSV_KEY_RE.sub(
                    "⚠️ Error generating the download link.", text
                )})

        return result

    async def process_secure_stream(
        self,
        encrypted_data: Dict[str, Any],
        agent_adapter: Optional[AgentAdapter] = None,
        http_context: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[str]:
        """
        Async generator — decrypts the request, calls the adapter's
        stream, and yields SSE events (spec §4.2).

        Each SSE event has the form:
            event: TaskStatusUpdateEvent | TaskArtifactUpdateEvent
            data: <json payload encrypted with the session's AES-256-GCM>

        The `data` payload is the dict returned by transport.encrypt_message()
        serialized as JSON: the STLS client must decrypt each chunk
        separately with the same session key.

        Args:
            encrypted_data: received AES-256-GCM POST body
            agent_adapter:  optional adapter (uses self.agent_adapter if None)

        Yields:
            str — SSE lines ready for StreamingResponse
        """
        from .stls_a2a_adapters_model import ERR_SESSION_EXPIRED
        session_id = encrypted_data.get('session_id')
        if not session_id:
            yield _sse_event("error", {"code": -32600, "message": "session_id missing"})
            return

        adapter = agent_adapter or self.agent_adapter
        if not adapter:
            yield _sse_event("error", {"code": -32600, "message": "No AgentAdapter configured"})
            return

        # ── Request decryption ────────────────────────────────────────────
        try:
            transport = self.get_transport(session_id)
            decrypted_msg = transport.decrypt_message(encrypted_data)
        except SessionExpiredError as e:
            yield _sse_event("error", {"code": ERR_SESSION_EXPIRED, "message": str(e)})
            return
        except Exception as e:
            logger.exception("SSE: decryption error")
            yield _sse_event("error", {"code": -32603, "message": f"Decrypt error: {e}"})
            return

        if decrypted_msg.method not in SSE_STREAM_METHODS:
            yield _sse_event("error", {
                "code": -32601,
                "message": f"Method '{decrypted_msg.method}' not supported on stream endpoint. "
                           "Use messages/sendStream or tasks/subscribe."
            })
            return

        if decrypted_msg.method in SSE_SEND_METHODS:
            decrypted_msg = decrypted_msg.model_copy(update={"method": CANONICAL_SEND_METHOD})

        params = decrypted_msg.params or {}

        # ── Common encrypted serializer ─────────────────────────────────────
        def _encrypt_event(payload: dict) -> str:
            encrypted = transport.encrypt_message(
                A2AMessage(
                    jsonrpc="2.0", method="event",
                    params=payload, id=decrypted_msg.id,
                ).model_dump(exclude_none=True)
            )
            return json.dumps(encrypted)

        # ── tasks/subscribe: attaches to an existing Task ──────────────────
        # Doesn't create a new Task or invoke the adapter. Looks up taskId in params,
        # then watches the TaskStore until the task reaches a terminal state.
        if decrypted_msg.method in SSE_SUBSCRIBE_METHODS:
            async for sse_line in self._stream_task_subscribe(params, _encrypt_event):
                yield sse_line
            return

        # ── messages/sendStream: creates a new Task and invokes the adapter ──────────
        # We pre-create the Task in the TaskStore BEFORE emitting any SSE event,
        # so the task_id the client receives via SSE matches exactly the
        # one it can later call tasks/get on.
        # Looks for contextId in all spec-compliant positions:
        #   1. params.contextId          (top-level, some implementations)
        #   2. params.message.contextId  (canonical position, A2A spec §4.1.2)
        context_id = (
            params.get("contextId")
            or params.get("message", {}).get("contextId")
            or None
        )
        pre_task   = await task_store.create(context_id=context_id)
        task_id    = pre_task.taskId
        context_id = pre_task.contextId   # uses the contextId actually saved

        # ── SUBMITTED ────────────────────────────────────────────────────────
        yield f"event: TaskStatusUpdateEvent\ndata: {_encrypt_event(_make_status_payload(task_id, context_id, 'submitted'))}\n\n"

        # ── WORKING ──────────────────────────────────────────────────────────
        yield f"event: TaskStatusUpdateEvent\ndata: {_encrypt_event(_make_status_payload(task_id, context_id, 'working'))}\n\n"

        # ── Stream from the adapter ───────────────────────────────────────────────
        try:
            if hasattr(adapter, 'stream') and callable(adapter.stream):
                # We pass the pre-created task_id to the adapter: _handle_messages_send_stream()
                # uses it instead of creating a new Task, guaranteeing that the Task in
                # the TaskStore has exactly the same UUID as the SSE events emitted above.
                params_for_adapter = {**params, "_stls_task_id": task_id, "contextId": context_id}
                async for chunk in _iter_with_heartbeat(
                    adapter.stream(params_for_adapter),
                    SSE_HEARTBEAT_SECONDS,
                    on_beat=lambda: task_store.touch(task_id),
                ):
                    if chunk is None:
                        # Beat: no chunk from the adapter within the interval.
                        # Plain SSE comment line: carries no payload,
                        # so it doesn't require encryption (protocol-level, §9.2.6).
                        yield SSE_KEEPALIVE_LINE
                        continue
                    # chunk["parts"] is an A2A list[Part] produced by _from_genai_parts.
                    chunk_parts = [
                        p.model_dump(by_alias=True) if hasattr(p, 'model_dump') else p
                        for p in chunk.get("parts", [])
                    ]
                    if not chunk_parts:
                        continue
                    chunk_parts = self._maybe_process_csv_key(chunk_parts)
                    ap = _make_artifact_payload(
                        task_id, context_id, chunk_parts,
                        append=chunk.get("append", True),
                        last_chunk=chunk.get("last_chunk", False),
                    )
                    yield f"event: TaskArtifactUpdateEvent\ndata: {_encrypt_event(ap)}\n\n"
                    if not chunk.get("last_chunk"):
                        yield f"event: TaskStatusUpdateEvent\ndata: {_encrypt_event(_make_status_payload(task_id, context_id, 'working'))}\n\n"
            else:
                logger.warning(
                    "SSE: adapter %s has no stream() method; falling back to handle_request()",
                    type(adapter).__name__
                )
                agent_request = a2a_to_agent_request({
                    'jsonrpc': decrypted_msg.jsonrpc,
                    'method': 'messages/send',
                    'params': params,
                    'id': decrypted_msg.id
                })
                # Heartbeat on the synchronous path too: the whole handle_request()
                # can block for minutes (adapter without stream()).
                _pending = asyncio.ensure_future(adapter.handle_request(agent_request))
                try:
                    while True:
                        done, _ = await asyncio.wait(
                            {_pending},
                            timeout=SSE_HEARTBEAT_SECONDS if SSE_HEARTBEAT_SECONDS > 0 else None,
                        )
                        if _pending in done:
                            break
                        yield SSE_KEEPALIVE_LINE
                        try:
                            await task_store.touch(task_id)
                        except Exception as beat_exc:
                            logger.debug("SSE heartbeat touch error (ignored): %s", beat_exc)
                except BaseException:
                    if not _pending.done():
                        _pending.cancel()
                    raise
                agent_response = _pending.result()
                response_dict = agent_response_to_a2a(agent_response)
                # The synchronous fallback returns A2A parts if the method was messages/send,
                # or flat text for legacy methods. We normalize both cases.
                fallback_parts: list = []
                r = response_dict.get('result') or {}
                if isinstance(r, dict):
                    if r.get('parts'):
                        # already-typed A2A parts (messages/send with _from_genai_parts)
                        fallback_parts = [
                            p.model_dump(by_alias=True) if hasattr(p, 'model_dump') else p
                            for p in r['parts']
                        ]
                    else:
                        # flat text from legacy generate/invoke/chat
                        text_result = r.get('text') or r.get('output') or json.dumps(r)
                        fallback_parts = [{"type": "text", "text": text_result, "mimeType": "text/plain"}]
                yield f"event: TaskArtifactUpdateEvent\ndata: {_encrypt_event(_make_artifact_payload(task_id, context_id, self._maybe_process_csv_key(fallback_parts), append=False, last_chunk=True))}\n\n"

            # ── COMPLETED ────────────────────────────────────────────────────
            yield f"event: TaskStatusUpdateEvent\ndata: {_encrypt_event(_make_status_payload(task_id, context_id, 'completed', final=True))}\n\n"

        except Exception as e:
            logger.exception("SSE: error during the adapter's stream")
            yield f"event: TaskStatusUpdateEvent\ndata: {_encrypt_event(_make_status_payload(task_id, context_id, 'failed', final=True))}\n\n"
            yield _sse_event("error", {"code": -32603, "message": str(e)})


def _agent_name_from_path(path: str) -> str:
    """
    Derives the agent name from the STLS path.
    /agents/calculator/secure-a2a → "calculator"
    /secure-a2a                   → "agent"
    """
    parts = [p for p in path.strip("/").split("/") if p and p not in ("secure-a2a", "a2a")]
    return parts[-1] if parts else "agent"


@asynccontextmanager
async def _registry_context(app: Any, registry: Any):
    """
    Async context manager for registering and shutting down agents in the PG registry.

    Used by _register_registry_lifespan via lifespan-chaining (same
    pattern as start_task_store_cleanup). Compatible with FastAPI lifespan=
    and with add_event_handler ("startup"/"shutdown").
    """
    agents = getattr(app, '_a2a_registry_agents', {})
    for agent_id, info in agents.items():
        try:
            await registry.register(**info)
            await registry.start_heartbeat(agent_id)
            logger.info("AgentRegistry: registered '%s'", agent_id)
        except Exception as exc:
            logger.error(
                "AgentRegistry startup: error registering '%s': %s", agent_id, exc
            )
    try:
        yield
    finally:
        agents = getattr(app, '_a2a_registry_agents', {})
        for agent_id in agents:
            try:
                await registry.shutdown(agent_id)
            except Exception as exc:
                logger.warning(
                    "AgentRegistry shutdown: error '%s': %s", agent_id, exc
                )
        await registry.close()


def _register_registry_lifespan(app: Any, registry: Any) -> None:
    """
    Integrates the PG registry's lifecycle into the app via lifespan-chaining.

    Idempotent: the first call chains the lifespan; subsequent calls
    add agent_id to ``app._a2a_registry_agents`` without touching
    the already-registered lifespan.

    Compatible with FastAPI ``lifespan=`` and plain Starlette.
    Doesn't use add_event_handler — which isn't called when a
    lifespan context manager is present on FastAPI.
    """
    if getattr(app, '_a2a_registry_lifespan_registered', False):
        return

    existing_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _lifespan(a: Any):
        if existing_lifespan is not None:
            async with existing_lifespan(a):
                async with _registry_context(a, registry):
                    yield
        else:
            async with _registry_context(a, registry):
                yield

    app.router.lifespan_context = _lifespan
    app._a2a_registry_lifespan_registered = True
    logger.debug("AgentRegistry: lifespan registered on the app")


async def create_pqc_a2a_transport(
    base_url: str,
    *,
    client_id: str = "pqc-a2a-client",
    handshake_path: str = "/stls/pqc/handshake",
    httpx_client: Optional[Any] = None,
) -> "SecureA2ATransportAdapter":
    """
    Creates a SecureA2ATransportAdapter via a PQC handshake (X25519 + ML-KEM-768).

    Replaces the manual sequence for base/arqit_py:
        kid = await provider.getSessionKeyIdentifier()
        key = await provider.getSessionKeyData(kid)
        adapter = SecureA2ATransportAdapter(session_key=key, session_id=kid, base_url=...)

    For PQC there's no pre-shared key: it's derived on-the-fly
    via a KEM exchange (X25519 DH + ML-KEM-768 decap + HKDF).
    Requires liboqs-python (uv pip install -e '.[pqc]').

    Args:
        base_url:       Base URL of the A2A server (e.g. "http://localhost:8090").
        client_id:      Client identifier for server-side audit.
        handshake_path: PQC endpoint path (default "/stls/pqc/handshake").
        httpx_client:   Optional httpx client to reuse for the handshake.

    Returns:
        SecureA2ATransportAdapter ready for send_secure_message/stream_secure_message.

    Raises:
        RuntimeError: If the handshake fails, the proof is invalid,
                      or liboqs isn't installed/compatible.
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
        raise RuntimeError(
            f"create_pqc_a2a_transport requires liboqs-python: {exc}. "
            "Install with: uv pip install -e '.[pqc]'"
        ) from exc

    # 1. Generates the X25519 + ML-KEM-768 keypair client-side
    pqc_client = KeyProviderClientPQC()
    try:
        pqc_client.start()
    except Exception as exc:
        raise RuntimeError(
            f"PQC keypair generation failed — is liboqs installed and compatible? {exc}"
        ) from exc

    # 2. Prepares and sends the handshake request
    req = PQCHandshakeRequest.generate(
        client_id,
        pqc_client.x25519_public_key_bytes,
        pqc_client.mlkem_public_key_bytes,
    )
    nonce = req.nonce
    url = base_url.rstrip("/") + handshake_path

    owns_client = httpx_client is None
    _http = httpx_client or httpx.AsyncClient()
    try:
        resp = await _http.post(url, json=req.model_dump())
    except httpx.HTTPError as exc:
        raise RuntimeError(f"PQC handshake HTTP request to {url} failed: {exc}") from exc
    finally:
        if owns_client:
            await _http.aclose()

    if resp.status_code != 200:
        raise RuntimeError(
            f"PQC handshake rejected by the server "
            f"(status={resp.status_code}): {resp.text}"
        )

    try:
        response = PQCHandshakeResponse.model_validate(resp.json())
    except Exception as exc:
        raise RuntimeError(f"Invalid PQC handshake response: {exc}") from exc

    # 3. Derives session_key via KEM on the client side
    session = pqc_client.createProviderSession()
    try:
        session_key = derive_session_key_client(
            session.x25519_private_key_bytes,
            session.mlkem_secret_key_bytes,
            response,
            nonce,
        )
    except Exception as exc:
        raise RuntimeError(f"PQC session_key derivation failed: {exc}") from exc
    finally:
        pqc_client.stop()  # zeroes private keys (best-effort)

    # 4. Verifies mutual proof of possession
    if not verify_pqc_handshake_proof(session_key, nonce, response):
        raise RuntimeError(
            "PQC proof of possession invalid — "
            "the server didn't derive the same session_key. "
            "Possible attack or misconfiguration."
        )

    logger.info(
        "PQC A2A transport created: session_id=%s kid=%s base_url=%s",
        response.session_id, response.kid, base_url,
    )

    return SecureA2ATransportAdapter(
        session_key=session_key,
        session_id=response.session_id,
        base_url=base_url,
    )


def create_secure_a2a_endpoint(
    app,
    agent,
    key_session_provider: KeySessionProvider,
    base_url: Optional[str] = None,
    path: str = "/secure-a2a",
    framework: str = 'auto',
    adapter_kwargs: Optional[Dict[str, Any]] = None,
    enable_plain_endpoint: bool = True,
    auth: Optional[AuthConfig] = None,
    api_key_manager: Optional[Any] = None,
    agent_name: Optional[str] = None,
):
    """
    Adds A2A endpoints (encrypted and, optionally, plain) to a FastAPI/Starlette app.

    Args:
        app:                    FastAPI/Starlette application
        agent:                  Agent from any supported framework
        key_session_provider:   Provider for STLS session keys
        base_url:               Base URL for the endpoints (default: None)
        path:                   Base path of the encrypted endpoints (default: /secure-a2a)
        framework:              Framework ('auto', 'google-adk', 'langchain', …)
        adapter_kwargs:         Additional parameters for the adapter
        enable_plain_endpoint:  If True, registers /a2a and /a2a/stream (unencrypted)
        auth:                   Legacy AuthConfig (in-memory, backward-compatible).
                                None (default) = no validation.
        api_key_manager:        Enterprise ApiKeyManager (optional).
                                If provided, takes priority over ``auth``.
                                Must already be started (await manager.start()).
                                Supports Redis/File/Postgres backends.
        agent_name:             Human-readable agent name for the PG registry.
                                Default: derived from the path (e.g. /agents/calc/secure-a2a → calc).
    """
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    # Creates the appropriate adapter for the framework
    kwargs = adapter_kwargs or {}
    agent_adapter = create_adapter(agent, framework=framework, **kwargs)

    # Creates the middleware with the adapter and the auth configuration
    middleware = SecureA2AHTTPMiddleware(
        key_session_provider,
        agent_adapter,
        auth=auth,
        api_key_manager=api_key_manager,
    )

    # ── Centralized registry on the app ─────────────────────────────────────
    if not hasattr(app, '_a2a_agent_registry'):
        app._a2a_agent_registry = {}   # path → AgentCard

    plain_path = None
    if enable_plain_endpoint:
        plain_path = path.replace("secure-", "") if "secure-" in path else f"{path}-plain"

    # the absolute url is required for standard clients to work
    agent_card = to_a2a(
        agent=agent,
        url=base_url + plain_path if base_url else plain_path,
        secure_url=base_url + path if base_url else path,
    )
    
    app._a2a_agent_registry[path] = agent_card

    # ── PG Agent Registry (if AUTH_PG_DSN configured) ───────────────────────
    # Derives the agent name from the explicit parameter or from the path
    # e.g. /agents/calculator/secure-a2a → "calculator"
    _name = agent_name or _agent_name_from_path(path)
    # Falls back to BASE_URL from the env when base_url isn't passed explicitly.
    # Consistent with the server's behavior (which reads BASE_URL from the same env).
    # Without this, sf_agents.base_url and sf_agents.port stay empty/NULL.
    import os as _os
    _effective_base_url = (base_url or _os.environ.get("BASE_URL", "")).rstrip("/")
    _registry = init_registry()
    if _registry is not None:
        _agent_id = make_agent_id(_name, _effective_base_url or path)
        if not hasattr(app, '_a2a_registry_agents'):
            app._a2a_registry_agents = {}
        app._a2a_registry_agents[_agent_id] = {
            "agent_id":     _agent_id,
            "agent_name":   _name,
            "base_url":     _effective_base_url,
            "plain_url":    (_effective_base_url + plain_path) if _effective_base_url and plain_path else plain_path,
            "secure_url":   (_effective_base_url + path) if _effective_base_url else path,
            "framework":    framework,
            "capabilities": agent_card.to_dict() if hasattr(agent_card, "to_dict") else {},
        }
        # _register_registry_lifespan uses lifespan-chaining: compatible with
        # FastAPI lifespan= and doesn't overwrite the lifespan already defined by the server.
        _register_registry_lifespan(app, _registry)
        logger.info("AgentRegistry: agent '%s' queued for registration", _name)

    # ── /.well-known/agent.json — registered ONLY ONCE ────────────────────
    well_known_registered = any(
        getattr(r, 'path', None) in ("/.well-known/agent.json", "/.well-known/agent-card.json")
        for r in app.routes
    )
    if not well_known_registered:
        async def well_known_handler(request: Request):
            """
            Spec §8.2 — if there's only one agent, returns its card;
            if there are several, returns the list of cards (one per line).
            Use ?agent=/path to filter a specific agent.
            """
            registry: dict = app._a2a_agent_registry

            # filter by specific path: /.well-known/agent.json?agent=/agents/foo
            agent_path = request.query_params.get("agent")
            if agent_path:
                card = registry.get(agent_path)
                if not card:
                    return JSONResponse(
                        content={"error": f"Agent '{agent_path}' not found"},
                        status_code=404
                    )
                return JSONResponse(
                    content=card.to_dict(),
                    headers={"Cache-Control": "public, max-age=3600"}
                )

            # single agent → direct card (standard spec §8.2)
            if len(registry) == 1:
                card = next(iter(registry.values()))
                return JSONResponse(
                    content=card.to_dict(),
                    headers={"Cache-Control": "public, max-age=3600"}
                )

            # multi-agent → array of cards with the path as key
            return JSONResponse(
                content=[
                    {"path": p, **c.to_dict()}
                    for p, c in registry.items()
                ],
                headers={"Cache-Control": "public, max-age=3600"}
            )

        app.routes.append(Route("/.well-known/agent.json",      well_known_handler, methods=["GET"]))
        app.routes.append(Route("/.well-known/agent-card.json", well_known_handler, methods=["GET"]))

    # ── Encrypted request handler ─────────────────────────────────────────────

    async def secure_a2a_handler(request: Request):
        """Handler for encrypted A2A requests"""
        auth_err = await middleware._check_auth_async(request)
        if auth_err:
            return auth_err

        encrypted_data: Optional[Dict[str, Any]] = None
        try:
            logger.debug(f"Received secure A2A request at {request.url}")
            logger.debug(f"Headers: {request.headers}")
            logger.debug(f"Raw body: {await request.body()}")
            logger.debug(f"Content-Type: {request.headers.get('Content-Type')}")
            logger.debug(f"X-A2A-Session-ID: {request.headers.get('X-A2A-Session-ID')}")
            
            encrypted_data = await request.json()
            http_ctx = middleware._extract_http_context(request)
            encrypted_response = await middleware.process_secure_request(
                encrypted_data, http_context=http_ctx or None
            )
            return JSONResponse(content=encrypted_response)

        except SessionExpiredError as e:
            from .stls_a2a_adapters_model import ERR_SESSION_EXPIRED
            # Standard JSON-RPC shape (not the generic {'error': str} blob
            # below) so that an STLS-aware client can recognize the code and
            # perform a new handshake without textually parsing the
            # message. 401: the request lacks valid credentials for
            # THIS session, it's neither a server failure (500) nor malformed (400).
            return JSONResponse(
                content={
                    'jsonrpc': '2.0',
                    'error': {'code': ERR_SESSION_EXPIRED, 'message': str(e)},
                    'id': encrypted_data.get('id') if encrypted_data else None,
                },
                status_code=401,
            )
        except ValueError as e:
            return JSONResponse(
                content={'error': str(e)},
                status_code=400
            )
        except Exception as e:
            return JSONResponse(
                content={
                    'error': f'Internal server error: {str(e)}',
                    'type': type(e).__name__
                },
                status_code=500
            )
    
    # ── PQC handshake endpoint (only when KEY_PROVIDER_NAME=pqc) ──────────────
    # Uses middleware.sessions to populate the same SessionCache shared
    # with get_transport(): when an encrypted request arrives, session_id
    # is found in the cache without going through the Arqit lazy derivation.
    if os.getenv("KEY_PROVIDER_NAME", "base").strip().lower() == "pqc":
        try:
            from stls_core import HANDSHAKE_PQC_PATH, PQCHandshakeRequest, PQCHandshakeError, perform_pqc_handshake as _perform_pqc_hs

            async def pqc_handshake_handler(request: Request):
                try:
                    body = await request.json()
                    pqc_req = PQCHandshakeRequest.model_validate(body)
                except Exception as exc:
                    return JSONResponse(
                        {"jsonrpc": "2.0", "error": {"code": -32600, "message": f"Invalid request: {exc}"}, "id": None},
                        status_code=400,
                    )
                try:
                    pqc_resp = await _perform_pqc_hs(pqc_req, middleware.sessions)
                except PQCHandshakeError as exc:
                    logger.warning("PQC Handshake failed: %s", exc.message)
                    status = 400 if exc.code == -32600 else 503
                    return JSONResponse(
                        {"jsonrpc": "2.0", "error": {"code": exc.code, "message": exc.message}, "id": None},
                        status_code=status,
                    )
                return JSONResponse(pqc_resp.model_dump())

            # Registers only once per app (several agents may call this function)
            if not any(getattr(r, "path", None) == HANDSHAKE_PQC_PATH for r in app.routes):
                app.routes.append(Route(HANDSHAKE_PQC_PATH, pqc_handshake_handler, methods=["POST"]))
                logger.info(
                    "PQC handshake endpoint registered on %s (agent path: %s)",
                    HANDSHAKE_PQC_PATH, path,
                )
        except ImportError as _e:
            logger.warning(
                "KEY_PROVIDER_NAME=pqc but liboqs isn't available — "
                "PQC endpoint not registered: %s", _e,
            )

    app.routes.append(Route(path, secure_a2a_handler, methods=["POST"]))

    # ── Capabilities endpoint ─────────────────────────────────────────────────

    async def capabilities_handler(request: Request):
        """Endpoint to get the agent's capabilities"""
        capabilities = agent_adapter.get_capabilities()
        capabilities['secure_transport'] = {
            'enabled': True,
            'encryption': 'AES-256-GCM',
            'key_derivation': 'HKDF-SHA256'
        }
        return JSONResponse(content=capabilities)
    
    app.routes.append(Route(f"{path}/capabilities", capabilities_handler, methods=["GET"]))

    # ── Encrypted SSE endpoint ──────────────────────────────────────────────────

    async def secure_a2a_stream_handler(request: Request):
        """
        POST {path}/stream  →  text/event-stream

        Accepts an AES-256-GCM request identical to /secure-a2a,
        but responds with SSE for messages/sendStream and tasks/subscribe.

        Each SSE event is encrypted with the same session key:
            event: TaskStatusUpdateEvent | TaskArtifactUpdateEvent | error
            data: <AES-256-GCM encrypted JSON>

        Mandatory headers in the response:
            Content-Type: text/event-stream
            Cache-Control: no-cache
            X-Accel-Buffering: no   ← disables nginx buffering
        """
        from starlette.responses import StreamingResponse

        auth_err = await middleware._check_auth_async(request)
        if auth_err:
            return auth_err

        try:
            encrypted_data = await request.json()
        except Exception:
            async def _parse_error():
                yield _sse_event("error", {"code": -32700, "message": "Parse error: body isn't valid JSON"})
            return StreamingResponse(
                _parse_error(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        http_ctx = middleware._extract_http_context(request)
        return StreamingResponse(
            middleware.process_secure_stream(encrypted_data, http_context=http_ctx or None),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",   # essential with nginx as reverse proxy
                "Connection": "keep-alive",
            },
        )

    app.routes.append(Route(f"{path}/stream", secure_a2a_stream_handler, methods=["POST"]))

    # ── Plain (unencrypted) endpoints for standard A2A interoperability ────────
    if enable_plain_endpoint:
        # Derives the plain path from the secure path:
        #   /secure-a2a          → /a2a
        #   /agents/secure-agent → /agents/agent
        # If "secure-" isn't in the path, appends the "-plain" suffix.
        plain_path = path.replace("secure-", "") if "secure-" in path else f"{path}-plain"
        logger.info(
            "Plain A2A endpoints enabled: POST %s  POST %s/stream",
            plain_path, plain_path,
        )

        async def plain_a2a_handler(request: Request):
            """
            POST {plain_path}  →  application/json
            Standard (unencrypted) JSON-RPC endpoint for standard A2A clients.
            Same logic as /secure-a2a but without AES-256-GCM encryption.
            """
            auth_err = await middleware._check_auth_async(request)
            if auth_err:
                return auth_err

            def _jsonrpc_error(code: int, message: str, rpc_id=None) -> JSONResponse:
                return JSONResponse(
                    content={"jsonrpc": "2.0",
                             "error": {"code": code, "message": message},
                             "id": rpc_id},
                    status_code=200,
                )

            try:
                plain_data = await request.json()
            except Exception:
                return _jsonrpc_error(-32700, "Parse error: request body is not valid JSON")

            http_ctx = middleware._extract_http_context(request)
            from starlette.responses import StreamingResponse as _SR
            try:
                response_dict = await middleware.process_plain_request(
                    plain_data, http_context=http_ctx or None
                )
                return JSONResponse(content=response_dict)
            except _SseMethodError as sse_err:
                # messages/sendStream (and aliases) arriving on the sync endpoint:
                # automatically promoted to an SSE StreamingResponse.
                return _SR(
                    middleware.process_plain_stream(sse_err.plain_data, http_context=http_ctx or None),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control":     "no-cache",
                        "X-Accel-Buffering": "no",
                        "Connection":        "keep-alive",
                    },
                )
            except ValueError as e:
                return _jsonrpc_error(-32600, f"Invalid request: {e}",
                                      rpc_id=plain_data.get("id") if isinstance(plain_data, dict) else None)
            except Exception as e:
                logger.exception("Unexpected error in plain_a2a_handler")
                return _jsonrpc_error(-32603, f"Internal error: {e}")

        async def plain_a2a_stream_handler(request: Request):
            """
            POST {plain_path}/stream  →  text/event-stream
            Standard (unencrypted) SSE endpoint for standard A2A clients.
            SSE events are plain JSON (TaskStatusUpdateEvent /
            TaskArtifactUpdateEvent) compliant with spec §4.2.
            """
            from starlette.responses import StreamingResponse

            auth_err = await middleware._check_auth_async(request)
            if auth_err:
                return auth_err

            try:
                plain_data = await request.json()
            except Exception:
                async def _parse_error():
                    yield _sse_event("error", {"code": -32700,
                                               "message": "Parse error: body isn't valid JSON"})
                return StreamingResponse(
                    _parse_error(),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )

            http_ctx = middleware._extract_http_context(request)
            return StreamingResponse(
                middleware.process_plain_stream(plain_data, http_context=http_ctx or None),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                },
            )

        app.routes.append(Route(plain_path,                plain_a2a_handler,        methods=["POST"]))
        app.routes.append(Route(f"{plain_path}/stream",    plain_a2a_stream_handler, methods=["POST"]))
        logger.info("Plain A2A routes registered: %s  %s/stream", plain_path, plain_path)

    return app