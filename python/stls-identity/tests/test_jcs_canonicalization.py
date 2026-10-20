"""
JCS (RFC 8785) canonicalization — dedicated tests.

Two things are checked:
    1. The property we actually need: issuer._sign() and
       verifier.verify_signature() must canonicalize identically, so a
       credential signed here verifies here (and, by construction, on
       any other RFC 8785-compliant bridge in a federation).
    2. A concrete case where the OLD stand-in (json.dumps(sort_keys=True))
       and the REAL rfc8785 output diverge — proving the swap wasn't
       cosmetic. Non-ASCII text is the clearest example: Python's
       json.dumps escapes it to \\uXXXX by default, RFC 8785 requires
       raw UTF-8 bytes. Two independently-implemented bridges (one using
       a real JCS library, one using Python's default json.dumps) would
       compute different signatures over the *same logical credential* —
       exactly the interop bug this fix prevents.
"""

import datetime as dt
import json

import rfc8785

from stls_identity import (
    IdentityManager,
    InMemoryIdentityKeystore,
    Sentinel,
    bootstrap_signing_key,
    build_did_document,
)


class _InMemoryStatusListManager:
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


def test_json_dumps_sort_keys_diverges_from_real_jcs_on_non_ascii():
    """The exact bug class this fix closes. Two 'canonicalizations' of
    the same dict, produced two different byte strings — anyone signing
    with one and verifying with the other would always fail."""
    payload = {"client_id": "cliente-società-più"}

    naive = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    real_jcs = rfc8785.dumps(payload)

    assert naive != real_jcs, (
        "if this assertion fails, cryptography/rfc8785 behavior changed — "
        "re-check whether the naive stand-in is safe again (it was not "
        "when this test was written)"
    )
    assert b"\\u" in naive, "json.dumps default-escapes non-ASCII to \\uXXXX"
    assert b"\\u" not in real_jcs, "RFC 8785 requires raw UTF-8, no \\u escapes"


def test_jcs_output_is_independent_of_python_dict_insertion_order():
    """RFC 8785's defining property: two dicts with the same keys/values
    but built in different order must canonicalize identically. This is
    what makes independent implementations converge on the same bytes."""
    a = {"z": 1, "a": 2, "m": {"y": 1, "b": 2}}
    b = {"a": 2, "m": {"b": 2, "y": 1}, "z": 1}

    assert rfc8785.dumps(a) == rfc8785.dumps(b)


async def test_credential_signed_and_verified_with_real_jcs_roundtrips():
    """Full sign -> verify path using the actual issuer.py/verifier.py
    code (not a reimplementation), confirming both sides agree after
    the swap from the sort_keys stand-in."""
    keystore = InMemoryIdentityKeystore()
    authority_key, authority_pub, _ = bootstrap_signing_key("authority", keystore)
    authority_did = "did:web:sofia.sparkle.example.com"
    authority_vm_id = f"{authority_did}#key-1"

    _, agent_pub, _ = bootstrap_signing_key("agent-società", keystore)  # non-ASCII agent_id on purpose
    agent_doc = build_did_document(
        host="sofia.sparkle.example.com",
        agent_path_segments=["agents", "società"],
        governing_authority_did=authority_did,
        signing_public_key=agent_pub,
    )

    async def _noop_audit_hook(entry):
        pass

    issuer = IdentityManager(
        governing_authority_did=authority_did,
        signing_key=authority_key,
        signing_verification_method_id=authority_vm_id,
        status_list_manager=_InMemoryStatusListManager(),
        status_list_publish_url="https://sofia.sparkle.example.com/identity/status-list",
        audit_hook=_noop_audit_hook,
    )

    credential = await issuer.issue_identity_credential(
        agent_did=agent_doc.id,
        client_id="agent-società",  # non-ASCII value inside the signed payload
    )

    sentinel = Sentinel(trusted_issuer_keys={authority_vm_id: authority_pub})
    # Must not raise — this is the case that a sort_keys/json.dumps
    # mismatch between issuer and verifier would have broken first,
    # since non-ASCII is exactly where the two diverge.
    await sentinel.verify_signature(credential)