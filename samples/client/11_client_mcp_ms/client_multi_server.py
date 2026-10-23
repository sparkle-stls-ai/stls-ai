"""
client_multi_server.py — "1 client -> N MCP servers" test with SecureToolsetManager
---------------------------------------------------------------------------------------
The MCP analogue of samples/client/10_client_ms/client_agent_multi_server.py.
Unlike stls-a2a's SecureSessionManager (which has a low-level,
ADK-free path via SecureA2ATransportAdapter.send_secure_message()),
stls_mcp's toolsets are inherently an ADK integration point
(STLSMCPToolset IS an ADK MCPToolset) — so the client here is
necessarily a minimal ADK root agent with BOTH toolsets attached as
tools=[...], one per MCP server, built via a single shared
SecureToolsetManager.

Each server's `ping` tool is named `ping_<server_label>` (set
server-side via @mcp.tool(name=...)) specifically so the two toolsets
don't expose two identically-named "ping" tools on the same agent —
the LLM picks a tool by name, so a shared name would make the two
servers indistinguishable to it.

Works, unmodified, with KEY_PROVIDER_NAME=base|pqc|arqit_py — same
.env switch as the stls-a2a samples.

Run (after starting server_mcp_ms_1 and server_mcp_ms_2):
    python client_multi_server.py
"""

import os

# Anchor the relative Arqit config paths (DEVICE_METADATA_FILEPATH,
# etc.) to this script's own directory — both aq-sf-mcp-client-ms-1.json
# and aq-sf-mcp-client-ms-2.json live here, with no filename collisions.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()

import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from stls_mcp import SecureToolsetManager

PROVIDER_NAME = os.getenv("KEY_PROVIDER_NAME", "pqc").strip().lower()

SERVERS = [
    ("server-mcp-ms-1", os.getenv("BASE_URL_1", "http://localhost:8201")),
    ("server-mcp-ms-2", os.getenv("BASE_URL_2", "http://localhost:8202")),
]

# Only relevant for arqit_py: a different device identity per server,
# resolved by base_url — same shape as stls-a2a's
# SecureSessionManager(arqit_client_configs=...).
ARQIT_CLIENT_CONFIGS = {
    SERVERS[0][1]: os.getenv("ARQIT_PY_CLIENT_CONFIG_1", "./aq-sf-mcp-client-ms-1.json"),
    SERVERS[1][1]: os.getenv("ARQIT_PY_CLIENT_CONFIG_2", "./aq-sf-mcp-client-ms-2.json"),
}

APP_NAME = "client_multi_server_mcp"


async def build_root_agent() -> Agent:
    manager = SecureToolsetManager(
        client_id="client-11-mcp-ms",
        provider_name=PROVIDER_NAME,
        arqit_client_configs=ARQIT_CLIENT_CONFIGS if PROVIDER_NAME == "arqit_py" else None,
    )
    toolsets = [await manager.get_toolset(base_url) for _, base_url in SERVERS]

    return Agent(
        name="root_multi_mcp_client",
        model="gemini-2.5-flash",
        description="Root agent with a ping tool from each of two encrypted MCP servers.",
        instruction=(
            "You have a ping_<server_label> tool for each connected MCP server.\n"
            "The user's message always names which server to use (e.g. "
            "'call ping_server_mcp_ms_1 with ...') — call exactly that tool, "
            "passing the rest of the message as its `message` argument.\n"
            "Return the tool's response verbatim, no modifications."
        ),
        tools=toolsets,
    )


async def ask(runner: Runner, session_service: InMemorySessionService, user_id: str, text: str) -> str:
    session = await session_service.create_session(app_name=APP_NAME, user_id=user_id)
    final_text = ""
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=text)]),
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = event.content.parts[0].text or final_text
    return final_text


async def main() -> None:
    root_agent = await build_root_agent()
    session_service = InMemorySessionService()
    runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)

    print(f"\n=== MCP multi-server test — provider={PROVIDER_NAME} ===")
    n_ok = 0
    for label, _ in SERVERS:
        tool_name = f"ping_{label.replace('-', '_')}"
        prompt = f"call {tool_name} with the message 'hello from the root agent'"
        try:
            answer = await ask(runner, session_service, "tester", prompt)
            print(f"  [{label}] OK — {answer}")
            n_ok += 1
        except Exception as exc:
            print(f"  [{label}] FAILED — {exc}")
    print(f"=== Done — {n_ok}/{len(SERVERS)} servers OK ===\n")


if __name__ == "__main__":
    asyncio.run(main())
