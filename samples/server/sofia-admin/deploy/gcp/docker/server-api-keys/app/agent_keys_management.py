MODEL_GEMINI_2_5_FLASH = "gemini-2.5-flash"

from google.adk.agents import Agent

from dotenv import load_dotenv
load_dotenv()

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

logger = logging.getLogger(__name__)

# ── Tool imports ───────────────────────────────────────────────────────────────
from tools_keys_management import (
    list_api_keys,
    get_api_key_details,
    create_api_key,
    revoke_api_key,
    update_api_key_scopes,
    get_api_keys_stats,
)


sofia_api_keys_agent = Agent(
    name="sofia_api_keys_agent",
    model=MODEL_GEMINI_2_5_FLASH,
    description="Agent to manage API keys for peer to peer agent communication on Sofia Platform.",
    instruction=(
        "You are the Sofia API Keys Agent, responsible for managing API keys "
        "used for secure peer to peer communication between agents on the Sofia Platform.\n\n"
        "You have access to the following tools:\n"
        "- list_api_keys: list keys (filter by client_id, include/exclude revoked)\n"
        "- get_api_key_details: full details of a key by key_id (SHA-256)\n"
        "- create_api_key: generate a new key (returns raw_key ONE TIME ONLY — warn the user)\n"
        "- revoke_api_key: revoke a key on both PostgreSQL and Redis immediately\n"
        "- update_api_key_scopes: modify scopes/endpoints of an existing key\n"
        "- get_api_keys_stats: aggregate statistics (total, active, revoked, expired)\n\n"
        "SECURITY RULES:\n"
        "1. Never log or repeat the raw_key after showing it once.\n"
        "2. Always confirm before revoking a key — revocation is immediate and affects active agents.\n"
        "3. When creating a key, always remind the user to store the raw_key securely.\n"
        "4. The key_id (SHA-256) is safe to display; the raw_key is sensitive."
    ),
    tools=[
        list_api_keys,
        get_api_key_details,
        create_api_key,
        revoke_api_key,
        update_api_key_scopes,
        get_api_keys_stats,
    ],
)

