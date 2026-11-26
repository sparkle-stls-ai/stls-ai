/**
 * interop/golden-vector.smoke.ts
 * --------------------------------
 * Cross-language wire-compatibility test. tests/fixtures/golden-vector.json
 * is BYTE-IDENTICAL to python/stls-identity/tests/fixtures/golden_vector.json
 * (copied, not regenerated independently) — generated once from the real
 * Python issuer.py/presentation.py/_sign() code (see that file's own
 * generation script, kept out of the committed tree) and committed to
 * both repos frozen.
 *
 * This is the single highest-risk piece of the whole port (see
 * signing.ts's module docstring): an independently-authored Node test
 * that merely re-signs-and-reverifies its OWN output could pass while
 * silently disagreeing with Python about the exact canonicalization
 * shape. This test instead canonicalizes/verifies data Python actually
 * produced, so a shape mismatch fails loudly here instead of only
 * showing up the first time a real cross-platform federation call is
 * attempted.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import canonicalize from "canonicalize";

import { decodeMultibaseEd25519PublicKey } from "../../src/did.js";
import { verifyCredentialSignature, verifyProofBoundPayload } from "../../src/signing.js";

const fixturePath = fileURLToPath(
  new URL("../fixtures/golden-vector.json", import.meta.url),
);
const fixture = JSON.parse(readFileSync(fixturePath, "utf-8"));

const authorityPublicKey = decodeMultibaseEd25519PublicKey(fixture.authorityPublicKeyMultibase);
const holderPublicKey = decodeMultibaseEd25519PublicKey(fixture.holderPublicKeyMultibase);

// T01 — canonicalizing the Python-produced credential (proof stripped)
// reproduces the EXACT bytes Python's rfc8785.dumps() produced.
{
  const { proof: _proof, ...credentialWithoutProof } = fixture.credential;
  const canonical = canonicalize(credentialWithoutProof);
  assert.equal(canonical, fixture.credentialUnsignedCanonicalBytes);
  console.log("OK: T01 Node canonicalize() matches Python rfc8785.dumps() for a credential");
}

// T02 — Node's Ed25519 verify accepts the signature Python's
// cryptography library produced over those exact bytes.
{
  const ok = verifyCredentialSignature(fixture.credential, authorityPublicKey);
  assert.ok(ok, "Node must verify a Python-issued NetworkAuthorityCredential's signature");
  console.log("OK: T02 Node verifies a Python-issued credential signature");
}

// T03 — same canonicalization match for the VerifiablePresentation
// (proof present, minus only proofValue).
{
  const { proof, ...rest } = fixture.presentation;
  const { proofValue: _proofValue, ...proofWithoutValue } = proof;
  const canonical = canonicalize({ ...rest, proof: proofWithoutValue });
  assert.equal(canonical, fixture.presentationMinusProofValueCanonicalBytes);
  console.log("OK: T03 Node canonicalize() matches Python rfc8785.dumps() for a presentation");
}

// T04 — Node verifies the holder signature Python produced over the
// presentation (proof-minus-proofValue dance).
{
  const ok = verifyProofBoundPayload(
    fixture.presentation,
    fixture.presentation.proof.proofValue,
    holderPublicKey,
  );
  assert.ok(ok, "Node must verify a Python-issued VerifiablePresentation's holder signature");
  console.log("OK: T04 Node verifies a Python-issued presentation signature");
}
