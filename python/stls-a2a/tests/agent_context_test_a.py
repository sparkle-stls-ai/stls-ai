"""
agent_context_test_a.py
-----------------------
Agent A — root agent with ContextualRemoteA2aAgent towards B.

Receives the request from the client, propagates the user context
(user_id, profile) to the remote agent_b via ContextualRemoteA2aAgent,
and returns B's diagnostic response.
"""

import os
from google.adk.agents import Agent
from google.adk.agents.remote_a2a_agent import AGENT_CARD_WELL_KNOWN_PATH
import httpx

from stls_a2a import ContextualRemoteA2aAgent

import logging
logger = logging.getLogger(__name__)

MODEL_GEMINI_3_5_FLASH = "gemini-2.5-flash"

BASE_URL_B  = os.getenv("BASE_URL_B",  "http://localhost:8002").rstrip("/")
API_KEY_B   = os.getenv("API_KEY_B",   "test-key-agent-b")


# ── Remote sub-agent B with context propagation ──────────────────────────────
agent_b_remote = ContextualRemoteA2aAgent(
    name="agent_b",
    description=(
        "Diagnostic agent that reports the user context it received."
    ),
    agent_card=f"{BASE_URL_B}{AGENT_CARD_WELL_KNOWN_PATH}",
    httpx_client=httpx.AsyncClient(
        headers={"X-API-Key": API_KEY_B},
        timeout=httpx.Timeout(60.0),
    ),
    context_keys=["user_id", "profile"],
)


# ── Remote sub-agent B with static X-STLS-* headers ──────────────────────────
# Alternative mode: the context is passed via static HTTP headers
# instead of session.state. Useful for service-to-service with a fixed identity.
# Note: the values are static — they don't change per request.
STATIC_USER_ID = "service-account-agent-a@tim-sparkle.com"
STATIC_PROFILE = {"tenant": "tim-sparkle", "role": "service", "department": "AI-Platform"}

import json as _json

agent_b_remote_static = ContextualRemoteA2aAgent(
    name="agent_b_static",
    description=(
        "Diagnostic agent (static context via X-STLS headers)."
    ),
    agent_card=f"{BASE_URL_B}{AGENT_CARD_WELL_KNOWN_PATH}",
    httpx_client=httpx.AsyncClient(
        headers={
            "X-API-Key":      API_KEY_B,
            # User context injected as static HTTP headers.
            # The STLS server reads them in _extract_http_context() and
            # propagates them to the adapter's context with priority 3.
            "X-STLS-User-Id": STATIC_USER_ID,
            "X-STLS-Profile": _json.dumps(STATIC_PROFILE),
        },
        timeout=httpx.Timeout(60.0),
    ),
    # Empty context_keys: doesn't propagate session.state — the context
    # comes exclusively from the static X-STLS-* headers above.
    context_keys=[],
)


# ── Root agent A ──────────────────────────────────────────────────────────────
agent_a = Agent(
    name="agent_a",
    model=MODEL_GEMINI_3_5_FLASH,
    description="Root agent that delegates to agent_b and reports its diagnostic.",
    instruction=(
        "You are a test orchestrator agent.\n\n"
        "ALWAYS delegate to a sub-agent — never answer yourself.\n\n"
        "- For ANY request about context, diagnosis, user context, "
        "or context propagation: delegate to 'agent_b'.\n"
        "- For ANY request about STATIC context or static headers: "
        "delegate to 'agent_b_static'.\n\n"
        "Return the sub-agent response verbatim. No modifications."
    ),
    sub_agents=[agent_b_remote, agent_b_remote_static],
)