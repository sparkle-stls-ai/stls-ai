"""
stls_mcp
--------
STLS extension of the Model Context Protocol: application-layer
symmetric encryption (AES-256-GCM + HMAC-SHA256 + HKDF-Expand) over a
second Streamable HTTP endpoint, alongside a 100% standard MCP endpoint
generated and managed entirely by the official SDK (mcp.server.fastmcp).

Design: "same session manager, byte-level proxy" — there is no second
FastMCP instance. The exact same tool/resource/prompt registered by the
developer is reachable both in plaintext (MCP_PLAIN_PATH) and encrypted
(MCP_STLS_PATH), with no duplication.

Cryptography, session cache, handshake (HMAC challenge/response with
proof-of-key-possession) and key provider (base/arqit_py) are shared
with stls-a2a via the stls-core package.

Typical usage:

    from mcp.server.fastmcp import FastMCP
    from stls_core import KeySessionProviderFactory, KeySessionProvider
    from stls_mcp import create_secure_mcp_app

    mcp = FastMCP("My Secure Server", stateless_http=True, json_response=True)

    @mcp.tool()
    def get_weather(city: str) -> str:
        return f"Weather in {city}: sunny"

    provider = KeySessionProviderFactory().get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.SERVER,
    )

    app = create_secure_mcp_app(mcp, provider)
    # uvicorn app:app --host 0.0.0.0 --port 8000
"""

from .stls_mcp_constants import (
    HEADER_MCP_PROTOCOL_VERSION,
    HEADER_MCP_SESSION_ID,
    MCP_PLAIN_PATH,
    MCP_STLS_PATH,
    STLS_EXTENSION_KEY,
    STLS_EXTENSION_URI,
)
from .stls_mcp_middleware import StlsMcpError, StlsMcpMiddleware
from .stls_mcp_auth import AuthConfig
from .stls_mcp_server import create_secure_mcp_app, stls_capabilities_fragment
from .stls_mcp_transport import (
    StlsHandshakeFailed,
    StlsHttpxTransport,
    perform_client_handshake,
    perform_pqc_client_handshake,
    stls_httpx_factory,
)
from .stls_mcp_secure_toolset import STLSMCPToolset, create_stls_mcp_toolset

__all__ = [
    # server.py — main entry point (server-side)
    "create_secure_mcp_app",
    "stls_capabilities_fragment",
    # transport.py — main entry point (client-side)
    "perform_client_handshake",
    "perform_pqc_client_handshake",
    "stls_httpx_factory",
    "StlsHttpxTransport",
    "StlsHandshakeFailed",
    # secure_toolset.py — client-side ADK integration (optional: lazy
    # google-adk import, inside STLSMCPToolset.__new__ — see that module
    # for why. import stls_mcp stays valid without ADK installed)
    "STLSMCPToolset",
    "create_stls_mcp_toolset",
    # middleware.py — exposed for whoever wants to assemble it manually
    # (e.g. integration into an existing Starlette/FastAPI app)
    "StlsMcpMiddleware",
    "StlsMcpError",
    # auth.py — static API key protection for create_secure_mcp_app().
    # For the enterprise tier (ApiKeyManager/ApiKeyManagerGroup, Redis/
    # File/Postgres) import from stls_core.auth and pass the
    # already-started instance to create_secure_mcp_app(api_key_manager=...).
    "AuthConfig",
    # constants.py
    "MCP_PLAIN_PATH",
    "MCP_STLS_PATH",
    "HEADER_MCP_SESSION_ID",
    "HEADER_MCP_PROTOCOL_VERSION",
    "STLS_EXTENSION_KEY",
    "STLS_EXTENSION_URI",
]

__version__ = "0.1.2"