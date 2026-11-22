"""
stls_identity.credential_cache
================================

Two caches for credentials an agent itself holds — same trust
perimeter reasoning as IdentityKeystoreBase (stls_id_keystore.py):
whoever can already read an agent's private key is already inside the
right perimeter to also read its own credentials, which travel
publicly with every call anyway.

- IdentityCredentialCacheBase: an agent's OWN current Identity
  Credential — a single slot (an agent has at most one at a time).
  Promoted from samples/server/sofia-admin's tools_identity.py
  (cache_own_identity_credential/get_cached_identity_credential) while
  scoping issue #45's follow-up — see load_identity_presenter() in
  stls_id_presentation.py.
- OperationalCredentialCacheBase: Operational Credentials an agent has
  RECEIVED via delegation — a collection (an agent can hold several at
  once, from different Grantors). Promoted the same way while scoping
  issue #54 — see load_delegation_presenter() in stls_id_presentation.py.

Both are now package-level primitives any Requestor can build on, not
just that sample's own chat tools.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from abc import ABC, abstractmethod

from .stls_id_models import NetworkAuthorityCredential, OperationalCredential


class IdentityCredentialCacheBase(ABC):
    """Interface for caching one agent's own current Identity Credential."""

    @abstractmethod
    def get(self, agent_id: str) -> NetworkAuthorityCredential | None:
        """Returns the cached credential, or None if absent/expired."""

    @abstractmethod
    def set(self, agent_id: str, credential: NetworkAuthorityCredential) -> None:
        """Caches the credential with a TTL matching its own validUntil.
        Silently a no-op if the credential has no validUntil or is
        already expired — nothing safe to cache either way."""

    @abstractmethod
    def delete(self, agent_id: str) -> None:
        """Removes whatever is cached for agent_id, if anything — e.g.
        after a revocation, so a stale cached credential doesn't keep
        looking valid (the credential itself never changes; only the
        external status list does, so the cache must be invalidated
        explicitly). A no-op if nothing was cached."""


class InMemoryIdentityCredentialCache(IdentityCredentialCacheBase):
    """Dev/test only — does not survive a process restart, same caveat
    as InMemoryIdentityKeystore."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[NetworkAuthorityCredential, dt.datetime | None]] = {}

    def get(self, agent_id: str) -> NetworkAuthorityCredential | None:
        entry = self._store.get(agent_id)
        if entry is None:
            return None
        credential, expires_at = entry
        if expires_at is not None and dt.datetime.now(dt.timezone.utc) > expires_at:
            del self._store[agent_id]
            return None
        return credential

    def set(self, agent_id: str, credential: NetworkAuthorityCredential) -> None:
        if credential.valid_until is not None and dt.datetime.now(dt.timezone.utc) > credential.valid_until:
            return
        self._store[agent_id] = (credential, credential.valid_until)

    def delete(self, agent_id: str) -> None:
        self._store.pop(agent_id, None)


class RedisIdentityCredentialCache(IdentityCredentialCacheBase):
    """Persistent — namespace isolated from the signing-key keystore
    (stls:identity:signingkey) and from stls-core's own caches. Plain
    sync redis client (not redis.asyncio), matching
    RedisIdentityKeystore's own choice: this is a startup-time/
    non-request-path lookup, not something that needs to share an
    event loop with anything else."""

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        namespace: str = "stls:identity:credential",
    ) -> None:
        try:
            import redis as redis_lib
        except ImportError as exc:
            raise ImportError(
                "redis>=5.0 is required for IDENTITY_CREDENTIAL_CACHE_BACKEND=redis. "
                "Install with: pip install 'redis>=5.0'"
            ) from exc
        self._r = redis_lib.Redis.from_url(redis_url, decode_responses=True)
        self._ns = namespace

    def _key(self, agent_id: str) -> str:
        return f"{self._ns}:{agent_id}"

    def get(self, agent_id: str) -> NetworkAuthorityCredential | None:
        raw = self._r.get(self._key(agent_id))
        if raw is None:
            return None
        return NetworkAuthorityCredential.model_validate(json.loads(raw))

    def set(self, agent_id: str, credential: NetworkAuthorityCredential) -> None:
        if credential.valid_until is None:
            return
        ttl = int((credential.valid_until - dt.datetime.now(dt.timezone.utc)).total_seconds())
        if ttl <= 0:
            return
        payload = credential.model_dump(mode="json", by_alias=True, exclude_none=True)
        self._r.set(self._key(agent_id), json.dumps(payload), ex=ttl)

    def delete(self, agent_id: str) -> None:
        self._r.delete(self._key(agent_id))


class IdentityCredentialCacheFactory:
    """Mirrors IdentityKeystoreFactory.build()."""

    @staticmethod
    def build() -> IdentityCredentialCacheBase:
        backend = os.getenv("IDENTITY_CREDENTIAL_CACHE_BACKEND", "memory").strip().lower()

        if backend == "redis":
            url = (
                os.getenv("IDENTITY_CREDENTIAL_CACHE_REDIS_URL")
                # STATUS_LIST_REDIS_URL fallback: samples/server/sofia-admin
                # already points its credential cache at the same Redis as
                # the status list under this name — recognized here so
                # existing deployments don't need a second URL just to
                # repeat the same value.
                or os.getenv("STATUS_LIST_REDIS_URL")
                or "redis://localhost:6379"
            )
            ns = os.getenv("IDENTITY_CREDENTIAL_CACHE_REDIS_NAMESPACE", "stls:identity:credential")
            return RedisIdentityCredentialCache(redis_url=url, namespace=ns)

        if backend == "memory":
            return InMemoryIdentityCredentialCache()

        raise ValueError(
            f"IDENTITY_CREDENTIAL_CACHE_BACKEND='{backend}' not recognized. "
            "Supported values: 'memory' (default), 'redis'."
        )


class OperationalCredentialCacheBase(ABC):
    """
    Interface for caching the Operational Credentials an agent has
    RECEIVED via delegation (issue #51/#54) — a COLLECTION, not a
    single slot like IdentityCredentialCacheBase above: an agent can
    hold several delegations at once, from different Grantors, for
    different scopes, all simultaneously valid.

    Promoted from samples/server/sofia-admin's tools_identity.py
    (cache_operational_credential/get_cached_operational_credential/
    list_cached_operational_credentials) while scoping issue #54 — same
    storage convention, now a package-level primitive
    load_delegation_presenter() (stls_id_presentation.py) builds on.
    """

    @abstractmethod
    def get(self, agent_id: str, credential_id: str) -> OperationalCredential | None:
        """Returns the cached credential, or None if absent/expired."""

    @abstractmethod
    def set(self, agent_id: str, credential: OperationalCredential) -> None:
        """Caches the credential with a TTL matching its own
        validUntil. Silently a no-op if the credential has no
        validUntil or is already expired — nothing safe to cache
        either way."""

    @abstractmethod
    def list_ids(self, agent_id: str) -> list[str]:
        """Every currently-valid (not expired) cached credential id
        for this agent. Self-cleaning: expired entries are dropped
        from the index as a side effect of calling this."""

    @abstractmethod
    def delete(self, agent_id: str, credential_id: str) -> None:
        """Removes one specific cached credential, if present — e.g.
        after it's been revoked or consumed. A no-op if not cached."""


class InMemoryOperationalCredentialCache(OperationalCredentialCacheBase):
    """Dev/test only — does not survive a process restart, same caveat
    as every other InMemory* store in this package."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, tuple[OperationalCredential, dt.datetime | None]]] = {}

    def get(self, agent_id: str, credential_id: str) -> OperationalCredential | None:
        bucket = self._store.get(agent_id, {})
        entry = bucket.get(credential_id)
        if entry is None:
            return None
        credential, expires_at = entry
        if expires_at is not None and dt.datetime.now(dt.timezone.utc) > expires_at:
            del bucket[credential_id]
            return None
        return credential

    def set(self, agent_id: str, credential: OperationalCredential) -> None:
        if credential.valid_until is None or dt.datetime.now(dt.timezone.utc) > credential.valid_until:
            return
        self._store.setdefault(agent_id, {})[credential.id] = (credential, credential.valid_until)

    def list_ids(self, agent_id: str) -> list[str]:
        now = dt.datetime.now(dt.timezone.utc)
        bucket = self._store.get(agent_id, {})
        expired = [cid for cid, (_, expires_at) in bucket.items() if expires_at is not None and now > expires_at]
        for cid in expired:
            del bucket[cid]
        return sorted(bucket.keys())

    def delete(self, agent_id: str, credential_id: str) -> None:
        self._store.get(agent_id, {}).pop(credential_id, None)


class RedisOperationalCredentialCache(OperationalCredentialCacheBase):
    """Persistent — same sync-client choice as RedisIdentityCredentialCache
    (startup-time/tool-call lookup, not a hot request path shared with
    an event loop). The index is a sorted set (score = validUntil
    epoch), same self-cleaning pattern StatusListManager-adjacent code
    already uses elsewhere in this codebase."""

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        namespace: str = "stls:identity:opcred",
    ) -> None:
        try:
            import redis as redis_lib
        except ImportError as exc:
            raise ImportError(
                "redis>=5.0 is required for OPERATIONAL_CREDENTIAL_CACHE_BACKEND=redis. "
                "Install with: pip install 'redis>=5.0'"
            ) from exc
        self._r = redis_lib.Redis.from_url(redis_url, decode_responses=True)
        self._ns = namespace

    def _key(self, agent_id: str, credential_id: str) -> str:
        return f"{self._ns}:{agent_id}:{credential_id}"

    def _index_key(self, agent_id: str) -> str:
        return f"{self._ns}_index:{agent_id}"

    def get(self, agent_id: str, credential_id: str) -> OperationalCredential | None:
        raw = self._r.get(self._key(agent_id, credential_id))
        if raw is None:
            return None
        return OperationalCredential.model_validate(json.loads(raw))

    def set(self, agent_id: str, credential: OperationalCredential) -> None:
        if credential.valid_until is None:
            return
        ttl = int((credential.valid_until - dt.datetime.now(dt.timezone.utc)).total_seconds())
        if ttl <= 0:
            return
        payload = credential.model_dump(mode="json", by_alias=True, exclude_none=True)
        self._r.set(self._key(agent_id, credential.id), json.dumps(payload), ex=ttl)
        self._r.zadd(self._index_key(agent_id), {credential.id: credential.valid_until.timestamp()})

    def list_ids(self, agent_id: str) -> list[str]:
        index_key = self._index_key(agent_id)
        now_epoch = dt.datetime.now(dt.timezone.utc).timestamp()
        self._r.zremrangebyscore(index_key, "-inf", now_epoch)
        return list(self._r.zrange(index_key, 0, -1))

    def delete(self, agent_id: str, credential_id: str) -> None:
        self._r.delete(self._key(agent_id, credential_id))
        self._r.zrem(self._index_key(agent_id), credential_id)


class OperationalCredentialCacheFactory:
    """Mirrors IdentityCredentialCacheFactory.build()."""

    @staticmethod
    def build() -> OperationalCredentialCacheBase:
        backend = os.getenv("OPERATIONAL_CREDENTIAL_CACHE_BACKEND", "memory").strip().lower()

        if backend == "redis":
            url = (
                os.getenv("OPERATIONAL_CREDENTIAL_CACHE_REDIS_URL")
                # STATUS_LIST_REDIS_URL fallback, same reasoning as
                # IdentityCredentialCacheFactory's own: existing
                # deployments already point every identity-adjacent
                # cache at the same Redis, no second URL needed just to
                # repeat the same value.
                or os.getenv("STATUS_LIST_REDIS_URL")
                or "redis://localhost:6379"
            )
            ns = os.getenv("OPERATIONAL_CREDENTIAL_CACHE_REDIS_NAMESPACE", "stls:identity:opcred")
            return RedisOperationalCredentialCache(redis_url=url, namespace=ns)

        if backend == "memory":
            return InMemoryOperationalCredentialCache()

        raise ValueError(
            f"OPERATIONAL_CREDENTIAL_CACHE_BACKEND='{backend}' not recognized. "
            "Supported values: 'memory' (default), 'redis'."
        )
