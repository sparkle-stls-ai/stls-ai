import os

from google.adk.agents import Agent
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.agents.remote_a2a_agent import AGENT_CARD_WELL_KNOWN_PATH

from dotenv import load_dotenv
load_dotenv()

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

logger = logging.getLogger(__name__)
# Modello LLM passato dall'esterno (variabile d'ambiente AGENT_MODEL).
# Il default mantiene il modello usato finora se la variabile non e' impostata.
DEFAULT_AGENT_MODEL = "gemini-3.5-flash"
AGENT_MODEL = (os.getenv("AGENT_MODEL") or DEFAULT_AGENT_MODEL).strip()
logger.info(f"--- Agent model: {AGENT_MODEL} ---")

# ── Tool imports ───────────────────────────────────────────────────────────────

from tools_redis import (
    list_tasks,
    get_task,
    get_task_context,
    search_tasks_by_state,
    get_tasks_stats,
    list_cached_api_keys,
    get_cached_api_key,
    is_api_key_revoked,
    list_revoked_key_ids,
    get_redis_server_info,
    count_keys_by_namespace,
    cleanup_stale_index_entries,
    # Session Cache (stls:sc:*) — rilevante per PQC e tutti i provider Redis
    list_stls_sessions,
    get_stls_session,
    get_stls_sessions_stats,
    delete_stls_session,
)


sofia_redis_agent = Agent(
    name="sofia_redis_agent",
    model=AGENT_MODEL,
    description="Agent to monitor and manage Redis instances used by the Sofia Platform.",
    instruction=(
        "You are the Sofia Redis Agent, responsible for monitoring and managing "
        "Redis instances used by the Sofia Platform.\n\n"
        "You have access to the following tools:\n"
        "- list_tasks / get_task / get_task_context: navigate A2A task data\n"
        "- search_tasks_by_state: filter tasks by state (working, completed, failed, submitted)\n"
        "- get_tasks_stats: aggregate counts per state\n"
        "- list_stls_sessions / get_stls_session: inspect active STLS sessions (stls:sc:*). "
        "Sessions created by the PQC provider have a UUID-format kid (provider_hint='pqc'); "
        "Arqit sessions show provider_hint='arqit_py'; fixed-key sessions show 'base'.\n"
        "- get_stls_sessions_stats: aggregate counts by provider (pqc/arqit_py/base), TTL stats\n"
        "- delete_stls_session: invalidate a session, forcing the client to re-handshake\n"
        "- list_cached_api_keys / get_cached_api_key: inspect API key cache\n"
        "- is_api_key_revoked / list_revoked_key_ids: check revocation SET\n"
        "- get_redis_server_info: version, memory, connected clients\n"
        "- count_keys_by_namespace: count keys per STLS-A2A namespace (includes stls:sc:*)\n"
        "- cleanup_stale_index_entries: remove expired entries from stls:index\n\n"
        "Always present data in a clear, readable format. "
        "When listing tasks, include state and context_id. "
        "When listing sessions, include provider_hint and TTL. "
        "Flag anomalies (e.g. many failed tasks, high memory usage, sessions with very low TTL, "
        "unexpected mix of provider types if a single provider is expected)."
    ),
    tools=[
        list_tasks,
        get_task,
        get_task_context,
        search_tasks_by_state,
        get_tasks_stats,
        list_stls_sessions,
        get_stls_session,
        get_stls_sessions_stats,
        delete_stls_session,
        list_cached_api_keys,
        get_cached_api_key,
        is_api_key_revoked,
        list_revoked_key_ids,
        get_redis_server_info,
        count_keys_by_namespace,
        cleanup_stale_index_entries,
    ],
)