/**
 * delegation.smoke.ts
 * ---------------------
 * Node mirror of python/stls-identity/tests/test_delegation.py:
 * DelegationManager's depth-1/role/validity-window enforcement, issue
 * -> Sentinel.verifyDelegationChain() round-trip, and revoke().
 */

import assert from "node:assert/strict";
import { ed25519 } from "@noble/curves/ed25519.js";
import { IdentityManager } from "../src/issuer.js";
import { DelegationManager } from "../src/delegation.js";
import { Sentinel } from "../src/verifier.js";
import type { NetworkAuthorityCredential, OperationalCredential } from "../src/models.js";

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
const GRANTOR_DID = "did:web:sofia.sparkle.example.com:agents:grantor";
const GRANTOR_VM_ID = `${GRANTOR_DID}#key-1`;
const REQUESTOR_DID = "did:web:sofia.sparkle.example.com:agents:requestor";

function setup() {
  const authority = ed25519.keygen();
  const grantor = ed25519.keygen();
  const identityStatus = new InMemoryStatusListManager();
  const opStatus = new InMemoryStatusListManager();

  const issuer = new IdentityManager({
    governingAuthorityDid: AUTHORITY_DID,
    signingKey: authority.secretKey,
    signingVerificationMethodId: AUTHORITY_VM_ID,
    statusListManager: identityStatus,
    statusListPublishUrl: "https://sofia.sparkle.example.com/identity/status-list",
  });

  const delegationManager = new DelegationManager({
    grantorDid: GRANTOR_DID,
    grantorSigningKey: grantor.secretKey,
    grantorVerificationMethodId: GRANTOR_VM_ID,
    statusListManager: opStatus,
    statusListPublishUrl: "https://sofia.sparkle.example.com/grantor/status-list",
  });

  const sentinel = new Sentinel({
    trustedIssuerKeys: new Map([
      [AUTHORITY_VM_ID, authority.publicKey],
      [GRANTOR_VM_ID, grantor.publicKey],
    ]),
  });

  return { authority, grantor, identityStatus, opStatus, issuer, delegationManager, sentinel };
}

async function issueGrantorIdentity(
  issuer: IdentityManager,
  roles: string[] = ["grantor"],
): Promise<NetworkAuthorityCredential> {
  return issuer.issueIdentityCredential({ agentDid: GRANTOR_DID, clientId: "agent-grantor", roles });
}

// T01 — issuing a delegation without the "grantor" role is rejected
{
  const { issuer, delegationManager } = setup();
  const grantorIdentity = await issueGrantorIdentity(issuer, []); // no "grantor" role
  await assert.rejects(
    () =>
      delegationManager.issueOperationalCredential({
        grantorIdentityCredential: grantorIdentity,
        requestorDid: REQUESTOR_DID,
        requestorClientId: "agent-requestor",
        scope: {},
      }),
    /grantor' role/,
  );
  console.log('OK: T01 rejects delegation without the "grantor" role');
}

// T02 — issuing a delegation with an identity credential for a DIFFERENT subject is rejected
{
  const { issuer, delegationManager } = setup();
  const notTheGrantorIdentity = await issuer.issueIdentityCredential({
    agentDid: "did:web:sofia.sparkle.example.com:agents:someone-else",
    clientId: "someone-else",
    roles: ["grantor"],
  });
  await assert.rejects(
    () =>
      delegationManager.issueOperationalCredential({
        grantorIdentityCredential: notTheGrantorIdentity,
        requestorDid: REQUESTOR_DID,
        requestorClientId: "agent-requestor",
        scope: {},
      }),
    /does not belong to this Grantor/,
  );
  console.log("OK: T02 rejects an identity credential belonging to a different subject");
}

// T03 — issuing a delegation with an OperationalCredential masquerading
// as the grantor identity is rejected (the runtime half of the depth-1 enforcement)
{
  const { issuer, delegationManager, sentinel } = setup();
  const grantorIdentity = await issueGrantorIdentity(issuer);
  const operational = await delegationManager.issueOperationalCredential({
    grantorIdentityCredential: grantorIdentity,
    requestorDid: REQUESTOR_DID,
    requestorClientId: "agent-requestor",
    scope: {},
  });

  await assert.rejects(
    () =>
      delegationManager.issueOperationalCredential({
        grantorIdentityCredential: operational as unknown as NetworkAuthorityCredential,
        requestorDid: "did:web:sofia.sparkle.example.com:agents:another-requestor",
        requestorClientId: "another-requestor",
        scope: {},
      }),
    TypeError,
  );
  void sentinel;
  console.log("OK: T03 rejects an OperationalCredential masquerading as the grantor identity");
}

// T04 — full chain: issue grantor identity, delegate, verify the chain
{
  const { issuer, delegationManager, sentinel } = setup();
  const grantorIdentity = await issueGrantorIdentity(issuer);
  const operational = await delegationManager.issueOperationalCredential({
    grantorIdentityCredential: grantorIdentity,
    requestorDid: REQUESTOR_DID,
    requestorClientId: "agent-requestor",
    scope: { action: "cross-connect-activate" },
  });

  assert.equal(operational.delegationDepth, 1);
  assert.equal(operational.issuer, GRANTOR_DID);
  assert.equal(operational.credentialSubject.id, REQUESTOR_DID);

  await sentinel.verifyDelegationChain(operational, {
    grantorIdentityCredential: grantorIdentity,
    grantorIdentityFetchTime: new Date(),
    grantorIdentityIsRevoked: false,
    operationalFetchTime: new Date(),
    operationalIsRevoked: false,
  });
  console.log("OK: T04 full chain issues and verifies");
}

// T05 — revoking the Grantor's identity fails the chain even though the
// OperationalCredential's OWN status is untouched (two independent lists)
{
  const { issuer, delegationManager, sentinel } = setup();
  const grantorIdentity = await issueGrantorIdentity(issuer);
  const operational = await delegationManager.issueOperationalCredential({
    grantorIdentityCredential: grantorIdentity,
    requestorDid: REQUESTOR_DID,
    requestorClientId: "agent-requestor",
    scope: {},
  });

  await assert.rejects(() =>
    sentinel.verifyDelegationChain(operational, {
      grantorIdentityCredential: grantorIdentity,
      grantorIdentityFetchTime: new Date(),
      grantorIdentityIsRevoked: true, // Grantor's own identity revoked
      operationalFetchTime: new Date(),
      operationalIsRevoked: false, // the delegation itself was never touched
    }),
  );
  console.log("OK: T05 revoking the Grantor's identity fails the chain independently");
}

// T06 — revoke() on the DelegationManager flips the operational credential's own bit
{
  const { issuer, delegationManager, opStatus } = setup();
  const grantorIdentity = await issueGrantorIdentity(issuer);
  const operational: OperationalCredential = await delegationManager.issueOperationalCredential({
    grantorIdentityCredential: grantorIdentity,
    requestorDid: REQUESTOR_DID,
    requestorClientId: "agent-requestor",
    scope: {},
  });

  await delegationManager.revoke(operational);
  assert.equal(await opStatus.isRevoked(operational.credentialStatus.statusListIndex), true);
  console.log("OK: T06 DelegationManager.revoke() flips the operational credential's bit");
}

// T07 — issuing with an expired grantor identity credential is rejected
{
  const { issuer, delegationManager } = setup();
  const grantorIdentity = await issueGrantorIdentity(issuer);
  const expired: NetworkAuthorityCredential = {
    ...grantorIdentity,
    validUntil: new Date(Date.now() - 1000).toISOString(),
  };
  await assert.rejects(
    () =>
      delegationManager.issueOperationalCredential({
        grantorIdentityCredential: expired,
        requestorDid: REQUESTOR_DID,
        requestorClientId: "agent-requestor",
        scope: {},
      }),
    /outside its validity window/,
  );
  console.log("OK: T07 rejects delegation on an expired grantor identity credential");
}
