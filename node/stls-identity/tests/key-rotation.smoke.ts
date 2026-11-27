/**
 * key-rotation.smoke.ts
 * ----------------------
 * Node mirror of python/stls-identity's test_agent_key_rotation.py
 * (issue #42 — single-agent key rotation / DID Document key history).
 */

import assert from "node:assert/strict";
import { ed25519 } from "@noble/curves/ed25519.js";

import {
  agentIdFromVerificationMethodId,
  bootstrapSigningKey,
  buildDidDocument,
  currentVerificationMethod,
  domainAndPathToDid,
  makeDidDocumentHolderKeyResolver,
  resolveHolderKeyAt,
  rotateAgentSigningKey,
  rotateSigningKey,
} from "../src/did.js";
import { toPydanticIsoString } from "../src/models.js";
import { InMemoryIdentityKeystore } from "../src/keystore.js";
import { buildVerifiablePresentation } from "../src/presentation.js";
import { CredentialVerificationError, Sentinel } from "../src/verifier.js";
import { IdentityManager, type StatusListManagerLike } from "../src/issuer.js";
import type { DIDDocument, NetworkAuthorityCredential } from "../src/models.js";

const HOST = "sofia.sparkle.example.com";
const AUTHORITY_DID = `did:web:${HOST}`;

class InMemoryStatusListManager implements StatusListManagerLike {
  private next = 0;
  async allocateIndex(): Promise<number> {
    return this.next++;
  }
  async revoke(_index: number): Promise<void> {}
}

class FakeDidDocumentStore {
  private readonly docs = new Map<string, DIDDocument>();
  async get(agentId: string): Promise<DIDDocument | null> {
    return this.docs.get(agentId) ?? null;
  }
  async put(agentId: string, document: DIDDocument): Promise<void> {
    this.docs.set(agentId, document);
  }
}

async function bootstrapAgent(keystore: InMemoryIdentityKeystore, agentId: string) {
  const { privateKey, publicKey } = await bootstrapSigningKey(agentId, keystore);
  const document = buildDidDocument({
    host: HOST,
    agentPathSegments: ["agents", agentId],
    governingAuthorityDid: AUTHORITY_DID,
    signingPublicKey: publicKey,
  });
  return { privateKey, publicKey, document };
}

// T01 — rotation closes the previous key and opens a new one
{
  const keystore = new InMemoryIdentityKeystore();
  const { document } = await bootstrapAgent(keystore, "sofia-admin-coordinator");
  const oldVmId = document.verificationMethod[0].id;
  assert.equal(document.verificationMethod[0].validUntil, undefined);

  const newKey = ed25519.keygen();
  const now = new Date();
  const rotated = rotateAgentSigningKey(document, { newSigningPublicKey: newKey.publicKey, now });

  assert.equal(rotated.verificationMethod.length, 2);
  const oldEntry = rotated.verificationMethod.find((vm) => vm.id === oldVmId)!;
  assert.equal(oldEntry.validUntil, toPydanticIsoString(now));

  const newEntry = currentVerificationMethod(rotated);
  assert.notEqual(newEntry.id, oldVmId);
  assert.equal(newEntry.validUntil, undefined);
  assert.deepEqual(rotated.authentication, [newEntry.id]);
  assert.deepEqual(rotated.assertionMethod, [newEntry.id]);
  console.log("OK: T01 rotation closes previous key, opens a new one");
}

// T02 — multiple rotations keep exactly one current entry
{
  const keystore = new InMemoryIdentityKeystore();
  let { document } = await bootstrapAgent(keystore, "agent-087");

  for (let i = 0; i < 3; i++) {
    const fresh = ed25519.keygen();
    document = rotateAgentSigningKey(document, { newSigningPublicKey: fresh.publicKey });
  }

  assert.equal(document.verificationMethod.length, 4);
  const current = currentVerificationMethod(document);
  assert.deepEqual(document.authentication, [current.id]);
  const closed = document.verificationMethod.filter((vm) => vm.id !== current.id);
  assert.ok(closed.every((vm) => vm.validUntil != null));
  console.log("OK: T02 multiple rotations keep exactly one current entry");
}

// T03 — currentVerificationMethod() throws when ambiguous
{
  const keystore = new InMemoryIdentityKeystore();
  const { document } = await bootstrapAgent(keystore, "agent-ambiguous");
  const doubled: DIDDocument = {
    ...document,
    verificationMethod: [...document.verificationMethod, ...document.verificationMethod],
  };
  assert.throws(() => currentVerificationMethod(doubled));
  console.log("OK: T03 currentVerificationMethod() rejects ambiguous document");
}

// T04 — resolveHolderKeyAt() picks the key valid at signing time
{
  const keystore = new InMemoryIdentityKeystore();
  const { publicKey: oldPub, document } = await bootstrapAgent(keystore, "agent-rotating");
  const oldVmId = document.verificationMethod[0].id;

  const tBeforeRotation = new Date();
  const rotationTime = new Date(tBeforeRotation.getTime() + 3_600_000);
  const newKey = ed25519.keygen();
  const rotated = rotateAgentSigningKey(document, {
    newSigningPublicKey: newKey.publicKey,
    now: rotationTime,
  });
  const newVmId = currentVerificationMethod(rotated).id;

  const resolvedOld = resolveHolderKeyAt(rotated, oldVmId, tBeforeRotation);
  assert.ok(resolvedOld !== null);
  assert.deepEqual(resolvedOld, oldPub);

  assert.equal(
    resolveHolderKeyAt(rotated, oldVmId, new Date(rotationTime.getTime() + 1000)),
    null,
  );

  const resolvedNew = resolveHolderKeyAt(rotated, newVmId, new Date(rotationTime.getTime() + 1000));
  assert.ok(resolvedNew !== null);
  assert.deepEqual(resolvedNew, newKey.publicKey);

  assert.equal(resolveHolderKeyAt(rotated, `${document.id}#key-99`, tBeforeRotation), null);
  console.log("OK: T04 resolveHolderKeyAt() picks the key valid at signing time");
}

// T05 — resolveHolderKeyAt() respects revocation even inside the validity window
{
  const keystore = new InMemoryIdentityKeystore();
  const { document } = await bootstrapAgent(keystore, "agent-revoked");
  const vmId = document.verificationMethod[0].id;
  const signedAt = new Date();
  const revokedAt = new Date(signedAt.getTime() - 60_000);

  const revoked: DIDDocument = {
    ...document,
    verificationMethod: [{ ...document.verificationMethod[0], revokedAt: revokedAt.toISOString() }],
  };
  assert.equal(resolveHolderKeyAt(revoked, vmId, signedAt), null);
  console.log("OK: T05 resolveHolderKeyAt() respects revocation inside the validity window");
}

// T06 — agentIdFromVerificationMethodId() round-trips, rejects Authority key
{
  const did = domainAndPathToDid(HOST, "agents", "sofia-admin-coordinator");
  assert.equal(agentIdFromVerificationMethodId(`${did}#key-2`, HOST), "sofia-admin-coordinator");
  assert.equal(agentIdFromVerificationMethodId(`${AUTHORITY_DID}#key-1`, HOST), null);
  console.log("OK: T06 agentIdFromVerificationMethodId() round-trips, rejects Authority key");
}

// T07 — IdentityKeystoreBase.rotate() overwrites the stored private key
{
  const keystore = new InMemoryIdentityKeystore();
  const { privateKey: oldKey } = await bootstrapAgent(keystore, "agent-x");
  const newKey = await keystore.rotate("agent-x");
  const stored = await keystore.get("agent-x");
  assert.deepEqual(stored, newKey);
  assert.notDeepEqual(stored, oldKey);
  console.log("OK: T07 keystore.rotate() overwrites the stored private key");
}

// T08 — a presentation signed with the OLD key still verifies after rotation
{
  const keystore = new InMemoryIdentityKeystore();
  const didStore = new FakeDidDocumentStore();
  const agentId = "agent-presenting";

  const { privateKey: oldKey, publicKey: oldPub, document } = await bootstrapAgent(keystore, agentId);
  await didStore.put(agentId, document);
  const oldVmId = document.verificationMethod[0].id;

  const issuer = new IdentityManager({
    governingAuthorityDid: AUTHORITY_DID,
    signingKey: oldKey,
    signingVerificationMethodId: oldVmId,
    statusListManager: new InMemoryStatusListManager(),
    statusListPublishUrl: `https://${HOST}/identity/status-list`,
  });
  const credential: NetworkAuthorityCredential = await issuer.issueIdentityCredential({
    agentDid: document.id,
    clientId: agentId,
  });

  const challenge = "challenge-t08";
  const vp = buildVerifiablePresentation({
    holderDid: document.id,
    holderSigningKey: oldKey,
    holderVerificationMethodId: oldVmId,
    credentials: [credential],
    challenge,
  });

  const newKey = await keystore.rotate(agentId);
  const newPub = ed25519.getPublicKey(newKey);
  const rotated = rotateAgentSigningKey(document, { newSigningPublicKey: newPub });
  await didStore.put(agentId, rotated);

  const resolver = makeDidDocumentHolderKeyResolver(didStore, HOST);
  const sentinel = new Sentinel({
    trustedIssuerKeys: new Map([[oldVmId, oldPub]]),
    holderKeyResolver: resolver,
  });

  await sentinel.verifyPresentationProof(vp, { expectedChallenge: challenge }); // must not throw
  console.log("OK: T08 presentation signed with old key still verifies after rotation");
}

// T09 — a presentation signed with the NEW key resolves via fallback, no cache update
{
  const keystore = new InMemoryIdentityKeystore();
  const didStore = new FakeDidDocumentStore();
  const agentId = "agent-presenting-2";

  const { privateKey: oldKey, document } = await bootstrapAgent(keystore, agentId);
  await didStore.put(agentId, document);

  const newKey = await keystore.rotate(agentId);
  const newPub = ed25519.getPublicKey(newKey);
  const rotated = rotateAgentSigningKey(document, { newSigningPublicKey: newPub });
  await didStore.put(agentId, rotated);
  const newVmId = currentVerificationMethod(rotated).id;

  const issuer = new IdentityManager({
    governingAuthorityDid: AUTHORITY_DID,
    signingKey: oldKey,
    signingVerificationMethodId: document.verificationMethod[0].id,
    statusListManager: new InMemoryStatusListManager(),
    statusListPublishUrl: `https://${HOST}/identity/status-list`,
  });
  const credential: NetworkAuthorityCredential = await issuer.issueIdentityCredential({
    agentDid: document.id,
    clientId: agentId,
  });

  const challenge = "challenge-t09";
  const vp = buildVerifiablePresentation({
    holderDid: document.id,
    holderSigningKey: newKey,
    holderVerificationMethodId: newVmId,
    credentials: [credential],
    challenge,
  });

  const resolver = makeDidDocumentHolderKeyResolver(didStore, HOST);
  const sentinel = new Sentinel({ trustedIssuerKeys: new Map(), holderKeyResolver: resolver });

  await sentinel.verifyPresentationProof(vp, { expectedChallenge: challenge }); // must not throw
  console.log("OK: T09 presentation signed with new key resolves via fallback");
}

// T10 — without holderKeyResolver configured, an unknown holder key is rejected (strictly additive)
{
  const keystore = new InMemoryIdentityKeystore();
  const agentId = "agent-no-resolver";
  const { privateKey: key, document } = await bootstrapAgent(keystore, agentId);
  const vmId = document.verificationMethod[0].id;

  const issuer = new IdentityManager({
    governingAuthorityDid: AUTHORITY_DID,
    signingKey: key,
    signingVerificationMethodId: vmId,
    statusListManager: new InMemoryStatusListManager(),
    statusListPublishUrl: `https://${HOST}/identity/status-list`,
  });
  const credential: NetworkAuthorityCredential = await issuer.issueIdentityCredential({
    agentDid: document.id,
    clientId: agentId,
  });

  const challenge = "challenge-t10";
  const vp = buildVerifiablePresentation({
    holderDid: document.id,
    holderSigningKey: key,
    holderVerificationMethodId: vmId,
    credentials: [credential],
    challenge,
  });

  const sentinel = new Sentinel({ trustedIssuerKeys: new Map() }); // no holderKeyResolver
  await assert.rejects(
    () => sentinel.verifyPresentationProof(vp, { expectedChallenge: challenge }),
    CredentialVerificationError,
  );
  console.log("OK: T10 unknown holder key rejected when holderKeyResolver isn't configured");
}

// T11 — rotateSigningKey() full orchestration
{
  const keystore = new InMemoryIdentityKeystore();
  const didStore = new FakeDidDocumentStore();
  const agentId = "agent-full-orchestration";

  const { document } = await bootstrapAgent(keystore, agentId);
  await didStore.put(agentId, document);
  const oldVmId = document.verificationMethod[0].id;

  const { newPrivateKey, document: updatedDocument } = await rotateSigningKey(
    agentId,
    keystore,
    didStore,
  );

  const storedKey = await keystore.get(agentId);
  assert.deepEqual(storedKey, newPrivateKey);
  const stored = await didStore.get(agentId);
  assert.equal(stored!.id, updatedDocument.id);
  const current = currentVerificationMethod(stored!);
  assert.notEqual(current.id, oldVmId);
  const oldEntry = stored!.verificationMethod.find((vm) => vm.id === oldVmId)!;
  assert.ok(oldEntry.validUntil != null);
  console.log("OK: T11 rotateSigningKey() full orchestration");
}

// T12 — rotateSigningKey() throws for an agent with no DID Document
{
  const keystore = new InMemoryIdentityKeystore();
  const didStore = new FakeDidDocumentStore();
  await assert.rejects(() => rotateSigningKey("never-bootstrapped", keystore, didStore));
  console.log("OK: T12 rotateSigningKey() throws for a never-bootstrapped agent");
}

console.log("ALL KEY ROTATION SMOKE CHECKS PASSED");
