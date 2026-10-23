"""
client_agent_multi_server.py — the client itself is an ADK agent
--------------------------------------------------------------------
Unlike client_multi_server.py (a plain script driving SecureSessionManager
directly), here the client is a real ADK root `Agent` that delegates to
two remote secure sub-agents via `ContextualSecureRemoteA2aAgent` — the
drop-in encrypted replacement for ADK's `RemoteA2aAgent`.

This validates that `ContextualSecureRemoteA2aAgent` is itself adequate
for the multi-server `arqit_py` scenario: it is, with NO code changes,
because `arqit_client_configs` lives entirely on `SecureSessionManager`
(not on `ContextualSecureRemoteA2aAgent`), and the class already accepts
a pre-built `session_manager=` to share across sub-agents — exactly the
pattern used below. See the "ContextualSecureRemoteA2aAgent/
SecureSessionManager" note in python/stls-a2a/README.md's "Current
Limitations and Notes".

Works, unmodified, with KEY_PROVIDER_NAME=base|pqc|arqit_py — same
.env switch as the other two scripts in this directory.

Run (after starting server_ms_1 and server_ms_2):
    python client_agent_multi_server.py
"""

import os

# Anchor the relative Arqit config paths (DEVICE_METADATA_FILEPATH,
# etc.) to this script's own directory.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()

import asyncio
import logging
from urllib.parse import urlsplit

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

from google.adk.agents import Agent
from google.adk.agents.remote_a2a_agent import AGENT_CARD_WELL_KNOWN_PATH
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from stls_a2a import ContextualSecureRemoteA2aAgent, SecureSessionManager

PROVIDER_NAME = os.getenv("KEY_PROVIDER_NAME", "pqc").strip().lower()

SERVERS = [
    ("server_ms_1", os.getenv("BASE_URL_1", "http://localhost:8101/agents/server-ms-1")),
    ("server_ms_2", os.getenv("BASE_URL_2", "http://localhost:8102/agents/server-ms-2")),
]

# Only relevant for arqit_py: a different device identity per server,
# resolved by base_url — same mapping used by client_multi_server.py.
ARQIT_CLIENT_CONFIGS = {
    SERVERS[0][1]: os.getenv("ARQIT_PY_CLIENT_CONFIG_1", "./aq-sf-client-ms-1.json"),
    SERVERS[1][1]: os.getenv("ARQIT_PY_CLIENT_CONFIG_2", "./aq-sf-client-ms-2.json"),
}

# One SecureSessionManager shared by both remote sub-agents below — this
# is the documented pattern for talking to several arqit_py-secured
# counterparts from one process (see ContextualSecureRemoteA2aAgent's
# own docstring: "session_manager: ... to reuse across several agents").
_session_manager = SecureSessionManager(
    client_id="client-agent-multi-server",
    provider_name=PROVIDER_NAME,
    arqit_client_configs=ARQIT_CLIENT_CONFIGS if PROVIDER_NAME == "arqit_py" else None,
)

def _host_root(url: str) -> str:
    """
    scheme://netloc, no path — create_secure_a2a_endpoint() always serves
    .well-known/agent-card.json at the app root, never under the
    per-agent path prefix (/agents/server-ms-1/...). The card's own
    "url"/"secureUrl" fields are relative and get resolved by
    RemoteA2aAgent against wherever the card itself was fetched from,
    so this only needs to get the fetch right.
    """
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


remote_agents = [
    ContextualSecureRemoteA2aAgent(
        name=label,
        description=f"Remote diagnostic agent {label}, reached over encrypted STLS transport.",
        agent_card=f"{_host_root(base_url)}{AGENT_CARD_WELL_KNOWN_PATH}",
        session_manager=_session_manager,
    )
    for label, base_url in SERVERS
]

root_agent = Agent(
    name="root_multi_server_client",
    model="gemini-2.5-flash",
    description="Root agent that delegates to one of two remote secure sub-agents.",
    instruction=(
        "You have two sub-agents: server_ms_1 and server_ms_2.\n"
        "The user's message always names which one to use (e.g. "
        "'ask server_ms_1 to ...') — delegate to exactly that sub-agent, "
        "passing through the rest of the message as its `message` argument.\n"
        "Return the sub-agent's response verbatim, no modifications."
    ),
    sub_agents=remote_agents,
)

APP_NAME = "client_agent_multi_server"
session_service = InMemorySessionService()
runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)


async def ask(user_id: str, text: str) -> str:
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
    """
    A failure on one server (e.g. an exhausted GOOGLE_API_KEY on the
    Gemini side) must not prevent trying the others — this is validating
    the session/transport layer, not the LLM's answer.
    """
    print(f"\n=== client_agent_multi_server — provider={PROVIDER_NAME} ===")
    n_ok = 0
    for label, _ in SERVERS:
        prompt = f"ask {label} to ping with the message 'hello from the root agent'"
        try:
            answer = await ask("tester", prompt)
            print(f"  [{label}] OK — {answer}")
            n_ok += 1
        except Exception as exc:
            print(f"  [{label}] FAILED — {exc}")
    print(f"=== Done — {n_ok}/{len(SERVERS)} servers OK ===\n")


if __name__ == "__main__":
    asyncio.run(main())
