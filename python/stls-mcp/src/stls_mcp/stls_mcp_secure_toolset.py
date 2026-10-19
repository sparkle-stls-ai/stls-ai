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

If a real multi-toolset cache is needed in the future (several
different MCP servers in the same process), stls-a2a's
SecureSessionManager pattern applies identically here: a cache keyed by
base_url, a lock against double handshakes. Not implemented in this
first version because the current goal is a single toolset per test
agent — see vault_agent_test.py.

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

import logging
from typing import Any, Callable, Optional

import httpx

from .stls_mcp_transport import (
    StlsHandshakeFailed,
    perform_client_handshake,
    perform_pqc_client_handshake,
    stls_httpx_factory,
)
from .stls_mcp_constants import MCP_STLS_PATH

logger = logging.getLogger(__name__)

__all__ = [
    "STLSMCPToolset",
    "create_stls_mcp_toolset",
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
    session_key: Optional[bytes] = None,
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
                         (requires a session_key already resolved from your
                         own KeySessionProvider, exactly like the server
                         side — see stls_core.KeySessionProviderFactory).
        session_key:    required if provider != "pqc". Ignored for "pqc"
                         (the key originates from the KEM handshake itself).
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
        ValueError:           provider != "pqc" but session_key not provided.
    """
    if provider == "pqc":
        session_id, transport = await perform_pqc_client_handshake(
            base_url,
            client_id=client_id,
            handshake_path=handshake_path or "/stls/pqc/handshake",
            httpx_client=handshake_httpx_client,
        )
    else:
        if session_key is None:
            raise ValueError(
                f"provider={provider!r} requires an explicit session_key "
                "(resolved from your own KeySessionProvider, like the server side). "
                "Only 'pqc' derives the key from the handshake itself."
            )
        session_id, transport = await perform_client_handshake(
            base_url,
            session_key,
            client_id=client_id,
            handshake_path=handshake_path or "/stls/handshake",
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
