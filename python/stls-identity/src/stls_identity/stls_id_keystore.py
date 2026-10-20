"""
stls_identity.keystore
========================

Persistent store for the Ed25519 *signing* keypair of an agent's DID.

This is NOT stls_core.providers.ArqitKeyStoreBase, deliberately. That
store is scoped to ephemeral SKA session-key material with a
pending -> delivered -> evicted lifecycle and idle-TTL semantics (GETEX
on every read). Reusing it here would silently expire an agent's
identity if it doesn't handshake for an hour — the opposite of what a
DID signing key needs.

This module follows the same *shape* as stls-core's factory pattern
(ABC + env-selected backend, same naming spirit as
SessionCacheFactory / ArqitKeyStoreFactory) but with none of the TTL
machinery: a signing key is written once at bootstrap and read forever
after, until explicit rotation.

Environment variables:
    IDENTITY_KEYSTORE_BACKEND          memory (default) | redis
    IDENTITY_KEYSTORE_REDIS_URL        redis://localhost:6379
    IDENTITY_KEYSTORE_REDIS_NAMESPACE  stls:identity:signingkey (prefix)

Redis structure (backend=redis):
    {namespace}:{agent_id}  ->  STRING, raw Ed25519 private key bytes
                                 (32 bytes). No TTL — persistent by design.

Production note: for GCP deployments, the natural home for this secret
is Secret Manager (same as other long-lived credentials in the stack),
not Redis. RedisIdentityKeystore is provided for parity with the rest
of stls-core's Redis-backed components and for local/dev use;
swap in a SecretManagerIdentityKeystore before this goes to production
on GCP — the ABC is the extension point for that.
"""

from __future__ import annotations

import base64
import os
from abc import ABC, abstractmethod

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives import serialization


class IdentityKeystoreBase(ABC):
    """Interface for persisting one Ed25519 signing key per agent_id."""

    @abstractmethod
    def get(self, agent_id: str) -> Ed25519PrivateKey | None:
        """Returns the stored key, or None if this agent has never
        bootstrapped an identity yet."""

    @abstractmethod
    def put(self, agent_id: str, private_key: Ed25519PrivateKey) -> None:
        """Persists the key. No TTL, no expiry — overwritten only on
        explicit rotation."""

    def get_or_create(self, agent_id: str) -> tuple[Ed25519PrivateKey, bool]:
        """Convenience used at agent bootstrap: returns (key, created).
        created=True the first time an agent is seen, False on every
        subsequent restart — so did.py can decide whether to also
        (re)publish the DID Document or just serve the existing one."""
        existing = self.get(agent_id)
        if existing is not None:
            return existing, False
        new_key = Ed25519PrivateKey.generate()
        self.put(agent_id, new_key)
        return new_key, True


class InMemoryIdentityKeystore(IdentityKeystoreBase):
    """Dev/test only — does not survive a process restart, same caveat
    as InMemorySessionCache / InMemoryArqitKeyStore."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def get(self, agent_id: str) -> Ed25519PrivateKey | None:
        raw = self._store.get(agent_id)
        if raw is None:
            return None
        return Ed25519PrivateKey.from_private_bytes(raw)

    def put(self, agent_id: str, private_key: Ed25519PrivateKey) -> None:
        self._store[agent_id] = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )


class RedisIdentityKeystore(IdentityKeystoreBase):
    """Persistent, namespace-isolated from both stls:sc (sessions) and
    stls:ks (Arqit ephemeral keys) — no collision even on shared Redis."""

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        namespace: str = "stls:identity:signingkey",
    ) -> None:
        try:
            import redis as redis_lib
        except ImportError as exc:
            raise ImportError(
                "redis>=5.0 is required for IDENTITY_KEYSTORE_BACKEND=redis. "
                "Install with: pip install 'redis>=5.0'"
            ) from exc

        self._r = redis_lib.Redis.from_url(redis_url, decode_responses=False)
        self._ns = namespace

    def _key(self, agent_id: str) -> str:
        return f"{self._ns}:{agent_id}"

    def get(self, agent_id: str) -> Ed25519PrivateKey | None:
        raw = self._r.get(self._key(agent_id))  # plain GET — no TTL, no GETEX
        if raw is None:
            return None
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(raw))

    def put(self, agent_id: str, private_key: Ed25519PrivateKey) -> None:
        raw = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        # plain SET — deliberately no `ex=`: persistent, not a cache entry.
        self._r.set(self._key(agent_id), base64.b64encode(raw))


class IdentityKeystoreFactory:
    """Mirrors SessionCacheFactory.build() / ArqitKeyStoreFactory.build()."""

    @staticmethod
    def build() -> IdentityKeystoreBase:
        backend = os.getenv("IDENTITY_KEYSTORE_BACKEND", "memory").strip().lower()

        if backend == "redis":
            url = os.getenv("IDENTITY_KEYSTORE_REDIS_URL", "redis://localhost:6379")
            ns = os.getenv("IDENTITY_KEYSTORE_REDIS_NAMESPACE", "stls:identity:signingkey")
            return RedisIdentityKeystore(redis_url=url, namespace=ns)

        if backend == "memory":
            return InMemoryIdentityKeystore()

        raise ValueError(
            f"IDENTITY_KEYSTORE_BACKEND='{backend}' not recognized. "
            "Supported values: 'memory' (default), 'redis'. "
            "For GCP production, consider a Secret Manager backend "
            "(not yet implemented — extend IdentityKeystoreBase)."
        )