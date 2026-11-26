/**
 * presentation.smoke.ts
 * ----------------------
 * Node mirror of python/stls-identity/tests/test_presentation.py's pure
 * (no keystore/credential-cache I/O) coverage: build + verify a
 * VerifiablePresentation end-to-end using this package's own
 * signing.ts, and the replay-protection property the whole
 * proof-minus-proofValue design exists for.
 */

import assert from "node:assert/strict";
import { ed25519 } from "@noble/curves/ed25519.js";
import {
  attachIdentityPresentationHeader,
  buildVerifiablePresentation,
  identityChallengeHandlerFromEnv,
  loadDelegationPresenter,
  loadIdentityPresenter,
} from "../src/presentation.js";
import { verifyProofBoundPayload } from "../src/signing.js";
import { KeyStorageTier, PRESENTATION_HEADER, type NetworkAuthorityCredential } from "../src/models.js";
import { IdentityKeystoreFactory } from "../src/keystore.js";
import { IdentityCredentialCacheFactory, OperationalCredentialCacheFactory } from "../src/credential-cache.js";

function makeFixtureCredential(): NetworkAuthorityCredential {
  return {
    "@context": ["https://www.w3.org/ns/credentials/v2"],
    id: "urn:uuid:11111111-0000-4000-8000-000000000001",
    type: ["VerifiableCredential", "NetworkAuthorityCredential"],
    schemaVersion: "1.0",
    issuer: "did:web:sofia.sparkle.example.com",
    validFrom: "2026-01-01T00:00:00.000Z",
    validUntil: "2026-12-31T00:00:00.000Z",
    credentialSubject: {
      id: "did:web:sofia.sparkle.example.com:agents:087",
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
    proof: {
      type: "DataIntegrityProof",
      cryptosuite: "eddsa-jcs-2022",
      created: "2026-01-01T00:00:00.000Z",
      verificationMethod: "did:web:sofia.sparkle.example.com#key-1",
      proofPurpose: "assertionMethod",
      proofValue: "z-not-checked-by-this-test",
    },
  };
}

// T01 — build + verify round-trip
{
  const holder = ed25519.keygen();
  const holderDid = "did:web:sofia.sparkle.example.com:agents:087";
  const holderVmId = `${holderDid}#key-1`;

  const vp = buildVerifiablePresentation({
    holderDid,
    holderSigningKey: holder.secretKey,
    holderVerificationMethodId: holderVmId,
    credentials: [makeFixtureCredential()],
    challenge: "challenge-0001",
    now: new Date("2026-01-15T12:00:00.000Z"),
  });

  assert.equal(vp.holder, holderDid);
  assert.equal(vp.proof?.challenge, "challenge-0001");
  assert.ok(vp.proof?.proofValue.startsWith("z"));

  const ok = verifyProofBoundPayload(vp, vp.proof!.proofValue, holder.publicKey);
  assert.ok(ok, "a presentation must verify against the holder key that signed it");
  console.log("OK: T01 build + verify VerifiablePresentation round-trip");
}

// T02 — tampering with the challenge AFTER signing must break verification
// (the exact replay attack signing-over-proof-minus-proofValue prevents).
{
  const holder = ed25519.keygen();
  const vp = buildVerifiablePresentation({
    holderDid: "did:web:sofia.sparkle.example.com:agents:087",
    holderSigningKey: holder.secretKey,
    holderVerificationMethodId: "did:web:sofia.sparkle.example.com:agents:087#key-1",
    credentials: [makeFixtureCredential()],
    challenge: "original-challenge",
  });

  const tampered = { ...vp, proof: { ...vp.proof!, challenge: "replayed-challenge" } };
  const ok = verifyProofBoundPayload(tampered, tampered.proof.proofValue, holder.publicKey);
  assert.equal(ok, false, "overwriting proof.challenge after signing must invalidate the signature");
  console.log("OK: T02 tampering with proof.challenge after signing breaks verification");
}

// T03 — verification fails against the wrong public key
{
  const holder = ed25519.keygen();
  const impostor = ed25519.keygen();
  const vp = buildVerifiablePresentation({
    holderDid: "did:web:sofia.sparkle.example.com:agents:087",
    holderSigningKey: holder.secretKey,
    holderVerificationMethodId: "did:web:sofia.sparkle.example.com:agents:087#key-1",
    credentials: [makeFixtureCredential()],
    challenge: "challenge-0002",
  });

  const ok = verifyProofBoundPayload(vp, vp.proof!.proofValue, impostor.publicKey);
  assert.equal(ok, false);
  console.log("OK: T03 verification fails against the wrong public key");
}

// T04 — attachIdentityPresentationHeader round-trips base64(JSON(vp))
{
  const holder = ed25519.keygen();
  const vp = buildVerifiablePresentation({
    holderDid: "did:web:sofia.sparkle.example.com:agents:087",
    holderSigningKey: holder.secretKey,
    holderVerificationMethodId: "did:web:sofia.sparkle.example.com:agents:087#key-1",
    credentials: [makeFixtureCredential()],
    challenge: "challenge-0003",
  });

  const headers = attachIdentityPresentationHeader(vp);
  assert.ok(PRESENTATION_HEADER in headers);
  const decoded = JSON.parse(Buffer.from(headers[PRESENTATION_HEADER], "base64").toString("utf-8"));
  assert.equal(decoded.holder, vp.holder);
  assert.equal(decoded.proof.proofValue, vp.proof!.proofValue);
  console.log("OK: T04 attachIdentityPresentationHeader base64(JSON) round-trip");
}

// T05 — loadIdentityPresenter(): null when no signing key / no cached credential; builds otherwise
//
// IdentityKeystoreFactory.build()/IdentityCredentialCacheFactory.build()
// construct a FRESH in-memory instance on every call (same as the
// Python port — its own test for this exact scenario patches the
// factory's build() the same way, for the same reason), so this test
// patches the static build() methods to return one shared instance
// for the duration of the test, same approach.
{
  const sharedKeystore = IdentityKeystoreFactory.build();
  const sharedCache = IdentityCredentialCacheFactory.build();
  const originalKeystoreBuild = IdentityKeystoreFactory.build;
  const originalCacheBuild = IdentityCredentialCacheFactory.build;
  IdentityKeystoreFactory.build = () => sharedKeystore;
  IdentityCredentialCacheFactory.build = () => sharedCache;

  try {
    const missing = await loadIdentityPresenter("agent-087", {
      governingAuthorityHost: "sofia.sparkle.example.com",
    });
    assert.equal(missing, null);

    await sharedKeystore.getOrCreate("agent-087");
    const stillMissingCredential = await loadIdentityPresenter("agent-087", {
      governingAuthorityHost: "sofia.sparkle.example.com",
    });
    assert.equal(stillMissingCredential, null);

    const credential = makeFixtureCredential();
    await sharedCache.set("agent-087", {
      ...credential,
      validUntil: new Date(Date.now() + 60_000).toISOString(),
    });

    const presenter = await loadIdentityPresenter("agent-087", {
      governingAuthorityHost: "sofia.sparkle.example.com",
    });
    assert.ok(presenter);
    const headers = presenter!.onChallenge("challenge-loader-0001");
    assert.ok(PRESENTATION_HEADER in headers);
  } finally {
    IdentityKeystoreFactory.build = originalKeystoreBuild;
    IdentityCredentialCacheFactory.build = originalCacheBuild;
  }
  console.log("OK: T05 loadIdentityPresenter() null-until-fully-bootstrapped, then builds");
}

// T06 — loadDelegationPresenter(): null when no cached operational credential; builds otherwise
{
  const sharedKeystore = IdentityKeystoreFactory.build();
  const sharedOpCache = OperationalCredentialCacheFactory.build();
  const originalKeystoreBuild = IdentityKeystoreFactory.build;
  const originalOpCacheBuild = OperationalCredentialCacheFactory.build;
  IdentityKeystoreFactory.build = () => sharedKeystore;
  OperationalCredentialCacheFactory.build = () => sharedOpCache;

  await sharedKeystore.getOrCreate("agent-requestor");

  const grantorIdentity = makeFixtureCredential();

  const missing = await loadDelegationPresenter(
    "agent-requestor",
    "urn:uuid:op-1",
    grantorIdentity,
    { governingAuthorityHost: "sofia.sparkle.example.com" },
  );
  assert.equal(missing, null);

  await sharedOpCache.set("agent-requestor", {
    "@context": ["https://www.w3.org/ns/credentials/v2"],
    id: "urn:uuid:op-1",
    type: ["VerifiableCredential", "OperationalCredential"],
    schemaVersion: "1.0",
    issuer: "did:web:sofia.sparkle.example.com:agents:grantor",
    validFrom: new Date().toISOString(),
    validUntil: new Date(Date.now() + 60_000).toISOString(),
    credentialSubject: { id: "did:web:sofia.sparkle.example.com:agents:requestor", clientId: "agent-requestor", scope: {} },
    credentialStatus: {
      id: "https://sofia.sparkle.example.com/identity/status-list#1",
      type: "BitstringStatusListEntry",
      statusPurpose: "revocation",
      statusListIndex: 1,
      statusListCredential: "https://sofia.sparkle.example.com/identity/status-list",
    },
    delegationDepth: 1,
  });

  const presenter = await loadDelegationPresenter(
    "agent-requestor",
    "urn:uuid:op-1",
    grantorIdentity,
    { governingAuthorityHost: "sofia.sparkle.example.com" },
  );
  assert.ok(presenter);
  assert.ok(PRESENTATION_HEADER in presenter!.onChallenge("challenge-loader-0002"));

  IdentityKeystoreFactory.build = originalKeystoreBuild;
  OperationalCredentialCacheFactory.build = originalOpCacheBuild;
  console.log("OK: T06 loadDelegationPresenter() null-until-cached, then builds");
}

// T07 — identityChallengeHandlerFromEnv(): undefined when unset; throws on half-configuration
{
  delete process.env.IDENTITY_AGENT_ID;
  delete process.env.GOVERNING_AUTHORITY_HOST;
  assert.equal(await identityChallengeHandlerFromEnv(), undefined);

  process.env.IDENTITY_AGENT_ID = "agent-087";
  await assert.rejects(() => identityChallengeHandlerFromEnv());
  delete process.env.IDENTITY_AGENT_ID;

  console.log("OK: T07 identityChallengeHandlerFromEnv() off-by-default, fails loudly if half-configured");
}
