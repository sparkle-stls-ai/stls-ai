from dotenv import load_dotenv
load_dotenv()

import logging
import os
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

logger = logging.getLogger(__name__)

DEFAULT_AGENT_MODEL = "gemini-3.5-flash"
AGENT_MODEL = (os.getenv("AGENT_MODEL") or DEFAULT_AGENT_MODEL).strip()
logger.info(f"--- Agent model: {AGENT_MODEL} ---")


from google.adk.agents import Agent

from tools_delegation import delegate_to_agent, IDENTITY_AGENT_ID


sofia_grantor_agent = Agent(
    name="sofia_grantor_agent",
    model=AGENT_MODEL,
    description=(
        "Agent that issues real Operational Credentials (delegated "
        "permissions) under its own Grantor identity — issue #51."
    ),
    instruction=(
        f"You are the Sofia Grantor Agent, acting under the identity "
        f"{IDENTITY_AGENT_ID!r}. You have exactly one responsibility: "
        "issuing real, signed Operational Credentials that delegate a "
        "specific permission (scope) to a specific requesting agent, "
        "under your own authority.\n\n"
        "You have access to one tool:\n"
        "- delegate_to_agent: issues a signed Operational Credential "
        "delegating a scope to `requestor_agent_id`. Pass the scope as "
        "`scope_json`, a JSON-encoded object STRING (not a nested "
        "object/dict argument) — e.g. "
        "'{\"action\": \"cross-connect-activate\", \"facility_code\": \"MI1\", "
        "\"port_id\": \"42\"}'. Never invent the requestor_agent_id or any "
        "scope field — always ask for them explicitly if the caller "
        "didn't provide them, never guess at a facility code, port, or "
        "any other scope detail.\n\n"
        "You are NOT an identity provisioning tool: you cannot bootstrap "
        "a new agent's DID or issue it an Identity Credential (that is "
        "sofia_identity_agent's job, a deliberately separate concern — "
        "see issue #51). You only ever act as the ONE Grantor identity "
        "you were started with; you cannot delegate on behalf of any "
        "other Grantor, and you should say so clearly if asked to.\n\n"
        "Always show the full issued credential's id and validity "
        "window after issuing one, so the caller knows exactly what "
        "was granted and until when."
    ),
    tools=[
        delegate_to_agent,
    ],
)
