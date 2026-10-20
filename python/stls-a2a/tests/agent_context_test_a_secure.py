"""
agent_context_test_a_secure.py
-------------------------------
Variante CIFRATA di agent_context_test_a.py — stesso identico scenario
(root agent A delega a B, propagazione user_id/profile), ma la delega
verso B transita su /secure-a2a (AES-256-GCM via handshake PQC) invece
che sul plain /a2a in chiaro.

Unica differenza rispetto all'originale: ContextualRemoteA2aAgent →
ContextualSecureRemoteA2aAgent, e niente httpx_client esplicito (lo
costruisce la classe stessa con lo STLSHttpxTransport iniettato).

Uso come banco di prova: puntare questo file e agent_context_test_a.py
sullo stesso agent_context_test_b.py (deve esporre l'endpoint PQC
handshake, cioè girare con KEY_PROVIDER_NAME=pqc) e confrontare il
traffico osservato — ci si aspetta /secure-a2a con body cifrato invece
di /a2a in chiaro, stesso risultato applicativo (user_id/profile
propagati identici).
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

# Un solo SecureSessionManager condiviso fra i due sub-agent sotto: se B è
# lo stesso host per entrambi, la seconda istanza riusa la sessione già
# handshakata dalla prima invece di aprirne una seconda inutilmente.
_session_manager = SecureSessionManager(client_id="agent_a_secure")


# ── Sub-agent remoto B con propagazione contesto, traffico cifrato ───────────
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


# ── Sub-agent remoto B con header statici X-STLS-*, traffico cifrato ────────
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
    # context_keys vuoto: il contesto arriva solo dagli header statici sopra,
    # propagati come extra_headers e letti server-side da _extract_http_context.
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