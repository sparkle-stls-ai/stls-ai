"""
stls_core_auth_loaders.py
------------------------
Interchangeable loaders for STLS API keys.

Selected via the AUTH_LOADER_BACKEND env var:
    static   → StaticLoader   (in-memory, compatible with the current AuthConfig)
    file     → FileLoader     (YAML/JSON, path from AUTH_KEYS_FILE)
    postgres → PostgresLoader (asyncpg, dsn from AUTH_PG_DSN)

The BackgroundRefresher in auth_manager calls load_all() periodically.
ApiKeyManager calls load_one() only on a cache miss.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .stls_core_auth_models import (
    KeyRecord,
    LoaderBackend,
    hash_key,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------

class ApiKeyLoader(ABC):
    """
    Common interface for all loaders.

    Every implementation must be safe for concurrent use
    (parallel asyncio calls from the manager and the refresher).
    """

    @abstractmethod
    async def load_all(self) -> list[KeyRecord]:
        """
        Loads all available KeyRecords.

        Called by the BackgroundRefresher on every refresh cycle.
        Must return an empty list (not raise) if the source is
        temporarily unavailable — the refresher logs it and retries
        on the next interval.
        """
        ...

    @abstractmethod
    async def load_one(self, key_id: str) -> KeyRecord | None:
        """
        Loads a single KeyRecord by key_id (SHA-256 hex).

        Called by ApiKeyManager on a cache miss.
        Returns None if not found.
        """
        ...

    async def close(self) -> None:
        """Releases resources (connections, file handles). Override if needed."""


# ---------------------------------------------------------------------------
# StaticLoader — compatible with the current AuthConfig, for test/dev
# ---------------------------------------------------------------------------

class StaticLoader(ApiKeyLoader):
    """
    In-memory loader: the keys are passed directly to the constructor.

    Maintains compatibility with the AuthConfig system already in
    production. Accepts either already-built KeyRecords, dicts with
    KeyRecord's fields, or plaintext (raw_key + client_id) for
    simplicity in tests.

    Usage example with plaintext (backward compatible):

        loader = StaticLoader.from_plaintext({
            "my-secret-key-1": {"client_id": "agent-weather",
                                "scopes": ["secure-a2a"]},
            "my-secret-key-2": {"client_id": "agent-finance",
                                "scopes": ["secure-a2a", "admin"]},
        })

    Usage with the current AuthConfig:

        loader = StaticLoader.from_auth_config(auth_config)
    """

    def __init__(self, records: list[KeyRecord]) -> None:
        # index by key_id for O(1) lookup
        self._store: dict[str, KeyRecord] = {r.key_id: r for r in records}

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_plaintext(
        cls,
        keys: dict[str, dict[str, Any]],
    ) -> "StaticLoader":
        """
        Builds the loader from a dict {raw_key: {client_id, scopes, ...}}.

        The plaintext is hashed immediately and then discarded.
        """
        records: list[KeyRecord] = []
        for raw_key, props in keys.items():
            kid = hash_key(raw_key)
            rec = KeyRecord(
                key_id=kid,
                client_id=props.get("client_id", "unknown"),
                scopes=list(props.get("scopes", [])),
                endpoints=list(props.get("endpoints", [])),
                description=props.get("description", ""),
                metadata=dict(props.get("metadata", {})),
            )
            records.append(rec)
        return cls(records)

    @classmethod
    def from_auth_config(cls, auth_config: Any) -> "StaticLoader":
        """
        Builds the loader from STLS's existing AuthConfig.

        Compatible with the current structure: auth_config.api_keys (a
        list of plaintext strings) and auth_config.bearer_tokens.
        """
        records: list[KeyRecord] = []

        api_keys: list[str] = getattr(auth_config, "api_keys", []) or []
        for raw_key in api_keys:
            if not raw_key:
                continue
            records.append(KeyRecord(
                key_id=hash_key(raw_key),
                client_id="legacy-api-key",
                scopes=["secure-a2a"],
                endpoints=[],
                description="Migrated from AuthConfig.api_keys",
            ))

        bearer_tokens: list[str] = getattr(auth_config, "bearer_tokens", []) or []
        for token in bearer_tokens:
            if not token:
                continue
            records.append(KeyRecord(
                key_id=hash_key(token),
                client_id="legacy-bearer",
                scopes=["secure-a2a"],
                endpoints=[],
                description="Migrated from AuthConfig.bearer_tokens",
            ))

        return cls(records)

    # ------------------------------------------------------------------
    # ApiKeyLoader impl
    # ------------------------------------------------------------------

    async def load_all(self) -> list[KeyRecord]:
        return list(self._store.values())

    async def load_one(self, key_id: str) -> KeyRecord | None:
        return self._store.get(key_id)

    def add(self, record: KeyRecord) -> None:
        """Adds or updates a record at runtime (useful in tests)."""
        self._store[record.key_id] = record

    def remove(self, key_id: str) -> None:
        """Removes a record at runtime."""
        self._store.pop(key_id, None)

    def __len__(self) -> int:
        return len(self._store)


# ---------------------------------------------------------------------------
# FileLoader — YAML or JSON, reloadable at runtime
# ---------------------------------------------------------------------------

class FileLoader(ApiKeyLoader):
    """
    Loads API keys from a YAML or JSON file.

    Path configurable via the AUTH_KEYS_FILE env var.
    Supports runtime reload: if the file changes on disk, the
    BackgroundRefresher reads the updated version on the next cycle
    (no SIGHUP needed — the mtime is compared on every load_all() call).

    Expected YAML format::

        keys:
          - key_id: "<sha256 hex>"          # or raw_key (see below)
            client_id: "agent-weather"
            scopes:
              - secure-a2a
            endpoints:
              - /secure-a2a
              - /secure-a2a/stream
            description: "Weather agent"
            expires_at: "2027-01-01T00:00:00Z"  # optional

    If the file has ``raw_key`` instead of ``key_id``, the loader
    hashes it and discards the plaintext in memory.

    Equivalent flat-list JSON format::

        [
          {"key_id": "...", "client_id": "...", "scopes": ["secure-a2a"]},
          ...
        ]
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path or os.environ.get("AUTH_KEYS_FILE", "api_keys.yaml"))
        self._last_mtime: float = 0.0
        self._cache: dict[str, KeyRecord] = {}

    # ------------------------------------------------------------------
    # ApiKeyLoader impl
    # ------------------------------------------------------------------

    async def load_all(self) -> list[KeyRecord]:
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            logger.warning("FileLoader: file non trovato: %s", self._path)
            return []

        if mtime != self._last_mtime or not self._cache:
            logger.debug("FileLoader: ricarico %s (mtime cambiato)", self._path)
            records = self._parse_file()
            self._cache = {r.key_id: r for r in records}
            self._last_mtime = mtime

        return list(self._cache.values())

    async def load_one(self, key_id: str) -> KeyRecord | None:
        # forces a reload if the cache is empty or the file changed
        await self.load_all()
        return self._cache.get(key_id)

    # ------------------------------------------------------------------
    # Internal parsing
    # ------------------------------------------------------------------

    def _parse_file(self) -> list[KeyRecord]:
        """Reads and validates the YAML/JSON file, returns a list of KeyRecords."""
        suffix = self._path.suffix.lower()
        text = self._path.read_text(encoding="utf-8")

        try:
            if suffix in (".yaml", ".yml"):
                raw = self._parse_yaml(text)
            else:
                raw = json.loads(text)
        except Exception as exc:
            logger.error("FileLoader: errore parsing %s: %s", self._path, exc)
            return []

        # normalizes: accepts either a dict with a "keys" field or a flat list
        if isinstance(raw, dict):
            items = raw.get("keys", [])
        elif isinstance(raw, list):
            items = raw
        else:
            logger.error("FileLoader: formato non riconosciuto in %s", self._path)
            return []

        records: list[KeyRecord] = []
        for item in items:
            try:
                record = self._item_to_record(item)
                records.append(record)
            except Exception as exc:
                logger.warning("FileLoader: record non valido ignorato: %s — %s", item, exc)

        logger.info("FileLoader: caricati %d record da %s", len(records), self._path)
        return records

    @staticmethod
    def _item_to_record(item: dict[str, Any]) -> KeyRecord:
        """Converts a YAML/JSON dict into a KeyRecord, handling raw_key."""
        if "raw_key" in item and "key_id" not in item:
            item = dict(item)  # copy so as not to mutate the original
            item["key_id"] = hash_key(item.pop("raw_key"))
        return KeyRecord.from_dict(item)

    @staticmethod
    def _parse_yaml(text: str) -> Any:
        """
        Parses YAML with a graceful fallback if PyYAML isn't installed.

        For production environments it's recommended to add pyyaml to
        the dependencies. In development, JSON is always available.
        """
        try:
            import yaml  # type: ignore
            return yaml.safe_load(text)
        except ImportError:
            # fallback: try parsing as JSON (a superset of YAML for simple cases)
            logger.warning(
                "FileLoader: pyyaml non installato, tento JSON parse del file .yaml"
            )
            return json.loads(text)


# ---------------------------------------------------------------------------
# PostgresLoader — asyncpg, for production environments
# ---------------------------------------------------------------------------

class PostgresLoader(ApiKeyLoader):
    """
    Loads API keys from PostgreSQL via asyncpg.

    Configuration via env:
        AUTH_PG_DSN          PostgreSQL DSN (e.g. postgresql://user:pass@host/db)
        SF_API_KEYS_TABLE    Table name (default: sf_api_keys)

    The connection pool is created on first use (lazy init).

    Expected schema::

        CREATE TABLE sf_api_keys (
            key_id      TEXT        PRIMARY KEY,
            client_id   TEXT        NOT NULL,
            scopes      TEXT[]      DEFAULT '{}',
            endpoints   TEXT[]      DEFAULT '{}',
            expires_at  TIMESTAMPTZ,
            created_at  TIMESTAMPTZ DEFAULT now(),
            revoked     BOOLEAN     DEFAULT false,
            description TEXT        DEFAULT '',
            metadata    JSONB       DEFAULT '{}'
        );

    The loader automatically filters out rows with ``revoked = true``.
    Actual revocation is handled by RedisKeyCache (the SET stls:revoked).
    """

    # Default table — overridable via the SF_API_KEYS_TABLE env var
    DEFAULT_TABLE = "sf_api_keys"

    def __init__(
        self,
        dsn: str | None = None,
        min_pool_size: int = 1,
        max_pool_size: int = 5,
        table: str | None = None,
    ) -> None:
        self._dsn = dsn or os.environ.get("AUTH_PG_DSN", "")
        self._min_pool = min_pool_size
        self._max_pool = max_pool_size
        self._pool: Any = None  # asyncpg.Pool, typed as Any to avoid a module-level import
        # Table name: explicit parameter > SF_API_KEYS_TABLE env > default
        self._table = table or os.environ.get("SF_API_KEYS_TABLE", self.DEFAULT_TABLE)
        logger.debug("PostgresLoader: table=%s", self._table)

    # ------------------------------------------------------------------
    # Pool lifecycle
    # ------------------------------------------------------------------

    async def _get_pool(self) -> Any:
        """Creates the asyncpg pool on first use (lazy)."""
        if self._pool is not None:
            return self._pool
        try:
            import asyncpg  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "PostgresLoader richiede asyncpg. "
                "Installa con: uv add asyncpg"
            ) from exc

        if not self._dsn:
            raise RuntimeError(
                "PostgresLoader: AUTH_PG_DSN non configurato."
            )

        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min_pool,
            max_size=self._max_pool,
            command_timeout=10,
        )
        logger.info("PostgresLoader: pool creato (min=%d, max=%d, table=%s)", self._min_pool, self._max_pool, self._table)
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            logger.info("PostgresLoader: pool chiuso")

    # ------------------------------------------------------------------
    # ApiKeyLoader impl
    # ------------------------------------------------------------------

    async def load_all(self) -> list[KeyRecord]:
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT key_id, client_id, scopes, endpoints,
                           expires_at, created_at, description, metadata
                    FROM {self._table}
                    WHERE revoked = false
                    ORDER BY created_at DESC
                    """
                )
            records = [self._row_to_record(r) for r in rows]
            logger.info("PostgresLoader: caricati %d record da DB", len(records))
            return records
        except Exception as exc:
            logger.error("PostgresLoader.load_all() fallito: %s", exc)
            return []

    async def load_one(self, key_id: str) -> KeyRecord | None:
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"""
                    SELECT key_id, client_id, scopes, endpoints,
                           expires_at, created_at, description, metadata
                    FROM {self._table}
                    WHERE key_id = $1 AND revoked = false
                    """,
                    key_id,
                )
            if row is None:
                return None
            return self._row_to_record(row)
        except Exception as exc:
            logger.error("PostgresLoader.load_one(%s) fallito: %s", key_id[:12], exc)
            return None

    # ------------------------------------------------------------------
    # Row → KeyRecord conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: Any) -> KeyRecord:
        """
        Converts an asyncpg Record into a KeyRecord.

        asyncpg returns:
        - TEXT[]     → list[str]
        - JSONB      → an already-deserialized dict, OR a string if the
                       driver hasn't deserialized it yet (e.g. PG18 +
                       asyncpg 0.31). Both cases handled via _safe_dict().
        - TIMESTAMPTZ → an aware datetime (UTC)
        """
        return KeyRecord(
            key_id=row["key_id"],
            client_id=row["client_id"],
            scopes=list(row["scopes"] or []),
            endpoints=list(row["endpoints"] or []),
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            description=row.get("description", "") or "",
            metadata=_safe_dict(row.get("metadata")),
        )


def _safe_dict(value: Any) -> dict:
    """
    Safely converts a JSONB column value into a dict.

    asyncpg usually deserializes JSONB → dict automatically, but with
    some PostgreSQL versions (e.g. PG 18) or asyncpg versions (0.31)
    it may return a JSON string or None.
    """
    import json as _json
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = _json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


# ---------------------------------------------------------------------------
# Factory — builds the correct loader based on the AUTH_LOADER_BACKEND env var
# ---------------------------------------------------------------------------

def create_loader(
    backend: str | LoaderBackend | None = None,
    **kwargs: Any,
) -> ApiKeyLoader:
    """
    Factory function. Instantiates the correct loader.

    Args:
        backend: "static" | "file" | "postgres", or a LoaderBackend enum.
                 Default: the AUTH_LOADER_BACKEND env var, fallback "static".
        **kwargs: Arguments passed to the specific loader's constructor.
                  StaticLoader: records=[...]
                  FileLoader:   path="..."
                  PostgresLoader: dsn="...", min_pool_size=1, max_pool_size=5

    Raises:
        ValueError: if the backend is not recognized.
    """
    if backend is None:
        backend = os.environ.get("AUTH_LOADER_BACKEND", LoaderBackend.STATIC.value)

    if isinstance(backend, LoaderBackend):
        backend = backend.value

    backend = backend.lower().strip()

    if backend == LoaderBackend.STATIC.value:
        records = kwargs.pop("records", [])
        return StaticLoader(records=records)

    if backend == LoaderBackend.FILE.value:
        path = kwargs.pop("path", None)
        return FileLoader(path=path)

    if backend == LoaderBackend.POSTGRES.value:
        dsn = kwargs.pop("dsn", None)
        min_pool = kwargs.pop("min_pool_size", 1)
        max_pool = kwargs.pop("max_pool_size", 5)
        table = kwargs.pop("table", None)
        return PostgresLoader(dsn=dsn, min_pool_size=min_pool, max_pool_size=max_pool, table=table)

    raise ValueError(
        f"Backend loader non riconosciuto: {backend!r}. "
        f"Valori validi: {[b.value for b in LoaderBackend]}"
    )
