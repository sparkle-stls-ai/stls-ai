/**
 * did.smoke.ts
 * ------------
 * Node mirror of the pure (no-keystore-I/O) parts of
 * python/stls-identity's did.py tests: multibase encode/decode
 * round-trip, domain/DID conversions, buildDidDocument shape.
 */

import assert from "node:assert/strict";
import bs58 from "bs58";
import { ed25519 } from "@noble/curves/ed25519.js";
import {
  buildDidDocument,
  decodeMultibaseEd25519PublicKey,
  didToResolutionUrl,
  domainAndPathToDid,
  multibaseEncodeEd25519Pub,
} from "../src/did.js";

// T01 — domain/path -> DID -> resolution URL round-trip
{
  const did = domainAndPathToDid("sofia.sparkle.example.com", "agents", "087");
  assert.equal(did, "did:web:sofia.sparkle.example.com:agents:087");
  assert.equal(
    didToResolutionUrl(did),
    "https://sofia.sparkle.example.com/agents/087/did.json",
  );
  console.log("OK: T01 domain/path -> did:web -> resolution URL");
}

// T02 — root DID (no path segments) resolves to .well-known
{
  const did = domainAndPathToDid("sofia.sparkle.example.com");
  assert.equal(did, "did:web:sofia.sparkle.example.com");
  assert.equal(
    didToResolutionUrl(did),
    "https://sofia.sparkle.example.com/.well-known/did.json",
  );
  console.log("OK: T02 root DID resolves to .well-known/did.json");
}

// T03 — host with a port gets ':' escaped to '%3A', and back
{
  const did = domainAndPathToDid("localhost:8443", "agents", "x");
  assert.equal(did, "did:web:localhost%3A8443:agents:x");
  assert.equal(didToResolutionUrl(did), "https://localhost:8443/agents/x/did.json");
  console.log("OK: T03 host port colon-escaping round-trips");
}

// T04 — multibase encode/decode round-trip for a real Ed25519 public key
{
  const kp = ed25519.keygen();
  const encoded = multibaseEncodeEd25519Pub(kp.publicKey);
  assert.ok(encoded.startsWith("z"));
  const decoded = decodeMultibaseEd25519PublicKey(encoded);
  assert.deepEqual(Buffer.from(decoded), Buffer.from(kp.publicKey));
  console.log("OK: T04 multibase Ed25519 pubkey round-trips");
}

// T05 — decode rejects a non-'z'-prefixed string
{
  assert.throws(() => decodeMultibaseEd25519PublicKey("not-multibase"));
  console.log("OK: T05 decode rejects missing 'z' prefix");
}

// T06 — decode rejects a key with the wrong multicodec prefix
{
  const kp = ed25519.keygen();
  // Re-encode without the ed25519-pub multicodec prefix (plain base58 of
  // the raw key, as if it were a different key type's multibase string).
  const wrongPrefix = "z" + bs58.encode(kp.publicKey);
  assert.throws(() => decodeMultibaseEd25519PublicKey(wrongPrefix));
  console.log("OK: T06 decode rejects wrong multicodec prefix");
}

// T07 — buildDidDocument shape, including the two distinct `controller` fields
{
  const kp = ed25519.keygen();
  const now = new Date("2026-01-15T12:00:00.000Z");
  const doc = buildDidDocument({
    host: "sofia.sparkle.example.com",
    agentPathSegments: ["agents", "087"],
    governingAuthorityDid: "did:web:sofia.sparkle.example.com",
    signingPublicKey: kp.publicKey,
    now,
  });

  assert.equal(doc.id, "did:web:sofia.sparkle.example.com:agents:087");
  assert.equal(doc.controller, "did:web:sofia.sparkle.example.com");
  assert.equal(doc.verificationMethod.length, 1);
  assert.equal(doc.verificationMethod[0].id, `${doc.id}#key-1`);
  // The agent operates its own key — not the Authority.
  assert.equal(doc.verificationMethod[0].controller, doc.id);
  assert.deepEqual(doc.authentication, [`${doc.id}#key-1`]);
  assert.deepEqual(doc.assertionMethod, [`${doc.id}#key-1`]);
  // toPydanticIsoString() drops an exact-zero millisecond fraction —
  // see models.ts's docstring on why this must match Pydantic's own
  // round-trip format, not plain Date.toISOString().
  assert.equal(doc.created, "2026-01-15T12:00:00Z");
  console.log("OK: T07 buildDidDocument shape and controller split");
}
