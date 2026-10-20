"""
stls_identity.trust_registry_client
=======================================

TrustAnchorRegistryClient — what Sentinel actually calls (indirectly) to
answer "who are the recognized Governing Authorities, and what are
their current public keys". Step 3 of the pipeline ("resolve the
issuer's key") for the Identity Credential case.

Design constraint carried over from the whole discussion: this cache is
touched rarely and is small (tens of entries), NOT per-request — the
whole point of the "Chi Possiede Cosa" split is that revocation state
(large, per-issuer, changes often) never lives here, only Authority
identity (small, shared, changes rarely).

Deliberately does NOT change Sentinel's constructor signature.
Sentinel already takes `trusted_issuer_keys: dict[str, Ed25519PublicKey]`
— this client's job is to PRODUCE that dict (via as_trusted_keys()),
refreshed periodically, not to make Sentinel aware a registry exists at
all. Same separation of concerns as StatusListManager not knowing
anything about HTTP.
"""

from __future__ import annotations

import datetime as dt
import logging

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .stls_id_did import decode_multibase_ed25519_public_key
from .stls_id_models import TrustAnchorEntry
from .stls_id_trust_registry_store import TrustAnchorRegistryStoreBase

logger = logging.getLogger(__name__)

TRUST_ANCHOR_CACHE_TTL_SECONDS = 3600
"""How long a cached registry snapshot may be served before a refresh is
required. Deliberately much longer than the 5-minute MRI used for
credential revocation (MRI_SECONDS, status.py) — recognizing a NEW
Authority or offboarding one is a rare, deliberate governance action
(Opzione A/B/C onboarding), not a per-credential event. An hour is a
starting point, not a spec requirement — tune per deployment."""


class TrustAnchorRegistryStaleError(Exception):
    """Raised when resolve()/as_trusted_keys() is asked to serve a cache
    older than TRUST_ANCHOR_CACHE_TTL_SECONDS and no refresh has
    happened. Same fail-closed posture as verifier.py's MRI check —
    stale trust data is worse than no data, because it can silently
    keep trusting an Authority that was just offboarded."""


class TrustAnchorRegistryClient:
    """
    One instance per verifying platform (Sparkle's Sentinel-side
    process). Wraps a TrustAnchorRegistryStoreBase — direct Postgres
    access for now, since Sparkle IS the Option A anchor operator
    hosting its own store. When an external operator (B, C) needs to
    resolve Sparkle's registry over HTTP instead of direct DB access,
    swap the store for an HTTP-fetching implementation of the same
    TrustAnchorRegistryStoreBase ABC — this class does not change.
    """

    def __init__(self, store: TrustAnchorRegistryStoreBase):
        self._store = store
        self._cache: dict[str, Ed25519PublicKey] = {}
        self._last_refresh: dt.datetime | None = None

    async def refresh(self) -> int:
        """Pulls every active (non-revoked) entry from the store and
        rebuilds the cache from scratch — a revoked Authority silently
        disappears from the next refresh, no separate "remove" step
        needed. Returns the number of entries now cached.

        Call this from a periodic task (same spirit as
        BackgroundRefresher in stls-core), not per-request."""
        entries = await self._store.list_active()
        new_cache: dict[str, Ed25519PublicKey] = {}
        for entry in entries:
            new_cache[entry.verification_method_id] = self._decode_public_key(entry)

        self._cache = new_cache
        self._last_refresh = dt.datetime.now(dt.timezone.utc)
        logger.info("TrustAnchorRegistryClient.refresh: %d authorities cached", len(new_cache))
        return len(new_cache)

    def as_trusted_keys(self) -> dict[str, Ed25519PublicKey]:
        """Returns the cache in exactly the shape Sentinel's constructor
        expects: `Sentinel(trusted_issuer_keys=client.as_trusted_keys())`.

        Raises:
            TrustAnchorRegistryStaleError: refresh() has never run, or
                the cache is older than TRUST_ANCHOR_CACHE_TTL_SECONDS.
        """
        self._assert_fresh()
        return dict(self._cache)  # copy — caller must not mutate our cache

    def resolve(self, verification_method_id: str) -> Ed25519PublicKey | None:
        """Single-key lookup, same freshness guarantee as as_trusted_keys()."""
        self._assert_fresh()
        return self._cache.get(verification_method_id)

    def _assert_fresh(self) -> None:
        if self._last_refresh is None:
            raise TrustAnchorRegistryStaleError(
                "refresh() has never been called — no trust anchor data available"
            )
        age = dt.datetime.now(dt.timezone.utc) - self._last_refresh
        if age.total_seconds() > TRUST_ANCHOR_CACHE_TTL_SECONDS:
            raise TrustAnchorRegistryStaleError(
                f"trust anchor cache stale ({age.total_seconds():.0f}s > "
                f"{TRUST_ANCHOR_CACHE_TTL_SECONDS}s) — fail closed, call refresh()"
            )

    async def resolve_at(
        self, authority_id: str, verification_method_id: str, at: dt.datetime,
    ) -> Ed25519PublicKey | None:
        """
        Resolves a key that may no longer be current — was this
        verification_method_id the trusted key for this Authority at
        time `at`? Goes straight to the store (NOT the cache: this is
        for the rare/investigative case of verifying an old credential
        against a since-rotated key, not the per-request hot path that
        as_trusted_keys()/resolve() serve).

        Callers should pass the credential's OWN signing time
        (CredentialProof.created), not "now" — verifying a fresh
        credential should still go through as_trusted_keys()/resolve(),
        which enforce the cache freshness guarantee this method
        deliberately does not.

        Prefer resolve_historical() if you don't already have the
        authority_id on hand — Sentinel's fallback path uses that one,
        since a credential's `proof` carries a verification_method, never
        the registry's own authority_id.
        """
        entry = await self._store.get_key_valid_at(authority_id, verification_method_id, at)
        if entry is None:
            return None
        return self._decode_public_key(entry)

    async def resolve_historical(
        self, verification_method_id: str, at: dt.datetime,
    ) -> Ed25519PublicKey | None:
        """
        Same as resolve_at(), but keyed by verification_method_id alone
        — what Sentinel.verify_signature() actually calls when a key
        isn't in the live cache (as_trusted_keys()) and might simply be
        a since-rotated Authority key rather than genuinely unknown.

        Also bypasses the cache and its freshness guarantee, for the
        same reason as resolve_at() — this is the deliberately-slower,
        rare fallback path, not the hot path.
        """
        entry = await self._store.get_key_valid_at_by_verification_method(verification_method_id, at)
        if entry is None:
            return None
        return self._decode_public_key(entry)

    @staticmethod
    def _decode_public_key(entry: TrustAnchorEntry) -> Ed25519PublicKey:
        return decode_multibase_ed25519_public_key(entry.public_key_multibase)