"""
stls_a2a_agent_registry.py
--------------------------
Agent Registry with heartbeat on PostgreSQL.

Every agent registered in create_secure_a2a_endpoint() is written to
the sf_agents table and periodically updates last_heartbeat.
An agent is considered live if last_heartbeat > now() - threshold.

Table schema (created automatically if it doesn't exist):

    CREATE TABLE sf_agents (
        agent_id        TEXT PRIMARY KEY,
        agent_name      TEXT NOT NULL,
        host            TEXT NOT NULL,
        port            INT,
        base_url        TEXT NOT NULL,
        plain_url       TEXT,
        secure_url      TEXT,
        framework       TEXT,
        capabilities    JSONB DEFAULT '{}',
        registered_at   TIMESTAMPTZ DEFAULT now(),
        last_heartbeat  TIMESTAMPTZ DEFAULT now(),
        status          TEXT DEFAULT 'active'
    );

Configuration via env:
    AUTH_PG_DSN              PostgreSQL DSN (same connection as auth)
    SF_AGENTS_TABLE          Table name (default: sf_agents)
    SF_HEARTBEAT_INTERVAL    Seconds between heartbeats (default: 120)
    SF_LIVENESS_THRESHOLD    Seconds after which an agent is offline (default: 360)
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Any

logger = logging.getLogger(__name__)

AGENTS_TABLE         = os.getenv("SF_AGENTS_TABLE", "sf_agents")
HEARTBEAT_INTERVAL   = int(os.getenv("SF_HEARTBEAT_INTERVAL", 120))
LIVENESS_THRESHOLD   = int(os.getenv("SF_LIVENESS_THRESHOLD", 360))


# ---------------------------------------------------------------------------
# DDL — automatic table creation
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    agent_id        TEXT        PRIMARY KEY,
    agent_name      TEXT        NOT NULL,
    host            TEXT        NOT NULL,
    port            INT,
    base_url        TEXT        NOT NULL,
    plain_url       TEXT,
    secure_url      TEXT,
    framework       TEXT,
    capabilities    JSONB       NOT NULL DEFAULT '{}',
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_heartbeat  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT        NOT NULL DEFAULT 'active'
);

COMMENT ON TABLE {table} IS 'STLS-A2A agent registry with liveness heartbeat';
COMMENT ON COLUMN {table}.agent_id IS 'agent_name@host:port — unique per process';
COMMENT ON COLUMN {table}.status IS 'active | draining | offline';
"""


# ---------------------------------------------------------------------------
# AgentRegistry
# ---------------------------------------------------------------------------

class AgentRegistry:
    """
    Manages agent registration and heartbeat on PostgreSQL.

    Lifecycle:
        registry = AgentRegistry()
        await registry.register(agent_id, ...)  # at endpoint mount
        await registry.start_heartbeat(agent_id) # starts the asyncio task
        ...
        await registry.shutdown(agent_id)        # sets status=draining
        await registry.close()                   # closes the pool
    """

    def __init__(
        self,
        dsn: str | None = None,
        table: str = AGENTS_TABLE,
        heartbeat_interval: int = HEARTBEAT_INTERVAL,
        liveness_threshold: int = LIVENESS_THRESHOLD,
    ) -> None:
        self._dsn       = dsn or os.getenv("AUTH_PG_DSN", "")
        self._table     = table
        self._interval  = heartbeat_interval
        self._threshold = liveness_threshold
        self._pool: Any = None
        self._tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Pool lifecycle
    # ------------------------------------------------------------------

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        try:
            import asyncpg  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "AgentRegistry requires asyncpg. Install it with: uv add asyncpg"
            ) from exc
        if not self._dsn:
            raise RuntimeError(
                "AgentRegistry: AUTH_PG_DSN not configured."
            )
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1,
            max_size=3,
            command_timeout=10,
        )
        # Create the table if it doesn't exist
        async with self._pool.acquire() as conn:
            await conn.execute(_DDL.replace('{table}', self._table))
        logger.info("AgentRegistry: pool ready, table=%s", self._table)
        return self._pool

    async def close(self) -> None:
        """Stops all heartbeat tasks and closes the pool."""
        for _agent_id, task in list(self._tasks.items()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        if self._pool:
            await self._pool.close()
            self._pool = None
        logger.info("AgentRegistry: closed")

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def register(
        self,
        agent_id: str,
        agent_name: str,
        base_url: str,
        plain_url: str | None = None,
        secure_url: str | None = None,
        framework: str = "",
        capabilities: dict | None = None,
    ) -> None:
        """
        Registers or updates an agent in the sf_agents table.
        Uses UPSERT: if the agent is already present, updates all fields
        and resets status to 'active'.

        base_url and port are derived with this priority:
          1. Explicit base_url passed as a parameter
          2. BASE_URL env var (e.g. Cloud Run service URL)
          3. Built from http://localhost:{PORT}  (local development)
        """
        import json as _json
        pool = await self._get_pool()
        host = socket.gethostname()

        # Resolve base_url and port with a robust fallback.
        # If base_url is empty (not passed by create_secure_a2a_endpoint
        # and BASE_URL not set in the env), build a local URL
        # using PORT so that port and base_url never stay NULL/"".
        _env_base = os.environ.get("BASE_URL", "").rstrip("/")
        _env_port = int(os.environ.get("PORT", 8000))
        _effective_url = base_url or _env_base or f"http://localhost:{_env_port}"
        port = _extract_port(_effective_url) or _env_port
        # Always write a populated base_url to the DB
        base_url = _effective_url

        # Also update plain_url and secure_url if they were relative (no host)
        if plain_url and plain_url.startswith("/"):
            plain_url = _effective_url + plain_url
        if secure_url and secure_url.startswith("/"):
            secure_url = _effective_url + secure_url

        caps = _json.dumps(capabilities or {})

        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._table}
                    (agent_id, agent_name, host, port, base_url,
                     plain_url, secure_url, framework, capabilities,
                     registered_at, last_heartbeat, status)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,now(),now(),'active')
                ON CONFLICT (agent_id) DO UPDATE SET
                    agent_name     = EXCLUDED.agent_name,
                    host           = EXCLUDED.host,
                    port           = EXCLUDED.port,
                    base_url       = EXCLUDED.base_url,
                    plain_url      = EXCLUDED.plain_url,
                    secure_url     = EXCLUDED.secure_url,
                    framework      = EXCLUDED.framework,
                    capabilities   = EXCLUDED.capabilities,
                    last_heartbeat = now(),
                    status         = 'active'
                """,
                agent_id, agent_name, host, port, base_url,
                plain_url, secure_url, framework, caps,
            )
        logger.info(
            "AgentRegistry: registered agent_id=%s framework=%s base_url=%s",
            agent_id, framework, base_url,
        )

    async def start_heartbeat(self, agent_id: str) -> None:
        """Starts the asyncio task that updates last_heartbeat every SF_HEARTBEAT_INTERVAL s."""
        if agent_id in self._tasks and not self._tasks[agent_id].done():
            return
        task = asyncio.create_task(
            self._heartbeat_loop(agent_id),
            name=f"stls-heartbeat-{agent_id}",
        )
        self._tasks[agent_id] = task
        logger.info(
            "AgentRegistry: heartbeat started for %s (interval=%ds)",
            agent_id, self._interval,
        )

    async def shutdown(self, agent_id: str) -> None:
        """Sets status='draining' and stops the heartbeat (call on app shutdown)."""
        # Stop the task
        task = self._tasks.pop(agent_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Update the status
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE {self._table} SET status='draining' WHERE agent_id=$1",
                    agent_id,
                )
            logger.info("AgentRegistry: agent_id=%s → status=draining", agent_id)
        except Exception as exc:
            logger.error("AgentRegistry.shutdown(%s): %s", agent_id, exc)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def list_live(self) -> list[dict]:
        """
        Returns all live agents (recent last_heartbeat).
        An agent is live if: status='active' AND last_heartbeat > now() - threshold.
        """
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT agent_id, agent_name, host, port, base_url,
                           plain_url, secure_url, framework, capabilities,
                           registered_at, last_heartbeat, status,
                           EXTRACT(EPOCH FROM (now() - last_heartbeat))::int AS seconds_ago
                    FROM {self._table}
                    WHERE status = 'active'
                      AND last_heartbeat > now() - make_interval(secs => $1)
                    ORDER BY agent_name, host
                    """,
                    float(self._threshold),
                )
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.error("AgentRegistry.list_live(): %s", exc)
            return []

    async def list_all(self) -> list[dict]:
        """Returns all agents (live + offline + draining)."""
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT agent_id, agent_name, host, port, base_url,
                           plain_url, secure_url, framework,
                           registered_at, last_heartbeat, status,
                           EXTRACT(EPOCH FROM (now() - last_heartbeat))::int AS seconds_ago,
                           CASE
                             WHEN status != 'active' THEN status
                             WHEN last_heartbeat > now() - make_interval(secs => $1)
                             THEN 'live'
                             ELSE 'offline'
                           END AS liveness
                    FROM {self._table}
                    ORDER BY agent_name, host
                    """,
                    float(self._threshold),
                )
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.error("AgentRegistry.list_all(): %s", exc)
            return []

    async def heartbeat(self, agent_id: str) -> None:
        """Updates last_heartbeat for an agent (single operation)."""
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE {self._table} SET last_heartbeat=now() WHERE agent_id=$1",
                    agent_id,
                )
        except Exception as exc:
            logger.error("AgentRegistry.heartbeat(%s): %s", agent_id, exc)

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self, agent_id: str) -> None:
        """Asyncio loop: updates last_heartbeat every _interval seconds."""
        while True:
            try:
                await asyncio.sleep(self._interval)
                await self.heartbeat(agent_id)
                logger.debug("AgentRegistry: heartbeat OK → %s", agent_id)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    "AgentRegistry: heartbeat failed for %s: %s — retrying next cycle",
                    agent_id, exc,
                )


# ---------------------------------------------------------------------------
# Global singleton (shared by all agents in the same app)
# ---------------------------------------------------------------------------

_registry: AgentRegistry | None = None


def get_registry() -> AgentRegistry | None:
    """Returns the singleton instance, or None if PG isn't configured."""
    return _registry


def init_registry(dsn: str | None = None) -> AgentRegistry | None:
    """
    Initializes the AgentRegistry singleton if AUTH_PG_DSN is configured.
    Called automatically by create_secure_a2a_endpoint().
    Idempotent: subsequent calls return the existing instance.
    """
    global _registry
    if _registry is not None:
        return _registry

    effective_dsn = dsn or os.getenv("AUTH_PG_DSN", "")
    if not effective_dsn:
        return None

    _registry = AgentRegistry(dsn=effective_dsn)
    logger.info(
        "AgentRegistry: initialized (table=%s, interval=%ds, threshold=%ds)",
        AGENTS_TABLE, HEARTBEAT_INTERVAL, LIVENESS_THRESHOLD,
    )
    return _registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_agent_id(agent_name: str, base_url: str) -> str:
    """
    Builds a per-process unique agent_id.
    Format: {agent_name}@{hostname}:{port}
    """
    port = _extract_port(base_url)
    host = socket.gethostname()
    return f"{agent_name}@{host}:{port}" if port else f"{agent_name}@{host}"


# Default ports by scheme — used when the URL has no explicit port.
# Cloud Run, Azure Container Apps, and similar always expose on 443 (HTTPS).
_DEFAULT_PORTS: dict[str, int] = {"https": 443, "http": 80}


def _extract_port(url: str) -> int | None:
    """
    Extracts the port from a URL.

    If the port is explicit in the URL (e.g. http://host:8001/path), returns it.
    If absent, uses the scheme's default port (https→443, http→80).
    Returns None only if the URL is malformed or the scheme is unknown.

    Examples:
        "http://localhost:8001"          → 8001
        "https://my-agent.run.app"       → 443   (previously returned None)
        "http://my-agent.internal"       → 80    (previously returned None)
        "/agents/calculator/secure-a2a"  → None  (path, not a URL)
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.port:
            return parsed.port
        # Implicit port from the scheme
        return _DEFAULT_PORTS.get(parsed.scheme)
    except Exception:
        return None