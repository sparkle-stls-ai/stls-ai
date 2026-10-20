"""
stls_identity.trust_registry_store
=====================================

Postgres-backed storage for the Trust Anchor Registry — Governance
Option A from the design review: a centralized registry operated by one
anchor member (Sparkle), fastest to stand up for a pilot. Options B
(quorum signatures) and C (transparency log) are future governance
layers *on top of* this same data shape, not a different schema — this
module doesn't need to change if the federation later migrates to B/C.

Key history (added after the Fase 2 review): an Authority can have
MULTIPLE rows over its lifetime, one per key rotation. The primary key
is therefore (authority_id, verification_method_id), not authority_id
alone. Exactly one row per authority_id should have valid_until IS NULL
at any time — that's the "current" key. Older rows are kept, not
deleted, specifically so a credential signed before a rotation still
resolves against the key that was actually valid when it was signed.

Same conventions as postgres_store.py deliberately: lazy pool init,
env-var-with-override DSN/table name, get_ddl()/table as the
env-override-safe accessors (see the regression this pattern fixed:
test_table_property_and_ddl_respect_env_override in test_postgres_store.py).

Environment variables:
    IDENTITY_PG_DSN                 (shared with postgres_store.py — same DB)
    TRUST_REGISTRY_PG_TABLE         default: sf_identity_trust_anchors
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .stls_id_did import _multibase_encode_ed25519_pub
from .stls_id_models import TrustAnchorEntry

logger = logging.getLogger(__name__)


def build_trust_anchor_entry(
    *,
    authority_id: str,
    did: str,
    verification_method_id: str,
    public_key: Ed25519PublicKey,
    onboarded_at: dt.datetime | None = None,
) -> TrustAnchorEntry:
    """
    Onboarding/rotation helper: builds a TrustAnchorEntry from an
    Authority's Ed25519 public key — the administrative counterpart to
    TrustAnchorRegistryClient's read path. Not itself a network call;
    the caller still has to persist the result via
    TrustAnchorRegistryStoreBase.put() (Option A: a deliberate,
    out-of-band governance action, not something a credential flow
    triggers automatically). Used identically for the FIRST onboarding
    of an Authority and for every later key rotation — put() tells them
    apart, this helper doesn't need to.
    """
    raw_pub = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return TrustAnchorEntry(
        authority_id=authority_id,
        did=did,
        verification_method_id=verification_method_id,
        public_key_multibase=_multibase_encode_ed25519_pub(raw_pub),
        onboarded_at=onboarded_at or dt.datetime.now(dt.timezone.utc),
    )


class TrustAnchorRegistryStoreBase(ABC):
    @abstractmethod
    async def get(self, authority_id: str) -> TrustAnchorEntry | None:
        """The CURRENT key generation for this Authority (valid_until IS
        NULL) — same contract as before key history was added, except it
        no longer hides a revoked-but-current entry (check
        entry.revoked_at yourself, or use list_active() for "currently
        trustworthy")."""

    @abstractmethod
    async def get_key_valid_at(
        self, authority_id: str, verification_method_id: str, at: dt.datetime,
    ) -> TrustAnchorEntry | None:
        """The entry for this specific key, IF it was the trusted key
        for this Authority at time `at` (onboarded_at <= at <
        valid_until-or-now) AND not revoked before `at`. This is what
        makes an old credential, signed with a since-rotated key, still
        verifiable — resolve using the credential's own signing time,
        not "now"."""

    @abstractmethod
    async def get_key_valid_at_by_verification_method(
        self, verification_method_id: str, at: dt.datetime,
    ) -> TrustAnchorEntry | None:
        """Same lookup as get_key_valid_at(), but keyed by
        verification_method_id alone — no authority_id required. This
        is the method Sentinel actually needs: a credential's `proof`
        carries a verification_method (a DID fragment), never the
        registry's short authority_id, so requiring the latter would
        make Sentinel's fallback path unusable without an extra,
        unnecessary DID->authority_id lookup of its own."""

    @abstractmethod
    async def put(self, entry: TrustAnchorEntry) -> None:
        """Upserts this key generation AND, if a different key was
        previously current for the same authority_id, closes it
        (valid_until = entry.onboarded_at) atomically. Works identically
        for first-time onboarding (nothing to close) and rotation
        (exactly one prior row to close)."""

    @abstractmethod
    async def list_active(self) -> list[TrustAnchorEntry]:
        """Every CURRENT entry (valid_until IS NULL, revoked_at IS
        NULL) — one per non-offboarded Authority. What
        TrustAnchorRegistryClient.refresh() pulls to build its live
        cache."""

    @abstractmethod
    async def get_history(self, authority_id: str) -> list[TrustAnchorEntry]:
        """Every key generation this Authority has ever had, oldest
        first — audit trail, not used by the live verification path."""

    @abstractmethod
    async def revoke_key(self, authority_id: str, verification_method_id: str, revoked_at: dt.datetime) -> None:
        """Emergency revocation of ONE specific key generation (e.g.
        suspected compromise) — stronger than rotation: get_key_valid_at()
        must refuse this key even for timestamps before revoked_at that
        would otherwise be inside its validity window."""

    @abstractmethod
    async def revoke_authority(self, authority_id: str, revoked_at: dt.datetime) -> None:
        """Offboards the Authority entirely — every key generation,
        past and current, stops being trusted for anything from
        revoked_at onward. Does NOT delete any row (audit)."""

    @abstractmethod
    async def close(self) -> None: ...


class PostgresTrustAnchorRegistryStore(TrustAnchorRegistryStoreBase):
    DEFAULT_TABLE = "sf_identity_trust_anchors"

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
        self._table = table or os.environ.get("TRUST_REGISTRY_PG_TABLE", self.DEFAULT_TABLE)

    @property
    def table(self) -> str:
        """Actual table name in use — respects TRUST_REGISTRY_PG_TABLE.
        Use this, never DEFAULT_TABLE, for any external query/assertion."""
        return self._table

    def get_ddl(self) -> str:
        """DDL on this instance's ACTUAL table name — see
        postgres_store.py's get_ddl() docstring for why the module-level
        shortcut would be wrong if the table name is overridden.

        Primary key is (authority_id, verification_method_id): one row
        per key generation, not one row per Authority — that's the
        schema change that makes key history possible."""
        return f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                authority_id           TEXT        NOT NULL,
                verification_method_id TEXT        NOT NULL,
                did                    TEXT        NOT NULL,
                document               JSONB       NOT NULL,
                onboarded_at           TIMESTAMPTZ NOT NULL,
                valid_until            TIMESTAMPTZ,
                revoked_at             TIMESTAMPTZ,
                PRIMARY KEY (authority_id, verification_method_id)
            );
            CREATE INDEX IF NOT EXISTS {self._table}_current_idx
                ON {self._table} (authority_id) WHERE valid_until IS NULL;
        """

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        try:
            import asyncpg  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "PostgresTrustAnchorRegistryStore requires asyncpg. "
                "Install with: uv add asyncpg"
            ) from exc

        if not self._dsn:
            raise RuntimeError("PostgresTrustAnchorRegistryStore: IDENTITY_PG_DSN not configured.")

        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=self._min_pool, max_size=self._max_pool, command_timeout=10,
        )
        logger.info(
            "PostgresTrustAnchorRegistryStore: pool created (table=%s)", self._table,
        )
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def get(self, authority_id: str) -> TrustAnchorEntry | None:
        """The CURRENT key generation for this Authority (valid_until IS
        NULL) — regardless of whether it has since been revoked. Callers
        that need "is this Authority currently trustworthy" should check
        entry.revoked_at themselves (see bridge.py's get_anchor()), or
        use list_active() which already excludes revoked entries."""
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT document FROM {self._table} WHERE authority_id = $1 AND valid_until IS NULL",
                    authority_id,
                )
            if row is None:
                return None
            return self._row_to_entry(row["document"])
        except Exception as exc:
            logger.error("PostgresTrustAnchorRegistryStore.get(%s) failed: %s", authority_id, exc)
            return None

    async def get_key_valid_at(
        self, authority_id: str, verification_method_id: str, at: dt.datetime,
    ) -> TrustAnchorEntry | None:
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"""SELECT document FROM {self._table}
                        WHERE authority_id = $1 AND verification_method_id = $2
                          AND onboarded_at <= $3
                          AND (valid_until IS NULL OR valid_until > $3)
                          AND (revoked_at IS NULL OR revoked_at > $3)""",
                    authority_id, verification_method_id, at,
                )
            if row is None:
                return None
            return self._row_to_entry(row["document"])
        except Exception as exc:
            logger.error(
                "PostgresTrustAnchorRegistryStore.get_key_valid_at(%s, %s, %s) failed: %s",
                authority_id, verification_method_id, at, exc,
            )
            return None

    async def get_key_valid_at_by_verification_method(
        self, verification_method_id: str, at: dt.datetime,
    ) -> TrustAnchorEntry | None:
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"""SELECT document FROM {self._table}
                        WHERE verification_method_id = $1
                          AND onboarded_at <= $2
                          AND (valid_until IS NULL OR valid_until > $2)
                          AND (revoked_at IS NULL OR revoked_at > $2)""",
                    verification_method_id, at,
                )
            if row is None:
                return None
            return self._row_to_entry(row["document"])
        except Exception as exc:
            logger.error(
                "PostgresTrustAnchorRegistryStore.get_key_valid_at_by_verification_method(%s, %s) failed: %s",
                verification_method_id, at, exc,
            )
            return None

    async def put(self, entry: TrustAnchorEntry) -> None:
        """
        Read-modify-write for the row being closed (same lesson as
        revoke_key(): document and columns must move together, in the
        same statement, or they silently desync — caught here by
        test_rotation_closes_previous_key_and_get_returns_the_new_one
        against a real database: the column updated correctly, but
        get_history() reads `document`, which still showed the old
        (unclosed) state until this fix.
        """
        payload = entry.model_dump(mode="json", by_alias=True, exclude_none=True)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                previous_row = await conn.fetchrow(
                    f"""SELECT document FROM {self._table}
                        WHERE authority_id = $1 AND valid_until IS NULL
                          AND verification_method_id != $2""",
                    entry.authority_id, entry.verification_method_id,
                )
                if previous_row is not None:
                    previous_entry = self._row_to_entry(previous_row["document"])
                    closed = previous_entry.model_copy(update={"valid_until": entry.onboarded_at})
                    closed_payload = closed.model_dump(mode="json", by_alias=True, exclude_none=True)
                    await conn.execute(
                        f"""UPDATE {self._table} SET document = $3::jsonb, valid_until = $4
                            WHERE authority_id = $1 AND verification_method_id = $2""",
                        entry.authority_id, previous_entry.verification_method_id,
                        json.dumps(closed_payload), entry.onboarded_at,
                    )

                await conn.execute(
                    f"""
                    INSERT INTO {self._table}
                        (authority_id, verification_method_id, did, document,
                         onboarded_at, valid_until, revoked_at)
                    VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
                    ON CONFLICT (authority_id, verification_method_id) DO UPDATE
                        SET did = EXCLUDED.did,
                            document = EXCLUDED.document,
                            valid_until = EXCLUDED.valid_until,
                            revoked_at = EXCLUDED.revoked_at
                    """,
                    entry.authority_id, entry.verification_method_id, entry.did,
                    json.dumps(payload), entry.onboarded_at, entry.valid_until, entry.revoked_at,
                )
        logger.info(
            "PostgresTrustAnchorRegistryStore: upsert authority_id=%s verification_method_id=%s",
            entry.authority_id, entry.verification_method_id,
        )

    async def list_active(self) -> list[TrustAnchorEntry]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT document FROM {self._table} WHERE valid_until IS NULL AND revoked_at IS NULL"
            )
        return [self._row_to_entry(r["document"]) for r in rows]

    async def get_history(self, authority_id: str) -> list[TrustAnchorEntry]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT document FROM {self._table} WHERE authority_id = $1 ORDER BY onboarded_at ASC",
                authority_id,
            )
        return [self._row_to_entry(r["document"]) for r in rows]

    async def revoke_key(self, authority_id: str, verification_method_id: str, revoked_at: dt.datetime) -> None:
        """Read-modify-write, same lesson as the original single-key-per-
        authority revoke() bug this replaced: document (JSONB) and the
        revoked_at column must be updated together in the SAME
        statement, with revoked_at passed as a real timestamptz
        parameter — not stuffed into the JSONB via a text cast, which
        mismatches the column's own type and is exactly the kind of
        divergence that made the old revoke() silently not work."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT document FROM {self._table} WHERE authority_id = $1 AND verification_method_id = $2",
                authority_id, verification_method_id,
            )
            if row is None:
                logger.warning(
                    "PostgresTrustAnchorRegistryStore.revoke_key: no row for "
                    "authority_id=%s verification_method_id=%s",
                    authority_id, verification_method_id,
                )
                return
            entry = self._row_to_entry(row["document"])
            updated = entry.model_copy(update={"revoked_at": revoked_at})
            payload = updated.model_dump(mode="json", by_alias=True, exclude_none=True)
            await conn.execute(
                f"""UPDATE {self._table} SET document = $3::jsonb, revoked_at = $4
                    WHERE authority_id = $1 AND verification_method_id = $2""",
                authority_id, verification_method_id, json.dumps(payload), revoked_at,
            )
        logger.info(
            "PostgresTrustAnchorRegistryStore: revoked key authority_id=%s verification_method_id=%s",
            authority_id, verification_method_id,
        )

    async def revoke_authority(self, authority_id: str, revoked_at: dt.datetime) -> None:
        """Revokes EVERY key generation for this Authority — implemented
        by reading the full history and revoking each row individually
        via revoke_key(), so the document/column sync fix that
        revoke_key() already gets is not duplicated here."""
        history = await self.get_history(authority_id)
        for entry in history:
            if entry.revoked_at is None:
                await self.revoke_key(authority_id, entry.verification_method_id, revoked_at)
        logger.info("PostgresTrustAnchorRegistryStore: revoked ALL keys for authority_id=%s", authority_id)

    @staticmethod
    def _row_to_entry(raw: Any) -> TrustAnchorEntry:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return TrustAnchorEntry.model_validate(data)