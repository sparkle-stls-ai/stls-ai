"""
Issue #42 — single-agent key rotation (DID Document key history).

Covers:
    - rotate_agent_signing_key(): closes the previous verificationMethod,
      appends a new one, repoints authentication/assertionMethod
    - current_verification_method(): survives a rotated document,
      raises on an ambiguous/empty one
    - resolve_holder_key_at(): old key resolves for timestamps before
      rotation, new key for after, revoked/unknown resolve to None
    - agent_id_from_verification_method_id(): round-trips with
      domain_and_path_to_did(), rejects a non-agent (Authority) DID
    - IdentityKeystoreBase.rotate(): overwrites the stored private key
    - end-to-end with Sentinel.verify_presentation_proof(): a
      presentation signed with the OLD key still verifies via
      holder_key_resolver after rotation; one signed with the NEW key
      verifies too, even though trusted_issuer_keys was never updated
    - rotate_signing_key(): the full orchestration (keystore + store)
      against an in-memory DIDDocumentStoreBase-shaped fake
"""

import datetime as dt
import uuid

import pytest

from stls_identity import (
    DIDDocument,
    InMemoryIdentityKeystore,
    Sentinel,
    agent_id_from_verification_method_id,
    build_did_document,
    build_verifiable_presentation,
    bootstrap_signing_key,
    current_verification_method,
    domain_and_path_to_did,
    make_did_document_holder_key_resolver,
    resolve_holder_key_at,
    rotate_agent_signing_key,
    rotate_signing_key,
)

HOST = "sofia.sparkle.example.com"
AUTHORITY_DID = f"did:web:{HOST}"


class _FakeDIDDocumentStore:
    """In-memory stand-in for DIDDocumentStoreBase — no Postgres
    needed, same spirit as InMemoryIdentityKeystore for the signing
    key. Only implements what rotate_signing_key()/
    make_did_document_holder_key_resolver() actually call."""

    def __init__(self):
        self._docs: dict[str, DIDDocument] = {}

    async def get(self, agent_id: str) -> DIDDocument | None:
        return self._docs.get(agent_id)

    async def put(self, agent_id: str, document: DIDDocument) -> None:
        self._docs[agent_id] = document


def _bootstrap_agent(keystore, agent_id: str):
    key, pub, _ = bootstrap_signing_key(agent_id, keystore)
    document = build_did_document(
        host=HOST, agent_path_segments=["agents", agent_id],
        governing_authority_did=AUTHORITY_DID, signing_public_key=pub,
    )
    return key, pub, document


# ---------------------------------------------------------------------------
# rotate_agent_signing_key() — pure, in-memory
# ---------------------------------------------------------------------------

def test_rotation_closes_previous_key_and_opens_a_new_one():
    keystore = InMemoryIdentityKeystore()
    old_key, old_pub, document = _bootstrap_agent(keystore, "sofia-admin-coordinator")
    old_vm_id = document.verification_method[0].id
    assert document.verification_method[0].valid_until is None

    new_key = InMemoryIdentityKeystore().rotate("unused")  # just a fresh key, store irrelevant here
    now = dt.datetime.now(dt.timezone.utc)
    rotated = rotate_agent_signing_key(document, new_signing_public_key=new_key.public_key(), now=now)

    assert len(rotated.verification_method) == 2
    old_entry = next(vm for vm in rotated.verification_method if vm.id == old_vm_id)
    assert old_entry.valid_until == now

    new_entry = current_verification_method(rotated)
    assert new_entry.id != old_vm_id
    assert new_entry.valid_from == now
    assert new_entry.valid_until is None
    assert rotated.authentication == [new_entry.id]
    assert rotated.assertion_method == [new_entry.id]


def test_rotation_is_idempotent_in_shape_across_multiple_rotations():
    keystore = InMemoryIdentityKeystore()
    _, _, document = _bootstrap_agent(keystore, "agent-087")

    for _ in range(3):
        fresh = InMemoryIdentityKeystore().rotate("unused")
        document = rotate_agent_signing_key(document, new_signing_public_key=fresh.public_key())

    assert len(document.verification_method) == 4  # original + 3 rotations
    current = current_verification_method(document)
    assert document.authentication == [current.id]
    # every non-current entry is closed
    closed = [vm for vm in document.verification_method if vm.id != current.id]
    assert all(vm.valid_until is not None for vm in closed)


def test_current_verification_method_raises_when_ambiguous():
    keystore = InMemoryIdentityKeystore()
    _, _, document = _bootstrap_agent(keystore, "agent-ambiguous")
    # Hand-corrupt: reopen the only entry a second time (two "current" rows).
    doubled = document.model_copy(update={
        "verification_method": document.verification_method + document.verification_method,
    })
    with pytest.raises(ValueError):
        current_verification_method(doubled)


# ---------------------------------------------------------------------------
# resolve_holder_key_at()
# ---------------------------------------------------------------------------

def test_resolve_holder_key_at_picks_the_key_valid_at_signing_time():
    keystore = InMemoryIdentityKeystore()
    old_key, old_pub, document = _bootstrap_agent(keystore, "agent-rotating")
    old_vm_id = document.verification_method[0].id

    t_before_rotation = dt.datetime.now(dt.timezone.utc)
    rotation_time = t_before_rotation + dt.timedelta(hours=1)
    new_key = InMemoryIdentityKeystore().rotate("unused")
    rotated = rotate_agent_signing_key(document, new_signing_public_key=new_key.public_key(), now=rotation_time)
    new_vm_id = current_verification_method(rotated).id

    # Old key resolves for a signature made before the rotation.
    resolved_old = resolve_holder_key_at(rotated, old_vm_id, t_before_rotation)
    assert resolved_old is not None
    assert resolved_old.public_bytes_raw() == old_pub.public_bytes_raw()

    # Old key id does NOT resolve for a time after it was closed.
    assert resolve_holder_key_at(rotated, old_vm_id, rotation_time + dt.timedelta(seconds=1)) is None

    # New key resolves for a time after the rotation.
    resolved_new = resolve_holder_key_at(rotated, new_vm_id, rotation_time + dt.timedelta(seconds=1))
    assert resolved_new is not None
    assert resolved_new.public_bytes_raw() == new_key.public_key().public_bytes_raw()

    # Unknown verification_method_id -> None.
    assert resolve_holder_key_at(rotated, f"{document.id}#key-99", t_before_rotation) is None


def test_resolve_holder_key_at_respects_revocation_even_inside_validity_window():
    keystore = InMemoryIdentityKeystore()
    _, _, document = _bootstrap_agent(keystore, "agent-revoked")
    vm_id = document.verification_method[0].id
    signed_at = dt.datetime.now(dt.timezone.utc)
    revoked_at = signed_at - dt.timedelta(minutes=1)  # revoked BEFORE this signature's own timestamp

    revoked_vm = document.verification_method[0].model_copy(update={"revoked_at": revoked_at})
    document = document.model_copy(update={"verification_method": [revoked_vm]})

    assert resolve_holder_key_at(document, vm_id, signed_at) is None


# ---------------------------------------------------------------------------
# agent_id_from_verification_method_id()
# ---------------------------------------------------------------------------

def test_agent_id_from_verification_method_id_round_trips():
    did = domain_and_path_to_did(HOST, "agents", "sofia-admin-coordinator")
    vm_id = f"{did}#key-2"
    assert agent_id_from_verification_method_id(vm_id, HOST) == "sofia-admin-coordinator"


def test_agent_id_from_verification_method_id_rejects_authority_key():
    vm_id = f"{AUTHORITY_DID}#key-1"
    assert agent_id_from_verification_method_id(vm_id, HOST) is None


# ---------------------------------------------------------------------------
# IdentityKeystoreBase.rotate()
# ---------------------------------------------------------------------------

def test_keystore_rotate_overwrites_the_stored_private_key():
    keystore = InMemoryIdentityKeystore()
    old_key, _, _ = _bootstrap_agent(keystore, "agent-x")

    new_key = keystore.rotate("agent-x")

    stored = keystore.get("agent-x")
    assert stored.public_key().public_bytes_raw() == new_key.public_key().public_bytes_raw()
    assert stored.public_key().public_bytes_raw() != old_key.public_key().public_bytes_raw()


# ---------------------------------------------------------------------------
# End-to-end: Sentinel.verify_presentation_proof() across a rotation
# ---------------------------------------------------------------------------

async def test_presentation_signed_with_old_key_still_verifies_after_rotation():
    keystore = InMemoryIdentityKeystore()
    did_store = _FakeDIDDocumentStore()
    agent_id = "agent-presenting"

    old_key, old_pub, document = _bootstrap_agent(keystore, agent_id)
    await did_store.put(agent_id, document)
    old_vm_id = document.verification_method[0].id

    # Build the VP BEFORE rotation, with the old key — simulates a
    # presentation created, then rotation happening before it's verified.
    challenge = str(uuid.uuid4())
    from stls_identity import NetworkAuthorityCredential, IdentityManager

    class _InMemoryStatusListManager:
        async def allocate_index(self):
            return 0
        async def revoke(self, index):
            pass
        async def is_revoked(self, index):
            return False

    issuer = IdentityManager(
        governing_authority_did=AUTHORITY_DID,
        signing_key=old_key,  # irrelevant signer identity for this test's purpose
        signing_verification_method_id=old_vm_id,
        status_list_manager=_InMemoryStatusListManager(),
        status_list_publish_url="https://sofia.sparkle.example.com/identity/status-list",
    )
    credential: NetworkAuthorityCredential = await issuer.issue_identity_credential(
        agent_did=document.id, client_id=agent_id,
    )
    vp = build_verifiable_presentation(
        holder_did=document.id, holder_signing_key=old_key,
        holder_verification_method_id=old_vm_id, credentials=[credential],
        challenge=challenge,
    )

    # Now rotate — old_vm_id is closed, no longer current.
    new_key = keystore.rotate(agent_id)
    rotated = rotate_agent_signing_key(document, new_signing_public_key=new_key.public_key())
    await did_store.put(agent_id, rotated)

    # Sentinel has NOT been told about the holder's key at all — it must
    # fall back to the DID Document's own history.
    resolver = make_did_document_holder_key_resolver(did_store, governing_authority_host=HOST)
    sentinel = Sentinel(trusted_issuer_keys={old_vm_id: old_pub}, holder_key_resolver=resolver)

    await sentinel.verify_presentation_proof(vp, expected_challenge=challenge)  # must not raise


async def test_presentation_signed_with_new_key_resolves_via_fallback_without_cache_update():
    keystore = InMemoryIdentityKeystore()
    did_store = _FakeDIDDocumentStore()
    agent_id = "agent-presenting-2"

    old_key, old_pub, document = _bootstrap_agent(keystore, agent_id)
    await did_store.put(agent_id, document)

    new_key = keystore.rotate(agent_id)
    rotated = rotate_agent_signing_key(document, new_signing_public_key=new_key.public_key())
    await did_store.put(agent_id, rotated)
    new_vm_id = current_verification_method(rotated).id

    challenge = str(uuid.uuid4())
    from stls_identity import NetworkAuthorityCredential, IdentityManager

    class _InMemoryStatusListManager:
        async def allocate_index(self):
            return 0
        async def revoke(self, index):
            pass
        async def is_revoked(self, index):
            return False

    issuer = IdentityManager(
        governing_authority_did=AUTHORITY_DID,
        signing_key=old_key, signing_verification_method_id=document.verification_method[0].id,
        status_list_manager=_InMemoryStatusListManager(),
        status_list_publish_url="https://sofia.sparkle.example.com/identity/status-list",
    )
    credential: NetworkAuthorityCredential = await issuer.issue_identity_credential(
        agent_did=document.id, client_id=agent_id,
    )
    vp = build_verifiable_presentation(
        holder_did=document.id, holder_signing_key=new_key,
        holder_verification_method_id=new_vm_id, credentials=[credential],
        challenge=challenge,
    )

    # trusted_issuer_keys is EMPTY for the holder — only the resolver can find it.
    resolver = make_did_document_holder_key_resolver(did_store, governing_authority_host=HOST)
    sentinel = Sentinel(trusted_issuer_keys={}, holder_key_resolver=resolver)

    await sentinel.verify_presentation_proof(vp, expected_challenge=challenge)  # must not raise


async def test_presentation_rejected_when_no_holder_key_resolver_configured():
    """Same as before issue #42: a holder key absent from
    trusted_issuer_keys is rejected outright when holder_key_resolver
    isn't configured — the new parameter is strictly additive."""
    keystore = InMemoryIdentityKeystore()
    agent_id = "agent-no-resolver"
    key, pub, document = _bootstrap_agent(keystore, agent_id)
    vm_id = document.verification_method[0].id

    from stls_identity import NetworkAuthorityCredential, IdentityManager, CredentialVerificationError

    class _InMemoryStatusListManager:
        async def allocate_index(self):
            return 0
        async def revoke(self, index):
            pass
        async def is_revoked(self, index):
            return False

    issuer = IdentityManager(
        governing_authority_did=AUTHORITY_DID,
        signing_key=key, signing_verification_method_id=vm_id,
        status_list_manager=_InMemoryStatusListManager(),
        status_list_publish_url="https://sofia.sparkle.example.com/identity/status-list",
    )
    credential: NetworkAuthorityCredential = await issuer.issue_identity_credential(
        agent_did=document.id, client_id=agent_id,
    )
    challenge = str(uuid.uuid4())
    vp = build_verifiable_presentation(
        holder_did=document.id, holder_signing_key=key,
        holder_verification_method_id=vm_id, credentials=[credential],
        challenge=challenge,
    )

    sentinel = Sentinel(trusted_issuer_keys={})  # no holder_key_resolver
    with pytest.raises(CredentialVerificationError):
        await sentinel.verify_presentation_proof(vp, expected_challenge=challenge)


# ---------------------------------------------------------------------------
# rotate_signing_key() — full orchestration
# ---------------------------------------------------------------------------

async def test_rotate_signing_key_full_orchestration():
    keystore = InMemoryIdentityKeystore()
    did_store = _FakeDIDDocumentStore()
    agent_id = "agent-full-orchestration"

    old_key, _, document = _bootstrap_agent(keystore, agent_id)
    await did_store.put(agent_id, document)
    old_vm_id = document.verification_method[0].id

    new_key, updated_document = await rotate_signing_key(agent_id, keystore, did_store)

    assert keystore.get(agent_id).public_key().public_bytes_raw() == new_key.public_key().public_bytes_raw()
    stored = await did_store.get(agent_id)
    assert stored.id == updated_document.id
    current = current_verification_method(stored)
    assert current.id != old_vm_id
    old_entry = next(vm for vm in stored.verification_method if vm.id == old_vm_id)
    assert old_entry.valid_until is not None


async def test_rotate_signing_key_raises_for_agent_with_no_did_document():
    keystore = InMemoryIdentityKeystore()
    did_store = _FakeDIDDocumentStore()
    with pytest.raises(ValueError):
        await rotate_signing_key("never-bootstrapped", keystore, did_store)
