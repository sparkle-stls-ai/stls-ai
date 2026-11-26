/**
 * signing.ts
 * ----------
 * The single shared implementation of the RFC 8785 (JCS) canonicalize +
 * Ed25519 sign/verify + base58btc 'z'-encode dance used by
 * stls_id_issuer.py::_sign(), stls_id_delegation.py::_sign(),
 * stls_id_presentation.py::build_verifiable_presentation(), and mirrored
 * on the verify side by stls_id_verifier.py's Sentinel.
 *
 * Deliberately factored out into ONE module rather than reimplemented
 * at each of those four call sites: this exact byte-level dance is the
 * single highest cross-language-compatibility risk in the whole port.
 * One implementation here means there is only one place that can get
 * the shape wrong, instead of several independently-written copies
 * that could each individually look right while silently disagreeing
 * with the Python side (or with each other).
 *
 * There are TWO distinct shapes in the Python source — conflating them
 * would silently break verification, so they get two distinct function
 * pairs here rather than one that tries to cover both:
 *
 *   1. CREDENTIAL signing (issuer.py::_sign(), delegation.py::_sign(),
 *      verifier.py::verify_signature()): the `proof` key is ABSENT
 *      ENTIRELY from the canonicalized payload (Python:
 *      `credential.model_copy(update={"proof": None})` then
 *      `exclude_none=True`, which drops the whole key — not just
 *      `proofValue`). The CredentialProof object is only constructed
 *      and attached AFTER signing; its own `created` timestamp is
 *      never part of the signed bytes.
 *      -> signCredentialPayload() / verifyCredentialSignature() below.
 *
 *   2. PRESENTATION signing (presentation.py::build_verifiable_presentation(),
 *      verifier.py::verify_presentation_proof()): the `proof` object
 *      IS present in the canonicalized payload, with every field
 *      (including `challenge` and `created`) except `proofValue`
 *      itself — this is what makes `challenge` tamper-evident (see
 *      presentation.ts's docstring for why that matters).
 *      -> signProofBoundPayload() / verifyProofBoundPayload() below.
 *
 * JSON.stringify (and `canonicalize`) already drop keys whose value is
 * `undefined`, the same behavior Pydantic's `exclude_none=True` gives
 * Python — so a plain object built with an absent field left
 * `undefined` round-trips identically. Nothing here is ever mutated:
 * each function builds a local copy to canonicalize rather than
 * touching the caller's object.
 */

import canonicalize from "canonicalize";
import { ed25519 } from "@noble/curves/ed25519.js";
import bs58 from "bs58";

function canonicalizeStrict(value: unknown): string {
  const canonical = canonicalize(value);
  if (canonical === undefined) {
    throw new Error("canonicalize() returned undefined — payload was not a plain JSON value");
  }
  return canonical;
}

function decodeMultibaseSignature(proofValueMultibase: string): Uint8Array | null {
  if (!proofValueMultibase.startsWith("z")) {
    return null;
  }
  try {
    return bs58.decode(proofValueMultibase.slice(1));
  } catch {
    return null;
  }
}

function encodeMultibaseSignature(signature: Uint8Array): string {
  return "z" + bs58.encode(signature);
}

// ---------------------------------------------------------------------------
// 1. Credential signing — proof key entirely absent while signing.
// ---------------------------------------------------------------------------

/**
 * Canonicalizes `payloadWithoutProof` (which must NOT contain a `proof`
 * key — pass the credential fields before any CredentialProof is
 * constructed) and signs it with `signingKey` (a raw 32-byte Ed25519
 * private key / seed). Returns the multibase ('z' + base58btc) encoded
 * signature, ready to go into a freshly-built CredentialProof.proofValue.
 */
export function signCredentialPayload(
  payloadWithoutProof: object,
  signingKey: Uint8Array,
): string {
  const canonical = canonicalizeStrict(payloadWithoutProof);
  const signature = ed25519.sign(new TextEncoder().encode(canonical), signingKey);
  return encodeMultibaseSignature(signature);
}

/**
 * Verifies a signed credential's proof. `signedPayloadWithProof` is the
 * full credential INCLUDING its `proof` object; this function builds
 * the proof-less copy itself (mirroring Python's
 * `model_copy(update={"proof": None})`) before canonicalizing, so
 * callers never need to construct that shape by hand.
 */
export function verifyCredentialSignature<
  T extends { proof?: { proofValue?: string } | null },
>(signedPayloadWithProof: T, publicKey: Uint8Array): boolean {
  const proof = signedPayloadWithProof.proof;
  if (!proof?.proofValue) {
    return false;
  }
  const signature = decodeMultibaseSignature(proof.proofValue);
  if (!signature) {
    return false;
  }
  const { proof: _proof, ...payloadWithoutProof } = signedPayloadWithProof;
  const canonical = canonicalizeStrict(payloadWithoutProof);
  try {
    return ed25519.verify(signature, new TextEncoder().encode(canonical), publicKey);
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// 2. Presentation signing — proof object present, minus proofValue only.
// ---------------------------------------------------------------------------

type PayloadWithProofObject = {
  proof?: { proofValue?: unknown } | null | undefined;
};

function canonicalizeMinusProofValue<T extends PayloadWithProofObject>(payload: T): string {
  const proof = payload.proof;
  if (!proof) {
    throw new Error("signing payload has no `proof` object to sign/verify over");
  }
  const { proofValue: _proofValue, ...proofWithoutValue } = proof;
  const canonicalTarget = { ...payload, proof: proofWithoutValue };
  return canonicalizeStrict(canonicalTarget);
}

/**
 * Canonicalizes `payloadWithProof` with `proof.proofValue` removed
 * (every other proof field, including `challenge`, stays in the signed
 * bytes) and signs it with `signingKey`. Returns the multibase-encoded
 * signature, ready to assign onto `proof.proofValue`.
 */
export function signProofBoundPayload<T extends PayloadWithProofObject>(
  payloadWithProof: T,
  signingKey: Uint8Array,
): string {
  const canonical = canonicalizeMinusProofValue(payloadWithProof);
  const signature = ed25519.sign(new TextEncoder().encode(canonical), signingKey);
  return encodeMultibaseSignature(signature);
}

/**
 * Inverse check: canonicalizes `payloadWithProof` with `proof.proofValue`
 * removed, decodes `proofValueMultibase` (the multibase string actually
 * present on the presentation), and verifies the Ed25519 signature
 * against `publicKey`. Returns false on any malformed proofValue rather
 * than throwing — callers decide how to surface that as a verification
 * failure.
 */
export function verifyProofBoundPayload<T extends PayloadWithProofObject>(
  payloadWithProof: T,
  proofValueMultibase: string,
  publicKey: Uint8Array,
): boolean {
  const signature = decodeMultibaseSignature(proofValueMultibase);
  if (!signature) {
    return false;
  }
  const canonical = canonicalizeMinusProofValue(payloadWithProof);
  try {
    return ed25519.verify(signature, new TextEncoder().encode(canonical), publicKey);
  } catch {
    return false;
  }
}
