"""
server.py — server_mcp_ms_2
------------------------------
The other of the two MCP servers used to test "1 client -> N MCP
servers" with stls_mcp.SecureToolsetManager (see
samples/client/11_client_mcp_ms/client_multi_server.py) — the MCP
analogue of samples/server/10_server_ms/server_ms_2 for stls-a2a.

Works, unmodified, with KEY_PROVIDER_NAME=base|pqc|arqit_py — the
provider choice lives entirely in this directory's .env.

Run:
    python server.py
"""

import os

# Anchor Arqit's relative config paths (DEVICE_METADATA_FILEPATH, etc.)
# to this script's own directory, regardless of the cwd the process was
# launched from — see ARQIT_PEERING_NOTES.md in stls-core.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()

import logging

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

from stls_core import KeySessionProviderFactory
from stls_core.providers.stls_core_providers_base import KeySessionProvider
from stls_mcp import create_secure_mcp_app

SERVER_LABEL = os.getenv("SERVER_LABEL", "server-mcp-ms-2")
PORT = int(os.getenv("PORT", "8202"))
MCP_PLAIN_PATH = os.getenv("MCP_PLAIN_PATH", "/mcp")
MCP_STLS_PATH = os.getenv("MCP_STLS_PATH", "/mcp-stls")

_allowed_hosts_env = os.getenv("MCP_ALLOWED_HOSTS", "")
ALLOWED_HOSTS = [h.strip() for h in _allowed_hosts_env.split(",") if h.strip()] or [
    "127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*",
]

mcp = FastMCP(
    SERVER_LABEL,
    instructions=f"Minimal diagnostic MCP server for {SERVER_LABEL} (multi-server test).",
    stateless_http=True,
    json_response=True,
    streamable_http_path=MCP_PLAIN_PATH,
    transport_security=TransportSecuritySettings(allowed_hosts=ALLOWED_HOSTS),
)


@mcp.tool(name=f"ping_{SERVER_LABEL.replace('-', '_')}")
def ping(message: str) -> str:
    """Returns an echo tagged with the label of the server that answered."""
    import json
    return json.dumps({"server": SERVER_LABEL, "echo": message})


# name=None -> reads KEY_PROVIDER_NAME from the environment: works
# identically for base/pqc/arqit_py, no per-provider branch here.
provider = KeySessionProviderFactory().get_session_provider(
    session_type=KeySessionProvider.KeySessionProviderType.SERVER,
    port=PORT,
)
app = create_secure_mcp_app(mcp, provider, plain_path=MCP_PLAIN_PATH, stls_path=MCP_STLS_PATH)

if __name__ == "__main__":
    import uvicorn

    logger.info(
        "%s starting on port %d (KEY_PROVIDER_NAME=%s, plain=%s, stls=%s)",
        SERVER_LABEL, PORT, os.getenv("KEY_PROVIDER_NAME", "pqc"), MCP_PLAIN_PATH, MCP_STLS_PATH,
    )
    uvicorn.run(app, host="0.0.0.0", port=PORT)
