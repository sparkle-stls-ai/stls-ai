"""
tools_delegation.py
---------------------
Tools for a Grantor agent (issue #51) — generic, configured entirely
via env vars (IDENTITY_AGENT_ID, GOVERNING_AUTHORITY_HOST, ...), same
convention every other identity-aware service in this sample follows.
One running copy of this service IS one specific Grantor — which one
is a deployment decision (which .env it loads), never hardcoded here.

NEVER performs identity provisioning (bootstrap_agent_identity /
issue_identity_credential_for_agent(roles=["grantor"])) — that stays
identity_manager's job, administrative and centralized (see
bootstrap_grantor_agent_identity.py). What THIS module does is the act
of delegating itself, under this agent's own authority, resolved from
its own identity (stls_identity.load_delegation_manager(), issue #51) —
the same package primitive mirrors stls_identity.load_identity_presenter()
(issue #45), just loading something that can ACT as a Grantor instead
of something that can PRESENT an identity.

Expected env vars:
  IDENTITY_AGENT_ID             required — which agent_id's identity this
                                 process presents and delegates as. Must
                                 already be bootstrapped (bootstrap_agent_identity)
                                 AND issued an Identity Credential with
                                 roles=["grantor"] (issue_identity_credential_for_agent) —
                                 both via identity_manager, one time, before
                                 this service can do anything.
  GOVERNING_AUTHORITY_HOST      did:web host segment, must match
                                 identity_manager's own.
  IDENTITY_KEYSTORE_BACKEND/_REDIS_URL/_REDIS_NAMESPACE
                                 SAME values as identity_manager's own .env —
                                 this is the SAME shared keystore, not a
                                 separate one.
  IDENTITY_CREDENTIAL_CACHE_BACKEND
                                 must be "redis" (and point at the SAME
                                 Redis as identity_manager's own credential
                                 cache) — "memory" (the default) means this
                                 process never finds the credential
                                 identity_manager cached when it issued it.
  IDENTITY_AUTHORITY_ID, STATUS_LIST_REDIS_URL, STATUS_LIST_PUBLISH_URL
                                 SAME platform-wide status list identity_manager
                                 publishes to — Operational Credentials this
                                 agent issues share it, rather than each
                                 Grantor getting its own separately-published
                                 list (a real simplification; see this
                                 sample's README for the tradeoff).
"""

from __future__ import annotations

import json
import logging
import os

from dotenv import load_dotenv

load_dotenv()

import redis.asyncio as redis_asyncio

from stls_identity import (
    IdentityCredentialCacheFactory,
    StatusListManager,
    domain_and_path_to_did,
    load_delegation_manager,
)

logger = logging.getLogger(__name__)

IDENTITY_AGENT_ID = os.environ["IDENTITY_AGENT_ID"]
_GOVERNING_AUTHORITY_HOST = os.environ.get("GOVERNING_AUTHORITY_HOST", "sofia.sparkle.example.com")
_IDENTITY_AUTHORITY_ID = os.environ.get("IDENTITY_AUTHORITY_ID", "sofia-authority")
_STATUS_LIST_PUBLISH_URL = os.environ.get(
    "STATUS_LIST_PUBLISH_URL", "https://sofia.sparkle.example.com/identity/status-list"
)

_delegation_manager = None


async def _get_delegation_manager():
    """Built once, cached for the life of this process. The current
    Identity Credential is re-fetched on every delegate_to_agent() call
    instead (see below), not cached here, since it CAN change
    (reissued/revoked) independently of this object's lifetime.

    KNOWN LIMITATION (issue #42, now implemented at the package level):
    this cache is never invalidated, so if this Grantor's signing key
    is ever rotated (stls_identity.rotate_signing_key()) while this
    service keeps running, every delegation issued afterward signs
    with the stale grantor_verification_method_id this manager was
    built with — load_delegation_manager() defaults that id to
    "{holder_did}#key-1" when not told otherwise, which becomes WRONG
    the moment a rotation closes that key generation (see
    load_delegation_manager()'s own docstring). A verifier then finds
    that verification method CLOSED at the new credential's own
    signing time and rejects it outright. Restarting this service
    after a rotation picks up the new key correctly; this module does
    not yet resolve or refresh the current verification_method_id on
    its own, since doing so needs a DIDDocumentStoreBase this process
    doesn't otherwise hold (see identity_manager/server.py's
    _populate_coordinator_key() for the pattern to follow if this ever
    needs to survive a live rotation without a restart)."""
    global _delegation_manager
    if _delegation_manager is not None:
        return _delegation_manager

    status_redis = redis_asyncio.from_url(
        os.environ.get("STATUS_LIST_REDIS_URL", "redis://localhost:6379"),
        decode_responses=False,
    )
    status_list_manager = StatusListManager(
        redis_client=status_redis, authority_id=_IDENTITY_AUTHORITY_ID,
    )
    manager = load_delegation_manager(
        IDENTITY_AGENT_ID,
        governing_authority_host=_GOVERNING_AUTHORITY_HOST,
        status_list_manager=status_list_manager,
        status_list_publish_url=_STATUS_LIST_PUBLISH_URL,
    )
    if manager is None:
        raise RuntimeError(
            f"{IDENTITY_AGENT_ID!r} has no signing key, no cached Identity "
            "Credential, or its credential doesn't declare the 'grantor' "
            "role yet — run bootstrap_grantor_agent_identity.py from "
            "identity_manager first."
        )
    _delegation_manager = manager
    return _delegation_manager


async def delegate_to_agent(
    requestor_agent_id: str,
    scope_json: str,
    requestor_governing_authority_host: str | None = None,
) -> dict:
    """
    Issues a real Operational Credential delegating a scope to
    requestor_agent_id — the actual act of delegation (issue #51),
    performed under THIS agent's own authority. Never call this to
    delegate on behalf of a DIFFERENT Grantor — this process can only
    ever act as the one identity it loaded at startup (IDENTITY_AGENT_ID).

    Args:
        requestor_agent_id: the agent receiving the delegated permission.
        scope_json: domain-specific grant, as a JSON-encoded object
            (a STRING, not a dict — any `dict`-typed parameter here,
            typed or not, makes google-adk generate a schema Gemini's
            function-calling API rejects with
            "Unknown name 'additional_properties'"; a plain string
            parameter sidesteps that entirely) — e.g. for a facility
            Grantor: '{"action": "cross-connect-activate",
            "facility_code": "MI1", "port_id": "42"}'. Opaque to this
            tool beyond being valid JSON — meaningful only to whatever
            later validates and acts on the resulting Operational
            Credential.
        requestor_governing_authority_host: the Requestor's own
            Governing Authority host — defaults to this Grantor's own
            (the common case: delegating to another agent on the same
            platform). Pass a different host for a Requestor on
            another federated platform (the #48 external-delegation
            shape, just in the other direction: this Grantor delegating
            OUT instead of validating one coming IN).

    Returns:
        The signed Operational Credential as a JSON-compatible dict,
        or {'error': '...'}.
    """
    try:
        try:
            scope = json.loads(scope_json)
        except Exception as e:
            return {"error": f"scope_json is not valid JSON: {e}"}
        if not isinstance(scope, dict):
            return {"error": f"scope_json must decode to a JSON object, got {type(scope).__name__}"}

        manager = await _get_delegation_manager()

        credential = IdentityCredentialCacheFactory.build().get(IDENTITY_AGENT_ID)
        if credential is None:
            return {
                "error": f"{IDENTITY_AGENT_ID!r} has no currently cached Identity "
                "Credential — has it expired, or been revoked, since this service started?"
            }

        requestor_did = domain_and_path_to_did(
            requestor_governing_authority_host or _GOVERNING_AUTHORITY_HOST,
            "agents", requestor_agent_id,
        )
        op_credential = await manager.issue_operational_credential(
            grantor_identity_credential=credential,
            requestor_did=requestor_did,
            requestor_client_id=requestor_agent_id,
            scope=scope,
        )
        return op_credential.model_dump(mode="json", by_alias=True, exclude_none=True)
    except Exception as e:
        logger.error("delegate_to_agent error: %s", e)
        return {"error": str(e)}
