"""
stls_mcp_secure_toolset.py
----------------------------
ADK <-> stls-mcp integration: builds an MCPToolset that talks to an MCP
server over the STLS ENCRYPTED channel (/mcp-stls) instead of the plain
/mcp, using the handshake and httpx transport already available in
stls_mcp_transport.py.

Why a dedicated module is needed (passing url="/mcp-stls" isn't enough)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Changing only the URL doesn't encrypt anything: /mcp-stls expects a
JSON body that's already encrypted (StlsMcpMiddleware decrypts it
server-side) and a valid X-STLS-Session-ID header from a session that
already went through the handshake — things the "stock" httpx client
used internally by MCPToolset doesn't know how to do.

We need to intercept at the httpx.AsyncBaseTransport level, exactly like
StlsHttpxTransport (stls_mcp_transport.py) already does — but ADK's
StreamableHTTPConnectionParams doesn't expose a parameter to inject a
custom httpx_client_factory (an open feature request, not yet in a
stable release: https://github.com/google/adk-python/issues/3005). The
verified workaround (same issue, maintainers' comment) is subclassing
both MCPSessionManager and MCPToolset. Same pattern used there, just
parameterized here to accept the STLS factory instead of a hardcoded
one.

Handshake: lazy or eager?
~~~~~~~~~~~~~~~~~~~~~~~~
Unlike ContextualSecureRemoteA2aAgent (stls-a2a, where the handshake is
lazy on first use, inside SecureSessionManager.get_adapter()), here it
must necessarily be EAGER: MCPSessionManager._create_client() is a
SYNCHRONOUS method that expects an already-working httpx_client_factory
— there's no async hook at the right point to do the handshake on the
fly the first time ADK calls a tool. create_stls_mcp_toolset() is
therefore an async function that must be awaited BEFORE building the
Agent (not directly inside tools=[...]), exactly like yesterday's
session_manager.get_adapter() but with an explicit await point on the
caller's side instead of hidden inside the first call.

Multi-server caching: SecureToolsetManager (below) implements exactly
the pattern this docstring used to defer — a cache keyed by base_url, a
lock against double handshakes, same shape as stls-a2a's
SecureSessionManager — built on stls-core's establish_session(), which
handles resolve+handshake uniformly for every provider (pqc included;
see issue #5, "Restrict stls-core's public API surface" — raw session
key bytes no longer pass through this module at all).

Importing google-adk: lazy, inside STLSMCPToolset.__new__
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
stls-mcp is also used by purely server-side processes (vault_server.py
and similar) that don't have — and shouldn't need — google-adk
installed: it's a protocol-agnostic MCP extension, not an ADK-specific
package. If this module imported `google.adk` at module scope,
`from stls_mcp import create_stls_mcp_toolset` (in __init__.py) would
make the entire stls_mcp package unimportable without ADK installed — a
regression for anyone using it server-side only.

Fix: same pattern as ContextualRemoteA2aAgent in stls-a2a
(stls_a2a_remote.py) — the google-adk import happens ONLY inside
STLSMCPToolset.__new__, i.e. at actual instantiation time, not at
module load time. `import stls_mcp` always stays safe; the explicit
error (ImportError with a clear message) only arrives if and when
someone actually tries to build an encrypted toolset without ADK
installed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

import httpx

from stls_core import establish_session

from .stls_mcp_transport import StlsHandshakeFailed, stls_httpx_factory
from .stls_mcp_constants import MCP_STLS_PATH

logger = logging.getLogger(__name__)

__all__ = [
    "STLSMCPToolset",
    "create_stls_mcp_toolset",
    "SecureToolsetManager",
]


class STLSMCPToolset:
    """
    Drop-in replacement for MCPToolset (google.adk.tools.mcp_tool) that
    routes all requests through an STLS-encrypted httpx_client_factory
    instead of the stock httpx client.

    Build it via create_stls_mcp_toolset() (which performs the handshake
    and prepares the factory) rather than directly, unless you already
    have an STLS session ready from elsewhere.

    The returned instance is a real MCPToolset (isinstance-compatible,
    the same reason ContextualRemoteA2aAgent in stls-a2a returns a real
    RemoteA2aAgent) — __new__ dynamically builds the workaround's two
    subclasses (see the module docstring) only at instantiation time,
    when google-adk is guaranteed to be available.
    """

    def __new__(
        cls,
        *,
        httpx_client_factory: Callable[..., httpx.AsyncClient],
        connection_params: Any,
        errlog: Any = None,
        **kwargs: Any,
    ) -> Any:
        try:
            from datetime import timedelta

            from google.adk.tools.mcp_tool import MCPToolset as _MCPToolset
            from google.adk.tools.mcp_tool.mcp_session_manager import (
                MCPSessionManager as _MCPSessionManager,
                StreamableHTTPConnectionParams,
            )
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:
            raise ImportError(
                "STLSMCPToolset / create_stls_mcp_toolset require google-adk. "
                "Install it with: pip install stls_mcp[adk] "
                "or: pip install 'google-adk>=1.25.0,<2.0.0'"
            ) from exc

        # ── ADK workaround — see the module docstring for why.
        # Source: https://github.com/google/adk-python/issues/3005 (maintainers'
        # comment), parameterized here: httpx_client_factory injected at
        # construction time instead of hardcoded at module scope.
        class _STLSMCPSessionManager(_MCPSessionManager):
            def __init__(self, *, httpx_client_factory: Callable[..., httpx.AsyncClient], **kw: Any) -> None:
                super().__init__(**kw)
                self._stls_httpx_client_factory = httpx_client_factory

            def _create_client(self, merged_headers: Optional[dict[str, str]] = None):
                if not isinstance(self._connection_params, StreamableHTTPConnectionParams):
                    raise ValueError(
                        "STLSMCPToolset requires StreamableHTTPConnectionParams, "
                        f"got {self._connection_params.__class__.__name__}"
                    )
                return streamablehttp_client(
                    url=self._connection_params.url,
                    headers=merged_headers,
                    timeout=timedelta(seconds=self._connection_params.timeout),
                    sse_read_timeout=timedelta(seconds=self._connection_params.sse_read_timeout),
                    terminate_on_close=self._connection_params.terminate_on_close,
                    httpx_client_factory=self._stls_httpx_client_factory,
                )

        class _RealSTLSMCPToolset(_MCPToolset):
            def __init__(self, *, httpx_client_factory: Callable[..., httpx.AsyncClient], **kw: Any) -> None:
                super().__init__(**kw)
                self._mcp_session_manager = _STLSMCPSessionManager(
                    connection_params=self._connection_params,
                    errlog=self._errlog,
                    httpx_client_factory=httpx_client_factory,
                )

        instance = _RealSTLSMCPToolset(
            httpx_client_factory=httpx_client_factory,
            connection_params=connection_params,
            errlog=errlog,
            **kwargs,
        )
        return instance


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def create_stls_mcp_toolset(
    base_url: str,
    *,
    client_id: str,
    provider: str = "pqc",
    config_path: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    stls_path: str = MCP_STLS_PATH,
    handshake_path: Optional[str] = None,
    timeout: float = 30.0,
    sse_read_timeout: float = 300.0,
    headers: Optional[dict[str, str]] = None,
    handshake_httpx_client: Optional[httpx.AsyncClient] = None,
    **toolset_kwargs: Any,
) -> STLSMCPToolset:
    """
    Performs the STLS handshake and returns a ready MCPToolset, connected
    to the server's encrypted channel (stls_path, default /mcp-stls).

    Args:
        base_url:       base URL of the MCP server (e.g. "http://localhost:8765").
        client_id:      client identifier for the handshake (server-side
                         audit/logging, not a secret).
        provider:       "pqc" (default — no pre-shared key required,
                         X25519+ML-KEM-768 KEM handshake) or "base"/"arqit_py"
                         — the session key is resolved internally via
                         stls_core.establish_session(), never passed in by
                         the caller (issue #5, "Restrict stls-core's public
                         API surface": raw key material no longer flows
                         through this function's own signature).
        config_path:    arqit_py only — device config override for THIS
                         call, forwarded to establish_session(). Ignored
                         for "pqc"/"base".
        host, port:     forwarded to establish_session() (base/arqit_py
                         only — kept for interface parity with
                         KeySessionProviderFactory.get_session_provider();
                         ignored for "pqc").
        stls_path:      path of the encrypted endpoint (defaults to
                         stls_mcp_constants.MCP_STLS_PATH, "/mcp-stls").
        handshake_path: path of the handshake endpoint. Defaults to
                         "/stls/pqc/handshake" for pqc, "/stls/handshake"
                         otherwise.
        timeout:         StreamableHTTPConnectionParams.timeout (seconds).
        sse_read_timeout: StreamableHTTPConnectionParams.sse_read_timeout
                         (seconds) — only relevant if the server ever uses
                         chunk-by-chunk encrypted SSE streaming (not yet
                         supported by StlsMcpMiddleware v0.1, which buffers
                         the entire response — see stls_mcp_middleware.py).
        headers:         application-level HTTP headers (e.g.
                         {"X-API-Key": "..."}) forwarded on EVERY MCP
                         request to stls_path — NOT on the handshake (that
                         endpoint stays always open by design, see
                         stls_mcp_auth.py). If the server has
                         AUTH_ENABLED=true, this must be set to the same
                         key configured server-side, otherwise every call
                         (after a successful handshake, since that stays
                         unprotected) gets a 401.
        handshake_httpx_client: optional httpx client reused only for the
                         handshake (not for application traffic, which
                         goes through the encrypted transport built
                         afterwards).
        **toolset_kwargs: other MCPToolset kwargs (e.g. tool_filter).

    Returns:
        STLSMCPToolset ready to pass in tools=[...] to an ADK Agent.

    Raises:
        StlsHandshakeFailed: HTTP handshake failed or invalid proof of
                              possession.
    """
    session_id, _kid, transport = await establish_session(
        provider_name=provider,
        base_url=base_url,
        client_id=client_id,
        config_path=config_path,
        host=host,
        port=port,
        handshake_path=handshake_path,
        httpx_client=handshake_httpx_client,
    )

    logger.info(
        "create_stls_mcp_toolset: STLS session ready (provider=%s, session_id=%s) against %s%s",
        provider, session_id, base_url, stls_path,
    )

    factory = stls_httpx_factory(session_id, transport, stls_path=stls_path)

    # Lazy import — same reason as STLSMCPToolset.__new__ (see the module
    # docstring): this function can only be called if the caller chose
    # the --secure path, but the module as a whole stays importable
    # without ADK for server-side-only users.
    try:
        from google.adk.tools.mcp_tool.mcp_session_manager import (
            StreamableHTTPConnectionParams,
        )
    except ImportError as exc:
        raise ImportError(
            "create_stls_mcp_toolset requires google-adk. Install it with: "
            "pip install stls_mcp[adk] or: pip install 'google-adk>=1.25.0,<2.0.0'"
        ) from exc

    _params_kwargs: dict[str, Any] = {
        "url": base_url.rstrip("/") + stls_path,
        "timeout": timeout,
        "sse_read_timeout": sse_read_timeout,
    }
    if headers is not None:
        _params_kwargs["headers"] = headers
    connection_params = StreamableHTTPConnectionParams(**_params_kwargs)

    return STLSMCPToolset(
        httpx_client_factory=factory,
        connection_params=connection_params,
        **toolset_kwargs,
    )


# ---------------------------------------------------------------------------
# Multi-server cache — stls-a2a's SecureSessionManager pattern, ported
# ---------------------------------------------------------------------------

class SecureToolsetManager:
    """
    Cache of encrypted STLS MCP toolsets by base_url, with a lazy build
    on first use — the stls-mcp analogue of stls-a2a's
    SecureSessionManager. An asyncio.Lock per base_url prevents duplicate
    concurrent handshakes/builds when multiple requests to the same
    target start in parallel before the first toolset is ready.

    A single instance should be shared across all agents/tools of a
    process that talk to the same MCP servers — avoids one handshake per
    caller toward the same server if several callers need it.

    Args:
        client_id:      identifier for the handshake (server-side
                         audit/logging, not a secret) — forwarded to
                         create_stls_mcp_toolset() for every target.
        provider_name:  "pqc" (default — no pre-shared key required) |
                         "base" | "arqit_py". The session key is resolved
                         internally by create_stls_mcp_toolset() via
                         stls-core's establish_session() for every
                         provider — this class only adds the base_url
                         cache/lock on top.
        arqit_client_configs: optional {base_url: config_path} map, used
                         only when provider_name="arqit_py" — lets one
                         process hold distinct Arqit device configs for
                         different MCP servers, same mapping shape and
                         same env-var fallback as
                         SecureSessionManager(arqit_client_configs=...).
        handshake_httpx_client: optional httpx client reused for pqc
                         handshakes (not for application traffic, which
                         goes through each toolset's own encrypted
                         transport). If None, create_stls_mcp_toolset()
                         builds one per handshake.
    """

    def __init__(
        self,
        *,
        client_id: str = "stls-secure-mcp-client",
        provider_name: str = "pqc",
        arqit_client_configs: Optional[dict[str, str]] = None,
        handshake_httpx_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.client_id = client_id
        self._provider_name = provider_name.strip().lower()
        self._arqit_client_configs = arqit_client_configs or {}
        self._handshake_httpx_client = handshake_httpx_client
        self._toolsets: dict[str, STLSMCPToolset] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, base_url: str) -> asyncio.Lock:
        lock = self._locks.get(base_url)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[base_url] = lock
        return lock

    async def get_toolset(self, base_url: str, **toolset_kwargs: Any) -> STLSMCPToolset:
        """
        Returns the cached toolset for base_url, building it (via
        create_stls_mcp_toolset(), which resolves+handshakes internally
        for every provider) on first use. Extra kwargs (stls_path,
        headers, tool_filter, ...) are passed through to
        create_stls_mcp_toolset() on a cache miss only — they have no
        effect on an already-cached toolset.
        """
        toolset = self._toolsets.get(base_url)
        if toolset is not None:
            return toolset

        async with self._lock_for(base_url):
            toolset = self._toolsets.get(base_url)
            if toolset is not None:
                return toolset

            toolset = await create_stls_mcp_toolset(
                base_url,
                client_id=self.client_id,
                provider=self._provider_name,
                config_path=self._arqit_client_configs.get(base_url),
                handshake_httpx_client=self._handshake_httpx_client,
                **toolset_kwargs,
            )

            self._toolsets[base_url] = toolset
            return toolset

    def invalidate(self, base_url: str) -> None:
        """Removes the cached toolset: the next get_toolset() rebuilds it."""
        self._toolsets.pop(base_url, None)
