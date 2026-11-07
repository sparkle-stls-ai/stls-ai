"""
agent.py
--------
Sofia Platform — Admin Agent with specialized sub-agents.
Each sub-agent is equipped with the tools specific to its domain.
"""


from google.adk.agents import Agent
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.agents.remote_a2a_agent import AGENT_CARD_WELL_KNOWN_PATH

from stls_a2a import ContextualSecureRemoteA2aAgent

from dotenv import load_dotenv
import httpx
load_dotenv()

import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

logger = logging.getLogger(__name__)

# LLM model passed in from outside (AGENT_MODEL environment variable).
# The default keeps the model used so far if the variable isn't set.
DEFAULT_AGENT_MODEL = "gemini-3.5-flash"
AGENT_MODEL = (os.getenv("AGENT_MODEL") or DEFAULT_AGENT_MODEL).strip()
logger.info(f"--- Agent model: {AGENT_MODEL} ---")


BASE_URL_REDIS=os.getenv("BASE_URL_REDIS", "http://localhost:8013").rstrip("/")
API_KEY_REDIS=os.getenv("API_KEY_REDIS", "")

BASE_URL_API_KEYS=os.getenv("BASE_URL_API_KEYS", "http://localhost:8012").rstrip("/")
API_KEY_API_KEYS=os.getenv("API_KEY_API_KEYS", "")

BASE_URL_MONITORING=os.getenv("BASE_URL_MONITORING", "http://localhost:8011").rstrip("/")
API_KEY_MONITORING=os.getenv("API_KEY_MONITORING", "")


# ══════════════════════════════════════════════════════════════════════════════
# SUB-AGENTS
# ══════════════════════════════════════════════════════════════════════════════


sofia_redis_agent = RemoteA2aAgent(
    name="sofia_redis_agent",
    description=(
        "Agent to monitor and manage Redis instances used by the Sofia Platform."
    ),
    agent_card=f"{BASE_URL_REDIS}{AGENT_CARD_WELL_KNOWN_PATH}",
    httpx_client=httpx.AsyncClient(
        headers={"X-API-Key": API_KEY_REDIS},
        timeout=httpx.Timeout(60.0),
    ),    
)


sofia_api_keys_agent = ContextualSecureRemoteA2aAgent(
    name="sofia_api_keys_agent",
    description=(
        "Agent to manage API keys for peer to peer agent communication on Sofia Platform."
    ),
    agent_card=f"{BASE_URL_API_KEYS}{AGENT_CARD_WELL_KNOWN_PATH}",
    extra_headers={"X-API-Key": API_KEY_API_KEYS},
    # No session.state context to propagate here — encrypted transport only,
    # same behavior as the plain RemoteA2aAgent it replaces.
    context_keys=[],
)


sofia_monitoring_agent = RemoteA2aAgent(
    name="sofia_monitoring_agent",
    description=(
        "Agent to monitor Sofia Platform and its agents."
    ),
    agent_card=f"{BASE_URL_MONITORING}{AGENT_CARD_WELL_KNOWN_PATH}",
    httpx_client=httpx.AsyncClient(
        headers={"X-API-Key": API_KEY_MONITORING},
        timeout=httpx.Timeout(60.0),
    ),
    
)


# ══════════════════════════════════════════════════════════════════════════════
# ROOT AGENT
# ══════════════════════════════════════════════════════════════════════════════

root_agent = Agent(
    name="sofia_admin_agent",
    model=AGENT_MODEL,
    description="Sofia Platform Administrator Agent.",
    instruction=(
        "You are a helpful assistant to administer Sofia Platform, "
        "an agentic AI distributed system powered by Sparkle's STLS A2A.\n\n"
        "You use a set of specialized sub-agents to perform operations:\n\n"
        "1. 'sofia_monitoring_agent' — monitor AI agents: status, capabilities, liveness, "
        "framework breakdown, platform health dashboard.\n"
        "2. 'sofia_api_keys_agent' — manage API keys: create, revoke, update scopes, "
        "list keys per client, usage statistics.\n"
        "3. 'sofia_redis_agent' — inspect and manage Redis: browse tasks by state or ID, "
        "inspect API key cache, check revocation SET, monitor server health.\n\n"
        "Routing hints:\n"
        "- Questions about 'which agents are running' or 'is agent X healthy' → sofia_monitoring_agent\n"
        "- Requests to 'create/revoke/list API keys' → sofia_api_keys_agent\n"
        "- Questions about 'tasks', 'Redis memory', 'cache entries' → sofia_redis_agent\n"
        "- For cross-domain questions (e.g. 'is the key for agent X in Redis?') "
        "coordinate both sofia_api_keys_agent and sofia_redis_agent."
    ),
    sub_agents=[sofia_monitoring_agent, sofia_api_keys_agent, sofia_redis_agent],
)