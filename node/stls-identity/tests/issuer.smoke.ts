/**
 * issuer.smoke.ts
 * ----------------
 * Node mirror of python/stls-identity/tests/test_identity_e2e.py's
 * IdentityManager coverage: issue -> Sentinel.verify() round-trip,
 * and revoke() flipping the status bit.
 */

import assert from "node:assert/strict";
import { ed25519 } from "@noble/curves/ed25519.js";
import { IdentityManager } from "../src/issuer.js";
import { Sentinel } from "../src/verifier.js";

class InMemoryStatusListManager {
  private next = 0;
  private revoked = new Set<number>();
  async allocateIndex(): Promise<number> {
    return this.next++;
  }
  async revoke(index: number): Promise<void> {
    this.revoked.add(index);
  }
  async isRevoked(index: number): Promise<boolean> {
    return this.revoked.has(index);
  }
}

const AUTHORITY_DID = "did:web:sofia.sparkle.example.com";
const AUTHORITY_VM_ID = `${AUTHORITY_DID}#key-1`;

// T01 — issue -> verify (full pipeline) succeeds
{
  const authority = ed25519.keygen();
  const status = new InMemoryStatusListManager();
  const issuer = new IdentityManager({
    governingAuthorityDid: AUTHORITY_DID,
    signingKey: authority.secretKey,
    signingVerificationMethodId: AUTHORITY_VM_ID,
    statusListManager: status,
    statusListPublishUrl: "https://sofia.sparkle.example.com/identity/status-list",
    auditHook: async () => {},
  });

  const credential = await issuer.issueIdentityCredential({
    agentDid: "did:web:sofia.sparkle.example.com:agents:087",
    clientId: "agent-087",
    roles: ["grantor"],
  });

  assert.equal(credential.credentialSubject.roles[0], "grantor");
  assert.equal(credential.credentialStatus.statusListIndex, 0);

  const sentinel = new Sentinel({ trustedIssuerKeys: new Map([[AUTHORITY_VM_ID, authority.publicKey]]) });
  await sentinel.verify(credential, { statusBitstringFetchTime: new Date(), isRevoked: false });
  console.log("OK: T01 issue -> Sentinel.verify() round-trip");
}

// T02 — revoke() flips the status bit that Sentinel.verify() checks
{
  const authority = ed25519.keygen();
  const status = new InMemoryStatusListManager();
  const issuer = new IdentityManager({
    governingAuthorityDid: AUTHORITY_DID,
    signingKey: authority.secretKey,
    signingVerificationMethodId: AUTHORITY_VM_ID,
    statusListManager: status,
    statusListPublishUrl: "https://sofia.sparkle.example.com/identity/status-list",
  });

  const credential = await issuer.issueIdentityCredential({
    agentDid: "did:web:sofia.sparkle.example.com:agents:088",
    clientId: "agent-088",
  });
  await issuer.revoke(credential);

  const isRevoked = await status.isRevoked(credential.credentialStatus.statusListIndex);
  assert.equal(isRevoked, true);

  const sentinel = new Sentinel({ trustedIssuerKeys: new Map([[AUTHORITY_VM_ID, authority.publicKey]]) });
  await assert.rejects(() =>
    sentinel.verify(credential, { statusBitstringFetchTime: new Date(), isRevoked: true }),
  );
  console.log("OK: T02 revoke() flips the bit Sentinel.verify() checks");
}

// T03 — two credentials issued in sequence get distinct, increasing status indices
{
  const authority = ed25519.keygen();
  const status = new InMemoryStatusListManager();
  const issuer = new IdentityManager({
    governingAuthorityDid: AUTHORITY_DID,
    signingKey: authority.secretKey,
    signingVerificationMethodId: AUTHORITY_VM_ID,
    statusListManager: status,
    statusListPublishUrl: "https://sofia.sparkle.example.com/identity/status-list",
  });

  const c1 = await issuer.issueIdentityCredential({ agentDid: "did:web:x:agents:a", clientId: "a" });
  const c2 = await issuer.issueIdentityCredential({ agentDid: "did:web:x:agents:b", clientId: "b" });
  assert.equal(c1.credentialStatus.statusListIndex, 0);
  assert.equal(c2.credentialStatus.statusListIndex, 1);
  assert.notEqual(c1.id, c2.id);
  console.log("OK: T03 sequential issuance gets distinct status indices and ids");
}
