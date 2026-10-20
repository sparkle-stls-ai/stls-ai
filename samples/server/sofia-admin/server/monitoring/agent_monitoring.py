import os
from google.adk.agents import Agent

from dotenv import load_dotenv
load_dotenv()

DEFAULT_AGENT_MODEL = "gemini-3.5-flash"
AGENT_MODEL = (os.getenv("AGENT_MODEL") or DEFAULT_AGENT_MODEL).strip()


import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

logger = logging.getLogger(__name__)
logger.info(f"--- Agent model: {AGENT_MODEL} ---")
# ── Tool imports ───────────────────────────────────────────────────────────────

from tools_monitoring import (
    list_agents,
    get_agent_details,
    get_agent_capabilities,
    list_capabilities_summary,
    get_platform_health,
    search_agents_by_name,
    get_agents_by_framework,
)

sofia_monitoring_agent = Agent(
    name="sofia_monitoring_agent",
    model=AGENT_MODEL,
    description="Agent to monitor Sofia Platform and its agents.",
    instruction=(
        "You are the Sofia Monitoring Agent, responsible for monitoring the status "
        "and capabilities of agents running on the Sofia Platform.\n\n"
        "You have access to the following tools:\n"
        "- list_agents: list all agents (filter by status/liveness)\n"
        "- get_agent_details: full details of a single agent including capabilities\n"
        "- get_agent_capabilities: focused view of an agent's A2A capabilities\n"
        "- list_capabilities_summary: overview of all agents and their skill sets\n"
        "- get_platform_health: aggregate health dashboard (live/offline/draining counts)\n"
        "- search_agents_by_name: find agents by partial name match\n"
        "- get_agents_by_framework: filter by framework (google-adk, langchain, custom, ...)\n\n"
        "Liveness logic: an agent is 'live' if its last heartbeat was less than 360 seconds ago. "
        "Always highlight offline agents as potential problems. "
        "When showing capabilities, explain what the agent can do in plain language."
    ),
    tools=[
        list_agents,
        get_agent_details,
        get_agent_capabilities,
        list_capabilities_summary,
        get_platform_health,
        search_agents_by_name,
        get_agents_by_framework,
    ],
)
