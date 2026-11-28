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
from stls_identity import identity_challenge_handler_from_env

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

BASE_URL_IDENTITY=os.getenv("BASE_URL_IDENTITY", "http://localhost:8014").rstrip("/")
API_KEY_IDENTITY=os.getenv("API_KEY_IDENTITY", "")

BASE_URL_GRANTOR=os.getenv("BASE_URL_GRANTOR", "http://localhost:8015").rstrip("/")
API_KEY_GRANTOR=os.getenv("API_KEY_GRANTOR", "")


# ══════════════════════════════════════════════════════════════════════════════
# COORDINATOR'S OWN IDENTITY (issue #45) — optional, off until bootstrapped
# ══════════════════════════════════════════════════════════════════════════════
#
# One call, fully driven by IDENTITY_AGENT_ID + GOVERNING_AUTHORITY_HOST
# (see coordinator/.env) — generalized into stls_identity itself
# (load_identity_presenter()/identity_challenge_handler_from_env(),
# stls_id_presentation.py) from what used to be ~40 lines of hand-rolled
# keystore/Redis/DID resolution here. Reads the SAME persistent stores
# identity_manager's own tools (tools_identity.py) write to — the
# identity keystore (private key) and the credential cache (Redis) —
# NOT a static snapshot file: whichever path bootstraps/issues the
# coordinator's identity (the standalone bootstrap_coordinator_identity.py
# script, or just asking sofia_identity_agent in chat) ends up in the
# SAME stores, so this always picks up the latest one on every restart.
#
# IDENTITY_AGENT_ID unset, or no key/credential found for it yet:
# returns None — no presentation ever attached, same as not configuring
# identity_challenge_handler at all. The coordinator keeps working
# against every sub-agent exactly as before; only sofia_identity_agent
# calls fail (with ERR_IDENTITY_CHALLENGE_REQUIRED) once
# identity_manager's own REQUIRE_IDENTITY_PRESENTATION=true, until
# bootstrapped.

_identity_challenge_handler = identity_challenge_handler_from_env()


# ══════════════════════════════════════════════════════════════════════════════
# SUB-AGENTS
# ══════════════════════════════════════════════════════════════════════════════


sofia_redis_agent = ContextualSecureRemoteA2aAgent(
    name="sofia_redis_agent",
    description=(
        "Agent to monitor and manage Redis instances used by the Sofia Platform."
    ),
    agent_card=f"{BASE_URL_REDIS}{AGENT_CARD_WELL_KNOWN_PATH}",
    extra_headers={"X-API-Key": API_KEY_REDIS},
    # No session.state context to propagate here — encrypted transport only,
    # same behavior as the plain RemoteA2aAgent it replaces (issue #80
    # follow-up: every sub-agent except monitoring now goes over the
    # encrypted channel).
    context_keys=[],
    # Issue #80 follow-up: same coordinator identity as sofia_identity_agent
    # below — None until the coordinator has been bootstrapped, harmless
    # no-op until the redis sub-agent's own REQUIRE_IDENTITY_PRESENTATION=true.
    identity_challenge_handler=_identity_challenge_handler,
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
    # Issue #80 follow-up: same coordinator identity as sofia_identity_agent
    # below — None until the coordinator has been bootstrapped (see
    # _identity_challenge_handler above), harmless no-op until
    # keys_management's own REQUIRE_IDENTITY_PRESENTATION=true.
    identity_challenge_handler=_identity_challenge_handler,
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


sofia_identity_agent = ContextualSecureRemoteA2aAgent(
    name="sofia_identity_agent",
    description=(
        "Agent to manage W3C DID / Verifiable Credential identities for agents "
        "running on the Sofia Platform — bootstrap/issue/revoke identities, "
        "onboard trust anchors, validate credentials."
    ),
    agent_card=f"{BASE_URL_IDENTITY}{AGENT_CARD_WELL_KNOWN_PATH}",
    extra_headers={"X-API-Key": API_KEY_IDENTITY},
    context_keys=[],
    # Issue #45: None until the coordinator has been bootstrapped — see
    # _identity_challenge_handler above. Once identity_manager's own
    # REQUIRE_IDENTITY_PRESENTATION=true, this is what lets the retry-once
    # challenge/presentation flow happen transparently (same mechanism as
    # samples/server/19_identity_presentation).
    identity_challenge_handler=_identity_challenge_handler,
)


sofia_grantor_agent = ContextualSecureRemoteA2aAgent(
    name="sofia_grantor_agent",
    description=(
        "Agent that issues real Operational Credentials (delegated "
        "permissions) under its own Grantor identity — issue #51. "
        "One running copy = one specific Grantor (e.g. a facility's "
        "authority); which one is a deployment detail (BASE_URL_GRANTOR), "
        "not something the coordinator decides."
    ),
    agent_card=f"{BASE_URL_GRANTOR}{AGENT_CARD_WELL_KNOWN_PATH}",
    extra_headers={"X-API-Key": API_KEY_GRANTOR},
    context_keys=[],
    # Issue #80 follow-up: grantor_agent/server.py now builds its own
    # identity_verifier the same way identity_manager does — same
    # coordinator identity, harmless no-op until that agent's own
    # REQUIRE_IDENTITY_PRESENTATION=true.
    identity_challenge_handler=_identity_challenge_handler,
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
        "inspect API key cache, check revocation SET, monitor server health.\n"
        "4. 'sofia_identity_agent' — manage W3C DID / Verifiable Credential identities: "
        "bootstrap/issue/revoke an agent's identity, onboard/list/revoke trust anchors, "
        "validate identity or operational (delegated) credentials.\n"
        "5. 'sofia_grantor_agent' — issue a real, signed delegation (Operational "
        "Credential) to another agent, under this Grantor's own authority (e.g. "
        "'authorize agent X to activate cross-connects at facility MI1 for 24 "
        "hours'). This agent can only delegate on behalf of the ONE Grantor "
        "identity it was deployed as — it never provisions new identities "
        "(that's sofia_identity_agent's job) and never decides who gets to be "
        "a Grantor.\n\n"
        "Routing hints:\n"
        "- Questions about 'which agents are running' or 'is agent X healthy' → sofia_monitoring_agent\n"
        "- Requests to 'create/revoke/list API keys' → sofia_api_keys_agent\n"
        "- Questions about 'tasks', 'Redis memory', 'cache entries' → sofia_redis_agent\n"
        "- Questions about identity, DID, Verifiable Credentials, trust anchors or "
        "validating a delegation → sofia_identity_agent\n"
        "- Requests to actually GRANT/AUTHORIZE/DELEGATE a permission to an agent → "
        "sofia_grantor_agent\n"
        "- For cross-domain questions (e.g. 'is the key for agent X in Redis?') "
        "coordinate both sofia_api_keys_agent and sofia_redis_agent."
    ),
    sub_agents=[
        sofia_monitoring_agent, sofia_api_keys_agent, sofia_redis_agent,
        sofia_identity_agent, sofia_grantor_agent,
    ],
)