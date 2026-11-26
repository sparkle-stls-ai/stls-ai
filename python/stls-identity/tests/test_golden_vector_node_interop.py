"""
Cross-language wire-compatibility test, Python side.

tests/fixtures/golden_vector.json is BYTE-IDENTICAL to
node/stls-identity/tests/fixtures/golden-vector.json (same file,
copied — not independently regenerated). It was produced once by this
package's own IdentityManager._sign()/build_verifiable_presentation()
(see node/stls-identity's tests/interop/golden-vector.smoke.ts for the
Node-side half of this same check).

This proves the Python side of the port's compatibility claim the
other direction: it's not enough for Node to verify what Python
produced (already checked on the Node side) — Python must also accept
what is, in fact, its OWN historical output, confirming the fixture
itself stays valid as the implementation evolves. A real cross-runtime
claim (Node-issued, Python-verified and vice versa) is exercised
manually; this test guards the shared fixture against regressions in
either direction without needing a live Node process in CI.
"""

import json
from pathlib import Path

import rfc8785

from stls_identity.stls_id_did import decode_multibase_ed25519_public_key
from stls_identity.stls_id_models import NetworkAuthorityCredential, VerifiablePresentation
from stls_identity.stls_id_verifier import Sentinel

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden_vector.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def test_fixture_file_exists_and_is_shared_with_node():
    fixture = _load_fixture()
    assert "credential" in fixture
    assert "presentation" in fixture
    assert "credentialUnsignedCanonicalBytes" in fixture
    assert "presentationMinusProofValueCanonicalBytes" in fixture


def test_canonicalization_matches_recorded_bytes():
    fixture = _load_fixture()
    credential = dict(fixture["credential"])
    del credential["proof"]
    assert rfc8785.dumps(credential).decode("utf-8") == fixture["credentialUnsignedCanonicalBytes"]

    presentation = dict(fixture["presentation"])
    presentation["proof"] = dict(presentation["proof"])
    del presentation["proof"]["proofValue"]
    assert (
        rfc8785.dumps(presentation).decode("utf-8")
        == fixture["presentationMinusProofValueCanonicalBytes"]
    )


async def test_python_verifies_the_shared_fixture():
    fixture = _load_fixture()
    authority_pub = decode_multibase_ed25519_public_key(fixture["authorityPublicKeyMultibase"])
    holder_pub = decode_multibase_ed25519_public_key(fixture["holderPublicKeyMultibase"])

    credential = NetworkAuthorityCredential.model_validate(fixture["credential"])
    presentation = VerifiablePresentation.model_validate(fixture["presentation"])

    vm_id = fixture["credential"]["proof"]["verificationMethod"]
    holder_vm_id = fixture["presentation"]["proof"]["verificationMethod"]

    sentinel = Sentinel(trusted_issuer_keys={vm_id: authority_pub, holder_vm_id: holder_pub})

    await sentinel.verify_signature(credential)
    sentinel.verify_presentation_proof(
        presentation, expected_challenge=fixture["presentation"]["proof"]["challenge"]
    )
