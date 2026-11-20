"""
IdentityCredentialCache + load_identity_presenter() +
identity_challenge_handler_from_env() — issue #45's follow-up:
generalizing the per-agent "load my own identity as a presenter"
pattern first written ad hoc for samples/server/sofia-admin's
coordinator into a reusable package primitive.
"""

import datetime as dt

import pytest

from stls_identity import (
    CredentialStatus,
    CredentialSubject,
    IdentityCredentialCacheFactory,
    InMemoryIdentityCredentialCache,
    InMemoryIdentityKeystore,
    NetworkAuthorityCredential,
    Sentinel,
    bootstrap_signing_key,
    build_did_document,
    identity_challenge_handler_from_env,
    load_identity_presenter,
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


def _make_credential(*, valid_until=None) -> NetworkAuthorityCredential:
    now = dt.datetime.now(dt.timezone.utc)
    return NetworkAuthorityCredential(
        id="urn:uuid:1", issuer="did:web:ga.example", valid_from=now, valid_until=valid_until,
        credential_subject=CredentialSubject(id="did:web:agent", client_id="agent-1"),
        credential_status=CredentialStatus(id="https://x/status#0", status_list_index=0, status_list_credential="https://x/status"),
    )


# ---------------------------------------------------------------------------
# IdentityCredentialCache (in-memory backend — real TTL/expiry logic)
# ---------------------------------------------------------------------------

def test_in_memory_cache_roundtrips():
    cache = InMemoryIdentityCredentialCache()
    credential = _make_credential(valid_until=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))

    assert cache.get("agent-1") is None
    cache.set("agent-1", credential)
    assert cache.get("agent-1").id == credential.id


def test_in_memory_cache_rejects_already_expired_credential():
    cache = InMemoryIdentityCredentialCache()
    expired = _make_credential(valid_until=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1))

    cache.set("agent-1", expired)
    assert cache.get("agent-1") is None  # never cached in the first place


def test_in_memory_cache_expires_on_read_after_ttl():
    cache = InMemoryIdentityCredentialCache()
    credential = _make_credential(valid_until=dt.datetime.now(dt.timezone.utc) + dt.timedelta(milliseconds=1))
    cache.set("agent-1", credential)

    import time
    time.sleep(0.05)
    assert cache.get("agent-1") is None


def test_factory_defaults_to_memory_backend(monkeypatch):
    monkeypatch.delenv("IDENTITY_CREDENTIAL_CACHE_BACKEND", raising=False)
    cache = IdentityCredentialCacheFactory.build()
    assert isinstance(cache, InMemoryIdentityCredentialCache)


def test_factory_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv("IDENTITY_CREDENTIAL_CACHE_BACKEND", "nonsense")
    with pytest.raises(ValueError, match="not recognized"):
        IdentityCredentialCacheFactory.build()


# ---------------------------------------------------------------------------
# load_identity_presenter() — full real-crypto round trip
# ---------------------------------------------------------------------------

async def _bootstrap_and_issue(keystore, credential_cache, *, agent_id: str, host: str):
    """Mirrors bootstrap_agent_identity + issue_identity_credential_for_agent
    (tools_identity.py) at the primitive level, writing to the SAME two
    stores load_identity_presenter() reads from."""
    authority_did = "did:web:ga.example.com"
    authority_key, authority_pub, _ = bootstrap_signing_key("ga", keystore)
    authority_vm_id = f"{authority_did}#key-1"

    _, agent_pub, _ = bootstrap_signing_key(agent_id, keystore)
    doc = build_did_document(
        host=host, agent_path_segments=["agents", agent_id],
        governing_authority_did=authority_did, signing_public_key=agent_pub,
    )

    issuer = IdentityManager(
        governing_authority_did=authority_did, signing_key=authority_key,
        signing_verification_method_id=authority_vm_id,
        status_list_manager=_InMemoryStatusListManager(),
        status_list_publish_url="https://ga.example.com/identity/status-list",
        audit_hook=_noop_audit_hook,
    )
    credential = await issuer.issue_identity_credential(agent_did=doc.id, client_id=agent_id, roles=[])
    credential_cache.set(agent_id, credential)

    return Sentinel(trusted_issuer_keys={authority_vm_id: authority_pub, f"{doc.id}#key-1": agent_pub})


async def test_load_identity_presenter_returns_none_without_key(monkeypatch):
    monkeypatch.setenv("IDENTITY_KEYSTORE_BACKEND", "memory")
    monkeypatch.setenv("IDENTITY_CREDENTIAL_CACHE_BACKEND", "memory")
    assert load_identity_presenter("never-bootstrapped", governing_authority_host="x.example.com") is None


async def test_load_identity_presenter_full_roundtrip(monkeypatch):
    # IdentityKeystoreFactory/IdentityCredentialCacheFactory build a FRESH
    # backend instance per call — for "memory", that means
    # load_identity_presenter() would see an EMPTY store, not the one
    # _bootstrap_and_issue() just populated. Exercise the exact same
    # concern test_request_verifier.py and the others already avoid by
    # using real Redis-backed integration elsewhere; here, swap in
    # shared instances directly to prove the LOGIC (DID computation,
    # presenter construction, full Sentinel verification), independent
    # of which backend is selected.
    keystore = InMemoryIdentityKeystore()
    cache = InMemoryIdentityCredentialCache()
    monkeypatch.setattr(
        "stls_identity.stls_id_presentation.IdentityKeystoreFactory.build",
        staticmethod(lambda: keystore),
    )
    monkeypatch.setattr(
        "stls_identity.stls_id_presentation.IdentityCredentialCacheFactory.build",
        staticmethod(lambda: cache),
    )

    host = "requestor.example.com"
    sentinel = await _bootstrap_and_issue(keystore, cache, agent_id="agent-42", host=host)

    presenter = load_identity_presenter("agent-42", governing_authority_host=host)
    assert presenter is not None

    challenge = "chal-123"
    headers = presenter.on_challenge(challenge)
    assert list(headers.keys()) == ["X-STLS-Identity-Presentation"]

    import base64
    import json
    from stls_identity import VerifiablePresentation
    vp = VerifiablePresentation.model_validate(json.loads(base64.b64decode(headers["X-STLS-Identity-Presentation"])))
    sentinel.verify_presentation_proof(vp, expected_challenge=challenge)  # must not raise


# ---------------------------------------------------------------------------
# identity_challenge_handler_from_env() — the single-env-var wiring
# ---------------------------------------------------------------------------

def test_handler_from_env_returns_none_when_agent_id_unset(monkeypatch):
    monkeypatch.delenv("IDENTITY_AGENT_ID", raising=False)
    assert identity_challenge_handler_from_env() is None


def test_handler_from_env_raises_when_host_missing(monkeypatch):
    monkeypatch.setenv("IDENTITY_AGENT_ID", "some-agent")
    monkeypatch.delenv("GOVERNING_AUTHORITY_HOST", raising=False)
    with pytest.raises(ValueError, match="GOVERNING_AUTHORITY_HOST"):
        identity_challenge_handler_from_env()


def test_handler_from_env_returns_none_when_agent_not_bootstrapped(monkeypatch):
    monkeypatch.setenv("IDENTITY_AGENT_ID", "ghost-agent")
    monkeypatch.setenv("GOVERNING_AUTHORITY_HOST", "x.example.com")
    monkeypatch.setenv("IDENTITY_KEYSTORE_BACKEND", "memory")
    monkeypatch.setenv("IDENTITY_CREDENTIAL_CACHE_BACKEND", "memory")
    assert identity_challenge_handler_from_env() is None
