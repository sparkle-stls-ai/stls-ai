"""
stls_core_auth_store.py
----------------------
Cache Redis per le API key di STLS.

Schema chiavi Redis (namespace configurabile, default "stls"):
    {ns}:key:{key_id}   → JSON(KeyRecord), TTL = expires_at o AUTH_KEY_TTL
    {ns}:revoked        → SET di key_id revocati (lookup O(1))
    {ns}:keys:index     → SET di tutti i key_id noti (per bulk ops)

Il namespace permette a più ApiKeyManager sullo stesso Redis di non
collidere: ogni loader (static, file, postgres) usa il proprio namespace,
evitando che bulk_reload di un loader cancelli le chiavi degli altri.

Compatibile con:
    - GCP Memorystore (Redis 7+)
    - AWS ElastiCache standalone
    - Azure Cache for Redis (TLS via rediss://)
    - Redis locale per sviluppo

Dipendenza: redis>=7.4.0  (async client built-in, no redis[asyncio])
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from .stls_core_auth_models import (
    DEFAULT_KEY_TTL_SECONDS,
    REDIS_INDEX_SET,
    REDIS_KEY_PREFIX,
    REDIS_REVOKED_SET,
    KeyRecord,
)

# Default namespace (retrocompatibile con le versioni precedenti)
_DEFAULT_NAMESPACE = "stls"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RedisKeyCache
# ---------------------------------------------------------------------------

class RedisKeyCache:
    """
    Cache primaria per i KeyRecord, backed by Redis.

    Lifecycle:
        cache = RedisKeyCache()
        await cache.initialize()   # crea il client Redis
        ...
        await cache.close()        # chiude la connessione

    Oppure come async context manager:
        async with RedisKeyCache() as cache:
            record = await cache.get("abc123...")

    Tutti i metodi sono safe per uso concorrente (asyncio).
    Le operazioni bulk usano pipeline Redis per minimizzare i round-trip.
    """

    def __init__(
        self,
        url: str | None = None,
        key_ttl: int | None = None,
        decode_responses: bool = True,
        namespace: str | None = None,
    ) -> None:
        """
        Args:
            url:              URL Redis. Default: env REDIS_URL, poi "redis://localhost:6379".
                              Per TLS (Azure, ecc.): "rediss://..."
            key_ttl:          TTL in secondi per i KeyRecord senza expires_at.
                              Default: env AUTH_KEY_TTL, poi DEFAULT_KEY_TTL_SECONDS (3600).
            decode_responses: Decodifica automatica bytes→str. Default True.
            namespace:        Prefisso per le chiavi Redis. Default: env AUTH_REDIS_NS o "stls".
                              Usare namespace diversi per isolare loader distinti sullo stesso
                              Redis (es. "stls:static", "stls:file", "stls:pg").
                              Più istanze della stessa app devono condividere lo stesso
                              namespace per beneficiare della cache distribuita.
        """
        self._url = url or os.environ.get("REDIS_URL", "redis://localhost:6379")
        self._key_ttl = key_ttl or int(
            os.environ.get("AUTH_KEY_TTL", DEFAULT_KEY_TTL_SECONDS)
        )
        self._decode = decode_responses
        self._client: Any = None  # redis.asyncio.Redis, lazy init

        # Namespace: isola le chiavi di questo loader dagli altri sullo stesso Redis.
        # Priorità: parametro esplicito > env AUTH_REDIS_NS > "stls"
        ns = namespace or os.environ.get("AUTH_REDIS_NS") or _DEFAULT_NAMESPACE
        self._namespace   = ns
        self._key_prefix  = f"{ns}:key:"        # {ns}:key:{key_id}
        self._revoked_set = f"{ns}:revoked"     # SET dei key_id revocati
        self._index_set   = f"{ns}:keys:index"  # SET di tutti i key_id noti

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Crea il client Redis. Chiamare prima di qualsiasi operazione."""
        if self._client is not None:
            return
        try:
            from redis.asyncio import Redis  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "RedisKeyCache richiede redis>=7.4.0. "
                "Installa con: uv add 'redis>=7.4.0'"
            ) from exc

        self._client = Redis.from_url(
            self._url,
            decode_responses=self._decode,
            socket_timeout=5,
            socket_connect_timeout=5,
            retry_on_timeout=True,
        )
        # verifica connessione al primo utilizzo
        await self._client.ping()
        logger.info("RedisKeyCache: connesso a %s", self._sanitize_url(self._url))

    async def close(self) -> None:
        """Chiude la connessione Redis."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("RedisKeyCache: connessione chiusa")

    async def __aenter__(self) -> "RedisKeyCache":
        await self.initialize()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Operazioni singole
    # ------------------------------------------------------------------

    async def get(self, key_id: str) -> KeyRecord | None:
        """
        Recupera un KeyRecord dalla cache.

        Returns:
            KeyRecord se presente e non scaduto (Redis gestisce il TTL),
            None se assente.
        """
        await self._ensure_connected()
        redis_key = f"{self._key_prefix}{key_id}"
        try:
            data = await self._client.get(redis_key)
            if data is None:
                return None
            return KeyRecord.from_dict(json.loads(data))
        except Exception as exc:
            logger.error("RedisKeyCache.get(%s...): %s", key_id[:12], exc)
            return None

    async def set(self, record: KeyRecord) -> None:
        """
        Scrive un KeyRecord in cache con TTL appropriato.

        TTL calcolato come:
            - Se record.expires_at è impostato: secondi rimanenti fino alla scadenza.
            - Altrimenti: AUTH_KEY_TTL (default 3600s).
        In entrambi i casi non superiore a AUTH_KEY_TTL per limitare
        la finestra di validità di chiavi con scadenza lontana nel tempo.
        """
        await self._ensure_connected()
        ttl = self._compute_ttl(record)
        if ttl <= 0:
            # chiave già scaduta: non scrivere in cache
            logger.debug("RedisKeyCache.set: chiave %s... già scaduta, skip", record.key_id[:12])
            return
        try:
            pipe = self._client.pipeline()
            pipe.setex(f"{self._key_prefix}{record.key_id}", ttl, json.dumps(record.to_dict()))
            pipe.sadd(self._index_set, record.key_id)
            await pipe.execute()
        except Exception as exc:
            logger.error("RedisKeyCache.set(%s...): %s", record.key_id[:12], exc)

    async def delete(self, key_id: str) -> None:
        """Rimuove un KeyRecord dalla cache (senza revocarlo)."""
        await self._ensure_connected()
        try:
            pipe = self._client.pipeline()
            pipe.delete(f"{self._key_prefix}{key_id}")
            pipe.srem(self._index_set, key_id)
            await pipe.execute()
        except Exception as exc:
            logger.error("RedisKeyCache.delete(%s...): %s", key_id[:12], exc)

    # ------------------------------------------------------------------
    # Revocation
    # ------------------------------------------------------------------

    async def revoke(self, key_id: str) -> None:
        """
        Revoca una API key.

        Aggiunge il key_id al SET {ns}:revoked E rimuove la entry
        dalla cache. Il SET non ha TTL: le revoche sono permanenti
        finché non vengono esplicitamente cancellate.
        """
        await self._ensure_connected()
        try:
            pipe = self._client.pipeline()
            pipe.sadd(self._revoked_set, key_id)
            pipe.delete(f"{self._key_prefix}{key_id}")
            pipe.srem(self._index_set, key_id)
            await pipe.execute()
            logger.info("RedisKeyCache: revocata chiave %s...", key_id[:12])
        except Exception as exc:
            logger.error("RedisKeyCache.revoke(%s...): %s", key_id[:12], exc)

    async def is_revoked(self, key_id: str) -> bool:
        """
        Verifica se un key_id è nel SET di revoca.

        Operazione O(1). Chiamata come primo check in ApiKeyManager.validate()
        prima ancora di consultare la cache del record.
        """
        await self._ensure_connected()
        try:
            return bool(await self._client.sismember(self._revoked_set, key_id))
        except Exception as exc:
            logger.error("RedisKeyCache.is_revoked(%s...): %s", key_id[:12], exc)
            # fail-open: in caso di errore Redis non blocchiamo il servizio
            return False

    async def unrevoke(self, key_id: str) -> None:
        """
        Rimuove un key_id dal SET di revoca (operazione di emergenza).

        Non ripristina la cache: il record verrà ricaricato dal loader
        al prossimo accesso.
        """
        await self._ensure_connected()
        try:
            await self._client.srem(self._revoked_set, key_id)
            logger.info("RedisKeyCache: revoca rimossa per %s...", key_id[:12])
        except Exception as exc:
            logger.error("RedisKeyCache.unrevoke(%s...): %s", key_id[:12], exc)

    # ------------------------------------------------------------------
    # Bulk operations (usate dal BackgroundRefresher)
    # ------------------------------------------------------------------

    async def bulk_reload(self, records: list[KeyRecord]) -> int:
        """
        Sostituisce atomicamente tutti i KeyRecord in cache del namespace corrente.

        Algoritmo:
            1. Recupera l'indice corrente ({ns}:keys:index).
            2. Scrive tutti i nuovi record in pipeline.
            3. Rimuove i record non più presenti nel nuovo set.
            4. Aggiorna l'indice.

        Non tocca mai il SET di revoca né le chiavi di altri namespace.

        Returns:
            Numero di record scritti.
        """
        await self._ensure_connected()
        if not records:
            logger.warning("RedisKeyCache.bulk_reload: lista vuota, operazione annullata")
            return 0

        new_ids = {r.key_id for r in records}

        try:
            # 1. indice corrente (solo del namespace corrente)
            old_ids: set[str] = await self._client.smembers(self._index_set) or set()

            # 2. scrivi tutti i nuovi record in pipeline
            pipe = self._client.pipeline()
            for record in records:
                ttl = self._compute_ttl(record)
                if ttl <= 0:
                    continue
                pipe.setex(f"{self._key_prefix}{record.key_id}", ttl, json.dumps(record.to_dict()))
                pipe.sadd(self._index_set, record.key_id)

            # 3. rimuovi record obsoleti (presenti prima, assenti ora)
            stale_ids = old_ids - new_ids
            for stale_id in stale_ids:
                pipe.delete(f"{self._key_prefix}{stale_id}")
                pipe.srem(self._index_set, stale_id)

            await pipe.execute()

            if stale_ids:
                logger.info(
                    "RedisKeyCache[%s].bulk_reload: rimossi %d record obsoleti",
                    self._namespace, len(stale_ids),
                )
            logger.info(
                "RedisKeyCache[%s].bulk_reload: scritti %d record",
                self._namespace, len(records),
            )
            return len(records)

        except Exception as exc:
            logger.error("RedisKeyCache[%s].bulk_reload(): %s", self._namespace, exc)
            return 0

    async def list_all_ids(self) -> set[str]:
        """Restituisce tutti i key_id nell'indice del namespace corrente (per diagnostica/admin)."""
        await self._ensure_connected()
        try:
            return await self._client.smembers(self._index_set) or set()
        except Exception as exc:
            logger.error("RedisKeyCache.list_all_ids(): %s", exc)
            return set()

    async def list_revoked_ids(self) -> set[str]:
        """Restituisce tutti i key_id revocati nel namespace corrente (per diagnostica/admin)."""
        await self._ensure_connected()
        try:
            return await self._client.smembers(self._revoked_set) or set()
        except Exception as exc:
            logger.error("RedisKeyCache.list_revoked_ids(): %s", exc)
            return set()

    async def flush_all_keys(self) -> None:
        """
        Rimuove tutti i KeyRecord e l'indice dalla cache del namespace corrente.

        NON tocca il SET di revoca né le chiavi di altri namespace.
        Utile per forzare un reload completo dal loader.
        """
        await self._ensure_connected()
        try:
            ids = await self._client.smembers(self._index_set) or set()
            pipe = self._client.pipeline()
            for kid in ids:
                pipe.delete(f"{self._key_prefix}{kid}")
            pipe.delete(self._index_set)
            await pipe.execute()
            logger.info("RedisKeyCache[%s].flush_all_keys: rimossi %d record", self._namespace, len(ids))
        except Exception as exc:
            logger.error("RedisKeyCache.flush_all_keys(): %s", exc)

    # ------------------------------------------------------------------
    # Diagnostica
    # ------------------------------------------------------------------

    async def ping(self) -> bool:
        """Health check: True se Redis risponde."""
        try:
            await self._ensure_connected()
            return await self._client.ping()
        except Exception:
            return False

    async def stats(self) -> dict[str, Any]:
        """
        Statistiche sintetiche della cache.

        Returns dict con:
            cached_keys:   numero di record in cache nel namespace corrente
            revoked_keys:  numero di key_id revocati nel namespace corrente
            redis_url:     URL sanitizzato (password oscurata)
            key_ttl:       TTL di default configurato
            namespace:     namespace Redis usato da questa istanza
        """
        await self._ensure_connected()
        cached = len(await self.list_all_ids())
        revoked = len(await self.list_revoked_ids())
        return {
            "cached_keys": cached,
            "revoked_keys": revoked,
            "redis_url": self._sanitize_url(self._url),
            "key_ttl": self._key_ttl,
            "namespace": self._namespace,
        }

    # ------------------------------------------------------------------
    # Helpers interni
    # ------------------------------------------------------------------

    async def _ensure_connected(self) -> None:
        """Garantisce che il client Redis sia inizializzato."""
        if self._client is None:
            await self.initialize()

    def _compute_ttl(self, record: KeyRecord) -> int:
        """
        Calcola il TTL in secondi da assegnare al record in Redis.

        Usa il minore tra:
            - secondi rimanenti a expires_at (se impostato)
            - AUTH_KEY_TTL (ceiling di sicurezza)
        """
        if record.expires_at is None:
            return self._key_ttl

        now = datetime.now(timezone.utc)
        remaining = int((record.expires_at - now).total_seconds())
        return min(remaining, self._key_ttl)

    @staticmethod
    def _sanitize_url(url: str) -> str:
        """Oscura la password nell'URL Redis per i log."""
        try:
            from urllib.parse import urlparse, urlunparse
            parsed = urlparse(url)
            if parsed.password:
                netloc = f"{parsed.username}:***@{parsed.hostname}"
                if parsed.port:
                    netloc += f":{parsed.port}"
                return urlunparse(parsed._replace(netloc=netloc))
        except Exception:
            pass
        return url


# ---------------------------------------------------------------------------
# Helpers interni al modulo (mantenuti per retrocompatibilità)
# ---------------------------------------------------------------------------

def _rkey(key_id: str) -> str:
    """Costruisce la chiave Redis per un KeyRecord col namespace di default: stls:key:{key_id}."""
    return f"{REDIS_KEY_PREFIX}{key_id}"