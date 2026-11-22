"""
OperationalCredentialCache + load_delegation_presenter() — issue #54:
mirrors test_identity_credential_cache_and_loader.py (issue #45)
exactly, but for a Requestor presenting a received delegation
(Operational Credential + the Grantor's own Identity Credential)
instead of its own plain identity.
"""

import base64
import datetime as dt
import json
import time

import pytest

from stls_identity import (
    CredentialStatus,
    CredentialSubject,
    IdentityManager,
    InMemoryIdentityKeystore,
    InMemoryOperationalCredentialCache,
    NetworkAuthorityCredential,
    OperationalCredential,
    OperationalCredentialCacheFactory,
    OperationalCredentialSubject,
    Sentinel,
    VerifiablePresentation,
    bootstrap_signing_key,
    build_did_document,
    load_delegation_presenter,
)
from stls_identity.stls_id_delegation import DelegationManager


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


def _make_operational_credential(*, valid_until=None) -> OperationalCredential:
    now = dt.datetime.now(dt.timezone.utc)
    return OperationalCredential(
        id="urn:uuid:opcred-1", issuer="did:web:ga.example:agents:grantor-1",
        valid_from=now, valid_until=valid_until,
        credential_subject=OperationalCredentialSubject(
            id="did:web:ga.example:agents:requestor-1", client_id="requestor-1",
            scope={"action": "demo"},
        ),
        credential_status=CredentialStatus(id="https://x/status#1", status_list_index=1, status_list_credential="https://x/status"),
    )


# ---------------------------------------------------------------------------
# OperationalCredentialCache (in-memory backend — real TTL/expiry/collection logic)
# ---------------------------------------------------------------------------

def test_in_memory_cache_roundtrips():
    cache = InMemoryOperationalCredentialCache()
    credential = _make_operational_credential(valid_until=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))

    assert cache.get("requestor-1", "urn:uuid:opcred-1") is None
    cache.set("requestor-1", credential)
    assert cache.get("requestor-1", "urn:uuid:opcred-1").id == credential.id


def test_in_memory_cache_rejects_already_expired_credential():
    cache = InMemoryOperationalCredentialCache()
    expired = _make_operational_credential(valid_until=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1))

    cache.set("requestor-1", expired)
    assert cache.get("requestor-1", "urn:uuid:opcred-1") is None  # never cached in the first place


def test_in_memory_cache_expires_on_read_after_ttl():
    cache = InMemoryOperationalCredentialCache()
    credential = _make_operational_credential(valid_until=dt.datetime.now(dt.timezone.utc) + dt.timedelta(milliseconds=1))
    cache.set("requestor-1", credential)

    time.sleep(0.05)
    assert cache.get("requestor-1", "urn:uuid:opcred-1") is None


def test_in_memory_cache_is_a_collection_per_agent():
    """An agent can hold several delegations at once, from different
    Grantors — the whole reason this is a collection, not a single
    slot like IdentityCredentialCacheBase."""
    cache = InMemoryOperationalCredentialCache()
    valid_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)

    first = _make_operational_credential(valid_until=valid_until)
    second = _make_operational_credential(valid_until=valid_until)
    second.id = "urn:uuid:opcred-2"
    second.issuer = "did:web:ga.example:agents:grantor-2"

    cache.set("requestor-1", first)
    cache.set("requestor-1", second)

    assert cache.list_ids("requestor-1") == ["urn:uuid:opcred-1", "urn:uuid:opcred-2"]
    assert cache.get("requestor-1", "urn:uuid:opcred-1").issuer == "did:web:ga.example:agents:grantor-1"
    assert cache.get("requestor-1", "urn:uuid:opcred-2").issuer == "did:web:ga.example:agents:grantor-2"


def test_in_memory_cache_delete_removes_one_entry_only():
    cache = InMemoryOperationalCredentialCache()
    valid_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    first = _make_operational_credential(valid_until=valid_until)
    second = _make_operational_credential(valid_until=valid_until)
    second.id = "urn:uuid:opcred-2"
    cache.set("requestor-1", first)
    cache.set("requestor-1", second)

    cache.delete("requestor-1", "urn:uuid:opcred-1")

    assert cache.get("requestor-1", "urn:uuid:opcred-1") is None
    assert cache.get("requestor-1", "urn:uuid:opcred-2") is not None


def test_factory_defaults_to_memory_backend(monkeypatch):
    monkeypatch.delenv("OPERATIONAL_CREDENTIAL_CACHE_BACKEND", raising=False)
    cache = OperationalCredentialCacheFactory.build()
    assert isinstance(cache, InMemoryOperationalCredentialCache)


def test_factory_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv("OPERATIONAL_CREDENTIAL_CACHE_BACKEND", "nonsense")
    with pytest.raises(ValueError, match="not recognized"):
        OperationalCredentialCacheFactory.build()


# ---------------------------------------------------------------------------
# load_delegation_presenter() — full real-crypto round trip, including
# server-side verify_delegation_chain() via Sentinel.unpack_presentation()
# ---------------------------------------------------------------------------

async def test_load_delegation_presenter_returns_none_without_key(monkeypatch):
    monkeypatch.setenv("IDENTITY_KEYSTORE_BACKEND", "memory")
    monkeypatch.setenv("OPERATIONAL_CREDENTIAL_CACHE_BACKEND", "memory")
    now = dt.datetime.now(dt.timezone.utc)
    grantor_cred = NetworkAuthorityCredential(  # never actually read: keystore.get() returns None first
        id="urn:uuid:dummy", issuer="did:web:x.example", valid_from=now,
        credential_subject=CredentialSubject(id="did:web:x", client_id="grantor-1"),
        credential_status=CredentialStatus(id="https://x/status#0", status_list_index=0, status_list_credential="https://x/status"),
    )
    assert load_delegation_presenter(
        "never-bootstrapped", "urn:uuid:opcred-1", grantor_cred,
        governing_authority_host="x.example.com",
    ) is None


async def test_load_delegation_presenter_returns_none_without_cached_credential(monkeypatch):
    keystore = InMemoryIdentityKeystore()
    cache = InMemoryOperationalCredentialCache()
    monkeypatch.setattr(
        "stls_identity.stls_id_presentation.IdentityKeystoreFactory.build",
        staticmethod(lambda: keystore),
    )
    monkeypatch.setattr(
        "stls_identity.stls_id_presentation.OperationalCredentialCacheFactory.build",
        staticmethod(lambda: cache),
    )
    bootstrap_signing_key("requestor-1", keystore)

    now = dt.datetime.now(dt.timezone.utc)
    dummy_grantor_cred = NetworkAuthorityCredential(
        id="urn:uuid:dummy", issuer="did:web:ga.example", valid_from=now,
        credential_subject=CredentialSubject(id="did:web:x", client_id="grantor-1"),
        credential_status=CredentialStatus(id="https://x/status#0", status_list_index=0, status_list_credential="https://x/status"),
    )
    assert load_delegation_presenter(
        "requestor-1", "urn:uuid:never-cached", dummy_grantor_cred,
        governing_authority_host="ga.example.com",
    ) is None


async def _noop_audit_hook(entry):
    pass


async def test_load_delegation_presenter_full_roundtrip_verifies_server_side(monkeypatch):
    keystore = InMemoryIdentityKeystore()
    op_cache = InMemoryOperationalCredentialCache()
    monkeypatch.setattr(
        "stls_identity.stls_id_presentation.IdentityKeystoreFactory.build",
        staticmethod(lambda: keystore),
    )
    monkeypatch.setattr(
        "stls_identity.stls_id_presentation.OperationalCredentialCacheFactory.build",
        staticmethod(lambda: op_cache),
    )

    host = "ga.example.com"
    authority_did = "did:web:ga.example.com"
    authority_key, authority_pub, _ = bootstrap_signing_key("ga", keystore)
    authority_vm_id = f"{authority_did}#key-1"

    issuer = IdentityManager(
        governing_authority_did=authority_did, signing_key=authority_key,
        signing_verification_method_id=authority_vm_id,
        status_list_manager=_InMemoryStatusListManager(),
        status_list_publish_url="https://ga.example.com/identity/status-list",
        audit_hook=_noop_audit_hook,
    )

    # Grantor: bootstrapped, holds a real, Authority-signed NetworkAuthorityCredential with the "grantor" role.
    _, grantor_pub, _ = bootstrap_signing_key("grantor-1", keystore)
    grantor_doc = build_did_document(
        host=host, agent_path_segments=["agents", "grantor-1"],
        governing_authority_did=authority_did, signing_public_key=grantor_pub,
    )
    grantor_identity_credential = await issuer.issue_identity_credential(
        agent_did=grantor_doc.id, client_id="grantor-1", roles=["grantor"],
    )
    now = dt.datetime.now(dt.timezone.utc)

    # Requestor: bootstrapped, receives a real Operational Credential from the Grantor.
    _, requestor_pub, _ = bootstrap_signing_key("requestor-1", keystore)
    requestor_doc = build_did_document(
        host=host, agent_path_segments=["agents", "requestor-1"],
        governing_authority_did=authority_did, signing_public_key=requestor_pub,
    )

    grantor_key = keystore.get("grantor-1")
    delegation_manager = DelegationManager(
        grantor_did=grantor_doc.id, grantor_signing_key=grantor_key,
        grantor_verification_method_id=f"{grantor_doc.id}#key-1",
        status_list_manager=_InMemoryStatusListManager(),
        status_list_publish_url="https://ga.example.com/opstatus",
    )
    operational_credential = await delegation_manager.issue_operational_credential(
        grantor_identity_credential=grantor_identity_credential,
        requestor_did=requestor_doc.id, requestor_client_id="requestor-1",
        scope={"action": "demo-scope"},
    )
    op_cache.set("requestor-1", operational_credential)

    # The actual primitive under test.
    presenter = load_delegation_presenter(
        "requestor-1", operational_credential.id, grantor_identity_credential,
        governing_authority_host=host,
    )
    assert presenter is not None

    challenge = "chal-456"
    headers = presenter.on_challenge(challenge)
    assert list(headers.keys()) == ["X-STLS-Identity-Presentation"]

    vp = VerifiablePresentation.model_validate(json.loads(base64.b64decode(headers["X-STLS-Identity-Presentation"])))

    sentinel = Sentinel(trusted_issuer_keys={
        authority_vm_id: authority_pub,
        f"{grantor_doc.id}#key-1": grantor_pub,
        f"{requestor_doc.id}#key-1": requestor_pub,  # holder binding checks the Requestor's own key too
    })
    sentinel.verify_presentation_proof(vp, expected_challenge=challenge)  # must not raise: holder binding

    shape = sentinel.unpack_presentation(vp)
    assert len(shape) == 2  # this is the delegated-call shape, not plain identity
    unpacked_operational, unpacked_grantor_identity = shape

    await sentinel.verify_delegation_chain(
        unpacked_operational,
        grantor_identity_credential=unpacked_grantor_identity,
        grantor_identity_fetch_time=now, grantor_identity_is_revoked=False,
        operational_fetch_time=now, operational_is_revoked=False,
    )  # must not raise: the whole point of this primitive
