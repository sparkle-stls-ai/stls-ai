"""
agent_context_test_a_secure.py
-------------------------------
ENCRYPTED variant of agent_context_test_a.py — the exact same scenario
(root agent A delegates to B, user_id/profile propagation), but the
delegation to B goes over /secure-a2a (AES-256-GCM via PQC handshake)
instead of the plain, unencrypted /a2a.

Only difference from the original: ContextualRemoteA2aAgent →
ContextualSecureRemoteA2aAgent, and no explicit httpx_client (the
class itself builds one with the STLSHttpxTransport injected).

Use as a test bench: point this file and agent_context_test_a.py at
the same agent_context_test_b.py (which must expose the PQC handshake
endpoint, i.e. run with KEY_PROVIDER_NAME=pqc) and compare the observed
traffic — expect /secure-a2a with an encrypted body instead of plain
/a2a, with the same application-level result (identical user_id/profile
propagated).
"""

import os
from google.adk.agents import Agent
from google.adk.agents.remote_a2a_agent import AGENT_CARD_WELL_KNOWN_PATH

from stls_a2a import ContextualSecureRemoteA2aAgent, SecureSessionManager

import logging
logger = logging.getLogger(__name__)

MODEL_GEMINI_3_5_FLASH = "gemini-3.5-flash"

BASE_URL_B = os.getenv("BASE_URL_B", "http://localhost:8002").rstrip("/")
API_KEY_B  = os.getenv("API_KEY_B",  "test-key-agent-b")

# A single SecureSessionManager shared between the two sub-agents below: if B is
# the same host for both, the second instance reuses the session already
# handshaken by the first instead of needlessly opening a second one.
_session_manager = SecureSessionManager(client_id="agent_a_secure")


# ── Remote sub-agent B with context propagation, encrypted traffic ──────────
agent_b_remote_secure = ContextualSecureRemoteA2aAgent(
    name="agent_b",
    description=(
        "Diagnostic agent that reports the user context it received "
        "(encrypted transport)."
    ),
    agent_card=f"{BASE_URL_B}{AGENT_CARD_WELL_KNOWN_PATH}",
    session_manager=_session_manager,
    extra_headers={"X-API-Key": API_KEY_B},
    context_keys=["user_id", "profile"],
)


# ── Remote sub-agent B with static X-STLS-* headers, encrypted traffic ──────
STATIC_USER_ID = "service-account-agent-a@tim-sparkle.com"
STATIC_PROFILE = {"tenant": "tim-sparkle", "role": "service", "department": "AI-Platform"}

import json as _json

agent_b_remote_static_secure = ContextualSecureRemoteA2aAgent(
    name="agent_b_static",
    description=(
        "Diagnostic agent (static context via X-STLS headers, encrypted transport)."
    ),
    agent_card=f"{BASE_URL_B}{AGENT_CARD_WELL_KNOWN_PATH}",
    session_manager=_session_manager,
    extra_headers={
        "X-API-Key":      API_KEY_B,
        "X-STLS-User-Id": STATIC_USER_ID,
        "X-STLS-Profile": _json.dumps(STATIC_PROFILE),
    },
    # Empty context_keys: the context comes only from the static headers above,
    # propagated as extra_headers and read server-side by _extract_http_context.
    context_keys=[],
)


# ── Root agent A ──────────────────────────────────────────────────────────────
agent_a_secure = Agent(
    name="agent_a_secure",
    model=MODEL_GEMINI_3_5_FLASH,
    description="Root agent that delegates to agent_b over encrypted STLS transport.",
    instruction=(
        "You are a test orchestrator agent.\n\n"
        "ALWAYS delegate to a sub-agent — never answer yourself.\n\n"
        "- For ANY request about context, diagnosis, user context, "
        "or context propagation: delegate to 'agent_b'.\n"
        "- For ANY request about STATIC context or static headers: "
        "delegate to 'agent_b_static'.\n\n"
        "Return the sub-agent response verbatim. No modifications."
    ),
    sub_agents=[agent_b_remote_secure, agent_b_remote_static_secure],
)