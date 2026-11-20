"""
bootstrap_identity.py — 19_identity_presentation
---------------------------------------------------
One-time setup, run before server.py/client.py: creates a Governing
Authority and a Requestor agent in process (zero infra — same
zero-dependency pattern as stls-identity's own
tests/test_identity_e2e.py), issues the Requestor's
NetworkAuthorityCredential, and writes two files:

    authority_public.json    — the Authority's verification-method id
                                + public key (hex). Loaded by server.py
                                to build its trusted_issuer_keys.
    requestor_identity.json  — the Requestor's DID, private key (hex),
                                verification-method id, and its signed
                                NetworkAuthorityCredential. Loaded by
                                client.py to build an IdentityPresenter.

Run once:

    python bootstrap_identity.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from stls_identity import IdentityManager, InMemoryIdentityKeystore, bootstrap_signing_key, build_did_document

HERE = Path(__file__).parent


class _InMemoryStatusListManager:
    """Same zero-infra stand-in used by stls-identity's own tests — the
    issuer side needs SOME status list manager to allocate an index for
    the credential, even though this sample never revokes anything."""

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
    keystore = InMemoryIdentityKeystore()

    authority_did = "did:web:sample.sparkle.example.com"
    authority_key, authority_pub, _ = bootstrap_signing_key(
        "sample-governing-authority", keystore,
    )
    authority_vm_id = f"{authority_did}#key-1"

    requestor_key, requestor_pub, _ = bootstrap_signing_key("sample-requestor", keystore)
    requestor_doc = build_did_document(
        host="requestor.example.com", agent_path_segments=["agents", "requestor"],
        governing_authority_did=authority_did, signing_public_key=requestor_pub,
    )
    requestor_vm_id = f"{requestor_doc.id}#key-1"

    issuer = IdentityManager(
        governing_authority_did=authority_did, signing_key=authority_key,
        signing_verification_method_id=authority_vm_id,
        status_list_manager=_InMemoryStatusListManager(),
        status_list_publish_url="https://sample.sparkle.example.com/identity/status-list",
        audit_hook=_noop_audit_hook,
    )
    credential = await issuer.issue_identity_credential(
        agent_did=requestor_doc.id, client_id="sample-requestor", roles=[],
    )

    # Sentinel resolves BOTH a credential's issuer AND a presentation's
    # holder through the same trusted_issuer_keys mapping (see its own
    # docstring) — there is no dynamic did:web resolution for an
    # arbitrary holder key yet (a real deployment at that scale would
    # need one; out of scope for gap #1, same limitation
    # tests/test_presentation.py's own fixture already has). This
    # sample pre-shares the Requestor's verification key with the
    # Verifier out of band, exactly like that test fixture does.
    (HERE / "authority_public.json").write_text(json.dumps({
        "trusted_keys": {
            authority_vm_id: _pub_hex(authority_pub),
            requestor_vm_id: _pub_hex(requestor_pub),
        },
    }, indent=2))

    (HERE / "requestor_identity.json").write_text(json.dumps({
        "holder_did": requestor_doc.id,
        "holder_verification_method_id": requestor_vm_id,
        "private_key_hex": _priv_hex(requestor_key),
        "credential": credential.model_dump(mode="json", by_alias=True, exclude_none=True),
    }, indent=2))

    print(f"Wrote {HERE / 'authority_public.json'}")
    print(f"Wrote {HERE / 'requestor_identity.json'}")
    print(f"\nRequestor DID: {requestor_doc.id}")
    print(f"Authority DID: {authority_did}")


if __name__ == "__main__":
    asyncio.run(main())
