"""
W185 alignment fixes — Fase 1.

Covers the 4 fixes agreed in the response to the colleagues' review:
    1. verificationMethod.controller = agent's own DID, not the Authority's
    2. proof.type = DataIntegrityProof / cryptosuite = eddsa-jcs-2022
    3. schemaVersion, principalRef, keyStorageTier as declarative metadata
    4. roles + Grantor authorization check (covered in test_delegation.py,
       not repeated here)
"""

import datetime as dt

from stls_identity import (
    IdentityManager,
    InMemoryIdentityKeystore,
    KeyStorageTier,
    bootstrap_signing_key,
    build_did_document,
    build_verifiable_presentation,
)


class _InMemoryStatusListManager:
    def __init__(self):
        self._next = 0
        self._revoked = set()

    async def allocate_index(self):
        idx = self._next
        self._next += 1
        return idx

    async def revoke(self, index):
        self._revoked.add(index)

    async def is_revoked(self, index):
        return index in self._revoked


async def _noop_audit_hook(entry):
    pass


# ---------------------------------------------------------------------------
# Fix 1 — verificationMethod.controller vs DIDDocument.controller
# ---------------------------------------------------------------------------

def test_verification_method_controller_is_the_agent_not_the_authority():
    keystore = InMemoryIdentityKeystore()
    _, pub, _ = bootstrap_signing_key("agent-042", keystore)
    doc = build_did_document(
        host="sofia.sparkle.example.com", agent_path_segments=["agents", "042"],
        governing_authority_did="did:web:sofia.sparkle.example.com",
        signing_public_key=pub,
    )

    assert doc.controller == "did:web:sofia.sparkle.example.com"
    assert doc.verification_method[0].controller == doc.id
    assert doc.verification_method[0].controller != doc.controller, (
        "regression: verificationMethod.controller must NOT equal the "
        "Governing Authority DID — that was the bug this fix closed"
    )


# ---------------------------------------------------------------------------
# Fix 2 — proof.type / cryptosuite
# ---------------------------------------------------------------------------

async def test_credential_proof_uses_data_integrity_proof_type():
    keystore = InMemoryIdentityKeystore()
    authority_key, authority_pub, _ = bootstrap_signing_key("authority", keystore)
    _, agent_pub, _ = bootstrap_signing_key("agent-042", keystore)
    doc = build_did_document(
        host="sofia.sparkle.example.com", agent_path_segments=["agents", "042"],
        governing_authority_did="did:web:sofia.sparkle.example.com",
        signing_public_key=agent_pub,
    )

    issuer = IdentityManager(
        governing_authority_did="did:web:sofia.sparkle.example.com",
        signing_key=authority_key,
        signing_verification_method_id="did:web:sofia.sparkle.example.com#key-1",
        status_list_manager=_InMemoryStatusListManager(),
        status_list_publish_url="https://sofia.sparkle.example.com/identity/status-list",
        audit_hook=_noop_audit_hook,
    )
    credential = await issuer.issue_identity_credential(agent_did=doc.id, client_id="agent-042")

    assert credential.proof.type == "DataIntegrityProof"
    assert credential.proof.cryptosuite == "eddsa-jcs-2022"

    payload = credential.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert payload["proof"]["type"] == "DataIntegrityProof"
    assert payload["proof"]["cryptosuite"] == "eddsa-jcs-2022"
    assert "Ed25519Signature2020" not in str(payload)


async def test_presentation_proof_uses_data_integrity_proof_type():
    keystore = InMemoryIdentityKeystore()
    key, pub, _ = bootstrap_signing_key("agent-042", keystore)
    doc = build_did_document(
        host="sofia.sparkle.example.com", agent_path_segments=["agents", "042"],
        governing_authority_did="did:web:sofia.sparkle.example.com",
        signing_public_key=pub,
    )

    authority_key, authority_pub, _ = bootstrap_signing_key("authority", keystore)
    issuer = IdentityManager(
        governing_authority_did="did:web:sofia.sparkle.example.com",
        signing_key=authority_key,
        signing_verification_method_id="did:web:sofia.sparkle.example.com#key-1",
        status_list_manager=_InMemoryStatusListManager(),
        status_list_publish_url="https://sofia.sparkle.example.com/identity/status-list",
        audit_hook=_noop_audit_hook,
    )
    credential = await issuer.issue_identity_credential(agent_did=doc.id, client_id="agent-042")

    vp = build_verifiable_presentation(
        holder_did=doc.id, holder_signing_key=key,
        holder_verification_method_id=f"{doc.id}#key-1",
        credentials=[credential], challenge="test-challenge",
    )

    assert vp.proof.type == "DataIntegrityProof"
    assert vp.proof.cryptosuite == "eddsa-jcs-2022"


# ---------------------------------------------------------------------------
# Fix 3 — schemaVersion, principalRef, keyStorageTier
# ---------------------------------------------------------------------------

async def test_schema_version_present_on_identity_credential():
    keystore = InMemoryIdentityKeystore()
    authority_key, authority_pub, _ = bootstrap_signing_key("authority", keystore)
    _, agent_pub, _ = bootstrap_signing_key("agent-042", keystore)
    doc = build_did_document(
        host="sofia.sparkle.example.com", agent_path_segments=["agents", "042"],
        governing_authority_did="did:web:sofia.sparkle.example.com",
        signing_public_key=agent_pub,
    )
    issuer = IdentityManager(
        governing_authority_did="did:web:sofia.sparkle.example.com",
        signing_key=authority_key,
        signing_verification_method_id="did:web:sofia.sparkle.example.com#key-1",
        status_list_manager=_InMemoryStatusListManager(),
        status_list_publish_url="https://sofia.sparkle.example.com/identity/status-list",
        audit_hook=_noop_audit_hook,
    )
    credential = await issuer.issue_identity_credential(agent_did=doc.id, client_id="agent-042")

    assert credential.schema_version == "1.0"
    payload = credential.model_dump(mode="json", by_alias=True)
    assert payload["schemaVersion"] == "1.0"


async def test_principal_ref_and_key_storage_tier_default_and_override():
    keystore = InMemoryIdentityKeystore()
    authority_key, authority_pub, _ = bootstrap_signing_key("authority", keystore)
    _, agent_pub, _ = bootstrap_signing_key("agent-099", keystore)
    doc = build_did_document(
        host="sofia.sparkle.example.com", agent_path_segments=["agents", "099"],
        governing_authority_did="did:web:sofia.sparkle.example.com",
        signing_public_key=agent_pub,
    )
    issuer = IdentityManager(
        governing_authority_did="did:web:sofia.sparkle.example.com",
        signing_key=authority_key,
        signing_verification_method_id="did:web:sofia.sparkle.example.com#key-1",
        status_list_manager=_InMemoryStatusListManager(),
        status_list_publish_url="https://sofia.sparkle.example.com/identity/status-list",
        audit_hook=_noop_audit_hook,
    )

    # Default: no principalRef, TIER_1_SOFTWARE (the honest default — see
    # KeyStorageTier's docstring, every backend shipped today is software-only)
    default_credential = await issuer.issue_identity_credential(
        agent_did=doc.id, client_id="agent-099",
    )
    assert default_credential.credential_subject.principal_ref is None
    assert default_credential.credential_subject.key_storage_tier == KeyStorageTier.tier_1_software

    # Explicit override
    with_principal = await issuer.issue_identity_credential(
        agent_did=doc.id, client_id="agent-099",
        principal_ref="org:sparkle:bda",
        roles=["grantor"],
    )
    assert with_principal.credential_subject.principal_ref == "org:sparkle:bda"
    assert with_principal.credential_subject.roles == ["grantor"]

    payload = with_principal.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert payload["credentialSubject"]["principalRef"] == "org:sparkle:bda"
    assert payload["credentialSubject"]["keyStorageTier"] == "TIER_1_SOFTWARE"
    assert payload["credentialSubject"]["roles"] == ["grantor"]