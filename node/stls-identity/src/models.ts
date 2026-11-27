/**
 * models.ts
 * ---------
 * 1:1 port of python/stls-identity/src/stls_identity/stls_id_models.py.
 *
 * Plain TypeScript interfaces, not a runtime validation library (no zod
 * precedent in node/stls-core) — every field name below is the camelCase
 * WIRE name directly (matching Pydantic's `alias_generator=to_camel` +
 * `populate_by_name=True` output), not a Python-style snake_case name
 * with a separate alias. There is deliberately no separate "internal"
 * vs "wire" shape: these interfaces ARE the wire format.
 *
 * Validation of untrusted input (an incoming VerifiablePresentation
 * header, a DID Document fetched from a remote resolver) happens at
 * the call sites that actually cross a trust boundary (request-verifier.ts,
 * trust-registry-client.ts's remote fallback) — not here. Pydantic's
 * `extra="forbid"` behavior is reproduced at those specific boundaries,
 * not globally.
 *
 * Cross-language wire compatibility is the entire point of this file:
 * every field name, every @context string, every literal default must
 * match stls_id_models.py byte-for-byte once JCS-canonicalized. See
 * jcs-canonicalization.smoke.ts and tests/interop/golden-vector.smoke.ts.
 */

/**
 * Formats a Date as the EXACT ISO-8601 string Pydantic v2 produces for
 * a `datetime` field under `model_dump(mode="json")`, not plain
 * `Date.toISOString()`.
 *
 * This matters because Python's verifier re-validates an incoming
 * credential/presentation into a Pydantic model BEFORE canonicalizing
 * it for signature verification (`model_copy()` + `model_dump()`) —
 * so by the time it's canonicalized, every datetime string has
 * already been round-tripped through Python's own `datetime` type and
 * re-serialized in Pydantic's canonical form, which differs from
 * `Date.toISOString()` in two ways confirmed empirically against a
 * real Pydantic model:
 *   - a sub-second fraction of exactly zero is DROPPED entirely
 *     (`"...T12:00:00.000Z"` -> `"...T12:00:00Z"`), never emitted as
 *     `.000`.
 *   - any non-zero fraction is expanded to full 6-digit microseconds,
 *     zero-padded on the right (`.123` -> `.123000`), never left at
 *     3-digit millisecond precision.
 * Signing with the wrong format produces a credential/presentation
 * that fails to verify in Python the moment it's re-validated, even
 * though the signature itself was computed correctly over this
 * process's own (wrong-format) bytes — see
 * tests/interop/golden-vector.smoke.ts and the cross-runtime note in
 * this package's README for how this was caught.
 */
export function toPydanticIsoString(date: Date): string {
  const millis = date.getUTCMilliseconds();
  if (millis === 0) {
    return date.toISOString().replace(/\.000Z$/, "Z");
  }
  const micros = String(millis * 1000).padStart(6, "0");
  return date.toISOString().replace(/\.\d{3}Z$/, `.${micros}Z`);
}

// ---------------------------------------------------------------------------
// DID Document (did:web)
// ---------------------------------------------------------------------------

/**
 * Key history (issue #42, the per-agent analogue of TrustAnchorEntry's
 * own key history): an agent's DIDDocument.verificationMethod can hold
 * MULTIPLE entries over the agent's lifetime, one per key rotation.
 * Older entries are kept, not removed, so a credential/presentation
 * signed before a rotation still resolves against the key that was
 * actually valid when it was signed (see resolveHolderKeyAt() in
 * did.ts). `authentication`/`assertionMethod` on the owning DIDDocument
 * always point at ONLY the current entry.
 *
 * validFrom/validUntil/revokedAt are all optional for backward
 * compatibility with DID Documents published before this field
 * existed (a single, un-rotated key). A document built fresh via
 * buildDidDocument() always sets validFrom explicitly.
 */
export interface VerificationMethod {
  id: string; // e.g. "did:web:sofia.sparkle.example.com:agents:087#key-1"
  type: "Ed25519VerificationKey2020";
  controller: string; // the Governing Authority DID
  publicKeyMultibase: string; // multibase-encoded raw Ed25519 public key
  validFrom?: string | null; // ISO-8601 (UTC) — None = no known lower bound
  validUntil?: string | null; // ISO-8601 (UTC) — None = currently active
  revokedAt?: string | null; // ISO-8601 (UTC) — emergency revocation of THIS key generation
}

export function makeVerificationMethod(
  args: Omit<VerificationMethod, "type">,
): VerificationMethod {
  return { ...args, type: "Ed25519VerificationKey2020" };
}

export const DID_DOCUMENT_CONTEXT = [
  "https://www.w3.org/ns/did/v1",
  "https://w3id.org/security/suites/ed25519-2020/v1",
];

export interface DIDDocument {
  "@context": string[];
  id: string; // e.g. "did:web:sofia.sparkle.example.com:agents:087"
  controller: string; // Governing Authority DID
  verificationMethod: VerificationMethod[];
  authentication: string[];
  assertionMethod: string[];
  created: string; // ISO-8601 (UTC)
}

// ---------------------------------------------------------------------------
// Verifiable Credential — status list entry (revocation pointer)
// ---------------------------------------------------------------------------

export type StatusPurposeLiteral = "revocation" | "suspension";

export interface CredentialStatus {
  id: string; // URL of the status list credential + "#{statusListIndex}"
  type: "BitstringStatusListEntry";
  statusPurpose: StatusPurposeLiteral;
  statusListIndex: number;
  statusListCredential: string;
}

// ---------------------------------------------------------------------------
// Verifiable Credential — base + Phase 2 subtype
// ---------------------------------------------------------------------------

export enum KeyStorageTier {
  Tier1Software = "TIER_1_SOFTWARE",
  Tier2Tpm = "TIER_2_TPM",
  Tier3Hsm = "TIER_3_HSM",
}

export interface CredentialSubject {
  id: string; // the agent's DID
  clientId: string;
  principalRef?: string | null;
  keyStorageTier: KeyStorageTier;
  roles: string[];
}

export const VERIFIABLE_CREDENTIAL_CONTEXT = ["https://www.w3.org/ns/credentials/v2"];

export interface CredentialProof {
  type: "DataIntegrityProof";
  cryptosuite: "eddsa-jcs-2022";
  created: string; // ISO-8601 (UTC)
  verificationMethod: string;
  proofPurpose: "assertionMethod";
  proofValue: string; // multibase-encoded raw signature bytes
}

/**
 * Generic VC envelope shape shared by every concrete credential type —
 * matches Python's VerifiableCredential base model. Used where a
 * function (e.g. status.ts's fetchRemoteCredentialStatus) only needs
 * the fields common to any credential, not a specific subtype.
 */
export interface VerifiableCredential {
  "@context": string[];
  id: string; // urn:uuid:...
  schemaVersion: string;
  issuer: string;
  validFrom: string; // ISO-8601 (UTC)
  validUntil?: string | null;
  credentialStatus: CredentialStatus;
  proof?: CredentialProof | null;
}

export interface NetworkAuthorityCredential extends VerifiableCredential {
  type: ["VerifiableCredential", "NetworkAuthorityCredential"];
  credentialSubject: CredentialSubject;
}

export function isNetworkAuthorityCredential(
  credential: NetworkAuthorityCredential | OperationalCredential,
): credential is NetworkAuthorityCredential {
  return (credential.type as readonly string[]).includes("NetworkAuthorityCredential");
}

// ---------------------------------------------------------------------------
// Verifiable Credential — Phase 3: Operational Credential (delegation)
// ---------------------------------------------------------------------------

export interface OperationalCredentialSubject {
  id: string; // the Requestor's DID
  clientId: string;
  scope: Record<string, unknown>;
}

export interface OperationalCredential extends VerifiableCredential {
  type: ["VerifiableCredential", "OperationalCredential"];
  credentialSubject: OperationalCredentialSubject;
  delegationDepth: 1;
}

export function isOperationalCredential(
  credential: NetworkAuthorityCredential | OperationalCredential,
): credential is OperationalCredential {
  return (credential.type as readonly string[]).includes("OperationalCredential");
}

// ---------------------------------------------------------------------------
// Status list document (published, resolved by any verifier)
// ---------------------------------------------------------------------------

export interface StatusListCredential {
  "@context": string[];
  id: string; // published URL of this document
  type: ["VerifiableCredential", "BitstringStatusListCredential"];
  issuer: string;
  validFrom: string; // ISO-8601 (UTC)
  statusPurpose: StatusPurposeLiteral;
  encodedList: string; // base64url of the GZIP-compressed bitstring
  proof?: CredentialProof | null;
}

// ---------------------------------------------------------------------------
// Verifiable Presentation
// ---------------------------------------------------------------------------

/**
 * HTTP header a VerifiablePresentation travels under in an A2A request
 * (issue #15 gap #1) — base64 of its JSON serialization. Must match
 * PRESENTATION_HEADER in python/stls-identity's stls_id_models.py
 * exactly (shared source of truth between the client-side builder and
 * the server-side bridge).
 */
export const PRESENTATION_HEADER = "X-STLS-Identity-Presentation";

export interface PresentationProof {
  type: "DataIntegrityProof";
  cryptosuite: "eddsa-jcs-2022";
  created: string; // ISO-8601 (UTC)
  verificationMethod: string;
  proofPurpose: "authentication";
  challenge: string;
  proofValue: string;
}

export interface VerifiablePresentation {
  "@context": string[];
  type: ["VerifiablePresentation"];
  holder: string;
  verifiableCredential: (NetworkAuthorityCredential | OperationalCredential)[];
  proof?: PresentationProof | null;
}

// ---------------------------------------------------------------------------
// Trust Anchor Registry
// ---------------------------------------------------------------------------

export interface TrustAnchorEntry {
  authorityId: string;
  did: string;
  verificationMethodId: string;
  publicKeyMultibase: string;
  onboardedAt: string; // ISO-8601 (UTC)
  validUntil?: string | null;
  revokedAt?: string | null;
}
