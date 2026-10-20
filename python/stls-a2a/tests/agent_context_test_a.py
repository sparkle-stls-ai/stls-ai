"""
agent_context_test_a.py
-----------------------
Agente A — root agent con ContextualRemoteA2aAgent verso B.

Riceve la richiesta dal client, propaga il contesto utente
(user_id, profile) ad agent_b remoto tramite ContextualRemoteA2aAgent,
e restituisce la risposta di diagnostica di B.
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


# ── Sub-agent remoto B con propagazione contesto ──────────────────────────────
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


# ── Sub-agent remoto B con header statici X-STLS-* ───────────────────────────
# Modalità alternativa: il contesto viene passato via header HTTP statici
# invece di session.state. Utile per service-to-service con identità fissa.
# Nota: i valori sono statici — non cambiano per richiesta.
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
            # Contesto utente iniettato come header HTTP statici.
            # Il server STLS li legge in _extract_http_context() e li
            # propaga al context dell'adapter con priorità 3.
            "X-STLS-User-Id": STATIC_USER_ID,
            "X-STLS-Profile": _json.dumps(STATIC_PROFILE),
        },
        timeout=httpx.Timeout(60.0),
    ),
    # context_keys vuoto: non propaga session.state — il contesto
    # arriva esclusivamente dagli header X-STLS-* statici sopra.
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