"""
agent_context_test_b.py
-----------------------
Agente B — diagnostica contesto utente.

Legge user_id e profile dal session.state tramite FunctionTool.
NOTA: tool_context.state è un oggetto ADK State, non un dict.
Usare state.get(key, default) oppure state.to_dict() — NON dict(state).
"""

from google.adk.agents import Agent
from google.adk.tools.tool_context import ToolContext
import json
import logging

logger = logging.getLogger(__name__)

MODEL_GEMINI_3_5_FLASH = "gemini-3.5-flash"


def get_user_context(tool_context: ToolContext) -> dict:
    """
    Retrieves user_id and profile from the session state.

    The STLS-A2A framework populates the session state with the user
    context propagated by the caller (via message.metadata, params.metadata
    or X-STLS-* headers).

    Returns:
        dict with user_id and profile found in session state.
    """
    try:
        # State è un oggetto ADK custom — usa .get() o .to_dict()
        # NON usare dict(state): State non implementa __iter__
        state = tool_context.state
        user_id = state.get("user_id", "not set")
        profile  = state.get("profile", "not set")
    except Exception as e:
        logger.warning("get_user_context: errore lettura state: %s", e)
        user_id = "not set"
        profile  = "not set"

    logger.debug("get_user_context: user_id=%s", user_id)

    return {
        "user_id": str(user_id),
        "profile": json.dumps(profile, ensure_ascii=False)
                   if isinstance(profile, dict) else str(profile),
    }


agent_b = Agent(
    name="agent_b",
    model=MODEL_GEMINI_3_5_FLASH,
    description=(
        "Diagnostic agent that reports the user context (user_id and profile) "
        "it received in the current session."
    ),
    instruction=(
        "You are a diagnostic agent.\n\n"
        "Step 1: ALWAYS call the `get_user_context` tool — no exceptions.\n"
        "Step 2: Reply with ONLY the following JSON using the exact values "
        "returned by the tool. No markdown, no explanation, no extra text:\n"
        '{"user_id": "<tool result>", "profile": "<tool result>"}'
    ),
    tools=[get_user_context],
)