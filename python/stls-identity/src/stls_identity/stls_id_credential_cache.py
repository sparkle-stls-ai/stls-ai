"""
stls_identity.credential_cache
================================

Cache for an agent's OWN current Identity Credential — same trust
perimeter reasoning as IdentityKeystoreBase (stls_id_keystore.py):
whoever can already read an agent's private key is already inside the
right perimeter to also read its credential, which travels publicly
with every call anyway. A separate class because the two have
different lifecycles: the signing key is persistent with no TTL, the
credential self-expires (TTL = its own validUntil, no manual cleanup).

Promoted from samples/server/sofia-admin's tools_identity.py
(cache_own_identity_credential/get_cached_identity_credential) while
scoping issue #45's follow-up — same storage convention, now a
package-level primitive any Requestor can build on (see
load_identity_presenter() in stls_id_presentation.py), not just that
sample's own chat tools.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from abc import ABC, abstractmethod

from .stls_id_models import NetworkAuthorityCredential


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
