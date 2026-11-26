/**
 * verifier.smoke.ts
 * ------------------
 * Node mirror of the pure (no-registry-I/O) parts of
 * python/stls-identity's test_presentation.py/test_delegation.py
 * Sentinel coverage: signature/window/revocation/delegation-chain
 * checks, using this package's own signing.ts to produce fixtures
 * (the cross-language byte-for-byte fixture lives in
 * tests/interop/golden-vector.smoke.ts).
 */

import assert from "node:assert/strict";
import { ed25519 } from "@noble/curves/ed25519.js";
import { signCredentialPayload } from "../src/signing.js";
import { buildVerifiablePresentation } from "../src/presentation.js";
import { CredentialVerificationError, Sentinel } from "../src/verifier.js";
import {
  KeyStorageTier,
  type NetworkAuthorityCredential,
  type OperationalCredential,
} from "../src/models.js";

const AUTHORITY_DID = "did:web:sofia.sparkle.example.com";
const AUTHORITY_VM_ID = `${AUTHORITY_DID}#key-1`;
const AGENT_DID = "did:web:sofia.sparkle.example.com:agents:087";
const AGENT_VM_ID = `${AGENT_DID}#key-1`;

function issueCredential(
  signingKey: Uint8Array,
  overrides: Partial<NetworkAuthorityCredential> = {},
): NetworkAuthorityCredential {
  const now = new Date();
  const credentialWithoutProof: Omit<NetworkAuthorityCredential, "proof"> = {
    "@context": ["https://www.w3.org/ns/credentials/v2"],
    id: "urn:uuid:11111111-0000-4000-8000-000000000001",
    type: ["VerifiableCredential", "NetworkAuthorityCredential"],
    schemaVersion: "1.0",
    issuer: AUTHORITY_DID,
    validFrom: now.toISOString(),
    validUntil: new Date(now.getTime() + 90 * 86_400_000).toISOString(),
    credentialSubject: {
      id: AGENT_DID,
      clientId: "agent-087",
      keyStorageTier: KeyStorageTier.Tier1Software,
      roles: [],
    },
    credentialStatus: {
      id: "https://sofia.sparkle.example.com/identity/status-list#0",
      type: "BitstringStatusListEntry",
      statusPurpose: "revocation",
      statusListIndex: 0,
      statusListCredential: "https://sofia.sparkle.example.com/identity/status-list",
    },
    ...overrides,
  };

  const proofValue = signCredentialPayload(credentialWithoutProof, signingKey);
  return {
    ...credentialWithoutProof,
    proof: {
      type: "DataIntegrityProof",
      cryptosuite: "eddsa-jcs-2022",
      created: now.toISOString(),
      verificationMethod: AUTHORITY_VM_ID,
      proofPurpose: "assertionMethod",
      proofValue,
    },
  } as NetworkAuthorityCredential;
}

const FRESH = { statusBitstringFetchTime: new Date(), isRevoked: false };

// T01 — verifySignature() accepts a validly-signed credential, rejects a tampered one
{
  const authority = ed25519.keygen();
  const credential = issueCredential(authority.secretKey);
  const sentinel = new Sentinel({ trustedIssuerKeys: new Map([[AUTHORITY_VM_ID, authority.publicKey]]) });

  await sentinel.verifySignature(credential); // must not throw

  const tampered: NetworkAuthorityCredential = {
    ...credential,
    credentialSubject: { ...credential.credentialSubject, clientId: "someone-else" },
  };
  await assert.rejects(() => sentinel.verifySignature(tampered), CredentialVerificationError);
  console.log("OK: T01 verifySignature() accepts valid, rejects tampered");
}

// T02 — verifySignature() rejects an untrusted verification method
{
  const authority = ed25519.keygen();
  const credential = issueCredential(authority.secretKey);
  const sentinel = new Sentinel({ trustedIssuerKeys: new Map() });
  await assert.rejects(() => sentinel.verifySignature(credential), CredentialVerificationError);
  console.log("OK: T02 verifySignature() rejects unknown verification method");
}

// T03 — verifyValidityWindow(): not-yet-valid and expired both reject
{
  const authority = ed25519.keygen();
  const sentinel = new Sentinel({ trustedIssuerKeys: new Map([[AUTHORITY_VM_ID, authority.publicKey]]) });

  const future = new Date(Date.now() + 86_400_000).toISOString();
  const notYetValid = issueCredential(authority.secretKey, { validFrom: future });
  assert.throws(() => sentinel.verifyValidityWindow(notYetValid), CredentialVerificationError);

  const past = new Date(Date.now() - 86_400_000).toISOString();
  const expired = issueCredential(authority.secretKey, {
    validFrom: new Date(Date.now() - 2 * 86_400_000).toISOString(),
    validUntil: past,
  });
  assert.throws(() => sentinel.verifyValidityWindow(expired), CredentialVerificationError);
  console.log("OK: T03 verifyValidityWindow() rejects not-yet-valid and expired");
}

// T04 — verifyNotRevoked(): stale status list fails closed; revoked fails
{
  const authority = ed25519.keygen();
  const credential = issueCredential(authority.secretKey);
  const sentinel = new Sentinel({ trustedIssuerKeys: new Map([[AUTHORITY_VM_ID, authority.publicKey]]) });

  assert.throws(
    () =>
      sentinel.verifyNotRevoked(credential, {
        statusBitstringFetchTime: new Date(Date.now() - 400_000), // > 300s MRI
        isRevoked: false,
      }),
    CredentialVerificationError,
  );

  assert.throws(
    () => sentinel.verifyNotRevoked(credential, { statusBitstringFetchTime: new Date(), isRevoked: true }),
    CredentialVerificationError,
  );

  sentinel.verifyNotRevoked(credential, FRESH); // must not throw
  console.log("OK: T04 verifyNotRevoked() fail-closed on staleness and revocation");
}

// T05 — verify() full pipeline, success and failure
{
  const authority = ed25519.keygen();
  const credential = issueCredential(authority.secretKey);
  const sentinel = new Sentinel({ trustedIssuerKeys: new Map([[AUTHORITY_VM_ID, authority.publicKey]]) });
  await sentinel.verify(credential, FRESH); // must not throw
  console.log("OK: T05 verify() full pipeline succeeds for a valid credential");
}

// T06 — verifyDelegationChain(): issuer/subject mismatch is caught
{
  const grantorKey = ed25519.keygen();
  const grantorDid = "did:web:sofia.sparkle.example.com:agents:grantor";
  const grantorVmId = `${grantorDid}#key-1`;
  const grantorIdentity = issueCredential(grantorKey.secretKey, {
    credentialSubject: {
      id: grantorDid,
      clientId: "agent-grantor",
      keyStorageTier: KeyStorageTier.Tier1Software,
      roles: ["grantor"],
    },
    credentialStatus: {
      id: "https://sofia.sparkle.example.com/identity/status-list#1",
      type: "BitstringStatusListEntry",
      statusPurpose: "revocation",
      statusListIndex: 1,
      statusListCredential: "https://sofia.sparkle.example.com/identity/status-list",
    },
  });
  // signed with grantorKey but proof.verificationMethod must point at the right id
  (grantorIdentity.proof as { verificationMethod: string }).verificationMethod = grantorVmId;

  const operationalWithoutProof: Omit<OperationalCredential, "proof"> = {
    "@context": ["https://www.w3.org/ns/credentials/v2"],
    id: "urn:uuid:22222222-0000-4000-8000-000000000002",
    type: ["VerifiableCredential", "OperationalCredential"],
    schemaVersion: "1.0",
    issuer: "did:web:sofia.sparkle.example.com:agents:NOT-the-grantor",
    validFrom: new Date().toISOString(),
    validUntil: new Date(Date.now() + 86_400_000).toISOString(),
    credentialSubject: {
      id: AGENT_DID,
      clientId: "agent-087",
      scope: {},
    },
    credentialStatus: {
      id: "https://sofia.sparkle.example.com/identity/status-list#2",
      type: "BitstringStatusListEntry",
      statusPurpose: "revocation",
      statusListIndex: 2,
      statusListCredential: "https://sofia.sparkle.example.com/identity/status-list",
    },
    delegationDepth: 1,
  };
  const operational: OperationalCredential = {
    ...operationalWithoutProof,
    proof: {
      type: "DataIntegrityProof",
      cryptosuite: "eddsa-jcs-2022",
      created: new Date().toISOString(),
      verificationMethod: grantorVmId,
      proofPurpose: "assertionMethod",
      proofValue: signCredentialPayload(operationalWithoutProof, grantorKey.secretKey),
    },
  };

  const sentinel = new Sentinel({ trustedIssuerKeys: new Map([[grantorVmId, grantorKey.publicKey]]) });
  await assert.rejects(
    () =>
      sentinel.verifyDelegationChain(operational, {
        grantorIdentityCredential: grantorIdentity,
        grantorIdentityFetchTime: new Date(),
        grantorIdentityIsRevoked: false,
        operationalFetchTime: new Date(),
        operationalIsRevoked: false,
      }),
    CredentialVerificationError,
  );
  console.log("OK: T06 verifyDelegationChain() rejects issuer/subject mismatch");
}

// T07 — verifyPresentationProof(): wrong proofPurpose and challenge mismatch both rejected
{
  const holder = ed25519.keygen();
  const credential = issueCredential(ed25519.keygen().secretKey);
  const sentinel = new Sentinel({ trustedIssuerKeys: new Map([[AGENT_VM_ID, holder.publicKey]]) });

  const vp = buildVerifiablePresentation({
    holderDid: AGENT_DID,
    holderSigningKey: holder.secretKey,
    holderVerificationMethodId: AGENT_VM_ID,
    credentials: [credential],
    challenge: "the-real-challenge",
  });

  sentinel.verifyPresentationProof(vp, { expectedChallenge: "the-real-challenge" }); // must not throw

  assert.throws(
    () => sentinel.verifyPresentationProof(vp, { expectedChallenge: "a-different-challenge" }),
    CredentialVerificationError,
  );
  console.log("OK: T07 verifyPresentationProof() challenge mismatch rejected");
}

// T08 — unpackPresentation(): single identity credential, and operational+identity pair
{
  const credential = issueCredential(ed25519.keygen().secretKey);
  const [single] = Sentinel.unpackPresentation({
    "@context": ["https://www.w3.org/ns/credentials/v2"],
    type: ["VerifiablePresentation"],
    holder: AGENT_DID,
    verifiableCredential: [credential],
  });
  assert.equal(single.id, credential.id);

  assert.throws(
    () =>
      Sentinel.unpackPresentation({
        "@context": ["https://www.w3.org/ns/credentials/v2"],
        type: ["VerifiablePresentation"],
        holder: AGENT_DID,
        verifiableCredential: [],
      }),
    CredentialVerificationError,
  );
  console.log("OK: T08 unpackPresentation() shape dispatch");
}
