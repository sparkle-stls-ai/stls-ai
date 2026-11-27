"""
tools_identity.py
------------------
Tools for sofia_identity_agent.

Manages the lifecycle of Sofia agents' identities (DID + Identity
Credential) — same conceptual namespace as tools_redis.py
(stls:identity:*, stls_identity_* Postgres tables), same style: flat,
module-level functions, not closures over a dependency-injection
object.

Difference from tools_redis.py: there, every function opens/closes a
"use once and discard" Redis connection on every call (a sync Redis
client, cheap to recreate). Here the resources (Postgres pool, key
keystore, IdentityManager) are built ONCE and cached at module level
— recreating them on every call would break bootstrap idempotency (a
fresh InMemoryIdentityKeystore on every call would forget the key
just generated) and would waste a full asyncpg handshake on every tool
call.

NEVER performs the act of delegation: no function here calls
DelegationManager. Assigning the "grantor" role to an identity is
provisioning (issue_identity_credential_for_agent(roles=["grantor"])),
not the act of delegating — that remains up to the agent holding the
role, not this tool.

Expected env vars (same names as .env.example in the stls-identity package):
  IDENTITY_KEYSTORE_BACKEND       memory (default) | redis
  IDENTITY_KEYSTORE_REDIS_URL     redis://localhost:6379
  IDENTITY_KEYSTORE_REDIS_NAMESPACE  stls:identity:signingkey
  IDENTITY_PG_DSN                 asyncpg DSN for the DID store
  IDENTITY_PG_DID_TABLE           stls_identity_did_documents (default)
  STATUS_LIST_REDIS_URL           redis://localhost:6379 (also used by the credential cache)
  IDENTITY_AUTHORITY_ID           sofia-authority (default)
  GOVERNING_AUTHORITY_DID         did:web:sofia.sparkle.example.com
  GOVERNING_AUTHORITY_HOST        sofia.sparkle.example.com
  STATUS_LIST_PUBLISH_URL         public URL of the status list
"""

from __future__ import annotations

import base64
import datetime as dt
import gzip
import json
import logging
import os

import httpx
import redis.asyncio as redis_asyncio

from stls_identity import (
    CredentialVerificationError,
    IdentityCredentialCacheFactory,
    IdentityKeystoreFactory,
    IdentityManager,
    NetworkAuthorityCredential,
    OperationalCredential,
    OperationalCredentialCacheFactory,
    PostgresDIDDocumentStore,
    PostgresTrustAnchorRegistryStore,
    Sentinel,
    StatusListManager,
    TrustAnchorEntry,
    TrustAnchorRegistryClient,
    bootstrap_signing_key,
    build_did_document,
    current_verification_method,
    decode_multibase_ed25519_public_key,
    fetch_status_bit,
    rotate_signing_key,
)

logger = logging.getLogger(__name__)


# ── Config / resources cached at module level ────────────────────────────────
#
# Built on the FIRST call to a tool, then reused — not recreated on
# every invocation. _deps() is idempotent: calling it multiple times
# always returns the same object.

_GOVERNING_AUTHORITY_DID = os.environ.get("GOVERNING_AUTHORITY_DID", "did:web:sofia.sparkle.example.com")
_GOVERNING_AUTHORITY_HOST = os.environ.get("GOVERNING_AUTHORITY_HOST", "sofia.sparkle.example.com")
_GOVERNING_AUTHORITY_AGENT_ID = os.environ.get("GOVERNING_AUTHORITY_AGENT_ID", "governing-authority")
_IDENTITY_AUTHORITY_ID = os.environ.get("IDENTITY_AUTHORITY_ID", "sofia-authority")
_STATUS_LIST_PUBLISH_URL = os.environ.get(
    "STATUS_LIST_PUBLISH_URL", "https://sofia.sparkle.example.com/identity/status-list"
)
_AUTHORITY_VM_ID = f"{_GOVERNING_AUTHORITY_DID}#key-1"

# Issue #45: agent_id of the identity-presenting caller this sample is
# built around (the coordinator) — same env var name
# stls_identity.identity_challenge_handler_from_env() reads on the
# coordinator's own side (agent_coordinator.py), so one value drives
# both "which identity do I present" (coordinator) and "whose identity
# do I trust as a caller" (this module's own server.py, below).
IDENTITY_AGENT_ID = os.environ.get("IDENTITY_AGENT_ID", "sofia-admin-coordinator")

# Issuance/revocation log — audit ONLY, never consulted by the
# verification path (validate_agent_identity_credential doesn't touch
# it). It exists to answer "who did we issue status-list index N to",
# not to decide whether a credential is valid — that's always and only
# signature + status list, never this table. Not part of the
# stls-identity package: the "no central registry of issued
# credentials" principle belongs to the protocol, this is just an
# operational log kept by whoever operates it.
_CREDENTIAL_LOG_TABLE = os.environ.get("IDENTITY_PG_CREDENTIAL_LOG_TABLE", "sf_identity_credential_log")

_CREDENTIAL_LOG_DDL = """
    CREATE TABLE IF NOT EXISTS {table} (
        credential_id       TEXT PRIMARY KEY,
        agent_id            TEXT NOT NULL,
        status_list_index   INT NOT NULL,
        issued_at           TIMESTAMPTZ NOT NULL,
        valid_until          TIMESTAMPTZ,
        roles                JSONB,
        principal_ref        TEXT,
        revoked_at           TIMESTAMPTZ
    );
    CREATE INDEX IF NOT EXISTS {table}_status_list_index_idx ON {table} (status_list_index);
    CREATE INDEX IF NOT EXISTS {table}_agent_id_idx ON {table} (agent_id);
"""
# Operational Credential cache: stls_identity.OperationalCredentialCache
# (issue #54, promoted from this file's own hand-rolled Redis calls,
# same promotion #45 already did for the Identity Credential's own
# cache — see cache_own_identity_credential/get_cached_identity_credential
# below).


class _Deps:
    """Bundle of shared resources, built only once. Not a publicly
    exposed dataclass — it lives as a module implementation detail,
    consistent with tools_redis.py's "flat functions" style."""

    __slots__ = (
        "keystore", "did_store", "status_list_manager", "identity_manager",
        "sentinel", "cache_redis", "credential_cache", "operational_credential_cache",
        "pg_pool", "credential_log_table",
        "trust_registry_store", "trust_registry_client",
    )

    def __init__(
        self, keystore, did_store, status_list_manager, identity_manager,
        sentinel, cache_redis, credential_cache, operational_credential_cache,
        pg_pool, credential_log_table,
        trust_registry_store, trust_registry_client,
    ):
        self.keystore = keystore
        self.did_store = did_store
        self.status_list_manager = status_list_manager
        self.identity_manager = identity_manager
        self.sentinel = sentinel
        self.cache_redis = cache_redis
        self.credential_cache = credential_cache
        self.operational_credential_cache = operational_credential_cache
        self.pg_pool = pg_pool
        self.credential_log_table = credential_log_table
        self.trust_registry_store = trust_registry_store
        self.trust_registry_client = trust_registry_client


_deps_singleton: _Deps | None = None


async def _noop_audit_hook(entry) -> None:
    pass


async def _deps() -> _Deps:
    global _deps_singleton
    if _deps_singleton is not None:
        return _deps_singleton

    keystore = IdentityKeystoreFactory.build()

    authority_key, authority_pub, _ = bootstrap_signing_key(
        _GOVERNING_AUTHORITY_AGENT_ID, keystore
    )

    did_store = PostgresDIDDocumentStore(
        dsn=os.environ.get("IDENTITY_PG_DSN", ""),
        table=os.environ.get("IDENTITY_PG_DID_TABLE"),  # None -> package default if unset
    )
    pool = await did_store._get_pool()
    async with pool.acquire() as conn:
        await conn.execute(did_store.get_ddl())
        # Same pool as did_store — same database (IDENTITY_PG_DSN),
        # separate table. No second connection to manage.
        await conn.execute(_CREDENTIAL_LOG_DDL.format(table=_CREDENTIAL_LOG_TABLE))

    # The AUTHORITY's own DID Document — different from a regular
    # agent's, for two reasons:
    #   1. agent_path_segments=[] -> the resulting DID is a plain
    #      did:web:<host>, with no ":agents:..." suffix, because
    #      that's what GOVERNING_AUTHORITY_DID/_AUTHORITY_VM_ID
    #      actually expect (not an "agent" DID that happens to be
    #      called "governing-authority").
    #   2. governing_authority_did=_GOVERNING_AUTHORITY_DID (itself)
    #      -> the controller is self-referential: the Authority is
    #      the root of trust, it has no Authority above it to control
    #      it, unlike every agent (whose controller is instead the
    #      Authority).
    # Published under the storage key _GOVERNING_AUTHORITY_AGENT_ID —
    # that's the one the bridge reads for GET /.well-known/did.json.
    # Without this step, that endpoint would respond "authority DID
    # Document not bootstrapped yet" forever — it's not a transient
    # error, nobody ever publishes it on its own.
    authority_document = build_did_document(
        host=_GOVERNING_AUTHORITY_HOST,
        agent_path_segments=[],
        governing_authority_did=_GOVERNING_AUTHORITY_DID,
        signing_public_key=authority_pub,
    )
    await did_store.put(_GOVERNING_AUTHORITY_AGENT_ID, authority_document)

    # Trust Anchor Registry — Sparkle as anchor operator (Option A,
    # centralized). Full package store (including key history) — here
    # exposed with only the basic CRUD operations (onboard/list/revoke);
    # resolve_historical() and external-credential verification remain
    # out of scope for now.
    trust_registry_store = PostgresTrustAnchorRegistryStore(
        dsn=os.environ.get("IDENTITY_PG_DSN", ""),
        table=os.environ.get("TRUST_REGISTRY_PG_TABLE"),
    )
    trust_pool = await trust_registry_store._get_pool()
    async with trust_pool.acquire() as conn:
        await conn.execute(trust_registry_store.get_ddl())

    # Read-only registry client — used only to resolve an external
    # Authority's key in validate_external_identity_credential().
    # Distinct from the raw store above (that one handles
    # onboard/list/revoke, direct writes; this one provides a cache
    # with explicit refresh, meant for verification).
    trust_registry_client = TrustAnchorRegistryClient(trust_registry_store)

    # Two DISTINCT Redis clients, deliberately — not one shared.
    #
    # status_redis: decode_responses=False. The revocation bitstring is
    # raw binary data (SETBIT/GETBIT). With decode_responses=True,
    # StatusListManager.publish() would receive a str from GET and
    # pass it to gzip.compress(), which expects bytes — TypeError on
    # every publish (a real bug, found while testing the bridge:
    # "TypeError: a bytes-like object is required, not 'str'"). revoke()/
    # is_revoked()/allocate_index() aren't affected (SETBIT/GETBIT/INCR
    # always return integers, never strings) — that's why the bug stayed
    # silent here and only showed up in the bridge, which calls publish().
    #
    # cache_redis: decode_responses=True. The credential cache is
    # textual JSON (json.dumps/json.loads) and zrange() must return
    # str, not bytes, otherwise the keys built with f-strings in
    # functions like get_cached_operational_credential would break.
    status_redis = redis_asyncio.from_url(
        os.environ.get("STATUS_LIST_REDIS_URL", "redis://localhost:6379"),
        decode_responses=False,
    )
    cache_redis = redis_asyncio.from_url(
        os.environ.get("STATUS_LIST_REDIS_URL", "redis://localhost:6379"),
        decode_responses=True,
    )
    status_list_manager = StatusListManager(
        redis_client=status_redis, authority_id=_IDENTITY_AUTHORITY_ID
    )

    identity_manager = IdentityManager(
        governing_authority_did=_GOVERNING_AUTHORITY_DID,
        signing_key=authority_key,
        signing_verification_method_id=_AUTHORITY_VM_ID,
        status_list_manager=status_list_manager,
        status_list_publish_url=_STATUS_LIST_PUBLISH_URL,
        audit_hook=_noop_audit_hook,
    )

    sentinel = Sentinel(trusted_issuer_keys={_AUTHORITY_VM_ID: authority_pub})

    # Issue #45: package primitive (stls_id_credential_cache.py), not a
    # second hand-rolled Redis client — this is the SAME store
    # load_identity_presenter() reads from on whichever process presents
    # this agent's identity (e.g. the Sofia Admin coordinator). Sync
    # client, like IdentityKeystoreFactory's own — fine here, same
    # "startup/admin-tool call, not a hot request path" reasoning.
    credential_cache = IdentityCredentialCacheFactory.build()

    # Issue #54: same promotion, for the Operational Credentials this
    # agent RECEIVES via delegation (a collection, not a single slot —
    # see OperationalCredentialCacheFactory's own docstring).
    operational_credential_cache = OperationalCredentialCacheFactory.build()

    _deps_singleton = _Deps(
        keystore, did_store, status_list_manager, identity_manager, sentinel,
        cache_redis, credential_cache, operational_credential_cache,
        pool, _CREDENTIAL_LOG_TABLE, trust_registry_store,
        trust_registry_client,
    )
    return _deps_singleton


# ══════════════════════════════════════════════════════════════════════════════
# IDENTITY TOOLS
# ══════════════════════════════════════════════════════════════════════════════

async def bootstrap_agent_identity(agent_id: str) -> dict:
    """
    Generates (or retrieves, if it already exists) an agent's DID
    identity on the platform and publishes its DID Document.

    Idempotent: calling it again on an already-bootstrapped agent_id
    returns the same DID, created=False — it never generates a second
    key for the same agent. This also holds for an agent that will
    become a Grantor: the bootstrap is identical to any other agent's.

    Args:
        agent_id: the agent's identifier on the platform
            (e.g. "agent-087"), the same one currently used for KeyRecord.client_id.

    Returns:
        dict with 'did' (str) and 'created' (bool), or {'error': '...'}.
    """
    d = await _deps()
    try:
        _, public_key, created = bootstrap_signing_key(agent_id, d.keystore)
        document = build_did_document(
            host=_GOVERNING_AUTHORITY_HOST,
            agent_path_segments=["agents", agent_id],
            governing_authority_did=_GOVERNING_AUTHORITY_DID,
            signing_public_key=public_key,
        )
        await d.did_store.put(agent_id, document)
        return {"did": document.id, "created": created}
    except Exception as e:
        logger.error("bootstrap_agent_identity error: %s", e)
        return {"error": str(e)}


async def issue_identity_credential_for_agent(
    agent_id: str,
    roles: list[str] | None = None,
    principal_ref: str | None = None,
) -> dict:
    """
    Issues an Identity Credential for an agent that is ALREADY
    bootstrapped (bootstrap_agent_identity must have been called first).

    To make an agent a Grantor: pass roles=["grantor"] here — that's
    the only way this tool "knows" about a Grantor, as identity data,
    never as a delegation action (this module never calls
    DelegationManager).

    Args:
        agent_id: the same agent_id passed to bootstrap_agent_identity.
        roles: declared roles (e.g. ["grantor"]).
        principal_ref: opaque reference to the human/organization
            principal responsible for this agent (e.g. "org:sparkle:bda").

    Also always caches the issued credential (same effect as calling
    cache_own_identity_credential right after — issue #45: whoever reads
    an agent's own credential from the cache, e.g. the Sofia Admin
    coordinator's own identity_challenge_handler, must see it regardless
    of which path issued it).

    Returns:
        The signed credential as a JSON-compatible dict, or
        {'error': '...'} if the agent isn't bootstrapped yet.
    """
    d = await _deps()
    try:
        document = await d.did_store.get(agent_id)
        if document is None:
            return {"error": f"agent '{agent_id}' has no DID — call bootstrap_agent_identity first"}

        credential = await d.identity_manager.issue_identity_credential(
            agent_did=document.id,
            client_id=agent_id,
            roles=roles or [],
            principal_ref=principal_ref,
        )
        payload = credential.model_dump(mode="json", by_alias=True, exclude_none=True)

        # Audit log — never consulted by validate_agent_identity_credential,
        # it only serves to answer "who did we issue index N to" when the
        # credential itself is no longer retrievable (lost, never cached,
        # expired cache). If this INSERT fails, issuance has already
        # happened and the credential is still valid — we log the error
        # but don't invalidate it over a log-only problem.
        try:
            async with d.pg_pool.acquire() as conn:
                await conn.execute(
                    f"""INSERT INTO {d.credential_log_table}
                        (credential_id, agent_id, status_list_index, issued_at,
                         valid_until, roles, principal_ref)
                        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
                        ON CONFLICT (credential_id) DO NOTHING""",
                    credential.id, agent_id, credential.credential_status.status_list_index,
                    credential.valid_from, credential.valid_until,
                    json.dumps(roles or []), principal_ref,
                )
        except Exception as e:
            logger.error("credential log INSERT failed (credential is still valid): %s", e)

        # Issue #45: ALWAYS cache the newly issued credential — not just
        # "offered" by the agent's own instruction (agent_identity.py)
        # and left to whether the user accepts. The coordinator's own
        # identity_challenge_handler (agent_coordinator.py) reads this
        # SAME cache directly, so a credential issued through THIS tool
        # (via chat) must be visible there exactly like one issued via
        # bootstrap_coordinator_identity.py — one source of truth,
        # whichever path issued it. Failure here never invalidates the
        # issuance itself, same posture as the audit log INSERT above.
        try:
            await cache_own_identity_credential(agent_id, payload)
        except Exception as e:
            logger.error("auto-cache of issued credential failed (issuance still valid): %s", e)

        return payload
    except Exception as e:
        logger.error("issue_identity_credential_for_agent error: %s", e)
        return {"error": str(e)}


async def list_agents_with_identity() -> dict:
    """
    Lists all platform agents that already have a published DID — the
    inventory of identities, not of credentials (those aren't cached
    here: an Identity Credential travels with whoever receives it,
    same "Cosa Viaggia, Cosa È Pubblicato" principle as the rest of
    the system).

    Returns:
        dict with 'agent_ids' (list, alphabetical order) and 'count'.
    """
    d = await _deps()
    try:
        agent_ids = await d.did_store.list_agent_ids()
        return {"agent_ids": agent_ids, "count": len(agent_ids)}
    except Exception as e:
        logger.error("list_agents_with_identity error: %s", e)
        return {"error": str(e)}


async def get_agent_did_document(agent_id: str) -> dict:
    """
    Retrieves the published DID Document for a platform agent.

    Args:
        agent_id: the agent's identifier.

    Returns:
        dict with 'found' (bool) and 'document' if found.
    """
    d = await _deps()
    try:
        document = await d.did_store.get(agent_id)
        if document is None:
            return {"found": False}
        return {"found": True, "document": document.model_dump(mode="json", by_alias=True)}
    except Exception as e:
        logger.error("get_agent_did_document error: %s", e)
        return {"error": str(e)}


async def rotate_agent_identity(agent_id: str) -> dict:
    """
    Rotates an agent's own Ed25519 signing key (issue #42) — generates
    a fresh keypair and updates the agent's DID Document so the OLD
    key generation stays resolvable for anything already signed with
    it (Operational Credentials it issued as a Grantor, presentations
    it already sent), while anything signed AFTER this call must use
    the new one.

    Does NOT touch the agent's Identity Credential at all: that
    credential is signed by the Governing Authority's own key, never
    the agent's — rotating here does not invalidate it, and it never
    needs to be reissued because of a rotation.

    Refuses to rotate the Governing Authority's own identity — that is
    the Trust Anchor Registry's own key history, a separate, more
    sensitive operation (see onboard_trust_anchor/revoke_trust_anchor),
    not something this agent-scoped tool should ever touch.

    IMPORTANT caveat: any long-running process that already built an
    IdentityPresenter/DelegationManager for this agent_id (via
    load_identity_presenter()/load_delegation_manager(), e.g. the
    coordinator or a Grantor agent) keeps signing with the OLD key
    until it is restarted, or rebuilt with the new_verification_method_id
    this call returns — rotating here does not, by itself, reach a
    process that already loaded the previous key.

    Args:
        agent_id: the agent's identifier — must already be bootstrapped
            (bootstrap_agent_identity) with a published DID Document.

    Returns:
        dict with 'did', 'previous_verification_method_id',
        'new_verification_method_id', 'rotated_at', or {'error': '...'}.
    """
    if agent_id == _GOVERNING_AUTHORITY_AGENT_ID:
        return {
            "error": "refusing to rotate the Governing Authority's own identity here — "
            "that's the Trust Anchor Registry's key history, a separate and more "
            "sensitive operation (see onboard_trust_anchor/revoke_trust_anchor)."
        }

    d = await _deps()
    try:
        previous_document = await d.did_store.get(agent_id)
        if previous_document is None:
            return {"error": f"agent '{agent_id}' has no DID — call bootstrap_agent_identity first"}
        previous_vm_id = current_verification_method(previous_document).id

        _, rotated_document = await rotate_signing_key(agent_id, d.keystore, d.did_store)
        new_vm_id = current_verification_method(rotated_document).id

        return {
            "did": rotated_document.id,
            "previous_verification_method_id": previous_vm_id,
            "new_verification_method_id": new_vm_id,
            "rotated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error("rotate_agent_identity error: %s", e)
        return {"error": str(e)}


async def validate_agent_identity_credential(credential: dict) -> dict:
    """
    REALLY verifies an Identity Credential — signature, validity
    window, revocation state — not a simple lookup. The same check
    (Sentinel.verify()) a federated platform would perform in
    production. Useful in a demo to show both the valid case and,
    after a revocation, the fail-closed one.

    Args:
        credential: the exact dict returned by
            issue_identity_credential_for_agent.

    Returns:
        dict with 'valid' (bool) and 'reason' if invalid.
    """
    d = await _deps()
    try:
        parsed = NetworkAuthorityCredential.model_validate(credential)
    except Exception as e:
        return {"valid": False, "reason": f"malformed credential: {e}"}

    try:
        now = dt.datetime.now(dt.timezone.utc)
        is_revoked = await d.status_list_manager.is_revoked(
            parsed.credential_status.status_list_index
        )
        await d.sentinel.verify(parsed, status_bitstring_fetch_time=now, is_revoked=is_revoked)
        return {"valid": True}
    except CredentialVerificationError as e:
        return {"valid": False, "reason": str(e)}
    except Exception as e:
        logger.error("validate_agent_identity_credential error: %s", e)
        return {"error": str(e)}


async def revoke_agent_identity_credential(credential: dict) -> dict:
    """
    Revokes a previously issued Identity Credential.

    Also invalidates the agent's cache (stls:identity:credential:{agent_id})
    if it contains exactly this credential (same id) — without this
    step, get_cached_identity_credential would keep returning a
    perfectly valid-looking JSON even after the revocation: the
    credential itself never changes, the revocation only lives in the
    external status list, so the cache must be invalidated explicitly,
    it doesn't notice anything on its own.

    Doesn't delete blindly: if in the meantime the agent has a
    DIFFERENT (more recent) credential cached, that one stays — only
    the one that exactly matches the revocation is removed.

    Args:
        credential: the exact dict returned by
            issue_identity_credential_for_agent — not an id, the whole
            object (needed to trace back which status-list entry to
            point at).

    Returns:
        dict with 'revoked': True, 'credential_id' and 'cache_invalidated'
        (bool — indicates whether the cache held exactly this credential),
        or {'error': '...'}.
    """
    d = await _deps()
    try:
        parsed = NetworkAuthorityCredential.model_validate(credential)
        await d.identity_manager.revoke(parsed)

        cache_invalidated = False
        agent_id = parsed.credential_subject.client_id
        cached = d.credential_cache.get(agent_id)
        if cached is not None and cached.id == parsed.id:
            d.credential_cache.delete(agent_id)
            cache_invalidated = True

        # Same spirit as the log INSERT in issue_identity_credential_for_agent:
        # if this UPDATE fails, the real revocation (on the status list)
        # has already happened — the credential is already invalid, an
        # incomplete log shouldn't make a successful revocation look failed.
        try:
            async with d.pg_pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE {d.credential_log_table} SET revoked_at = $2 WHERE credential_id = $1",
                    parsed.id, dt.datetime.now(dt.timezone.utc),
                )
        except Exception as e:
            logger.error("credential log UPDATE failed (revocation still happened): %s", e)

        return {
            "revoked": True,
            "credential_id": parsed.id,
            "cache_invalidated": cache_invalidated,
        }
    except Exception as e:
        logger.error("revoke_agent_identity_credential error: %s", e)
        return {"error": str(e)}


async def describe_platform_identity_state() -> dict:
    """
    Quick overview of the platform's identity state — meant for demos
    ("show me the current situation"), not a full audit.

    Returns:
        dict with 'governing_authority_did', 'agents_with_identity'
        (count) and 'agent_ids'.
    """
    d = await _deps()
    try:
        agent_ids = await d.did_store.list_agent_ids()
        return {
            "governing_authority_did": _GOVERNING_AUTHORITY_DID,
            "agents_with_identity": len(agent_ids),
            "agent_ids": agent_ids,
        }
    except Exception as e:
        logger.error("describe_platform_identity_state error: %s", e)
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# CREDENTIAL CACHE
# ══════════════════════════════════════════════════════════════════════════════
#
# This is not the "central registry of issued credentials" that this
# system explicitly doesn't keep (see the rest of the module) — it's
# the agent's storage for ITS OWN credential, same trust perimeter as
# the keystore that already holds its private key. Whoever can already
# read an agent's private key is already inside the right perimeter to
# also read its credential, which ends up presented publicly on every
# call anyway — it's less sensitive than the key, not more.
#
# TTL = the credential's validUntil, not a fixed value: if the agent
# looks for its own credential and doesn't find it, the signal is
# "it's expired or I never asked for it" — no expiry logic to write by
# hand, it cleans itself up (same self-cleaning principle already used
# elsewhere in the system for expiry scores).

def _ttl_seconds_until(valid_until_iso: str) -> int:
    valid_until = dt.datetime.fromisoformat(valid_until_iso.replace("Z", "+00:00"))
    remaining = (valid_until - dt.datetime.now(dt.timezone.utc)).total_seconds()
    return max(int(remaining), 0)


async def cache_own_identity_credential(agent_id: str, credential: dict) -> dict:
    """
    Saves an agent's Identity Credential in its private storage —
    single slot (one identity at a time makes sense, not two). TTL
    equal to the credential's validUntil: it expires on its own when
    the credential is no longer valid, no manual cleanup needed.

    Args:
        agent_id: the agent the credential belongs to.
        credential: the exact dict returned by
            issue_identity_credential_for_agent.

    Returns:
        dict with 'cached': True and 'ttl_seconds', or {'error': '...'}
        if the credential is already expired or malformed.
    """
    d = await _deps()
    try:
        parsed = NetworkAuthorityCredential.model_validate(credential)
    except Exception as e:
        return {"error": f"malformed credential: {e}"}

    ttl = _ttl_seconds_until(parsed.valid_until.isoformat()) if parsed.valid_until else None
    if ttl is not None and ttl <= 0:
        return {"error": "the credential is already expired, not saving it"}

    try:
        # Issue #45: the package primitive (stls_identity.IdentityCredentialCache)
        # owns the actual storage/TTL decision now — same store
        # load_identity_presenter() reads from, so this and ANY other
        # caller end up looking at the same cache.
        d.credential_cache.set(agent_id, parsed)
        return {"cached": True, "ttl_seconds": ttl}
    except Exception as e:
        logger.error("cache_own_identity_credential error: %s", e)
        return {"error": str(e)}


async def get_cached_identity_credential(agent_id: str) -> dict:
    """
    Retrieves an agent's saved Identity Credential, if present and not
    expired.

    Args:
        agent_id: the agent to retrieve the credential for.

    Returns:
        dict with 'found': True and 'credential', or {'found': False}
        if not present or expired (the same thing, from the caller's
        point of view: the TTL makes them indistinguishable).
    """
    d = await _deps()
    try:
        credential = d.credential_cache.get(agent_id)
        if credential is None:
            return {"found": False}
        return {"found": True, "credential": credential.model_dump(mode="json", by_alias=True, exclude_none=True)}
    except Exception as e:
        logger.error("get_cached_identity_credential error: %s", e)
        return {"error": str(e)}


async def cache_operational_credential(agent_id: str, credential: dict) -> dict:
    """
    Saves an Operational Credential received via delegation in the
    agent's private storage — a collection, not a single slot: an
    agent can hold multiple delegations at once, from different
    Grantors, for different scopes. TTL = validUntil, same
    self-cleaning logic as the Identity Credential; the index (sorted
    set, score = validUntil epoch) cleans itself up on every
    list_cached_operational_credentials call.

    Args:
        agent_id: the agent holding the delegation (the Requestor).
        credential: the dict of an OperationalCredential (e.g. from a
            Grantor script/agent external to this module — this tool
            never issues them, it only caches them for whoever
            receives them).

    Returns:
        dict with 'cached': True, 'credential_id', 'ttl_seconds', or
        {'error': '...'} if already expired or malformed.
    """
    d = await _deps()
    try:
        parsed = OperationalCredential.model_validate(credential)
    except Exception as e:
        return {"error": f"malformed credential: {e}"}

    if parsed.valid_until is None:
        return {"error": "Operational Credential with no validUntil — cannot be cached safely"}

    ttl = _ttl_seconds_until(parsed.valid_until.isoformat())
    if ttl <= 0:
        return {"error": "the credential is already expired, not saving it"}

    try:
        # Issue #54: the package primitive (stls_identity.OperationalCredentialCache)
        # owns the actual storage/TTL/index decision now — same store
        # load_delegation_presenter() reads from, so this and ANY other
        # caller end up looking at the same cache.
        d.operational_credential_cache.set(agent_id, parsed)
        return {"cached": True, "credential_id": parsed.id, "ttl_seconds": ttl}
    except Exception as e:
        logger.error("cache_operational_credential error: %s", e)
        return {"error": str(e)}


async def list_cached_operational_credentials(agent_id: str) -> dict:
    """
    Lists the Operational Credentials currently valid (not expired) in
    the cache for an agent. Self-cleaning: expired entries are dropped
    from the index as a side effect of calling this (see
    OperationalCredentialCacheBase.list_ids()).

    Args:
        agent_id: the agent to list cached delegations for.

    Returns:
        dict with 'credential_ids' (list) and 'count'.
    """
    d = await _deps()
    try:
        credential_ids = d.operational_credential_cache.list_ids(agent_id)
        return {"credential_ids": credential_ids, "count": len(credential_ids)}
    except Exception as e:
        logger.error("list_cached_operational_credentials error: %s", e)
        return {"error": str(e)}


async def get_cached_operational_credential(agent_id: str, credential_id: str) -> dict:
    """
    Retrieves a specific Operational Credential cached for an agent.

    Args:
        agent_id: the agent holding the delegation.
        credential_id: the credential's id (from
            list_cached_operational_credentials).

    Returns:
        dict with 'found': True and 'credential', or {'found': False}
        if not present or expired.
    """
    d = await _deps()
    try:
        credential = d.operational_credential_cache.get(agent_id, credential_id)
        if credential is None:
            return {"found": False}
        return {"found": True, "credential": credential.model_dump(mode="json", by_alias=True, exclude_none=True)}
    except Exception as e:
        logger.error("get_cached_operational_credential error: %s", e)
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# STATUS LIST — verification of the published list via HTTP
# ══════════════════════════════════════════════════════════════════════════════

async def check_published_status_list() -> dict:
    """
    Verifies the status list AS ACTUALLY PUBLISHED by the bridge via
    HTTP — doesn't read Redis directly, checks the entire pipeline
    (Redis -> publish() -> Postgres cache -> HTTP). Useful to notice
    if the bridge's refresher ever got stuck (the same bug that
    happened in production: decode_responses=True silently broke
    publish(), and the published status list stayed empty forever —
    this tool would have made it visible in a conversation turn
    instead of a redis-cli debugging session).

    Shows ONLY the revoked indices (integers), never which agent they
    correspond to — not because that mapping doesn't exist (see
    find_agent_by_status_list_index, which consults it in a separate
    audit log), but because the public bitstring itself only says
    "index N is revoked", never "agent-002 is revoked": whoever reads
    this endpoint from outside the platform has no access to the
    audit log anyway. To trace back to the agent from inside here, see
    find_agent_by_status_list_index.

    Returns:
        dict with 'valid_from', 'total_bits', 'revoked_indices' (list
        of integers) and 'revoked_count', or {'error': '...'} if the
        bridge doesn't respond or still publishes an empty placeholder.
    """
    url = _STATUS_LIST_PUBLISH_URL
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        logger.error("check_published_status_list error: %s", e)
        return {"error": f"could not read the status list published at {url}: {e}"}

    encoded_list = payload.get("encodedList", "")
    if not encoded_list:
        return {"error": "the bridge responded but encodedList is empty/missing — refresher never succeeded?"}

    try:
        padded = encoded_list + "=" * (-len(encoded_list) % 4)
        raw = base64.urlsafe_b64decode(padded)
        decompressed = gzip.decompress(raw)
    except Exception as e:
        return {"error": f"could not decode encodedList (base64/gzip): {e}"}

    revoked_indices = [
        byte_idx * 8 + bit_idx
        for byte_idx, byte in enumerate(decompressed)
        for bit_idx in range(8)
        if (byte >> (7 - bit_idx)) & 1
    ]

    return {
        "valid_from": payload.get("validFrom"),
        "total_bits": len(decompressed) * 8,
        "revoked_indices": revoked_indices,
        "revoked_count": len(revoked_indices),
    }


# ══════════════════════════════════════════════════════════════════════════════
# CREDENTIAL LOG — audit, never used for verification
# ══════════════════════════════════════════════════════════════════════════════

async def find_agent_by_status_list_index(status_list_index: int) -> dict:
    """
    Answers "who did we issue this status-list index to" — consults
    ONLY the audit log (sf_identity_credential_log by default), NEVER
    the status list itself or Sentinel.verify(). This tool doesn't say
    whether the credential is still valid (for that see
    validate_agent_identity_credential or check_published_status_list)
    — it only says who that index historically belonged to.

    Args:
        status_list_index: the index found, for example, among
            check_published_status_list's revoked_indices.

    Returns:
        dict with 'found': bool and, if found, 'agent_id',
        'credential_id', 'issued_at', 'revoked_at' (None if not
        revoked), 'roles', 'principal_ref'.
    """
    d = await _deps()
    try:
        async with d.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""SELECT credential_id, agent_id, issued_at, valid_until,
                           roles, principal_ref, revoked_at
                    FROM {d.credential_log_table}
                    WHERE status_list_index = $1
                    ORDER BY issued_at DESC""",
                status_list_index,
            )
        if row is None:
            return {"found": False}
        return {
            "found": True,
            "credential_id": row["credential_id"],
            "agent_id": row["agent_id"],
            "issued_at": row["issued_at"].isoformat() if row["issued_at"] else None,
            "valid_until": row["valid_until"].isoformat() if row["valid_until"] else None,
            "roles": json.loads(row["roles"]) if row["roles"] else [],
            "principal_ref": row["principal_ref"],
            "revoked_at": row["revoked_at"].isoformat() if row["revoked_at"] else None,
        }
    except Exception as e:
        logger.error("find_agent_by_status_list_index error: %s", e)
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# TRUST ANCHOR REGISTRY — Sparkle as anchor operator (Option A)
# ══════════════════════════════════════════════════════════════════════════════
#
# Basic CRUD on the Authorities recognized by the federation. The
# package's store also supports key history and point-in-time
# revocation (revoke_key, not just revoke_authority) — only onboard/
# list/revoke are exposed here, the "very simple" agreed-upon scope.
# Verification of credentials issued by an external Authority stays
# out of scope: it would also require contacting that platform's
# bridge for its status list, a separate piece we don't want to build
# half-way.

async def onboard_trust_anchor(
    authority_id: str,
    did: str,
    verification_method_id: str,
    public_key_multibase: str,
) -> dict:
    """
    Registers an external Authority as recognized by the federation.

    The four parameters are copied directly from the DID Document
    published by the other platform (GET /.well-known/did.json or
    equivalent): did = its id, verification_method_id and
    public_key_multibase = from the matching verificationMethod entry.
    No network call here — onboarding is a deliberate administrative
    action, not automatic (Option A: centralized, Sparkle decides who
    joins the federation).

    Args:
        authority_id: short, readable id for administrative use (e.g.
            "operatore-b") — not cryptographic, bookkeeping only.
        did: the external Governing Authority's DID (e.g.
            "did:web:b.example.com").
        verification_method_id: the key's id (e.g.
            "did:web:b.example.com#key-1").
        public_key_multibase: the public key, copied exactly from
            their DID Document (multibase format, e.g. "z6Mk...").

    Returns:
        dict with 'onboarded': True and 'authority_id', or {'error': '...'}.
    """
    d = await _deps()
    try:
        entry = TrustAnchorEntry(
            authority_id=authority_id,
            did=did,
            verification_method_id=verification_method_id,
            public_key_multibase=public_key_multibase,
            onboarded_at=dt.datetime.now(dt.timezone.utc),
        )
        await d.trust_registry_store.put(entry)
        return {"onboarded": True, "authority_id": authority_id, "did": did}
    except Exception as e:
        logger.error("onboard_trust_anchor error: %s", e)
        return {"error": str(e)}


async def list_trust_anchors() -> dict:
    """
    Lists the Authorities currently recognized by the federation
    (current key generation, not revoked).

    Returns:
        dict with 'anchors' (list of {authority_id, did,
        verification_method_id, onboarded_at}) and 'count'.
    """
    d = await _deps()
    try:
        entries = await d.trust_registry_store.list_active()
        anchors = [
            {
                "authority_id": e.authority_id,
                "did": e.did,
                "verification_method_id": e.verification_method_id,
                "onboarded_at": e.onboarded_at.isoformat() if e.onboarded_at else None,
            }
            for e in entries
        ]
        return {"anchors": anchors, "count": len(anchors)}
    except Exception as e:
        logger.error("list_trust_anchors error: %s", e)
        return {"error": str(e)}


async def revoke_trust_anchor(authority_id: str) -> dict:
    """
    Offboards an Authority from the federation — closes EVERY key
    generation it ever had (history included), not just the current
    one. No row is deleted, it remains for audit.

    Args:
        authority_id: the id originally passed to onboard_trust_anchor.

    Returns:
        dict with 'revoked': True and 'authority_id', or {'error': '...'}.
    """
    d = await _deps()
    try:
        await d.trust_registry_store.revoke_authority(
            authority_id, dt.datetime.now(dt.timezone.utc)
        )
        return {"revoked": True, "authority_id": authority_id}
    except Exception as e:
        logger.error("revoke_trust_anchor error: %s", e)
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# EXTERNAL CREDENTIAL VERIFICATION — actually uses the Trust Anchor Registry
# ══════════════════════════════════════════════════════════════════════════════

# _decode_multibase_ed25519_public_key / _is_bit_revoked_at_url used to be
# defined here, duplicated from logic that genuinely lives in the
# stls-identity package but wasn't exported (decode_multibase_ed25519_public_key,
# stls_id_did.py) or didn't exist yet as a reusable primitive
# (fetch_status_bit/fetch_remote_credential_status, stls_id_status.py —
# promoted from this exact code while scoping issue #15's follow-up, #45).
# Both are now imported from the package below instead of duplicated a
# second time.


async def validate_external_identity_credential(credential: dict) -> dict:
    """
    Verifies an Identity Credential issued by an EXTERNAL Authority
    (not this platform) — unlike validate_agent_identity_credential,
    which only knows its own key and its own status list, this one:
      1. resolves the issuer's key via the Trust Anchor Registry (it
         must already have been onboarded with onboard_trust_anchor,
         otherwise it fails with "unknown or untrusted verification
         method" — correctly, not a bug)
      2. checks revocation at THE ISSUER'S BRIDGE, not ours — the URL
         isn't configured by hand, it's inside the credential itself
         (credentialStatus.statusListCredential)

    Args:
        credential: the dict of a NetworkAuthorityCredential issued by
            another platform (e.g. Operator B's Identity Manager).

    Returns:
        dict with 'valid': bool, and 'reason' if invalid.
    """
    d = await _deps()
    try:
        parsed = NetworkAuthorityCredential.model_validate(credential)
    except Exception as e:
        return {"valid": False, "reason": f"malformed credential: {e}"}

    try:
        await d.trust_registry_client.refresh()
        trusted_keys = d.trust_registry_client.as_trusted_keys()
    except Exception as e:
        logger.error("validate_external_identity_credential: registry refresh failed: %s", e)
        return {"error": f"could not read the trust anchor registry: {e}"}

    status_list_url = parsed.credential_status.status_list_credential
    try:
        is_revoked = await fetch_status_bit(
            status_list_url, parsed.credential_status.status_list_index
        )
    except Exception as e:
        logger.error("validate_external_identity_credential: status list check failed: %s", e)
        return {"valid": False, "reason": f"could not verify revocation at {status_list_url}: {e}"}

    external_sentinel = Sentinel(trusted_issuer_keys=trusted_keys)
    now = dt.datetime.now(dt.timezone.utc)
    try:
        await external_sentinel.verify(parsed, status_bitstring_fetch_time=now, is_revoked=is_revoked)
        return {"valid": True}
    except CredentialVerificationError as e:
        return {"valid": False, "reason": str(e)}
    except Exception as e:
        logger.error("validate_external_identity_credential error: %s", e)
        return {"error": str(e)}


async def validate_operational_credential(
    operational_credential: dict,
    grantor_identity_credential: dict,
) -> dict:
    """
    Verifies a delegation chain — Operational Credential + the
    Grantor's Identity Credential — when the Grantor is an agent OF
    THIS PLATFORM. Three checks in one, via
    Sentinel.verify_delegation_chain(): (a) the Grantor's Identity
    Credential passes signature/window/revocation on its own, (b) the
    Operational Credential's issuer matches that Identity Credential's
    subject, (c) the Operational Credential itself passes
    signature/window/revocation — on its own status list, not the
    Identity Credential's (different indices, different entries).

    For an external Grantor see validate_external_operational_credential.

    Args:
        operational_credential: the dict of the delegation to verify.
        grantor_identity_credential: the dict of the Grantor's Identity
            Credential that issued it.

    Returns:
        dict with 'valid': bool, and 'reason' if invalid.
    """
    d = await _deps()
    try:
        op = OperationalCredential.model_validate(operational_credential)
        grantor_id_cred = NetworkAuthorityCredential.model_validate(grantor_identity_credential)
    except Exception as e:
        return {"valid": False, "reason": f"malformed credential: {e}"}

    # d.sentinel (the shared module-level Sentinel) only knows the
    # Governing Authority's own key — correct for validate_agent_identity_credential,
    # but the OperationalCredential below is signed by the GRANTOR, not
    # the Authority. Resolve the grantor's own verification key from its
    # published DID Document (it's an agent of this platform, so
    # d.did_store has it) and build a one-off Sentinel that trusts both
    # keys — same pattern validate_external_operational_credential
    # already uses for an external grantor, just resolved from our own
    # did_store instead of a manually-supplied DID Document.
    grantor_agent_id = grantor_id_cred.credential_subject.client_id
    grantor_document = await d.did_store.get(grantor_agent_id)
    if grantor_document is None:
        return {
            "valid": False,
            "reason": f"grantor '{grantor_agent_id}' has no published DID — cannot resolve its signing key",
        }
    try:
        grantor_vm = current_verification_method(grantor_document)
        grantor_pub = decode_multibase_ed25519_public_key(grantor_vm.public_key_multibase)
    except Exception as e:
        return {"valid": False, "reason": f"malformed grantor DID Document: {e}"}

    _, authority_pub, _ = bootstrap_signing_key(_GOVERNING_AUTHORITY_AGENT_ID, d.keystore)
    delegation_sentinel = Sentinel(
        trusted_issuer_keys={_AUTHORITY_VM_ID: authority_pub, grantor_vm.id: grantor_pub}
    )

    now = dt.datetime.now(dt.timezone.utc)
    try:
        grantor_revoked = await d.status_list_manager.is_revoked(
            grantor_id_cred.credential_status.status_list_index
        )
        op_revoked = await d.status_list_manager.is_revoked(
            op.credential_status.status_list_index
        )
        await delegation_sentinel.verify_delegation_chain(
            op,
            grantor_identity_credential=grantor_id_cred,
            grantor_identity_fetch_time=now, grantor_identity_is_revoked=grantor_revoked,
            operational_fetch_time=now, operational_is_revoked=op_revoked,
        )
        return {"valid": True}
    except CredentialVerificationError as e:
        return {"valid": False, "reason": str(e)}
    except Exception as e:
        logger.error("validate_operational_credential error: %s", e)
        return {"error": str(e)}


async def validate_external_operational_credential(
    operational_credential: dict,
    grantor_identity_credential: dict,
    grantor_did_document: dict,
) -> dict:
    """
    Verifies a delegation chain when the Grantor is an agent of
    ANOTHER PLATFORM. Compared to the internal case, one extra piece
    of data is needed: the Trust Anchor Registry only contains
    Governing Authority keys, never individual agents' keys — so the
    Grantor's own key (the one that signed the Operational Credential)
    can't be resolved from there. It must be provided explicitly as
    its DID Document, copied from that platform's bridge (e.g.
    GET {host}/agents/{id}/did.json) — same principle already used for
    onboard_trust_anchor: no automatic resolution, deliberately copied
    data.

    Revocation of BOTH credentials is checked at their respective
    bridges (URL inside each credential,
    credentialStatus.statusListCredential) — never ours.

    Args:
        operational_credential: the dict of the delegation to verify.
        grantor_identity_credential: the dict of the Grantor's Identity
            Credential, issued by ITS Governing Authority (must already
            have been onboarded with onboard_trust_anchor).
        grantor_did_document: the Grantor's own DID Document, copied
            from its platform's bridge.

    Returns:
        dict with 'valid': bool, and 'reason' if invalid.
    """
    d = await _deps()
    try:
        op = OperationalCredential.model_validate(operational_credential)
        grantor_id_cred = NetworkAuthorityCredential.model_validate(grantor_identity_credential)
    except Exception as e:
        return {"valid": False, "reason": f"malformed credential: {e}"}

    try:
        vm = grantor_did_document["verificationMethod"][0]
        grantor_pub = decode_multibase_ed25519_public_key(vm["publicKeyMultibase"])
        grantor_vm_id = vm["id"]
    except Exception as e:
        return {"valid": False, "reason": f"malformed grantor_did_document: {e}"}

    try:
        await d.trust_registry_client.refresh()
        trusted_keys = dict(d.trust_registry_client.as_trusted_keys())
    except Exception as e:
        logger.error("validate_external_operational_credential: registry refresh failed: %s", e)
        return {"error": f"could not read the trust anchor registry: {e}"}
    trusted_keys[grantor_vm_id] = grantor_pub  # the Grantor's key, never in the registry
    external_sentinel = Sentinel(trusted_issuer_keys=trusted_keys)

    now = dt.datetime.now(dt.timezone.utc)
    try:
        grantor_revoked = await fetch_status_bit(
            grantor_id_cred.credential_status.status_list_credential,
            grantor_id_cred.credential_status.status_list_index,
        )
        op_revoked = await fetch_status_bit(
            op.credential_status.status_list_credential,
            op.credential_status.status_list_index,
        )
    except Exception as e:
        return {"valid": False, "reason": f"could not verify revocation: {e}"}

    try:
        await external_sentinel.verify_delegation_chain(
            op,
            grantor_identity_credential=grantor_id_cred,
            grantor_identity_fetch_time=now, grantor_identity_is_revoked=grantor_revoked,
            operational_fetch_time=now, operational_is_revoked=op_revoked,
        )
        return {"valid": True}
    except CredentialVerificationError as e:
        return {"valid": False, "reason": str(e)}
    except Exception as e:
        logger.error("validate_external_operational_credential error: %s", e)
        return {"error": str(e)}