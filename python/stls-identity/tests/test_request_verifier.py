"""
RequestPresentationVerifier (issue #15 gap #1) — the bridge between
Sentinel and stls-a2a's optional identity_verifier hook.

Does NOT re-litigate what test_presentation.py already proves at the
VP level (replay, tamper, wrong proof_purpose, holder-key substitution
— all exercised directly against Sentinel there). Here the focus is
the bridge itself: header encode/decode, the duck-typed result shape
stls-a2a expects, status_resolver wiring, and the two unpack shapes
(plain identity vs delegated) end to end through verify_request().
"""

import base64
import datetime as dt
import json
import uuid

from stls_identity import (
    DelegationManager,
    IdentityManager,
    InMemoryIdentityKeystore,
    PRESENTATION_HEADER,
    RequestPresentationVerifier,
    Sentinel,
    bootstrap_signing_key,
    build_did_document,
    build_verifiable_presentation,
    request_presentation_verifier_from_env,
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


class _FakeRequest:
    """Minimal stand-in for the ASGI request stls-a2a passes in — only
    `.headers.get()` is ever touched by RequestPresentationVerifier."""

    def __init__(self, headers: dict[str, str]):
        self.headers = headers


class _Scenario:
    """Same Authority -> Grantor 099 -> Requestor 042 scenario as
    test_presentation.py's _Scenario, reused here because the bridge
    sits one layer above exactly that flow."""

    async def setup(self):
        self.keystore = InMemoryIdentityKeystore()

        self.authority_did = "did:web:sofia.sparkle.example.com"
        self.authority_key, self.authority_pub, _ = bootstrap_signing_key(
            "sparkle-governing-authority", self.keystore
        )
        self.authority_vm_id = f"{self.authority_did}#key-1"

        self.grantor_key, self.grantor_pub, _ = bootstrap_signing_key(
            "sparkle-agent-099", self.keystore
        )
        self.grantor_doc = build_did_document(
            host="sofia.sparkle.example.com", agent_path_segments=["agents", "099"],
            governing_authority_did=self.authority_did, signing_public_key=self.grantor_pub,
        )
        self.grantor_vm_id = f"{self.grantor_doc.id}#key-1"

        self.requestor_key, self.requestor_pub, _ = bootstrap_signing_key("b-agent-042", self.keystore)
        self.requestor_doc = build_did_document(
            host="b.example.com", agent_path_segments=["agents", "042"],
            governing_authority_did="did:web:b.example.com", signing_public_key=self.requestor_pub,
        )
        self.requestor_vm_id = f"{self.requestor_doc.id}#key-1"

        self.issuer = IdentityManager(
            governing_authority_did=self.authority_did, signing_key=self.authority_key,
            signing_verification_method_id=self.authority_vm_id,
            status_list_manager=_InMemoryStatusListManager(),
            status_list_publish_url="https://sofia.sparkle.example.com/identity/status-list",
            audit_hook=_noop_audit_hook,
        )
        self.grantor_identity_credential = await self.issuer.issue_identity_credential(
            agent_did=self.grantor_doc.id, client_id="sparkle-agent-099",
            roles=["grantor"],
        )

        self.delegation = DelegationManager(
            grantor_did=self.grantor_doc.id, grantor_signing_key=self.grantor_key,
            grantor_verification_method_id=self.grantor_vm_id,
            status_list_manager=_InMemoryStatusListManager(),
            status_list_publish_url="https://sofia.sparkle.example.com/agents/099/status-list",
            audit_hook=_noop_audit_hook,
        )
        self.operational_credential = await self.delegation.issue_operational_credential(
            grantor_identity_credential=self.grantor_identity_credential,
            requestor_did=self.requestor_doc.id, requestor_client_id="b-agent-042",
            scope={"action": "invoke-workflow", "resource": "sparkle-agent-007"},
        )

        self.sentinel = Sentinel(trusted_issuer_keys={
            self.authority_vm_id: self.authority_pub,
            self.grantor_vm_id: self.grantor_pub,
            self.requestor_vm_id: self.requestor_pub,
        })
        return self


async def _scenario() -> _Scenario:
    return await _Scenario().setup()


async def _never_revoked(credential):
    return dt.datetime.now(dt.timezone.utc), False


def _header_for(vp) -> dict[str, str]:
    payload = vp.model_dump(mode="json", by_alias=True, exclude_none=True)
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return {PRESENTATION_HEADER: encoded}


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

async def test_valid_identity_presentation_is_accepted():
    s = await _scenario()
    challenge = str(uuid.uuid4())
    vp = build_verifiable_presentation(
        holder_did=s.grantor_doc.id, holder_signing_key=s.grantor_key,
        holder_verification_method_id=s.grantor_vm_id,
        credentials=[s.grantor_identity_credential], challenge=challenge,
    )
    verifier = RequestPresentationVerifier(s.sentinel, status_resolver=_never_revoked)

    result = await verifier.verify_request(_FakeRequest(_header_for(vp)), challenge=challenge)

    assert result.ok is True
    assert result.error is None
    # Plain identity presentation — no delegation involved (issue #74).
    assert result.requestor_did == s.grantor_doc.id
    assert result.grantor_did is None
    assert result.scope is None


async def test_valid_delegated_presentation_is_accepted():
    s = await _scenario()
    challenge = str(uuid.uuid4())
    vp = build_verifiable_presentation(
        holder_did=s.requestor_doc.id, holder_signing_key=s.requestor_key,
        holder_verification_method_id=s.requestor_vm_id,
        credentials=[s.operational_credential, s.grantor_identity_credential],
        challenge=challenge,
    )
    verifier = RequestPresentationVerifier(s.sentinel, status_resolver=_never_revoked)

    result = await verifier.verify_request(_FakeRequest(_header_for(vp)), challenge=challenge)

    assert result.ok is True
    # The verified, unpacked grant must survive past the ok/error check
    # (issue #74) — this is the whole point of the bridge: downstream
    # code (stls-a2a, stls-mcp) needs the scope, not just a boolean.
    assert result.requestor_did == s.requestor_doc.id
    assert result.grantor_did == s.grantor_doc.id
    assert result.scope == {"action": "invoke-workflow", "resource": "sparkle-agent-007"}


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------

async def test_wrong_challenge_is_rejected():
    s = await _scenario()
    vp = build_verifiable_presentation(
        holder_did=s.grantor_doc.id, holder_signing_key=s.grantor_key,
        holder_verification_method_id=s.grantor_vm_id,
        credentials=[s.grantor_identity_credential], challenge=str(uuid.uuid4()),
    )
    verifier = RequestPresentationVerifier(s.sentinel, status_resolver=_never_revoked)

    result = await verifier.verify_request(
        _FakeRequest(_header_for(vp)), challenge=str(uuid.uuid4()),  # different challenge
    )

    assert result.ok is False
    assert "challenge" in result.error


async def test_revoked_credential_is_rejected():
    s = await _scenario()
    challenge = str(uuid.uuid4())
    vp = build_verifiable_presentation(
        holder_did=s.grantor_doc.id, holder_signing_key=s.grantor_key,
        holder_verification_method_id=s.grantor_vm_id,
        credentials=[s.grantor_identity_credential], challenge=challenge,
    )

    async def _revoked(credential):
        return dt.datetime.now(dt.timezone.utc), True

    verifier = RequestPresentationVerifier(s.sentinel, status_resolver=_revoked)

    result = await verifier.verify_request(_FakeRequest(_header_for(vp)), challenge=challenge)

    assert result.ok is False
    assert "revoked" in result.error


async def test_missing_header_is_rejected():
    s = await _scenario()
    verifier = RequestPresentationVerifier(s.sentinel, status_resolver=_never_revoked)

    result = await verifier.verify_request(_FakeRequest({}), challenge="whatever")

    assert result.ok is False
    assert "missing" in result.error


async def test_malformed_header_is_rejected_without_raising():
    s = await _scenario()
    verifier = RequestPresentationVerifier(s.sentinel, status_resolver=_never_revoked)

    result = await verifier.verify_request(
        _FakeRequest({PRESENTATION_HEADER: "not-valid-base64!!"}), challenge="whatever",
    )

    assert result.ok is False
    assert "malformed" in result.error


# ── request_presentation_verifier_from_env() (issue #80) ──────────────────
# Full wiring (Sentinel/trust/DID resolution/status fetch) needs a real
# Postgres DID store and a real Redis-or-memory keystore — out of scope
# for a unit test here, same posture as identity_challenge_handler_from_env()'s
# own test coverage (presentation_test.py). Covers only the off-by-default
# guarantee and the fail-loud guard, which is what every caller actually
# relies on at import time.

def test_from_env_off_by_default(monkeypatch):
    monkeypatch.delenv("REQUIRE_IDENTITY_PRESENTATION", raising=False)
    assert request_presentation_verifier_from_env() is None


def test_from_env_disabled_explicitly(monkeypatch):
    monkeypatch.setenv("REQUIRE_IDENTITY_PRESENTATION", "false")
    assert request_presentation_verifier_from_env() is None


def test_from_env_enabled_without_host_or_dsn_raises(monkeypatch):
    monkeypatch.setenv("REQUIRE_IDENTITY_PRESENTATION", "true")
    monkeypatch.delenv("GOVERNING_AUTHORITY_HOST", raising=False)
    monkeypatch.delenv("IDENTITY_PG_DSN", raising=False)

    try:
        request_presentation_verifier_from_env()
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "GOVERNING_AUTHORITY_HOST" in str(exc)
        assert "IDENTITY_PG_DSN" in str(exc)
