"""
stls_mcp_constants.py
----------------------
Constants shared by the stls-mcp package.

MCP-specific paths, headers and STLS extension discovery keys.
Generic STLS cryptographic/header constants (X-STLS-Session-ID, etc.)
live in stls_core.stls_core_constants — only what's specific to the MCP
protocol lives here.
"""

# ---------------------------------------------------------------------------
# Endpoint paths
# ---------------------------------------------------------------------------

MCP_PLAIN_PATH: str = "/mcp"
"""
Path of the standard MCP endpoint, generated and managed entirely by the
official SDK (mcp.server.fastmcp.FastMCP.streamable_http_app()). No STLS
modification is applied on this path: it must behave indistinguishably
from a "pure" MCP server for any spec-compliant client (Claude Desktop,
claude.ai, MCP Inspector, etc.).
"""

MCP_STLS_PATH: str = "/mcp-stls"
"""
Path of the STLS encrypted MCP endpoint. Same session manager and same
tool/resource/prompt surface as MCP_PLAIN_PATH — the STLS middleware
decrypts inbound and encrypts outbound, then forwards internally to the
standard Starlette app mounted on MCP_PLAIN_PATH.
"""


# ---------------------------------------------------------------------------
# MCP-specific HTTP headers (official spec, not STLS)
# ---------------------------------------------------------------------------

HEADER_MCP_SESSION_ID: str = "mcp-session-id"
"""
Native session header of the MCP Streamable HTTP protocol.
Case-insensitive on the wire (ASGI headers are lowercase byte strings),
handled entirely by the SDK on MCP_PLAIN_PATH. On MCP_STLS_PATH our
middleware reads it for possible audit/logging but doesn't use it for
the cryptographic binding (that goes through HEADER_SESSION_ID in
stls_core, a distinct STLS value from the MCP session).
"""

HEADER_MCP_PROTOCOL_VERSION: str = "mcp-protocol-version"
"""
Header carrying the negotiated MCP protocol version, required by the
spec on requests following initialize(). Read/passed as-is by the STLS
middleware, never modified.
"""


# ---------------------------------------------------------------------------
# STLS extension discovery (capabilities.experimental.stls)
# ---------------------------------------------------------------------------

STLS_EXTENSION_KEY: str = "stls"
"""
Key under which the STLS extension is declared in the initialize()
response, inside capabilities.experimental. Example:

    {
        "capabilities": {
            "experimental": {
                "stls": {
                    "version": "1.0",
                    "encryption": "AES-256-GCM",
                    "hmac": "HMAC-SHA256",
                    "keyProvider": "arqit_py",
                    "endpoint": "/mcp-stls"
                }
            }
        }
    }

A client unaware of the extension simply ignores it (it's under
"experimental", by MCP design) and keeps using MCP_PLAIN_PATH.
"""

STLS_EXTENSION_URI: str = "https://stls-ai.com/extensions/mcp-secure-transport/v1"
"""Unique URI of the extension, consistent with the one already used in stls-a2a."""


# ---------------------------------------------------------------------------
# JSON-RPC error codes
# ---------------------------------------------------------------------------

ERR_SESSION_EXPIRED: int = -32004
"""
Same numeric value and meaning as stls_a2a's ERR_SESSION_EXPIRED
(stls_a2a_adapters_model.py): raised by StlsMcpMiddleware (status 401)
when the session cache has no entry for the given session_id — idle-TTL
or absolute max-age expiry (issue #6). Lets a client distinguish this
from any other 401 (e.g. an auth-layer API-key rejection), so only this
code justifies an automatic retry after re-handshake — see
StlsHttpxTransport's rehandshake parameter in stls_mcp_transport.py.
"""
