"""
meridian_mcp_server/server.py — 20_operator_identity_federation
---------------------------------------------------------------
Meridian's own tool server: the thing meridian_agent calls, over MCP,
once a Requestor's identity presentation has already cleared stls_a2a's
identity_verifier gate. This is a local, same-machine, same-operator
call — meridian_agent and meridian_mcp_server belong to the same
company, so the plain (unencrypted) /mcp endpoint would have been an
equally legitimate choice (the identity/delegation check already
happened one layer up, at the A2A boundary where Helios — a DIFFERENT
company — is the one being challenged). meridian_agent talks to the
STLS-protected /mcp-stls path anyway, so pqc is exercised end to end on
both legs of this demo, not because this particular call needs it.

One tool: check_and_apply_reroute. Fully simulated — no real network,
just enough state to make the "aha" moments legible (segment capacity,
a fake applied-at timestamp).
"""

from __future__ import annotations

import datetime as dt
import json
import os

from dotenv import load_dotenv

load_dotenv()

os.environ.setdefault("KEY_PROVIDER_NAME", "pqc")

from mcp.server.fastmcp import FastMCP

from stls_core import KeySessionProviderFactory
from stls_core.providers.stls_core_providers_base import KeySessionProvider
from stls_mcp import create_secure_mcp_app

PORT = int(os.getenv("PORT", "8711"))
MCP_PLAIN_PATH = os.getenv("MCP_PLAIN_PATH", "/mcp")
MCP_STLS_PATH = os.getenv("MCP_STLS_PATH", "/mcp-stls")

# Segment capacity is deliberately generous — this demo's rejections
# come from the identity layer, not from this tool ever saying no.
_SEGMENT_CAPACITY_PCT = {"MER-SEG-7": 42}

mcp = FastMCP(
    "meridian-network-ops",
    instructions="Applies a traffic reroute on a Meridian-owned network segment.",
    stateless_http=True,
    json_response=True,
    streamable_http_path=MCP_PLAIN_PATH,
)


@mcp.tool()
def check_and_apply_reroute(segment: str, reason: str) -> str:
    """Checks current load on `segment` and applies the reroute, `reason` for the audit trail."""
    capacity_pct = _SEGMENT_CAPACITY_PCT.get(segment, 15)
    result = {
        "segment": segment,
        "reason": reason,
        "capacity_before_pct": capacity_pct,
        "applied": True,
        "applied_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    return json.dumps(result)


provider = KeySessionProviderFactory().get_session_provider(
    session_type=KeySessionProvider.KeySessionProviderType.SERVER, port=PORT,
)
app = create_secure_mcp_app(mcp, provider, plain_path=MCP_PLAIN_PATH, stls_path=MCP_STLS_PATH)

if __name__ == "__main__":
    import uvicorn

    print(f"meridian_mcp_server starting on port {PORT} (plain={MCP_PLAIN_PATH})")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
