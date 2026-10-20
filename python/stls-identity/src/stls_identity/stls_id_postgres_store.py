"""
stls_identity.postgres_store
==============================

Postgres-backed storage for what bridge.py serves publicly:
    - one DID Document per agent (the "Agent DID Catalog")
    - the last published StatusListCredential snapshot, cached — never
      recomputed per HTTP request

Deliberately mirrors stls_core.auth.stls_core_auth_loaders.PostgresLoader:
same lazy pool init, same env-var-with-override pattern, same
try/except-log-and-return-None posture on read paths. Anyone who has
worked with PostgresLoader should recognize this immediately.

Environment variables:
    IDENTITY_PG_DSN      asyncpg DSN (e.g. postgresql://user:pw@host/db)
    IDENTITY_PG_DID_TABLE           default: stls_identity_did_documents
    IDENTITY_PG_STATUS_LIST_TABLE   default: stls_identity_status_list_cache

Expected schema::

    CREATE TABLE stls_identity_did_documents (
        agent_id    TEXT        PRIMARY KEY,
        did         TEXT        NOT NULL UNIQUE,
        document    JSONB       NOT NULL,
        created_at  TIMESTAMPTZ DEFAULT now(),
        updated_at  TIMESTAMPTZ DEFAULT now()
    );

    CREATE TABLE stls_identity_status_list_cache (
        authority_id  TEXT        PRIMARY KEY,
        document      JSONB       NOT NULL,   -- full StatusListCredential, signed
        generated_at  TIMESTAMPTZ NOT NULL
    );

The status list cache is written by a periodic task (same spirit as
BackgroundRefresher) that calls StatusListManager.publish() against
Redis and persists the result here — bridge.py reads ONLY from this
table, never directly from Redis, so the public endpoint stays cheap
even under heavy external verification load.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Any

from .stls_id_models import DIDDocument, StatusListCredential

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DID Document store
# ---------------------------------------------------------------------------

class DIDDocumentStoreBase(ABC):
    @abstractmethod
    async def get(self, agent_id: str) -> DIDDocument | None: ...

    @abstractmethod
    async def put(self, agent_id: str, document: DIDDocument) -> None: ...

    @abstractmethod
    async def list_agent_ids(self) -> list[str]:
        """Every agent_id with a published DID Document — the roster of
        platform agents that have an identity, not of their credentials
        (those live elsewhere, per-issuer). Meant for an operational
        inventory/console, not for the verification hot path."""

    @abstractmethod
    async def close(self) -> None: ...


class PostgresDIDDocumentStore(DIDDocumentStoreBase):
    """Same lazy-pool-init / env-override pattern as PostgresLoader."""

    DEFAULT_TABLE = "stls_identity_did_documents"

    def __init__(
        self,
        dsn: str | None = None,
        min_pool_size: int = 1,
        max_pool_size: int = 5,
        table: str | None = None,
    ) -> None:
        self._dsn = dsn or os.environ.get("IDENTITY_PG_DSN", "")
        self._min_pool = min_pool_size
        self._max_pool = max_pool_size
        self._pool: Any = None  # asyncpg.Pool — Any per non forzare import a livello modulo
        self._table = table or os.environ.get("IDENTITY_PG_DID_TABLE", self.DEFAULT_TABLE)

    @property
    def table(self) -> str:
        """The ACTUAL table name in use by this instance — respects
        IDENTITY_PG_DID_TABLE if set. Use this, never the fixed
        DEFAULT_TABLE, for any external query/assertion that must point
        at the same table get()/put() write to."""
        return self._table

    def get_ddl(self) -> str:
        """DDL on this instance's ACTUAL table name. The module also
        exposes a convenience DDL constant based on the default names —
        if IDENTITY_PG_DID_TABLE is set to a different name, that
        constant creates the WRONG table. Use this method (or the
        status store instance's own DDL) whenever table names might
        have been overridden via env."""
        return f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                agent_id    TEXT        PRIMARY KEY,
                did         TEXT        NOT NULL UNIQUE,
                document    JSONB       NOT NULL,
                created_at  TIMESTAMPTZ DEFAULT now(),
                updated_at  TIMESTAMPTZ DEFAULT now()
            );
        """

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        try:
            import asyncpg  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "PostgresDIDDocumentStore requires asyncpg. "
                "Install with: uv add asyncpg"
            ) from exc

        if not self._dsn:
            raise RuntimeError("PostgresDIDDocumentStore: IDENTITY_PG_DSN not configured.")

        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min_pool,
            max_size=self._max_pool,
            command_timeout=10,
        )
        logger.info(
            "PostgresDIDDocumentStore: pool created (min=%d, max=%d, table=%s)",
            self._min_pool, self._max_pool, self._table,
        )
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def get(self, agent_id: str) -> DIDDocument | None:
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT document FROM {self._table} WHERE agent_id = $1",
                    agent_id,
                )
            if row is None:
                return None
            return self._row_to_document(row["document"])
        except Exception as exc:
            logger.error("PostgresDIDDocumentStore.get(%s) failed: %s", agent_id, exc)
            return None

    async def put(self, agent_id: str, document: DIDDocument) -> None:
        payload = document.model_dump(mode="json", by_alias=True)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._table} (agent_id, did, document, updated_at)
                VALUES ($1, $2, $3::jsonb, now())
                ON CONFLICT (agent_id) DO UPDATE
                    SET document = EXCLUDED.document, updated_at = now()
                """,
                agent_id,
                document.id,
                json.dumps(payload),
            )
        logger.info("PostgresDIDDocumentStore: upsert agent_id=%s did=%s", agent_id, document.id)

    async def list_agent_ids(self) -> list[str]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(f"SELECT agent_id FROM {self._table} ORDER BY agent_id ASC")
        return [r["agent_id"] for r in rows]

    @staticmethod
    def _row_to_document(raw: Any) -> DIDDocument:
        """asyncpg usually deserializes JSONB -> dict; some driver/PG
        combinations still return the string form — same caution as
        PostgresLoader._row_to_record()."""
        data = json.loads(raw) if isinstance(raw, str) else raw
        return DIDDocument.model_validate(data)


# ---------------------------------------------------------------------------
# Status list cache store
# ---------------------------------------------------------------------------

class StatusListCacheStoreBase(ABC):
    @abstractmethod
    async def get(self, authority_id: str) -> StatusListCredential | None: ...

    @abstractmethod
    async def put(self, authority_id: str, document: StatusListCredential) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...


class PostgresStatusListCacheStore(StatusListCacheStoreBase):
    DEFAULT_TABLE = "stls_identity_status_list_cache"

    def __init__(
        self,
        dsn: str | None = None,
        min_pool_size: int = 1,
        max_pool_size: int = 5,
        table: str | None = None,
    ) -> None:
        self._dsn = dsn or os.environ.get("IDENTITY_PG_DSN", "")
        self._min_pool = min_pool_size
        self._max_pool = max_pool_size
        self._pool: Any = None
        self._table = table or os.environ.get(
            "IDENTITY_PG_STATUS_LIST_TABLE", self.DEFAULT_TABLE
        )

    @property
    def table(self) -> str:
        """Same as PostgresDIDDocumentStore.table — the ACTUAL table
        name, respecting IDENTITY_PG_STATUS_LIST_TABLE if set."""
        return self._table

    def get_ddl(self) -> str:
        """Same as PostgresDIDDocumentStore.get_ddl() — DDL on this
        instance's ACTUAL table name, not the fixed default."""
        return f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                authority_id  TEXT        PRIMARY KEY,
                document      JSONB       NOT NULL,
                generated_at  TIMESTAMPTZ NOT NULL
            );
        """

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        try:
            import asyncpg  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "PostgresStatusListCacheStore requires asyncpg. "
                "Install with: uv add asyncpg"
            ) from exc

        if not self._dsn:
            raise RuntimeError("PostgresStatusListCacheStore: IDENTITY_PG_DSN not configured.")

        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min_pool,
            max_size=self._max_pool,
            command_timeout=10,
        )
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def get(self, authority_id: str) -> StatusListCredential | None:
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT document FROM {self._table} WHERE authority_id = $1",
                    authority_id,
                )
            if row is None:
                return None
            raw = row["document"]
            data = json.loads(raw) if isinstance(raw, str) else raw
            return StatusListCredential.model_validate(data)
        except Exception as exc:
            logger.error("PostgresStatusListCacheStore.get(%s) failed: %s", authority_id, exc)
            return None

    async def put(self, authority_id: str, document: StatusListCredential) -> None:
        payload = document.model_dump(mode="json", by_alias=True, exclude_none=True)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._table} (authority_id, document, generated_at)
                VALUES ($1, $2::jsonb, now())
                ON CONFLICT (authority_id) DO UPDATE
                    SET document = EXCLUDED.document, generated_at = now()
                """,
                authority_id,
                json.dumps(payload),
            )
        logger.info("PostgresStatusListCacheStore: upsert authority_id=%s", authority_id)


# ---------------------------------------------------------------------------
# DDL helper — handy for a one-off migration on the DEFAULT names.
# If you overrode IDENTITY_PG_DID_TABLE / IDENTITY_PG_STATUS_LIST_TABLE,
# this constant creates the tables with the WRONG name — use the
# single instance's own get_ddl() method instead, which respects the
# override.
# ---------------------------------------------------------------------------

DDL = f"""
CREATE TABLE IF NOT EXISTS {PostgresDIDDocumentStore.DEFAULT_TABLE} (
    agent_id    TEXT        PRIMARY KEY,
    did         TEXT        NOT NULL UNIQUE,
    document    JSONB       NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS {PostgresStatusListCacheStore.DEFAULT_TABLE} (
    authority_id  TEXT        PRIMARY KEY,
    document      JSONB       NOT NULL,
    generated_at  TIMESTAMPTZ NOT NULL
);
"""