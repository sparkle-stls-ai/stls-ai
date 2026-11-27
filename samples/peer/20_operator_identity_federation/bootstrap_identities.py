"""
bootstrap_identities.py — 20_operator_identity_federation
-------------------------------------------------------------
One-time setup, run before anything else. Plays THREE roles in
sequence, in one process (same zero-ceremony pattern as
samples/server/19_identity_presentation/bootstrap_identity.py):

    1. The federation's Governing Authority — issues a plain
       NetworkAuthorityCredential ("network identity") to Helios's
       agent, and one to Meridian's agent, the latter with
       roles=["grantor"] (required before Meridian can delegate
       anything at all — see DelegationManager's role check).
    2. Meridian itself, now holding a grantor-roled identity credential —
       uses its own DelegationManager to issue Helios an
       OperationalCredential (the "permission slip") scoped to
       {"action": "reroute_traffic", "segment": "MER-SEG-7"}. A second,
       already-expired copy is issued too, for the "bad delegation" demo
       step — Sentinel checks a credential's OWN validity window and
       revocation status, never the caller-supplied scope (that's
       opaque to it by design), so "wrong scope" is not something the
       identity layer itself can reject; an expired credential is.

Writes everything each side needs into flat JSON files at this sample's
root — meridian_agent/, helios_agent/ and run_demo.py all read these
directly, the same file-handoff pattern 19_identity_presentation uses,
just extended to a second language. This is deliberate even though
IdentityKeystoreFactory/IdentityCredentialCacheFactory are ALSO
populated below (so pointing IDENTITY_KEYSTORE_BACKEND=redis etc. at a
real Redis lets you inspect the raw keys/credentials for real) — but
cross-process credential handoff here is always file-based, not
Factory-based, so the demo behaves identically whether or not Redis is
configured for the keystore/credential-cache backends.

REQUIRED regardless of the above: a reachable Redis at REDIS_URL. This
is NOT optional like the keystore/credential-cache backends — the real
stls_identity.StatusListManager has no in-memory variant (it hard-takes
a redis.asyncio.Redis client; the in-memory status-list stand-in used by
19_identity_presentation's bootstrap is a LOCAL STUB defined in that
sample, not part of the library). Revocation is the whole point of this
demo's "bonus" step, so it is not faked here — see the README.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
from pathlib import Path

import redis.asyncio as redis
from dotenv import load_dotenv

load_dotenv()

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from stls_identity import (
    DelegationManager,
    IdentityCredentialCacheFactory,
    IdentityKeystoreFactory,
    IdentityManager,
    OperationalCredentialCacheFactory,
    StatusListManager,
    bootstrap_signing_key,
    build_did_document,
)

HERE = Path(__file__).parent
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

GOVERNING_AUTHORITY_HOST = "federation.example.com"
HELIOS_HOST = "helios.example.com"
MERIDIAN_HOST = "meridian.example.com"
SEGMENT = "MER-SEG-7"


async def _noop_audit_hook(entry) -> None:
    pass


def _priv_hex(key: Ed25519PrivateKey) -> str:
    return key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    ).hex()


def _pub_hex(key: Ed25519PublicKey) -> str:
    return key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    ).hex()


async def main() -> None:
    redis_client = redis.from_url(REDIS_URL)
    await redis_client.ping()  # fail fast and loud if Redis isn't reachable

    keystore = IdentityKeystoreFactory.build()
    identity_cache = IdentityCredentialCacheFactory.build()
    operational_cache = OperationalCredentialCacheFactory.build()

    # ── Governing Authority: issues BOTH agents' network identities ──────
    authority_did = f"did:web:{GOVERNING_AUTHORITY_HOST}"
    authority_key, authority_pub, _ = bootstrap_signing_key("governing-authority", keystore)
    authority_vm_id = f"{authority_did}#key-1"

    helios_key, helios_pub, _ = bootstrap_signing_key("helios-agent", keystore)
    helios_doc = build_did_document(
        host=HELIOS_HOST, agent_path_segments=["agents", "helios-agent"],
        governing_authority_did=authority_did, signing_public_key=helios_pub,
    )
    helios_vm_id = f"{helios_doc.id}#key-1"

    meridian_key, meridian_pub, _ = bootstrap_signing_key("meridian-agent", keystore)
    meridian_doc = build_did_document(
        host=MERIDIAN_HOST, agent_path_segments=["agents", "meridian-agent"],
        governing_authority_did=authority_did, signing_public_key=meridian_pub,
    )
    meridian_vm_id = f"{meridian_doc.id}#key-1"

    ga_status = StatusListManager(redis_client, authority_id="governing-authority")
    governing_authority = IdentityManager(
        governing_authority_did=authority_did, signing_key=authority_key,
        signing_verification_method_id=authority_vm_id,
        status_list_manager=ga_status,
        status_list_publish_url=f"https://{GOVERNING_AUTHORITY_HOST}/identity/status-list",
        audit_hook=_noop_audit_hook,
    )

    helios_identity_credential = await governing_authority.issue_identity_credential(
        agent_did=helios_doc.id, client_id="helios-agent", roles=[],
    )
    meridian_identity_credential = await governing_authority.issue_identity_credential(
        agent_did=meridian_doc.id, client_id="meridian-agent", roles=["grantor"],
    )

    identity_cache.set("helios-agent", helios_identity_credential)
    identity_cache.set("meridian-agent", meridian_identity_credential)

    # ── Meridian, now a certified Grantor: delegates reroute_traffic ─────
    meridian_status = StatusListManager(redis_client, authority_id="meridian-agent")
    meridian_delegation = DelegationManager(
        grantor_did=meridian_doc.id, grantor_signing_key=meridian_key,
        grantor_verification_method_id=meridian_vm_id,
        status_list_manager=meridian_status,
        status_list_publish_url=f"https://{MERIDIAN_HOST}/identity/status-list",
        audit_hook=_noop_audit_hook,
    )

    operational_credential = await meridian_delegation.issue_operational_credential(
        grantor_identity_credential=meridian_identity_credential,
        requestor_did=helios_doc.id, requestor_client_id="helios-agent",
        scope={"action": "reroute_traffic", "segment": SEGMENT},
        valid_for=dt.timedelta(minutes=10),
    )
    operational_credential_expired = await meridian_delegation.issue_operational_credential(
        grantor_identity_credential=meridian_identity_credential,
        requestor_did=helios_doc.id, requestor_client_id="helios-agent",
        scope={"action": "reroute_traffic", "segment": SEGMENT},
        valid_for=dt.timedelta(seconds=-1),  # already expired the moment it's issued
    )

    operational_cache.set("helios-agent", operational_credential)
    operational_cache.set("helios-agent", operational_credential_expired)

    # ── Write what each side needs, in the shape it needs it ────────────
    def dump(cred) -> dict:
        return cred.model_dump(mode="json", by_alias=True, exclude_none=True)

    (HERE / "governing_authority_public.json").write_text(json.dumps({
        "trusted_keys": {
            authority_vm_id: _pub_hex(authority_pub),
            meridian_vm_id: _pub_hex(meridian_pub),
            helios_vm_id: _pub_hex(helios_pub),
        },
        "did_to_authority_id": {
            authority_did: "governing-authority",
            meridian_doc.id: "meridian-agent",
        },
    }, indent=2))

    (HERE / "helios_identity.json").write_text(json.dumps({
        "holder_did": helios_doc.id,
        "holder_verification_method_id": helios_vm_id,
        "private_key_hex": _priv_hex(helios_key),
        "operational_credential": dump(operational_credential),
        "operational_credential_expired": dump(operational_credential_expired),
        "grantor_identity_credential": dump(meridian_identity_credential),
    }, indent=2))

    (HERE / "meridian_identity.json").write_text(json.dumps({
        "agent_did": meridian_doc.id,
        "identity_credential": dump(meridian_identity_credential),
    }, indent=2))

    await redis_client.aclose()

    print(f"Wrote {HERE / 'governing_authority_public.json'}")
    print(f"Wrote {HERE / 'helios_identity.json'}")
    print(f"Wrote {HERE / 'meridian_identity.json'}")
    print(f"\nHelios DID:    {helios_doc.id}")
    print(f"Meridian DID:  {meridian_doc.id}")
    print(f"Authority DID: {authority_did}")
    print(f"\nOperational credential scope: {operational_credential.credential_subject.scope}")


if __name__ == "__main__":
    asyncio.run(main())
