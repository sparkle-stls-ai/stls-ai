"""
stls_core_auth_loaders.py
------------------------
Loader intercambiabili per le API key di STLS.

Selezione via env AUTH_LOADER_BACKEND:
    static   → StaticLoader   (in-memory, compatibile con AuthConfig attuale)
    file     → FileLoader     (YAML/JSON, path da AUTH_KEYS_FILE)
    postgres → PostgresLoader (asyncpg, dsn da AUTH_PG_DSN)

Il BackgroundRefresher in auth_manager chiama load_all() periodicamente.
ApiKeyManager chiama load_one() solo su cache miss.
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
    Interfaccia comune per tutti i loader.

    Ogni implementazione deve essere safe per uso concorrente
    (chiamate asyncio parallele da manager e refresher).
    """

    @abstractmethod
    async def load_all(self) -> list[KeyRecord]:
        """
        Carica tutti i KeyRecord disponibili.

        Chiamato dal BackgroundRefresher a ogni ciclo di refresh.
        Deve restituire una lista vuota (non alzare eccezione) se la
        sorgente è temporaneamente non disponibile — il refresher logga
        e riprova al prossimo intervallo.
        """
        ...

    @abstractmethod
    async def load_one(self, key_id: str) -> KeyRecord | None:
        """
        Carica un singolo KeyRecord per key_id (SHA-256 hex).

        Chiamato da ApiKeyManager su cache miss.
        Restituisce None se non trovato.
        """
        ...

    async def close(self) -> None:
        """Rilascia risorse (connessioni, file handle). Override se necessario."""


# ---------------------------------------------------------------------------
# StaticLoader — compatibile con AuthConfig attuale, per test/dev
# ---------------------------------------------------------------------------

class StaticLoader(ApiKeyLoader):
    """
    Loader in-memory: le chiavi sono passate direttamente al costruttore.

    Mantiene la compatibilità con il sistema AuthConfig già in produzione.
    Accetta sia KeyRecord già costruiti sia dict con i campi di KeyRecord,
    sia plaintext (raw_key + client_id) per semplicità nei test.

    Esempio di utilizzo con plaintext (retrocompatibile):

        loader = StaticLoader.from_plaintext({
            "my-secret-key-1": {"client_id": "agent-weather",
                                "scopes": ["secure-a2a"]},
            "my-secret-key-2": {"client_id": "agent-finance",
                                "scopes": ["secure-a2a", "admin"]},
        })

    Esempio con AuthConfig attuale:

        loader = StaticLoader.from_auth_config(auth_config)
    """

    def __init__(self, records: list[KeyRecord]) -> None:
        # index per key_id per lookup O(1)
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
        Costruisce il loader da un dict {raw_key: {client_id, scopes, ...}}.

        Il plaintext viene hashato immediatamente e poi scartato.
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
        Costruisce il loader dall'AuthConfig esistente di STLS.

        Compatibile con la struttura attuale: auth_config.api_keys (lista
        di stringhe plaintext) e auth_config.bearer_tokens.
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
        """Aggiunge o aggiorna un record a runtime (utile nei test)."""
        self._store[record.key_id] = record

    def remove(self, key_id: str) -> None:
        """Rimuove un record a runtime."""
        self._store.pop(key_id, None)

    def __len__(self) -> int:
        return len(self._store)


# ---------------------------------------------------------------------------
# FileLoader — YAML o JSON, ricaricabile a runtime
# ---------------------------------------------------------------------------

class FileLoader(ApiKeyLoader):
    """
    Carica le API key da un file YAML o JSON.

    Path configurabile via env AUTH_KEYS_FILE.
    Supporta reload a runtime: se il file cambia su disco, il
    BackgroundRefresher legge la versione aggiornata al prossimo ciclo
    (nessun SIGHUP necessario — il mtime viene confrontato a ogni load_all).

    Formato YAML atteso::

        keys:
          - key_id: "<sha256 hex>"          # oppure raw_key (vedi sotto)
            client_id: "agent-weather"
            scopes:
              - secure-a2a
            endpoints:
              - /secure-a2a
              - /secure-a2a/stream
            description: "Agente meteo"
            expires_at: "2027-01-01T00:00:00Z"  # opzionale

    Se nel file compare ``raw_key`` invece di ``key_id``, il loader
    esegue l'hash e scarta il plaintext in memoria.

    Formato JSON equivalente con lista piatta::

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
        # forza reload se cache vuota o file cambiato
        await self.load_all()
        return self._cache.get(key_id)

    # ------------------------------------------------------------------
    # Parsing interno
    # ------------------------------------------------------------------

    def _parse_file(self) -> list[KeyRecord]:
        """Legge e valida il file YAML/JSON, restituisce lista di KeyRecord."""
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

        # normalizza: accetta sia dict con chiave "keys" sia lista piatta
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
        """Converte un dict YAML/JSON in KeyRecord, gestendo raw_key."""
        if "raw_key" in item and "key_id" not in item:
            item = dict(item)  # copia per non mutare l'originale
            item["key_id"] = hash_key(item.pop("raw_key"))
        return KeyRecord.from_dict(item)

    @staticmethod
    def _parse_yaml(text: str) -> Any:
        """
        Parse YAML con fallback graceful se PyYAML non è installato.

        Per ambienti production si consiglia di aggiungere pyyaml alle
        dipendenze. In development, JSON è sempre disponibile.
        """
        try:
            import yaml  # type: ignore
            return yaml.safe_load(text)
        except ImportError:
            # fallback: prova a parsare come JSON (superset di YAML per casi semplici)
            logger.warning(
                "FileLoader: pyyaml non installato, tento JSON parse del file .yaml"
            )
            return json.loads(text)


# ---------------------------------------------------------------------------
# PostgresLoader — asyncpg, per ambienti production
# ---------------------------------------------------------------------------

class PostgresLoader(ApiKeyLoader):
    """
    Carica le API key da PostgreSQL via asyncpg.

    Configurazione via env:
        AUTH_PG_DSN          DSN PostgreSQL (es. postgresql://user:pass@host/db)
        SF_API_KEYS_TABLE    Nome tabella (default: sf_api_keys)

    Il connection pool viene creato al primo utilizzo (lazy init).

    Schema atteso::

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

    Il loader filtra automaticamente le righe con ``revoked = true``.
    La revoca effettiva viene gestita da RedisKeyCache (SET stls:revoked).
    """

    # Default tabella — sovrascrivibile via env SF_API_KEYS_TABLE
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
        self._pool: Any = None  # asyncpg.Pool, tipizzato come Any per evitare import a livello modulo
        # Nome tabella: parametro esplicito > env SF_API_KEYS_TABLE > default
        self._table = table or os.environ.get("SF_API_KEYS_TABLE", self.DEFAULT_TABLE)
        logger.debug("PostgresLoader: table=%s", self._table)

    # ------------------------------------------------------------------
    # Pool lifecycle
    # ------------------------------------------------------------------

    async def _get_pool(self) -> Any:
        """Crea il pool asyncpg al primo utilizzo (lazy)."""
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
    # Conversione row → KeyRecord
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: Any) -> KeyRecord:
        """
        Converte una asyncpg Record in KeyRecord.

        asyncpg restituisce:
        - TEXT[]     → list[str]
        - JSONB      → dict già deserializzato OPPURE stringa se il driver
                       non ha ancora deserializzato (es. PG18 + asyncpg 0.31).
                       Gestiamo entrambi i casi con _safe_dict().
        - TIMESTAMPTZ → datetime aware (UTC)
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
    Converte in dict in modo sicuro il valore di una colonna JSONB.

    asyncpg di solito deserializza JSONB → dict automaticamente,
    ma con alcune versioni di PostgreSQL (es. PG 18) o asyncpg (0.31)
    può restituire una stringa JSON o None.
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
# Factory — crea il loader corretto in base all'env AUTH_LOADER_BACKEND
# ---------------------------------------------------------------------------

def create_loader(
    backend: str | LoaderBackend | None = None,
    **kwargs: Any,
) -> ApiKeyLoader:
    """
    Factory function. Istanzia il loader corretto.

    Args:
        backend: "static" | "file" | "postgres" oppure LoaderBackend enum.
                 Default: env AUTH_LOADER_BACKEND, fallback "static".
        **kwargs: Argomenti passati al costruttore del loader specifico.
                  StaticLoader: records=[...]
                  FileLoader:   path="..."
                  PostgresLoader: dsn="...", min_pool_size=1, max_pool_size=5

    Raises:
        ValueError: se il backend non è riconosciuto.
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