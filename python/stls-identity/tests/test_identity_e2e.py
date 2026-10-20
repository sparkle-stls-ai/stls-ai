"""
End-to-end pipeline test — verbose by design.

Not just pass/fail: prints every intermediate artifact (DID Document,
signed credential, status list state, audit entries) so the pipeline
is legible on its own, without needing to read the implementation
alongside it.

Run with `-s` to see the output (pytest captures stdout by default):
    uv run pytest tests/test_e2e.py -v -s
"""

import asyncio
import datetime as dt
import json

from stls_identity import (
    CredentialVerificationError,
    IdentityManager,
    InMemoryIdentityKeystore,
    Sentinel,
    bootstrap_signing_key,
    build_did_document,
)

from stls_core.auth import AuditEntry


_captured_audit_entries: list[AuditEntry] = []


def _banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


def _print_json(label: str, payload: dict, truncate_fields: tuple[str, ...] = ()) -> None:
    """Pretty-prints a dict, truncating long crypto blobs (signature,
    public key) so the structural shape stays readable."""
    display = json.loads(json.dumps(payload, default=str))

    def _truncate(d):
        if isinstance(d, dict):
            for k, v in d.items():
                if k in truncate_fields and isinstance(v, str) and len(v) > 24:
                    d[k] = v[:20] + f"...({len(v)} chars)"
                else:
                    _truncate(v)
        elif isinstance(d, list):
            for item in d:
                _truncate(item)

    _truncate(display)
    print(f"--- {label} ---")
    print(json.dumps(display, indent=2, ensure_ascii=False))


async def capturing_audit_hook(entry: AuditEntry) -> None:
    """Stand-in for default_audit_hook() so the test can assert on what
    was audited, instead of only reading log output."""
    _captured_audit_entries.append(entry)
    print(f"  [AUDIT] result={entry.result.value:<10} client={entry.client_id:<12} "
          f"endpoint={entry.endpoint:<22} credential_id={entry.key_id}")


class InMemoryStatusListManager:
    """Stand-in for status.StatusListManager — same interface, no Redis.
    Verbose on purpose: prints the bit array itself so the revocation
    mechanism is visible, not just its effect."""

    def __init__(self):
        self._next = 0
        self._revoked: set[int] = set()

    async def allocate_index(self) -> int:
        idx = self._next
        self._next += 1
        print(f"  [STATUS-LIST] allocated index {idx}")
        return idx

    async def revoke(self, index: int) -> None:
        self._revoked.add(index)
        print(f"  [STATUS-LIST] bit {index} flipped -> revoked. "
              f"Full revoked set: {sorted(self._revoked)}")

    async def is_revoked(self, index: int) -> bool:
        result = index in self._revoked
        print(f"  [STATUS-LIST] read bit {index} -> revoked={result}")
        return result

    def render_bitstring(self, length: int = 8) -> str:
        return "".join("1" if i in self._revoked else "0" for i in range(length))


async def test_identity_lifecycle_e2e() -> None:
    _banner("STEP 1 — Governing Authority bootstrap (signing keypair)")
    keystore = InMemoryIdentityKeystore()

    authority_key, authority_pub, authority_created = bootstrap_signing_key(
        "governing-authority", keystore
    )
    authority_did = "did:web:sofia.sparkle.example.com"
    authority_vm_id = f"{authority_did}#key-1"
    print(f"  authority_did       = {authority_did}")
    print(f"  verification_method = {authority_vm_id}")
    print(f"  created (new key?)  = {authority_created}")

    _banner("STEP 1b — Restart simulation (keystore persistence)")
    authority_key_again, _, created_again = bootstrap_signing_key(
        "governing-authority", keystore
    )
    same_key = authority_key.private_bytes_raw() == authority_key_again.private_bytes_raw()
    print(f"  bootstrap_signing_key() called again with the SAME agent_id")
    print(f"  same private key returned = {same_key}")
    print(f"  created (should be False) = {created_again}")
    assert same_key
    assert created_again is False

    _banner("STEP 2 — Agent bootstrap: DID Document (Phase 1)")
    agent_key, agent_pub, _ = bootstrap_signing_key("agent-087", keystore)
    agent_doc = build_did_document(
        host="sofia.sparkle.example.com",
        agent_path_segments=["agents", "087"],
        governing_authority_did=authority_did,
        signing_public_key=agent_pub,
    )
    _print_json(
        "DID Document (what /agents/087/did.json would serve)",
        agent_doc.model_dump(mode="json", by_alias=True),
        truncate_fields=("publicKeyMultibase",),
    )

    _banner("STEP 3 — Identity Credential issuance (Phase 2)")
    status_list = InMemoryStatusListManager()
    issuer = IdentityManager(
        governing_authority_did=authority_did,
        signing_key=authority_key,
        signing_verification_method_id=authority_vm_id,
        status_list_manager=status_list,
        status_list_publish_url="https://sofia.sparkle.example.com/identity/status-list",
        audit_hook=capturing_audit_hook,
    )

    credential = await issuer.issue_identity_credential(
        agent_did=agent_doc.id,
        client_id="agent-087",  # same client_id used in today's KeyRecord
    )
    _print_json(
        "NetworkAuthorityCredential (signed, alongside the existing API key)",
        credential.model_dump(mode="json", by_alias=True, exclude_none=True),
        truncate_fields=("proofValue",),
    )
    print(f"  bitstring so far (8 bits) = {status_list.render_bitstring()}  "
          f"(all 0 = nothing revoked yet)")

    _banner("STEP 4 — Sentinel verification (credential is active)")
    sentinel = Sentinel(trusted_issuer_keys={authority_vm_id: authority_pub})
    now = dt.datetime.now(dt.timezone.utc)
    is_revoked = await status_list.is_revoked(credential.credential_status.status_list_index)
    await sentinel.verify(credential, status_bitstring_fetch_time=now, is_revoked=is_revoked)
    print("  RESULT: verification PASSED — signature valid, not expired, not revoked")

    _banner("STEP 5 — Revocation, then re-verify (must fail closed)")
    await issuer.revoke(credential)
    print(f"  bitstring after revoke (8 bits) = {status_list.render_bitstring()}")
    try:
        is_revoked = await status_list.is_revoked(credential.credential_status.status_list_index)
        await sentinel.verify(
            credential,
            status_bitstring_fetch_time=dt.datetime.now(dt.timezone.utc),
            is_revoked=is_revoked,
        )
        raise AssertionError("verification unexpectedly PASSED after revocation")
    except CredentialVerificationError as exc:
        print(f"  RESULT: verification correctly REJECTED — {exc}")

    _banner("STEP 6 — Tamper check (flip one character of the signature)")
    tampered = credential.model_copy(deep=True)
    original_tail = tampered.proof.proof_value[-6:]
    tampered.proof.proof_value = tampered.proof.proof_value[:-1] + (
        "a" if tampered.proof.proof_value[-1] != "a" else "b"
    )
    print(f"  original proof tail = ...{original_tail}")
    print(f"  tampered proof tail = ...{tampered.proof.proof_value[-6:]}")
    try:
        await sentinel.verify_signature(tampered)
        raise AssertionError("tampered signature unexpectedly PASSED verification")
    except CredentialVerificationError as exc:
        print(f"  RESULT: tamper check correctly REJECTED — {exc}")

    _banner("STEP 7 — Audit trail landed in the real stls-core AuditEntry stream")
    for entry in _captured_audit_entries:
        _print_json(f"AuditEntry ({entry.endpoint})", entry.to_dict())
    assert len(_captured_audit_entries) == 2
    assert _captured_audit_entries[0].endpoint == "stls-identity:issue"
    assert _captured_audit_entries[1].endpoint == "stls-identity:revoke"

    _banner("PIPELINE COMPLETE — all 7 stages passed")


if __name__ == "__main__":
    asyncio.run(test_identity_lifecycle_e2e())