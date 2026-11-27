/**
 * request-verifier.smoke.ts
 * ---------------------------
 * Node mirror of python/stls-identity/tests/test_request_verifier.py.
 */

import assert from "node:assert/strict";
import { ed25519 } from "@noble/curves/ed25519.js";
import { signCredentialPayload } from "../src/signing.js";
import { buildVerifiablePresentation, attachIdentityPresentationHeader } from "../src/presentation.js";
import { RequestPresentationVerifier } from "../src/request-verifier.js";
import { IdentityManager } from "../src/issuer.js";
import { DelegationManager } from "../src/delegation.js";
import { Sentinel } from "../src/verifier.js";
import { KeyStorageTier, PRESENTATION_HEADER, type NetworkAuthorityCredential } from "../src/models.js";

const AUTHORITY_DID = "did:web:sofia.sparkle.example.com";
const AUTHORITY_VM_ID = `${AUTHORITY_DID}#key-1`;
const AGENT_DID = "did:web:sofia.sparkle.example.com:agents:087";
const AGENT_VM_ID = `${AGENT_DID}#key-1`;

function issueCredential(signingKey: Uint8Array): NetworkAuthorityCredential {
  const now = new Date();
  const withoutProof = {
    "@context": ["https://www.w3.org/ns/credentials/v2"],
    id: "urn:uuid:11111111-0000-4000-8000-000000000001",
    type: ["VerifiableCredential", "NetworkAuthorityCredential"] as ["VerifiableCredential", "NetworkAuthorityCredential"],
    schemaVersion: "1.0",
    issuer: AUTHORITY_DID,
    validFrom: now.toISOString(),
    validUntil: new Date(now.getTime() + 86_400_000).toISOString(),
    credentialSubject: {
      id: AGENT_DID,
      clientId: "agent-087",
      keyStorageTier: KeyStorageTier.Tier1Software,
      roles: [],
    },
    credentialStatus: {
      id: "https://sofia.sparkle.example.com/identity/status-list#0",
      type: "BitstringStatusListEntry" as const,
      statusPurpose: "revocation" as const,
      statusListIndex: 0,
      statusListCredential: "https://sofia.sparkle.example.com/identity/status-list",
    },
  };
  return {
    ...withoutProof,
    proof: {
      type: "DataIntegrityProof",
      cryptosuite: "eddsa-jcs-2022",
      created: now.toISOString(),
      verificationMethod: AUTHORITY_VM_ID,
      proofPurpose: "assertionMethod",
      proofValue: signCredentialPayload(withoutProof, signingKey),
    },
  };
}

function fakeRequest(headers: Record<string, string>) {
  return { headers };
}

// T01 — end-to-end: build a presentation, attach header, verify it
{
  const authority = ed25519.keygen();
  const holder = ed25519.keygen();
  const credential = issueCredential(authority.secretKey);

  const vp = buildVerifiablePresentation({
    holderDid: AGENT_DID,
    holderSigningKey: holder.secretKey,
    holderVerificationMethodId: AGENT_VM_ID,
    credentials: [credential],
    challenge: "server-issued-challenge",
  });
  const headers = attachIdentityPresentationHeader(vp);

  const sentinel = new Sentinel({
    trustedIssuerKeys: new Map([
      [AUTHORITY_VM_ID, authority.publicKey],
      [AGENT_VM_ID, holder.publicKey],
    ]),
  });
  const verifier = new RequestPresentationVerifier(sentinel, async () => ({
    fetchTime: new Date(),
    isRevoked: false,
  }));

  const result = await verifier.verifyRequest(fakeRequest(headers), { challenge: "server-issued-challenge" });
  // Plain identity presentation (issue #74): requestorDid is set, but
  // there's no delegation involved, so grantorDid/scope stay undefined.
  assert.deepEqual(result, { ok: true, requestorDid: AGENT_DID });
  console.log("OK: T01 end-to-end verifyRequest() succeeds");
}

// T02 — missing header
{
  const sentinel = new Sentinel({ trustedIssuerKeys: new Map() });
  const verifier = new RequestPresentationVerifier(sentinel, async () => ({
    fetchTime: new Date(),
    isRevoked: false,
  }));
  const result = await verifier.verifyRequest(fakeRequest({}), { challenge: "x" });
  assert.equal(result.ok, false);
  assert.match(result.error ?? "", /missing presentation header/);
  console.log("OK: T02 missing header -> ok:false");
}

// T03 — malformed base64/JSON
{
  const sentinel = new Sentinel({ trustedIssuerKeys: new Map() });
  const verifier = new RequestPresentationVerifier(sentinel, async () => ({
    fetchTime: new Date(),
    isRevoked: false,
  }));
  const result = await verifier.verifyRequest(
    fakeRequest({ [PRESENTATION_HEADER]: "not-valid-base64!!!" }),
    { challenge: "x" },
  );
  assert.equal(result.ok, false);
  assert.match(result.error ?? "", /malformed presentation/);
  console.log("OK: T03 malformed presentation -> ok:false");
}

// T04 — wrong challenge surfaces as a CredentialVerificationError message, not a throw
{
  const authority = ed25519.keygen();
  const holder = ed25519.keygen();
  const credential = issueCredential(authority.secretKey);
  const vp = buildVerifiablePresentation({
    holderDid: AGENT_DID,
    holderSigningKey: holder.secretKey,
    holderVerificationMethodId: AGENT_VM_ID,
    credentials: [credential],
    challenge: "original-challenge",
  });
  const headers = attachIdentityPresentationHeader(vp);

  const sentinel = new Sentinel({
    trustedIssuerKeys: new Map([
      [AUTHORITY_VM_ID, authority.publicKey],
      [AGENT_VM_ID, holder.publicKey],
    ]),
  });
  const verifier = new RequestPresentationVerifier(sentinel, async () => ({
    fetchTime: new Date(),
    isRevoked: false,
  }));

  const result = await verifier.verifyRequest(fakeRequest(headers), { challenge: "different-challenge" });
  assert.equal(result.ok, false);
  assert.match(result.error ?? "", /challenge/);
  console.log("OK: T04 challenge mismatch surfaces as ok:false, not a throw");
}

// T05 — revoked credential surfaces as ok:false via the status resolver
{
  const authority = ed25519.keygen();
  const holder = ed25519.keygen();
  const credential = issueCredential(authority.secretKey);
  const vp = buildVerifiablePresentation({
    holderDid: AGENT_DID,
    holderSigningKey: holder.secretKey,
    holderVerificationMethodId: AGENT_VM_ID,
    credentials: [credential],
    challenge: "challenge-revoked-case",
  });
  const headers = attachIdentityPresentationHeader(vp);

  const sentinel = new Sentinel({
    trustedIssuerKeys: new Map([
      [AUTHORITY_VM_ID, authority.publicKey],
      [AGENT_VM_ID, holder.publicKey],
    ]),
  });
  const verifier = new RequestPresentationVerifier(sentinel, async () => ({
    fetchTime: new Date(),
    isRevoked: true,
  }));

  const result = await verifier.verifyRequest(fakeRequest(headers), { challenge: "challenge-revoked-case" });
  assert.equal(result.ok, false);
  assert.match(result.error ?? "", /revoked/);
  console.log("OK: T05 revoked credential surfaces as ok:false");
}

// T06 — delegated presentation (issue #74): the verified, unpacked
// grant must survive past the ok check — requestorDid/grantorDid/scope
// all populated, mirroring python's test_valid_delegated_presentation_is_accepted.
{
  class InMemoryStatusListManager {
    private next = 0;
    async allocateIndex(): Promise<number> {
      return this.next++;
    }
    async revoke(): Promise<void> {}
    async isRevoked(): Promise<boolean> {
      return false;
    }
  }

  const authority = ed25519.keygen();
  const grantor = ed25519.keygen();
  const requestor = ed25519.keygen();
  const grantorDid = "did:web:sofia.sparkle.example.com:agents:099";
  const grantorVmId = `${grantorDid}#key-1`;
  const requestorDid = "did:web:b.example.com:agents:042";
  const requestorVmId = `${requestorDid}#key-1`;

  const issuer = new IdentityManager({
    governingAuthorityDid: AUTHORITY_DID,
    signingKey: authority.secretKey,
    signingVerificationMethodId: AUTHORITY_VM_ID,
    statusListManager: new InMemoryStatusListManager(),
    statusListPublishUrl: "https://sofia.sparkle.example.com/identity/status-list",
  });
  const grantorIdentity = await issuer.issueIdentityCredential({
    agentDid: grantorDid, clientId: "sparkle-agent-099", roles: ["grantor"],
  });

  const delegationManager = new DelegationManager({
    grantorDid, grantorSigningKey: grantor.secretKey, grantorVerificationMethodId: grantorVmId,
    statusListManager: new InMemoryStatusListManager(),
    statusListPublishUrl: "https://sofia.sparkle.example.com/agents/099/status-list",
  });
  const operational = await delegationManager.issueOperationalCredential({
    grantorIdentityCredential: grantorIdentity,
    requestorDid, requestorClientId: "b-agent-042",
    scope: { action: "reroute_traffic", segment: "MER-SEG-7" },
  });

  const vp = buildVerifiablePresentation({
    holderDid: requestorDid,
    holderSigningKey: requestor.secretKey,
    holderVerificationMethodId: requestorVmId,
    credentials: [operational, grantorIdentity],
    challenge: "delegation-challenge",
  });
  const headers = attachIdentityPresentationHeader(vp);

  const sentinel = new Sentinel({
    trustedIssuerKeys: new Map([
      [AUTHORITY_VM_ID, authority.publicKey],
      [grantorVmId, grantor.publicKey],
      [requestorVmId, requestor.publicKey],
    ]),
  });
  const verifier = new RequestPresentationVerifier(sentinel, async () => ({
    fetchTime: new Date(), isRevoked: false,
  }));

  const result = await verifier.verifyRequest(fakeRequest(headers), { challenge: "delegation-challenge" });
  assert.deepEqual(result, {
    ok: true,
    requestorDid,
    grantorDid,
    scope: { action: "reroute_traffic", segment: "MER-SEG-7" },
  });
  console.log("OK: T06 delegated presentation carries requestorDid/grantorDid/scope through");
}
