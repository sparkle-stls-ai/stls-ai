"""
stls_a2a_secure_remote.py
--------------------------
ENCRYPTED intra-agent delegation: analogous to ContextualRemoteA2aAgent
(stls_a2a_remote.py) but with traffic to the remote sub-agent routed
over /secure-a2a (AES-256-GCM) instead of the plain /a2a endpoint.

Problem solved
~~~~~~~~~~~~~~
ContextualRemoteA2aAgent propagates the user context but leaves the
transport unchanged: RemoteA2aAgent uses a stock httpx.AsyncClient, so it
POSTs in the clear to the sub-agent card's `url` field (plain),
authenticated only via header (e.g. X-API-Key). No application-layer
encryption, no quantum-safe key agreement — even when both caller and
callee are STLS agents.

Solution
~~~~~~~~
STLSHttpxTransport is a custom httpx.AsyncBaseTransport: it intercepts
JSON-RPC requests aimed at an STLS agent's plain endpoint, encrypts them
and reroutes them to /secure-a2a (or /secure-a2a/stream for SSE), then
decrypts the response and returns it in the same shape RemoteA2aAgent
expects from the plain endpoint. The rest of the ADK/a2a-sdk chain knows
nothing about STLS.

ContextualSecureRemoteA2aAgent composes this transport with the context
propagation already present in ContextualRemoteA2aAgent: same
a2a_request_meta_provider, same behavior on session.state, plus the
encrypted traffic.

Session lifecycle
~~~~~~~~~~~~~~~~~
SecureSessionManager performs the PQC handshake (X25519 + ML-KEM-768) on
first use toward a given base_url and caches the resulting adapter. An
asyncio.Lock per base_url prevents duplicate concurrent handshakes when
multiple requests to the same target start in parallel before the first
session is ready.

If the server responds with ERR_SESSION_EXPIRED (-32004, introduced in
v0.1.4 — see SessionExpiredError in stls_a2a_integration.py), the cached
session is invalidated and the handshake is redone EXACTLY ONCE per
request, on the synchronous path. On the streaming path, the retry isn't
safe after the first byte has been emitted (we might have already
forwarded ADK events downstream): a mid-stream session error is
propagated as an "error" event, not silently retried.

Provider
~~~~~~~~
The default provider for the handshake is PQC (X25519 + ML-KEM-768),
chosen because it's the only one of the three (base/arqit_py/pqc)
designed for a client-initiated handshake with no pre-shared state —
consistent with the "an agent contacts another agent it doesn't yet
know" use case. base/arqit_py require a pre-provisioned session_id/kid on
the KeySessionProvider side and don't suit a generic client that
discovers the target at runtime.

Dependency on ADK
~~~~~~~~~~~~~~~~~~
Lazy google-adk import, identical to ContextualRemoteA2aAgent: the
module is importable without ADK installed, the explicit ImportError
only arrives at ContextualSecureRemoteA2aAgent's instantiation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlsplit

import httpx

import uuid as _uuid

from .stls_a2a_integration import (
    SecureA2ATransportAdapter,
    create_pqc_a2a_transport,
    _now_iso,
)
from .stls_a2a_remote import _DEFAULT_CONTEXT_KEYS, _make_meta_provider

logger = logging.getLogger(__name__)

__all__ = [
    "SecureSessionManager",
    "STLSHttpxTransport",
    "ContextualSecureRemoteA2aAgent",
]

if TYPE_CHECKING:
    pass

# ERR_SESSION_EXPIRED error code (stls_a2a_adapters_model.py) — imported
# by value rather than through the public export to avoid a circular
# import at the package level; it's a stable constant (-32004).
_ERR_SESSION_EXPIRED = -32004

# Methods expected to arrive via SSE from the ADK/a2a-sdk caller.
# Intentional copy (not imported) from SSE_STREAM_METHODS in
# stls_a2a_integration.py: here it's only used to recognize "streaming"
# intent when the Accept header isn't already text/event-stream.
_SSE_METHOD_HINTS = frozenset({
    "messages/sendStream",
    "message/sendStream",
    "message/stream",
    "tasks/subscribe",
    "task/subscribe",
})


def _base_url_of(url: str) -> str:
    """
    Derives the base_url to pass to SecureA2ATransportAdapter /
    create_pqc_a2a_transport, which then appends "/secure-a2a" to it (and
    "/secure-a2a/stream" for streaming) — see SecureA2ATransportAdapter
    .__init__ in stls_a2a_integration.py.

    HISTORICAL BUG (fixed): the first version returned only
    scheme://netloc, discarding any agent prefix (e.g. "/agents/agent-b").
    Against a multi-agent server mounted with a path like
    "/agents/agent-b/secure-a2a" (standard convention of
    create_secure_a2a_endpoint — see server_context_test.py), this
    produced a truncated base_url and thus a wrong secure_url
    ("http://host/secure-a2a" instead of
    "http://host/agents/agent-b/secure-a2a") → 404 on the first encrypted
    intra-agent call, silent because it didn't raise a recognizable
    exception on the ADK side.

    Fix: RemoteA2aAgent POSTs to the plain endpoint resolved from
    card.url, which by STLS convention is <base><prefix>/a2a (or
    <base><prefix>-plain if the original path didn't contain "secure-").
    We remove ONLY the final suffix of the plain path, preserving the
    rest — including any multi-agent prefix — so that
    SecureA2ATransportAdapter rebuilds the correct encrypted endpoint.

    NOTE: this value (with prefix) is the right one for the DATA
    endpoints (/secure-a2a, /secure-a2a/stream), but NOT for the PQC
    handshake — see _root_of() below and
    SecureSessionManager.get_adapter().
    """
    parts = urlsplit(url)
    path = parts.path
    for suffix in ("/a2a/stream", "/a2a", "-plain/stream", "-plain"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return f"{parts.scheme}://{parts.netloc}{path}"


def _root_of(url: str) -> str:
    """
    scheme://netloc, WITHOUT any path — the PQC handshake endpoint
    (/stls/pqc/handshake) is registered only once per FastAPI app at the
    root level by create_secure_a2a_endpoint (HANDSHAKE_PQC_PATH is a
    fixed path, not prefixed per-agent), regardless of where the specific
    agent's "path" is mounted. _base_url_of(), which preserves the
    prefix, is correct for /secure-a2a but wrong for the handshake: using
    it there produces a 404 on /agents/<name>/stls/pqc/handshake, which
    doesn't exist — the handshake lives only at /stls/pqc/handshake at
    the root.
    """
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class SecureSessionManager:
    """
    Cache of encrypted STLS sessions by base_url, with lazy PQC handshake.

    A single instance should be shared across all agents/sub-agents of a
    process that talk to the same targets — avoids one handshake per
    sub-agent toward the same server if several root agents contact it.

    Args:
        client_id:      identifier for server-side audit (PQCHandshakeRequest).
        httpx_client:    optional httpx client reused for handshakes
                         (not for application traffic, which goes through
                         the encrypted adapters). If None, creates an
                         internal one.
        handshake_path:  path of the PQC handshake endpoint.
    """

    def __init__(
        self,
        *,
        client_id: str = "stls-secure-remote-agent",
        httpx_client: Optional[httpx.AsyncClient] = None,
        handshake_path: str = "/stls/pqc/handshake",
    ) -> None:
        self.client_id = client_id
        self._handshake_path = handshake_path
        self._owns_http = httpx_client is None
        self._http = httpx_client or httpx.AsyncClient()
        self._sessions: dict[str, SecureA2ATransportAdapter] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, base_url: str) -> asyncio.Lock:
        lock = self._locks.get(base_url)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[base_url] = lock
        return lock

    async def get_adapter(self, base_url: str) -> SecureA2ATransportAdapter:
        """
        Returns the encrypted adapter for base_url (with its agent prefix,
        if any, e.g. "http://host/agents/agent-b"), handshaking lazily if
        absent.

        The PQC handshake is always done against the host's ROOT
        (_root_of), because /stls/pqc/handshake is a global per-app
        endpoint, not prefixed per-agent — see _root_of(). If base_url has
        a prefix (multi-agent case), the adapter's data URLs
        (_secure_url/_stream_url) are rebuilt on the requested base_url
        AFTER the handshake: session_key/session_id/transport remain the
        negotiated ones, the crypto session doesn't depend on the HTTP
        path the messages travel over.
        """
        adapter = self._sessions.get(base_url)
        if adapter is not None:
            return adapter

        async with self._lock_for(base_url):
            # Re-check after acquiring the lock: another coroutine
            # may have already completed the handshake in the meantime.
            adapter = self._sessions.get(base_url)
            if adapter is not None:
                return adapter

            handshake_base = _root_of(base_url)
            logger.info(
                "SecureSessionManager: PQC handshake toward %s (data on %s)",
                handshake_base, base_url,
            )
            adapter = await create_pqc_a2a_transport(
                handshake_base,
                client_id=self.client_id,
                handshake_path=self._handshake_path,
                httpx_client=self._http,
            )

            if base_url != handshake_base:
                # Data endpoint under a prefix (e.g. /agents/agent-b) —
                # rewrites ONLY the adapter's HTTP routing, not the
                # crypto session already negotiated against the host.
                adapter.base_url = base_url.rstrip("/")
                _b = adapter.base_url
                adapter._secure_url = _b if _b.endswith("/secure-a2a") else f"{_b}/secure-a2a"
                adapter._stream_url = (
                    f"{_b}/stream" if _b.endswith("/secure-a2a") else f"{_b}/secure-a2a/stream"
                )

            self._sessions[base_url] = adapter
            return adapter

    def invalidate(self, base_url: str) -> None:
        """Removes the cached session: the next get_adapter() re-handshakes."""
        self._sessions.pop(base_url, None)
        logger.info("SecureSessionManager: session invalidated for %s", base_url)

    async def aclose(self) -> None:
        """Closes the internal httpx client, if owned by this instance."""
        if self._owns_http:
            await self._http.aclose()


class _AsyncBytesStream(httpx.AsyncByteStream):
    """
    Adapts an async generator of bytes to the AsyncByteStream protocol
    required by httpx.Response(stream=...). httpx explicitly checks
    isinstance(response.stream, AsyncByteStream) — duck typing isn't
    enough, it must actually inherit from the public base class.
    """

    def __init__(self, agen):
        self._agen = agen

    def __aiter__(self):
        return self._agen

    async def aclose(self) -> None:
        aclose = getattr(self._agen, "aclose", None)
        if aclose is not None:
            await aclose()


class STLSHttpxTransport(httpx.AsyncBaseTransport):
    """
    httpx.AsyncBaseTransport that transparently encrypts JSON-RPC requests
    aimed at an STLS agent, without the caller (RemoteA2aAgent /
    a2a-sdk) needing to know anything about the STLS protocol.

    Passthrough for anything that isn't an application-layer JSON-RPC
    POST (discovery /.well-known/*, other HTTP methods, non-JSON body):
    those requests go to the real underlying httpx transport, unchanged.

    Retry-once on ERR_SESSION_EXPIRED (-32004) on BOTH paths, but with
    different scope:
      - synchronous path: no byte has yet been returned to the caller
        when the error arrives → retry is always safe.
      - SSE path: the retry happens ONLY if the error is the very first
        event of the generator (done via "priming": we consume the first
        event BEFORE returning the httpx.Response, so no byte has yet
        gone out to the ADK). If the session expires later in the
        stream, after real events have already been forwarded
        downstream, the retry does NOT happen: the error becomes a
        spec-compliant "error" event, never a silent retry that could
        duplicate side effects already triggered by the caller on
        events already received.

    Typical usage (drop-in where httpx_client=... is currently passed to
    RemoteA2aAgent / ContextualRemoteA2aAgent):

        session_manager = SecureSessionManager(client_id="agent_a")
        transport = STLSHttpxTransport(session_manager, extra_headers={"X-API-Key": API_KEY_B})
        httpx_client = httpx.AsyncClient(transport=transport)
    """

    def __init__(
        self,
        session_manager: SecureSessionManager,
        *,
        extra_headers: Optional[dict[str, str]] = None,
        inner_transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._sessions = session_manager
        self._extra_headers = dict(extra_headers or {})
        # Real transport for the passthrough (discovery, non-JSON-RPC requests).
        self._inner = inner_transport or httpx.AsyncHTTPTransport()

    async def aclose(self) -> None:
        await self._inner.aclose()

    # ── httpx entry point ───────────────────────────────────────────────────

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or ".well-known" in request.url.path:
            return await self._inner.handle_async_request(request)

        body = await request.aread()
        try:
            payload = json.loads(body)
        except Exception:
            # Not JSON-RPC (or not JSON at all) — unchanged passthrough.
            return await self._inner.handle_async_request(request)

        if not isinstance(payload, dict) or "jsonrpc" not in payload:
            return await self._inner.handle_async_request(request)

        base_url = _base_url_of(str(request.url))
        accept = request.headers.get("accept", "")
        is_stream = (
            "text/event-stream" in accept
            or payload.get("method") in _SSE_METHOD_HINTS
        )

        forward_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length", "content-type", "accept",
                                  "connection", "transfer-encoding")
        }
        forward_headers.update(self._extra_headers)

        if is_stream:
            return await self._handle_stream(request, base_url, payload, forward_headers)
        return await self._handle_sync(request, base_url, payload, forward_headers)

    # ── Synchronous path (messages/send, tasks/get, tasks/cancel, ...) ─────────

    async def _handle_sync(
        self,
        request: httpx.Request,
        base_url: str,
        payload: dict,
        headers: dict,
        *,
        _retried: bool = False,
    ) -> httpx.Response:
        adapter = await self._sessions.get_adapter(base_url)
        try:
            result = await adapter.send_secure_message(payload, extra_headers=headers)
        except httpx.HTTPStatusError as exc:
            if (
                exc.response.status_code == 401
                and not _retried
                and _looks_like_session_expired(exc.response)
            ):
                logger.info(
                    "STLSHttpxTransport: session expired toward %s, re-handshaking and retrying", base_url
                )
                self._sessions.invalidate(base_url)
                return await self._handle_sync(
                    request, base_url, payload, headers, _retried=True
                )
            raise

        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(result).encode("utf-8"),
            request=request,
        )

    # ── SSE path (messages/sendStream, tasks/subscribe) ─────────────────────

    async def _open_stream(
        self,
        base_url: str,
        payload: dict,
        headers: dict,
        *,
        _retried: bool = False,
    ):
        """
        Opens the stream toward the adapter and "primes" it (consumes) its
        very first event BEFORE any byte is returned to the httpx caller.

        This is what makes the retry safe: if the first event is
        {"event": "error", "code": ERR_SESSION_EXPIRED} we know the
        server hasn't emitted anything yet that could have reached the
        ADK (we're still inside handle_async_request, the httpx.Response
        doesn't exist yet) — so we invalidate the session and reopen the
        stream from scratch, exactly once.

        If the error appears later (second event onward), the caller of
        _open_stream (_handle_stream) receives it as a normal first
        element of the remaining sequence and propagates it as an
        "error" event — no retry at that point.

        Returns:
            (first_chunk, agen) — first_chunk is None if the stream is
            empty (no event, immediate StopAsyncIteration); agen is the
            async generator from which to keep consuming subsequent
            events (the first one has already been consumed into
            first_chunk).
        """
        adapter = await self._sessions.get_adapter(base_url)
        agen = adapter.stream_secure_message(payload, extra_headers=headers)

        try:
            first_chunk = await agen.__anext__()
        except StopAsyncIteration:
            return None, agen

        if (
            not _retried
            and first_chunk.get("event") == "error"
            and first_chunk.get("code") == _ERR_SESSION_EXPIRED
        ):
            logger.info(
                "STLSHttpxTransport: session expired toward %s (first SSE event, "
                "no byte emitted yet) — re-handshaking and retrying", base_url,
            )
            self._sessions.invalidate(base_url)
            aclose = getattr(agen, "aclose", None)
            if aclose is not None:
                await aclose()
            return await self._open_stream(base_url, payload, headers, _retried=True)

        return first_chunk, agen

    async def _handle_stream(
        self,
        request: httpx.Request,
        base_url: str,
        payload: dict,
        headers: dict,
    ) -> httpx.Response:
        rpc_id = payload.get("id")

        # Priming: opens the stream and consumes the first event (with an
        # optional retry-once on session expiry) BEFORE building the
        # Response — if this raises, the exception propagates directly
        # to the httpx caller as a normal transport error, no partial
        # Response is ever returned.
        first_chunk, agen = await self._open_stream(base_url, payload, headers)

        def _render(chunk: dict) -> bytes:
            event = chunk["event"]
            if event == "TaskArtifactUpdateEvent":
                return _sse_line("TaskArtifactUpdateEvent", {
                    "kind": "artifact-update",
                    "taskId": chunk["taskId"],
                    "contextId": chunk["contextId"],
                    "artifact": {
                        "artifactId": str(_uuid.uuid4()),
                        "parts": chunk["parts"],
                    },
                    "append": chunk.get("append", True),
                    "lastChunk": chunk.get("last_chunk", False),
                }, rpc_id)
            if event == "TaskStatusUpdateEvent":
                return _sse_line("TaskStatusUpdateEvent", {
                    "kind": "status-update",
                    "taskId": chunk["taskId"],
                    "contextId": chunk["contextId"],
                    "status": {"state": chunk["status"]},
                    "final": chunk.get("final", False),
                    "timestamp": _now_iso(),
                }, rpc_id)
            # event == "error" (already retried once if it was session-expired,
            # or a different/later error — in both cases there's no
            # further retry past this point).
            return _sse_line("error", {"code": chunk["code"], "message": chunk["message"]}, rpc_id)

        async def _sse_body():
            try:
                if first_chunk is not None:
                    yield _render(first_chunk)
                    if first_chunk.get("event") == "error":
                        return
                async for chunk in agen:
                    yield _render(chunk)
                    if chunk.get("event") == "error":
                        # Session expired (or other error) arrived mid-
                        # stream: we invalidate for the caller's NEXT
                        # attempt, but there's no retry here — bytes
                        # already emitted.
                        if chunk.get("code") == _ERR_SESSION_EXPIRED:
                            self._sessions.invalidate(base_url)
                        return
            except Exception as exc:
                logger.exception("STLSHttpxTransport: error during stream toward %s", base_url)
                yield _sse_line("error", {"code": -32603, "message": str(exc)}, rpc_id)

        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "cache-control": "no-cache",
            },
            stream=_AsyncBytesStream(_sse_body()),
            request=request,
        )


def _sse_line(event_type: str, payload: dict, rpc_id: Any) -> bytes:
    """
    Rebuilds an SSE line in the same format as the plain /a2a/stream
    endpoint (JSON-RPC 2.0 envelope), so a2a-sdk/ADK — which expect to
    parse exactly that format — see no difference from a genuinely plain
    call.
    """
    envelope = {"jsonrpc": "2.0", "id": rpc_id, "result": payload} if event_type != "error" \
        else {"jsonrpc": "2.0", "id": rpc_id, "error": payload}
    data = json.dumps(envelope)
    return f"event: {event_type}\ndata: {data}\n\n".encode("utf-8")


def _looks_like_session_expired(response: httpx.Response) -> bool:
    """
    Recognizes a 401 response as ERR_SESSION_EXPIRED (-32004) rather than
    an authentication 401 (invalid API key) — only the former justifies
    an automatic retry after re-handshake.
    """
    try:
        body = response.json()
    except Exception:
        return False
    error = body.get("error") if isinstance(body, dict) else None
    return isinstance(error, dict) and error.get("code") == _ERR_SESSION_EXPIRED


class ContextualSecureRemoteA2aAgent:
    """
    Drop-in replacement for RemoteA2aAgent with:
      1. user context propagation (identical to ContextualRemoteA2aAgent)
      2. AES-256-GCM encrypted traffic over /secure-a2a via a lazy PQC
         handshake (identical in effect to calling stream_secure_message/
         send_secure_message by hand, but transparent to the ADK)

    Args:
        name, agent_card:  identical to RemoteA2aAgent.
        context_keys:      keys propagated from session.state (default:
                            ["user_id", "profile"], same as ContextualRemoteA2aAgent).
        session_manager:    SecureSessionManager to reuse across several
                            agents (recommended to share one per process).
                            If None, creates a dedicated one for this
                            instance.
        extra_headers:      application headers (e.g. X-API-Key) injected
                            into every encrypted request, both sync and SSE.
        **kwargs:           other RemoteA2aAgent parameters (timeout,
                            description, etc.) — do NOT pass httpx_client:
                            this class builds it internally with the
                            STLSHttpxTransport injected.

    Raises:
        ImportError: if google-adk isn't installed (only at instantiation).
        TypeError:   if httpx_client is passed explicitly (it would be
                     silently overwritten: better to fail immediately
                     than give a false sense of control over the
                     transport).
    """

    def __new__(
        cls,
        name: str,
        agent_card: Any,
        *,
        context_keys: Optional[list[str]] = None,
        session_manager: Optional[SecureSessionManager] = None,
        extra_headers: Optional[dict[str, str]] = None,
        **kwargs: Any,
    ) -> Any:
        if "httpx_client" in kwargs:
            raise TypeError(
                "ContextualSecureRemoteA2aAgent builds its own httpx_client "
                "with STLSHttpxTransport injected — passing httpx_client "
                "explicitly would silently overwrite it. Use "
                "extra_headers= for application headers (e.g. X-API-Key) "
                "and session_manager= to share encrypted sessions across "
                "multiple agents."
            )

        try:
            from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
        except ImportError as exc:
            raise ImportError(
                "ContextualSecureRemoteA2aAgent requires google-adk. "
                "Install it with: pip install stls_a2a[google-adk] "
                "or: pip install 'google-adk[a2a]>=1.25.0'"
            ) from exc

        _keys = context_keys if context_keys is not None else _DEFAULT_CONTEXT_KEYS
        _stls_provider = _make_meta_provider(_keys)

        existing_provider = kwargs.pop("a2a_request_meta_provider", None)
        if existing_provider is not None:
            def _merged_provider(ctx: Any, msg: Any) -> dict[str, Any] | None:
                stls_meta = _stls_provider(ctx, msg) or {}
                custom_meta = existing_provider(ctx, msg) or {}
                merged = {**stls_meta, **custom_meta}
                return merged if merged else None
            final_provider = _merged_provider
        else:
            final_provider = _stls_provider

        _manager = session_manager or SecureSessionManager(client_id=name)
        _transport = STLSHttpxTransport(_manager, extra_headers=extra_headers)
        _httpx_client = httpx.AsyncClient(transport=_transport)

        instance = RemoteA2aAgent(
            name=name,
            agent_card=agent_card,
            a2a_request_meta_provider=final_provider,
            httpx_client=_httpx_client,
            **kwargs,
        )

        # Useful references for external introspection/cleanup (not used by
        # RemoteA2aAgent): lets whoever created the agent close the
        # SecureSessionManager or invalidate sessions without rebuilding them.
        instance._stls_session_manager = _manager   # type: ignore[attr-defined]
        instance._stls_transport = _transport         # type: ignore[attr-defined]

        logger.debug(
            "ContextualSecureRemoteA2aAgent '%s' initialized — "
            "context propagation: %s, transport: encrypted STLS (PQC)",
            name, _keys,
        )

        return instance