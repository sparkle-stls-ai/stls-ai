"""
tools_monitoring.py
-------------------
Tools for sofia_monitoring_agent.
Reads the sf_agents table and the sf_agents_live view to monitor the
status and capabilities of the agents active on the Sofia platform.

Expected env vars:
  AUTH_PG_DSN   postgresql://user:pass@host:5432/dbname
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)


# ── Connection ────────────────────────────────────────────────────────────────

def _conn():
    dsn = os.environ.get("AUTH_PG_DSN")
    if not dsn:
        raise RuntimeError("AUTH_PG_DSN not configured")
    return psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)


def _rows_to_list(cur) -> list[dict]:
    rows = cur.fetchall()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# MONITORING TOOLS
# ══════════════════════════════════════════════════════════════════════════════

def list_agents(
    status_filter: Optional[str] = None,
    liveness_filter: Optional[str] = None,
    limit: int = 100,
) -> dict:
    """
    Lists the agents registered on the Sofia platform with their liveness status.

    Args:
        status_filter:   Filter by the 'status' column: 'active', 'draining', 'offline'.
                         None = all.
        liveness_filter: Filter by liveness as computed by the view: 'live', 'offline', 'draining'.
                         None = all.
        limit:           Maximum number of rows (default 100).

    Returns:
        dict with 'agents' (list) and 'count'.
    """
    try:
        con = _conn()
        cur = con.cursor()

        conditions = []
        params: list = []

        if status_filter:
            conditions.append("status = %s")
            params.append(status_filter)
        if liveness_filter:
            conditions.append("liveness = %s")
            params.append(liveness_filter)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cur.execute(
            f"""
            SELECT agent_id, agent_name, host, port, base_url,
                   plain_url, secure_url, framework,
                   registered_at, last_heartbeat, seconds_ago,
                   status, liveness
            FROM sf_agents_live
            {where}
            ORDER BY agent_name, host
            LIMIT %s
            """,
            params + [limit],
        )
        agents = _rows_to_list(cur)
        # Convert datetime to ISO string for serialization
        for a in agents:
            for field in ("registered_at", "last_heartbeat"):
                if a.get(field):
                    a[field] = a[field].isoformat()
        cur.close()
        con.close()
        return {"agents": agents, "count": len(agents)}
    except Exception as e:
        logger.error("list_agents error: %s", e)
        return {"error": str(e)}


def get_agent_details(agent_id: str) -> dict:
    """
    Retrieves the full details of a single agent, including its capabilities.

    Args:
        agent_id: the agent's ID (e.g. 'calculator@host1:8001').

    Returns:
        dict with all of sf_agents_live's fields + 'capabilities' (expanded JSON).
    """
    try:
        con = _conn()
        cur = con.cursor()
        cur.execute(
            """
            SELECT a.*, l.seconds_ago, l.liveness
            FROM sf_agents a
            JOIN sf_agents_live l USING (agent_id)
            WHERE a.agent_id = %s
            """,
            (agent_id,),
        )
        row = cur.fetchone()
        if row is None:
            return {"error": f"Agent '{agent_id}' not found"}
        data = dict(row)
        for field in ("registered_at", "last_heartbeat"):
            if data.get(field):
                data[field] = data[field].isoformat()
        cur.close()
        con.close()
        return data
    except Exception as e:
        logger.error("get_agent_details error: %s", e)
        return {"error": str(e)}


def get_agent_capabilities(agent_id: str) -> dict:
    """
    Shows the capabilities an agent declares in A2A AgentCard format.

    Args:
        agent_id: the agent's ID (e.g. 'calculator@host1:8001').

    Returns:
        dict with 'agent_id', 'agent_name', 'capabilities' (expanded dict).
    """
    try:
        con = _conn()
        cur = con.cursor()
        cur.execute(
            "SELECT agent_id, agent_name, capabilities FROM sf_agents WHERE agent_id = %s",
            (agent_id,),
        )
        row = cur.fetchone()
        if row is None:
            return {"error": f"Agent '{agent_id}' not found"}
        data = dict(row)
        # capabilities is already a dict thanks to RealDictCursor + psycopg2 JSONB
        cur.close()
        con.close()
        return data
    except Exception as e:
        logger.error("get_agent_capabilities error: %s", e)
        return {"error": str(e)}


def list_capabilities_summary() -> dict:
    """
    Shows a summary of capabilities for all live agents.
    Useful to understand which skills are available on the platform.

    Returns:
        dict with 'summary': list of {agent_id, agent_name, capabilities_keys}.
    """
    try:
        con = _conn()
        cur = con.cursor()
        cur.execute(
            """
            SELECT a.agent_id, a.agent_name, a.framework,
                   a.capabilities, l.liveness, l.status
            FROM sf_agents a
            JOIN sf_agents_live l USING (agent_id)
            ORDER BY a.agent_name
            """
        )
        rows = _rows_to_list(cur)
        summary = []
        for r in rows:
            caps = r.get("capabilities") or {}
            summary.append({
                "agent_id": r["agent_id"],
                "agent_name": r["agent_name"],
                "framework": r["framework"],
                "liveness": r["liveness"],
                "status": r["status"],
                "capabilities": caps,
                "capabilities_keys": list(caps.keys()) if isinstance(caps, dict) else [],
            })
        cur.close()
        con.close()
        return {"summary": summary, "count": len(summary)}
    except Exception as e:
        logger.error("list_capabilities_summary error: %s", e)
        return {"error": str(e)}


def get_platform_health() -> dict:
    """
    Returns a concise picture of the platform's health:
    counts by status, offline agents, live agents.

    Returns:
        dict with 'total', 'live', 'offline', 'draining',
        'offline_agents' (list of agent_id + seconds_ago).
    """
    try:
        con = _conn()
        cur = con.cursor()

        # Aggregate counts
        cur.execute(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE liveness = 'live')     AS live,
                COUNT(*) FILTER (WHERE liveness = 'offline')  AS offline,
                COUNT(*) FILTER (WHERE liveness = 'draining') AS draining
            FROM sf_agents_live
            """
        )
        stats = dict(cur.fetchone())

        # Offline agents with useful info
        cur.execute(
            """
            SELECT agent_id, agent_name, host, port, seconds_ago, last_heartbeat
            FROM sf_agents_live
            WHERE liveness = 'offline'
            ORDER BY seconds_ago DESC
            """
        )
        offline = _rows_to_list(cur)
        for a in offline:
            if a.get("last_heartbeat"):
                a["last_heartbeat"] = a["last_heartbeat"].isoformat()

        cur.close()
        con.close()
        return {**stats, "offline_agents": offline}
    except Exception as e:
        logger.error("get_platform_health error: %s", e)
        return {"error": str(e)}


def search_agents_by_name(name_pattern: str) -> dict:
    """
    Searches for agents whose name contains the given string (case-insensitive).

    Args:
        name_pattern: part of the name to search for (e.g. 'calculator', 'redis').

    Returns:
        dict with 'agents' (list with liveness) and 'count'.
    """
    try:
        con = _conn()
        cur = con.cursor()
        cur.execute(
            """
            SELECT agent_id, agent_name, host, port, framework,
                   status, liveness, seconds_ago, base_url
            FROM sf_agents_live
            WHERE agent_name ILIKE %s
            ORDER BY agent_name
            """,
            (f"%{name_pattern}%",),
        )
        agents = _rows_to_list(cur)
        cur.close()
        con.close()
        return {"agents": agents, "count": len(agents)}
    except Exception as e:
        logger.error("search_agents_by_name error: %s", e)
        return {"error": str(e)}


def get_agents_by_framework(framework: str) -> dict:
    """
    Lists agents by framework (e.g. 'google-adk', 'langchain', 'custom').

    Args:
        framework: framework name (e.g. 'google-adk', 'langchain', 'semantic-kernel', 'custom').

    Returns:
        dict with 'agents' and 'count'.
    """
    try:
        con = _conn()
        cur = con.cursor()
        cur.execute(
            """
            SELECT agent_id, agent_name, host, port, framework,
                   status, liveness, seconds_ago
            FROM sf_agents_live
            WHERE framework = %s
            ORDER BY agent_name
            """,
            (framework,),
        )
        agents = _rows_to_list(cur)
        cur.close()
        con.close()
        return {"agents": agents, "count": len(agents), "framework": framework}
    except Exception as e:
        logger.error("get_agents_by_framework error: %s", e)
        return {"error": str(e)}