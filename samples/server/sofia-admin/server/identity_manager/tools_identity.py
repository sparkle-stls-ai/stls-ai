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
from base58 import b58decode
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from stls_identity import (
    CredentialVerificationError,
    IdentityKeystoreFactory,
    IdentityManager,
    NetworkAuthorityCredential,
    OperationalCredential,
    PostgresDIDDocumentStore,
    PostgresTrustAnchorRegistryStore,
    Sentinel,
    StatusListManager,
    TrustAnchorEntry,
    TrustAnchorRegistryClient,
    bootstrap_signing_key,
    build_did_document,
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
# Credential cache namespace — same Redis as the status list and
# keystore, same trust perimeter: whoever can already read an agent's
# private key is already inside the right perimeter to also read its
# credential (which ends up presented publicly on every call anyway —
# it's less sensitive than the key, not more).
_CRED_KEY_PREFIX = "stls:identity:credential"        # single slot, Identity Credential
_OPCRED_KEY_PREFIX = "stls:identity:opcred"           # collection, Operational Credential
_OPCRED_INDEX_PREFIX = "stls:identity:opcred_index"   # sorted set, score = validUntil epoch


class _Deps:
    """Bundle of shared resources, built only once. Not a publicly
    exposed dataclass — it lives as a module implementation detail,
    consistent with tools_redis.py's "flat functions" style."""

    __slots__ = (
        "keystore", "did_store", "status_list_manager", "identity_manager",
        "sentinel", "cache_redis", "pg_pool", "credential_log_table",
        "trust_registry_store", "trust_registry_client",
    )

    def __init__(
        self, keystore, did_store, status_list_manager, identity_manager,
        sentinel, cache_redis, pg_pool, credential_log_table, trust_registry_store,
        trust_registry_client,
    ):
        self.keystore = keystore
        self.did_store = did_store
        self.status_list_manager = status_list_manager
        self.identity_manager = identity_manager
        self.sentinel = sentinel
        self.cache_redis = cache_redis
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

    _deps_singleton = _Deps(
        keystore, did_store, status_list_manager, identity_manager, sentinel,
        cache_redis, pool, _CREDENTIAL_LOG_TABLE, trust_registry_store,
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
        cache_key = f"{_CRED_KEY_PREFIX}:{agent_id}"
        cached_raw = await d.cache_redis.get(cache_key)
        if cached_raw is not None:
            cached = json.loads(cached_raw)
            if cached.get("id") == parsed.id:
                await d.cache_redis.delete(cache_key)
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
        key = f"{_CRED_KEY_PREFIX}:{agent_id}"
        payload = json.dumps(credential)
        if ttl is not None:
            await d.cache_redis.set(key, payload, ex=ttl)
        else:
            await d.cache_redis.set(key, payload)
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
        raw = await d.cache_redis.get(f"{_CRED_KEY_PREFIX}:{agent_id}")
        if raw is None:
            return {"found": False}
        return {"found": True, "credential": json.loads(raw)}
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
        key = f"{_OPCRED_KEY_PREFIX}:{agent_id}:{parsed.id}"
        index_key = f"{_OPCRED_INDEX_PREFIX}:{agent_id}"
        expires_at_epoch = parsed.valid_until.timestamp()

        await d.cache_redis.set(key, json.dumps(credential), ex=ttl)
        await d.cache_redis.zadd(index_key, {parsed.id: expires_at_epoch})
        return {"cached": True, "credential_id": parsed.id, "ttl_seconds": ttl}
    except Exception as e:
        logger.error("cache_operational_credential error: %s", e)
        return {"error": str(e)}


async def list_cached_operational_credentials(agent_id: str) -> dict:
    """
    Lists the Operational Credentials currently valid (not expired) in
    the cache for an agent. First cleans up expired entries from the
    index — same self-cleaning pattern already used elsewhere in the
    system (ZREMRANGEBYSCORE on expiry scores).

    Args:
        agent_id: the agent to list cached delegations for.

    Returns:
        dict with 'credential_ids' (list) and 'count'.
    """
    d = await _deps()
    try:
        index_key = f"{_OPCRED_INDEX_PREFIX}:{agent_id}"
        now_epoch = dt.datetime.now(dt.timezone.utc).timestamp()

        await d.cache_redis.zremrangebyscore(index_key, "-inf", now_epoch)
        credential_ids = await d.cache_redis.zrange(index_key, 0, -1)
        return {"credential_ids": list(credential_ids), "count": len(credential_ids)}
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
        raw = await d.cache_redis.get(f"{_OPCRED_KEY_PREFIX}:{agent_id}:{credential_id}")
        if raw is None:
            return {"found": False}
        return {"found": True, "credential": json.loads(raw)}
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

_MULTICODEC_ED25519_PUB_PREFIX = b"\xed\x01"  # same multicodec stls-identity uses for publicKeyMultibase


def _decode_multibase_ed25519_public_key(multibase_str: str) -> Ed25519PublicKey:
    """
    Reconstructs an Ed25519PublicKey from a publicKeyMultibase — local
    to this file, not imported from stls_identity: the package's
    equivalent function isn't exported from its __init__.py in the
    installed version (ImportError found in production), so we keep it
    here instead of depending on an unstable internal detail. Same
    logic already verified against a real key (Operator B's, onboarded
    into the Trust Anchor Registry).
    """
    if not multibase_str.startswith("z"):
        raise ValueError(f"expected multibase prefix 'z' (base58btc), found: {multibase_str[:1]!r}")
    raw = b58decode(multibase_str[1:])
    prefix, key_bytes = raw[:2], raw[2:]
    if prefix != _MULTICODEC_ED25519_PUB_PREFIX:
        raise ValueError(
            f"unexpected multicodec {prefix!r}, expected {_MULTICODEC_ED25519_PUB_PREFIX!r} (ed25519-pub)"
        )
    return Ed25519PublicKey.from_public_bytes(key_bytes)


async def _is_bit_revoked_at_url(status_list_url: str, index: int) -> bool:
    """Fetch + decode (base64url + gzip) of a published status list,
    then checks the single bit. Third time this logic is needed
    (after check_published_status_list and validate_external_identity_credential)
    — extracted here instead of being duplicated a third time."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(status_list_url)
    resp.raise_for_status()
    payload = resp.json()
    encoded_list = payload.get("encodedList", "")
    if not encoded_list:
        raise ValueError(f"empty status list at {status_list_url}")
    padded = encoded_list + "=" * (-len(encoded_list) % 4)
    decompressed = gzip.decompress(base64.urlsafe_b64decode(padded))
    byte_idx, bit_idx = index // 8, index % 8
    return bool(len(decompressed) > byte_idx and (decompressed[byte_idx] >> (7 - bit_idx)) & 1)


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
        is_revoked = await _is_bit_revoked_at_url(
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

    now = dt.datetime.now(dt.timezone.utc)
    try:
        grantor_revoked = await d.status_list_manager.is_revoked(
            grantor_id_cred.credential_status.status_list_index
        )
        op_revoked = await d.status_list_manager.is_revoked(
            op.credential_status.status_list_index
        )
        await d.sentinel.verify_delegation_chain(
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
        grantor_pub = _decode_multibase_ed25519_public_key(vm["publicKeyMultibase"])
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
        grantor_revoked = await _is_bit_revoked_at_url(
            grantor_id_cred.credential_status.status_list_credential,
            grantor_id_cred.credential_status.status_list_index,
        )
        op_revoked = await _is_bit_revoked_at_url(
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