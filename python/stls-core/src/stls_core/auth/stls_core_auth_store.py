"""
stls_core_auth_store.py
----------------------
Redis cache for STLS API keys.

Redis key schema (configurable namespace, default "stls"):
    {ns}:key:{key_id}   → JSON(KeyRecord), TTL = expires_at or AUTH_KEY_TTL
    {ns}:revoked        → SET of revoked key_ids (O(1) lookup)
    {ns}:keys:index     → SET of all known key_ids (for bulk ops)

The namespace lets multiple ApiKeyManagers on the same Redis avoid
colliding: each loader (static, file, postgres) uses its own
namespace, so that one loader's bulk_reload doesn't wipe out another's
keys.

Compatible with:
    - GCP Memorystore (Redis 7+)
    - AWS ElastiCache standalone
    - Azure Cache for Redis (TLS via rediss://)
    - local Redis for development

Dependency: redis>=7.4.0  (built-in async client, no redis[asyncio] needed)
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

# Default namespace (backward compatible with earlier versions)
_DEFAULT_NAMESPACE = "stls"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RedisKeyCache
# ---------------------------------------------------------------------------

class RedisKeyCache:
    """
    Primary cache for KeyRecords, backed by Redis.

    Lifecycle:
        cache = RedisKeyCache()
        await cache.initialize()   # creates the Redis client
        ...
        await cache.close()        # closes the connection

    Or as an async context manager:
        async with RedisKeyCache() as cache:
            record = await cache.get("abc123...")

    All methods are safe for concurrent (asyncio) use.
    Bulk operations use Redis pipelines to minimize round-trips.
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
            url:              Redis URL. Default: env REDIS_URL, then "redis://localhost:6379".
                              For TLS (Azure, etc.): "rediss://..."
            key_ttl:          TTL in seconds for KeyRecords with no expires_at.
                              Default: env AUTH_KEY_TTL, then DEFAULT_KEY_TTL_SECONDS (3600).
            decode_responses: Automatic bytes→str decoding. Default True.
            namespace:        Prefix for the Redis keys. Default: env AUTH_REDIS_NS or "stls".
                              Use different namespaces to isolate distinct loaders on the same
                              Redis (e.g. "stls:static", "stls:file", "stls:pg").
                              Multiple instances of the same app must share the same
                              namespace to benefit from the distributed cache.
        """
        self._url = url or os.environ.get("REDIS_URL", "redis://localhost:6379")
        self._key_ttl = key_ttl or int(
            os.environ.get("AUTH_KEY_TTL", DEFAULT_KEY_TTL_SECONDS)
        )
        self._decode = decode_responses
        self._client: Any = None  # redis.asyncio.Redis, lazy init

        # Namespace: isolates this loader's keys from the others on the same Redis.
        # Priority: explicit parameter > AUTH_REDIS_NS env > "stls"
        ns = namespace or os.environ.get("AUTH_REDIS_NS") or _DEFAULT_NAMESPACE
        self._namespace   = ns
        self._key_prefix  = f"{ns}:key:"        # {ns}:key:{key_id}
        self._revoked_set = f"{ns}:revoked"     # SET of revoked key_ids
        self._index_set   = f"{ns}:keys:index"  # SET of all known key_ids

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Creates the Redis client. Call before any other operation."""
        if self._client is not None:
            return
        try:
            from redis.asyncio import Redis  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "RedisKeyCache requires redis>=7.4.0. "
                "Install it with: uv add 'redis>=7.4.0'"
            ) from exc

        self._client = Redis.from_url(
            self._url,
            decode_responses=self._decode,
            socket_timeout=5,
            socket_connect_timeout=5,
            retry_on_timeout=True,
        )
        # verifies the connection on first use
        await self._client.ping()
        logger.info("RedisKeyCache: connected to %s", self._sanitize_url(self._url))

    async def close(self) -> None:
        """Closes the Redis connection."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("RedisKeyCache: connection closed")

    async def __aenter__(self) -> "RedisKeyCache":
        await self.initialize()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Single-record operations
    # ------------------------------------------------------------------

    async def get(self, key_id: str) -> KeyRecord | None:
        """
        Retrieves a KeyRecord from the cache.

        Returns:
            KeyRecord if present and not expired (Redis handles the TTL),
            None if absent.
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
        Writes a KeyRecord to the cache with the appropriate TTL.

        TTL computed as:
            - If record.expires_at is set: remaining seconds until expiry.
            - Otherwise: AUTH_KEY_TTL (default 3600s).
        In both cases capped at AUTH_KEY_TTL, to limit the validity
        window of keys with a far-off expiry.
        """
        await self._ensure_connected()
        ttl = self._compute_ttl(record)
        if ttl <= 0:
            # key already expired: don't write it to the cache
            logger.debug("RedisKeyCache.set: key %s... already expired, skip", record.key_id[:12])
            return
        try:
            pipe = self._client.pipeline()
            pipe.setex(f"{self._key_prefix}{record.key_id}", ttl, json.dumps(record.to_dict()))
            pipe.sadd(self._index_set, record.key_id)
            await pipe.execute()
        except Exception as exc:
            logger.error("RedisKeyCache.set(%s...): %s", record.key_id[:12], exc)

    async def delete(self, key_id: str) -> None:
        """Removes a KeyRecord from the cache (without revoking it)."""
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
        Revokes an API key.

        Adds the key_id to the {ns}:revoked SET AND removes its cache
        entry. The SET has no TTL: revocations are permanent until
        explicitly cleared.
        """
        await self._ensure_connected()
        try:
            pipe = self._client.pipeline()
            pipe.sadd(self._revoked_set, key_id)
            pipe.delete(f"{self._key_prefix}{key_id}")
            pipe.srem(self._index_set, key_id)
            await pipe.execute()
            logger.info("RedisKeyCache: revoked key %s...", key_id[:12])
        except Exception as exc:
            logger.error("RedisKeyCache.revoke(%s...): %s", key_id[:12], exc)

    async def is_revoked(self, key_id: str) -> bool:
        """
        Checks whether a key_id is in the revocation SET.

        O(1) operation. Called as the first check in
        ApiKeyManager.validate(), before even consulting the record cache.
        """
        await self._ensure_connected()
        try:
            return bool(await self._client.sismember(self._revoked_set, key_id))
        except Exception as exc:
            logger.error("RedisKeyCache.is_revoked(%s...): %s", key_id[:12], exc)
            # fail-open: on a Redis error we don't block the service
            return False

    async def unrevoke(self, key_id: str) -> None:
        """
        Removes a key_id from the revocation SET (an emergency operation).

        Does not restore the cache entry: the record will be reloaded
        from the loader on the next access.
        """
        await self._ensure_connected()
        try:
            await self._client.srem(self._revoked_set, key_id)
            logger.info("RedisKeyCache: revocation removed for %s...", key_id[:12])
        except Exception as exc:
            logger.error("RedisKeyCache.unrevoke(%s...): %s", key_id[:12], exc)

    # ------------------------------------------------------------------
    # Bulk operations (used by the BackgroundRefresher)
    # ------------------------------------------------------------------

    async def bulk_reload(self, records: list[KeyRecord]) -> int:
        """
        Atomically replaces all KeyRecords in the cache for the current namespace.

        Algorithm:
            1. Retrieves the current index ({ns}:keys:index).
            2. Writes all the new records in a pipeline.
            3. Removes records no longer present in the new set.
            4. Updates the index.

        Never touches the revocation SET nor other namespaces' keys.

        Returns:
            Number of records written.
        """
        await self._ensure_connected()
        if not records:
            logger.warning("RedisKeyCache.bulk_reload: empty list, operation aborted")
            return 0

        new_ids = {r.key_id for r in records}

        try:
            # 1. current index (only for the current namespace)
            old_ids: set[str] = await self._client.smembers(self._index_set) or set()

            # 2. write all the new records in a pipeline
            pipe = self._client.pipeline()
            for record in records:
                ttl = self._compute_ttl(record)
                if ttl <= 0:
                    continue
                pipe.setex(f"{self._key_prefix}{record.key_id}", ttl, json.dumps(record.to_dict()))
                pipe.sadd(self._index_set, record.key_id)

            # 3. remove stale records (present before, absent now)
            stale_ids = old_ids - new_ids
            for stale_id in stale_ids:
                pipe.delete(f"{self._key_prefix}{stale_id}")
                pipe.srem(self._index_set, stale_id)

            await pipe.execute()

            if stale_ids:
                logger.info(
                    "RedisKeyCache[%s].bulk_reload: removed %d stale records",
                    self._namespace, len(stale_ids),
                )
            logger.info(
                "RedisKeyCache[%s].bulk_reload: wrote %d records",
                self._namespace, len(records),
            )
            return len(records)

        except Exception as exc:
            logger.error("RedisKeyCache[%s].bulk_reload(): %s", self._namespace, exc)
            return 0

    async def list_all_ids(self) -> set[str]:
        """Returns all key_ids in the current namespace's index (for diagnostics/admin)."""
        await self._ensure_connected()
        try:
            return await self._client.smembers(self._index_set) or set()
        except Exception as exc:
            logger.error("RedisKeyCache.list_all_ids(): %s", exc)
            return set()

    async def list_revoked_ids(self) -> set[str]:
        """Returns all revoked key_ids in the current namespace (for diagnostics/admin)."""
        await self._ensure_connected()
        try:
            return await self._client.smembers(self._revoked_set) or set()
        except Exception as exc:
            logger.error("RedisKeyCache.list_revoked_ids(): %s", exc)
            return set()

    async def flush_all_keys(self) -> None:
        """
        Removes all KeyRecords and the index from the current namespace's cache.

        Does NOT touch the revocation SET nor other namespaces' keys.
        Useful to force a full reload from the loader.
        """
        await self._ensure_connected()
        try:
            ids = await self._client.smembers(self._index_set) or set()
            pipe = self._client.pipeline()
            for kid in ids:
                pipe.delete(f"{self._key_prefix}{kid}")
            pipe.delete(self._index_set)
            await pipe.execute()
            logger.info("RedisKeyCache[%s].flush_all_keys: removed %d records", self._namespace, len(ids))
        except Exception as exc:
            logger.error("RedisKeyCache.flush_all_keys(): %s", exc)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    async def ping(self) -> bool:
        """Health check: True if Redis responds."""
        try:
            await self._ensure_connected()
            return await self._client.ping()
        except Exception:
            return False

    async def stats(self) -> dict[str, Any]:
        """
        Summary cache statistics.

        Returns a dict with:
            cached_keys:   number of records cached in the current namespace
            revoked_keys:  number of revoked key_ids in the current namespace
            redis_url:     sanitized URL (password redacted)
            key_ttl:       configured default TTL
            namespace:     Redis namespace used by this instance
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
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_connected(self) -> None:
        """Ensures the Redis client is initialized."""
        if self._client is None:
            await self.initialize()

    def _compute_ttl(self, record: KeyRecord) -> int:
        """
        Computes the TTL in seconds to assign to the record in Redis.

        Uses the smaller of:
            - the seconds remaining until expires_at (if set)
            - AUTH_KEY_TTL (a safety ceiling)
        """
        if record.expires_at is None:
            return self._key_ttl

        now = datetime.now(timezone.utc)
        remaining = int((record.expires_at - now).total_seconds())
        return min(remaining, self._key_ttl)

    @staticmethod
    def _sanitize_url(url: str) -> str:
        """Redacts the password in the Redis URL for logging."""
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
# Module-internal helpers (kept for backward compatibility)
# ---------------------------------------------------------------------------

def _rkey(key_id: str) -> str:
    """Builds the Redis key for a KeyRecord under the default namespace: stls:key:{key_id}."""
    return f"{REDIS_KEY_PREFIX}{key_id}"
