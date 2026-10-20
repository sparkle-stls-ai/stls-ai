"""
Phase 3 end-to-end: Grantor -> Requestor delegation.

Covers, against real crypto (not mocks):
    - the happy path: Authority issues identity to Grantor, Grantor
      delegates to Requestor, Sentinel verifies the full chain
    - revoking the Grantor's IDENTITY credential fails the chain, even
      though the OperationalCredential itself is untouched — this is
      the property that makes revoking a Grantor's standing actually
      matter, not just a documentation claim
    - revoking the OPERATIONAL credential directly also fails the chain
    - the depth-1 limit is enforced at runtime (TypeError), not just by
      convention — passing an OperationalCredential where a
      NetworkAuthorityCredential is required is rejected
    - a Grantor cannot delegate using an identity credential that isn't
      its own (subject mismatch)
    - tamper check on the OperationalCredential's signature
"""

import datetime as dt

import pytest

from stls_identity import (
    CredentialVerificationError,
    DelegationManager,
    IdentityManager,
    InMemoryIdentityKeystore,
    Sentinel,
    bootstrap_signing_key,
    build_did_document,
)


class _InMemoryStatusListManager:
    """Same stand-in used by test_e2e.py — one instance per authority_id
    would be more realistic (see delegation.py's docstring on why each
    Grantor gets its own status list), so the test below instantiates
    one PER role (Governing Authority, Grantor) rather than sharing one."""

    def __init__(self):
        self._next = 0
        self._revoked: set[int] = set()

    async def allocate_index(self) -> int:
        idx = self._next
        self._next += 1
        return idx

    async def revoke(self, index: int) -> None:
        self._revoked.add(index)

    async def is_revoked(self, index: int) -> bool:
        return index in self._revoked


async def _noop_audit_hook(entry):
    pass


class _Fixture:
    """Bundles everything a delegation test needs: an Authority that has
    issued an identity credential to a Grantor, and a DelegationManager
    ready to issue OperationalCredentials to a Requestor."""

    def __init__(self):
        self.keystore = InMemoryIdentityKeystore()
        self.authority_did = "did:web:sofia.sparkle.example.com"
        self.authority_vm_id = f"{self.authority_did}#key-1"
        self.authority_status = _InMemoryStatusListManager()

        self.grantor_status = _InMemoryStatusListManager()

        self.sentinel: Sentinel | None = None  # built once all keys are known

    async def setup(self):
        authority_key, authority_pub, _ = bootstrap_signing_key(
            "governing-authority", self.keystore
        )
        self.authority_key, self.authority_pub = authority_key, authority_pub

        grantor_key, grantor_pub, _ = bootstrap_signing_key("grantor-agent", self.keystore)
        grantor_doc = build_did_document(
            host="sofia.sparkle.example.com",
            agent_path_segments=["agents", "grantor"],
            governing_authority_did=self.authority_did,
            signing_public_key=grantor_pub,
        )
        self.grantor_did = grantor_doc.id
        self.grantor_vm_id = f"{self.grantor_did}#key-1"
        self.grantor_key, self.grantor_pub = grantor_key, grantor_pub

        _, requestor_pub, _ = bootstrap_signing_key("requestor-agent", self.keystore)
        requestor_doc = build_did_document(
            host="sofia.sparkle.example.com",
            agent_path_segments=["agents", "requestor"],
            governing_authority_did=self.authority_did,
            signing_public_key=requestor_pub,
        )
        self.requestor_did = requestor_doc.id

        self.issuer = IdentityManager(
            governing_authority_did=self.authority_did,
            signing_key=self.authority_key,
            signing_verification_method_id=self.authority_vm_id,
            status_list_manager=self.authority_status,
            status_list_publish_url="https://sofia.sparkle.example.com/identity/status-list",
            audit_hook=_noop_audit_hook,
        )

        self.grantor_identity_credential = await self.issuer.issue_identity_credential(
            agent_did=self.grantor_did,
            client_id="grantor-agent",
            roles=["grantor"],
        )

        self.delegation = DelegationManager(
            grantor_did=self.grantor_did,
            grantor_signing_key=self.grantor_key,
            grantor_verification_method_id=self.grantor_vm_id,
            status_list_manager=self.grantor_status,
            status_list_publish_url="https://sofia.sparkle.example.com/agents/grantor/status-list",
            audit_hook=_noop_audit_hook,
        )

        self.sentinel = Sentinel(trusted_issuer_keys={
            self.authority_vm_id: self.authority_pub,
            self.grantor_vm_id: self.grantor_pub,
        })
        return self


async def _fixture() -> _Fixture:
    return await _Fixture().setup()


ELOA_SCOPE = {
    "action": "cross-connect-activate",
    "facility_code": "MI1",
    "port_id": "42",
}


async def test_delegation_chain_verifies_end_to_end():
    f = await _fixture()

    op_cred = await f.delegation.issue_operational_credential(
        grantor_identity_credential=f.grantor_identity_credential,
        requestor_did=f.requestor_did,
        requestor_client_id="requestor-agent",
        scope=ELOA_SCOPE,
    )

    assert op_cred.delegation_depth == 1
    assert op_cred.credential_subject.scope == ELOA_SCOPE
    assert op_cred.issuer == f.grantor_did

    now = dt.datetime.now(dt.timezone.utc)
    # Must not raise.
    await f.sentinel.verify_delegation_chain(
        op_cred,
        grantor_identity_credential=f.grantor_identity_credential,
        grantor_identity_fetch_time=now,
        grantor_identity_is_revoked=await f.authority_status.is_revoked(
            f.grantor_identity_credential.credential_status.status_list_index
        ),
        operational_fetch_time=now,
        operational_is_revoked=await f.grantor_status.is_revoked(
            op_cred.credential_status.status_list_index
        ),
    )


async def test_revoking_grantor_identity_fails_the_chain_even_if_op_credential_untouched():
    f = await _fixture()

    op_cred = await f.delegation.issue_operational_credential(
        grantor_identity_credential=f.grantor_identity_credential,
        requestor_did=f.requestor_did,
        requestor_client_id="requestor-agent",
        scope=ELOA_SCOPE,
    )

    # Revoke the GRANTOR'S IDENTITY, not the operational credential.
    await f.issuer.revoke(f.grantor_identity_credential)

    now = dt.datetime.now(dt.timezone.utc)
    with pytest.raises(CredentialVerificationError, match="revoked"):
        await f.sentinel.verify_delegation_chain(
            op_cred,
            grantor_identity_credential=f.grantor_identity_credential,
            grantor_identity_fetch_time=now,
            grantor_identity_is_revoked=await f.authority_status.is_revoked(
                f.grantor_identity_credential.credential_status.status_list_index
            ),
            operational_fetch_time=now,
            operational_is_revoked=await f.grantor_status.is_revoked(
                op_cred.credential_status.status_list_index
            ),
        )


async def test_revoking_operational_credential_directly_fails_the_chain():
    f = await _fixture()

    op_cred = await f.delegation.issue_operational_credential(
        grantor_identity_credential=f.grantor_identity_credential,
        requestor_did=f.requestor_did,
        requestor_client_id="requestor-agent",
        scope=ELOA_SCOPE,
    )

    await f.delegation.revoke(op_cred)  # Grantor's identity stays valid

    now = dt.datetime.now(dt.timezone.utc)
    with pytest.raises(CredentialVerificationError, match="revoked"):
        await f.sentinel.verify_delegation_chain(
            op_cred,
            grantor_identity_credential=f.grantor_identity_credential,
            grantor_identity_fetch_time=now,
            grantor_identity_is_revoked=await f.authority_status.is_revoked(
                f.grantor_identity_credential.credential_status.status_list_index
            ),
            operational_fetch_time=now,
            operational_is_revoked=await f.grantor_status.is_revoked(
                op_cred.credential_status.status_list_index
            ),
        )


async def test_operational_credential_cannot_justify_further_delegation():
    """The depth-1 limit, enforced at runtime: an OperationalCredential
    passed where a NetworkAuthorityCredential is required is rejected
    by type, not silently accepted because a field happens to match."""
    f = await _fixture()

    op_cred = await f.delegation.issue_operational_credential(
        grantor_identity_credential=f.grantor_identity_credential,
        requestor_did=f.requestor_did,
        requestor_client_id="requestor-agent",
        scope=ELOA_SCOPE,
    )

    # Attempt to use the Requestor's newly-received OperationalCredential
    # as if it were an identity credential, to re-delegate further.
    would_be_sub_delegator = DelegationManager(
        grantor_did=f.requestor_did,
        grantor_signing_key=f.grantor_key,  # irrelevant to this check
        grantor_verification_method_id=f.grantor_vm_id,
        status_list_manager=_InMemoryStatusListManager(),
        status_list_publish_url="https://sofia.sparkle.example.com/agents/requestor/status-list",
        audit_hook=_noop_audit_hook,
    )

    with pytest.raises(TypeError, match="NetworkAuthorityCredential"):
        await would_be_sub_delegator.issue_operational_credential(
            grantor_identity_credential=op_cred,  # wrong type, on purpose
            requestor_did="did:web:sofia.sparkle.example.com:agents:third-party",
            requestor_client_id="third-party-agent",
            scope=ELOA_SCOPE,
        )


async def test_grantor_cannot_delegate_using_someone_elses_identity_credential():
    f = await _fixture()

    # A second, unrelated agent with its own valid identity credential.
    _, other_pub, _ = bootstrap_signing_key("other-agent", f.keystore)
    other_doc = build_did_document(
        host="sofia.sparkle.example.com",
        agent_path_segments=["agents", "other"],
        governing_authority_did=f.authority_did,
        signing_public_key=other_pub,
    )
    other_identity_credential = await f.issuer.issue_identity_credential(
        agent_did=other_doc.id,
        client_id="other-agent",
    )

    with pytest.raises(ValueError, match="does not belong to this Grantor"):
        await f.delegation.issue_operational_credential(
            grantor_identity_credential=other_identity_credential,  # not f.grantor's own
            requestor_did=f.requestor_did,
            requestor_client_id="requestor-agent",
            scope=ELOA_SCOPE,
        )


async def test_delegation_rejects_grantor_identity_without_grantor_role():
    """W185 alignment fix: holding a valid identity is not, by itself,
    authorization to delegate. An agent whose own NetworkAuthorityCredential
    does not declare the 'grantor' role must be rejected even though its
    identity is otherwise perfectly valid — same DID, same signature,
    same status list, everything else identical to the passing case."""
    f = await _fixture()

    unrelated_key, unrelated_pub, _ = bootstrap_signing_key("no-role-agent", f.keystore)
    unrelated_doc = build_did_document(
        host="sofia.sparkle.example.com", agent_path_segments=["agents", "norole"],
        governing_authority_did=f.authority_did, signing_public_key=unrelated_pub,
    )
    identity_without_role = await f.issuer.issue_identity_credential(
        agent_did=unrelated_doc.id, client_id="no-role-agent",
        # roles intentionally omitted — defaults to []
    )

    would_be_grantor = DelegationManager(
        grantor_did=unrelated_doc.id, grantor_signing_key=unrelated_key,
        grantor_verification_method_id=f"{unrelated_doc.id}#key-1",
        status_list_manager=_InMemoryStatusListManager(),
        status_list_publish_url="https://sofia.sparkle.example.com/agents/norole/status-list",
        audit_hook=_noop_audit_hook,
    )

    with pytest.raises(ValueError, match="does not declare the 'grantor' role"):
        await would_be_grantor.issue_operational_credential(
            grantor_identity_credential=identity_without_role,
            requestor_did=f.requestor_did,
            requestor_client_id="requestor-agent",
            scope=ELOA_SCOPE,
        )


async def test_delegation_rejects_expired_identity_credential():
    f = await _fixture()

    expired = f.grantor_identity_credential.model_copy(update={
        "valid_until": dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1),
    })

    with pytest.raises(ValueError, match="validity window"):
        await f.delegation.issue_operational_credential(
            grantor_identity_credential=expired,
            requestor_did=f.requestor_did,
            requestor_client_id="requestor-agent",
            scope=ELOA_SCOPE,
        )


async def test_operational_credential_tamper_check():
    f = await _fixture()

    op_cred = await f.delegation.issue_operational_credential(
        grantor_identity_credential=f.grantor_identity_credential,
        requestor_did=f.requestor_did,
        requestor_client_id="requestor-agent",
        scope=ELOA_SCOPE,
    )

    tampered = op_cred.model_copy(deep=True)
    tampered.credential_subject.scope["port_id"] = "999"  # widen the grant post-hoc

    with pytest.raises(CredentialVerificationError, match="signature"):
        await f.sentinel.verify_signature(tampered)