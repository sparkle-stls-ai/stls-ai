MODEL_GEMINI_2_5_FLASH = "gemini-2.5-flash"

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
)


sofia_redis_agent = Agent(
    name="sofia_redis_agent",
    model=MODEL_GEMINI_2_5_FLASH,
    description="Agent to monitor and manage Redis instances used by the Sofia Platform.",
    instruction=(
        "You are the Sofia Redis Agent, responsible for monitoring and managing "
        "Redis instances used by the Sofia Platform.\n\n"
        "You have access to the following tools:\n"
        "- list_tasks / get_task / get_task_context: navigate A2A task data\n"
        "- search_tasks_by_state: filter tasks by state (working, completed, failed, submitted)\n"
        "- get_tasks_stats: aggregate counts per state\n"
        "- list_cached_api_keys / get_cached_api_key: inspect API key cache\n"
        "- is_api_key_revoked / list_revoked_key_ids: check revocation SET\n"
        "- get_redis_server_info: version, memory, connected clients\n"
        "- count_keys_by_namespace: count keys per STLS-A2A namespace\n"
        "- cleanup_stale_index_entries: remove expired entries from stls:index\n\n"
        "Always present data in a clear, readable format. "
        "When listing tasks, include state and context_id. "
        "Flag anomalies (e.g. many failed tasks, high memory usage)."
    ),
    tools=[
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
    ],
)
