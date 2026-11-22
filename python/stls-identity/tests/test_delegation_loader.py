"""
load_delegation_manager() — issue #51's Grantor-agent follow-up:
mirrors load_identity_presenter() (issue #45) exactly, but returns a
ready-to-use DelegationManager instead of an IdentityPresenter, for a
real Grantor agent that needs to act as a Grantor at startup rather
than just present its own identity.
"""

import datetime as dt

import pytest

from stls_identity import (
    CredentialStatus,
    CredentialSubject,
    InMemoryIdentityCredentialCache,
    InMemoryIdentityKeystore,
    NetworkAuthorityCredential,
    bootstrap_signing_key,
    build_did_document,
    load_delegation_manager,
)
from stls_identity.stls_id_issuer import IdentityManager


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


def _make_credential(*, roles: list[str]) -> NetworkAuthorityCredential:
    now = dt.datetime.now(dt.timezone.utc)
    return NetworkAuthorityCredential(
        id="urn:uuid:grantor-cred", issuer="did:web:ga.example.com", valid_from=now,
        valid_until=now + dt.timedelta(days=90),
        credential_subject=CredentialSubject(id="did:web:ga.example.com:agents:grantor-1", client_id="grantor-1", roles=roles),
        credential_status=CredentialStatus(id="https://x/status#0", status_list_index=0, status_list_credential="https://x/status"),
    )


@pytest.fixture
def _memory_factories(monkeypatch):
    keystore = InMemoryIdentityKeystore()
    cache = InMemoryIdentityCredentialCache()
    monkeypatch.setattr(
        "stls_identity.stls_id_delegation.IdentityKeystoreFactory.build",
        staticmethod(lambda: keystore),
    )
    monkeypatch.setattr(
        "stls_identity.stls_id_delegation.IdentityCredentialCacheFactory.build",
        staticmethod(lambda: cache),
    )
    return keystore, cache


async def test_returns_none_without_a_key(_memory_factories):
    assert load_delegation_manager(
        "never-bootstrapped", governing_authority_host="x.example.com",
        status_list_manager=_InMemoryStatusListManager(),
        status_list_publish_url="https://x/status",
    ) is None


async def test_returns_none_without_a_cached_credential(_memory_factories):
    keystore, _ = _memory_factories
    bootstrap_signing_key("grantor-1", keystore)

    assert load_delegation_manager(
        "grantor-1", governing_authority_host="x.example.com",
        status_list_manager=_InMemoryStatusListManager(),
        status_list_publish_url="https://x/status",
    ) is None


async def test_returns_none_without_the_grantor_role(_memory_factories):
    keystore, cache = _memory_factories
    bootstrap_signing_key("grantor-1", keystore)
    cache.set("grantor-1", _make_credential(roles=[]))  # identity, but not a grantor

    assert load_delegation_manager(
        "grantor-1", governing_authority_host="x.example.com",
        status_list_manager=_InMemoryStatusListManager(),
        status_list_publish_url="https://x/status",
    ) is None


async def test_full_roundtrip_issues_a_real_operational_credential(_memory_factories):
    keystore, cache = _memory_factories
    _, grantor_pub, _ = bootstrap_signing_key("grantor-1", keystore)
    credential = _make_credential(roles=["grantor"])
    cache.set("grantor-1", credential)

    manager = load_delegation_manager(
        "grantor-1", governing_authority_host="ga.example.com",
        status_list_manager=_InMemoryStatusListManager(),
        status_list_publish_url="https://ga.example.com/identity/status-list",
    )
    assert manager is not None

    op_credential = await manager.issue_operational_credential(
        grantor_identity_credential=credential,
        requestor_did="did:web:ga.example.com:agents:requestor-1",
        requestor_client_id="requestor-1",
        scope={"action": "cross-connect-activate", "facility_code": "MI1"},
    )
    assert op_credential.issuer == "did:web:ga.example.com:agents:grantor-1"
    assert op_credential.proof is not None
    assert op_credential.credential_subject.scope == {"action": "cross-connect-activate", "facility_code": "MI1"}
