/**
 * credential-cache.smoke.ts
 * ---------------------------
 * Node mirror of python/stls-identity/tests/test_identity_credential_cache_and_loader.py's
 * in-memory coverage, plus a real-Redis round-trip when TEST_REDIS_URL
 * is set.
 */

import assert from "node:assert/strict";
import {
  IdentityCredentialCacheFactory,
  InMemoryIdentityCredentialCache,
  InMemoryOperationalCredentialCache,
  OperationalCredentialCacheFactory,
  RedisIdentityCredentialCache,
  RedisOperationalCredentialCache,
} from "../src/credential-cache.js";
import { KeyStorageTier, type NetworkAuthorityCredential, type OperationalCredential } from "../src/models.js";

function makeIdentityCredential(validUntil: string | null): NetworkAuthorityCredential {
  return {
    "@context": ["https://www.w3.org/ns/credentials/v2"],
    id: "urn:uuid:11111111-0000-4000-8000-000000000001",
    type: ["VerifiableCredential", "NetworkAuthorityCredential"],
    schemaVersion: "1.0",
    issuer: "did:web:sofia.sparkle.example.com",
    validFrom: "2026-01-01T00:00:00.000Z",
    validUntil: validUntil ?? undefined,
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
  };
}

function makeOperationalCredential(id: string, validUntil: string | null): OperationalCredential {
  return {
    "@context": ["https://www.w3.org/ns/credentials/v2"],
    id,
    type: ["VerifiableCredential", "OperationalCredential"],
    schemaVersion: "1.0",
    issuer: "did:web:sofia.sparkle.example.com:agents:grantor",
    validFrom: "2026-01-01T00:00:00.000Z",
    validUntil: validUntil ?? undefined,
    credentialSubject: {
      id: "did:web:sofia.sparkle.example.com:agents:requestor",
      clientId: "agent-requestor",
      scope: {},
    },
    credentialStatus: {
      id: "https://sofia.sparkle.example.com/identity/status-list#1",
      type: "BitstringStatusListEntry",
      statusPurpose: "revocation",
      statusListIndex: 1,
      statusListCredential: "https://sofia.sparkle.example.com/identity/status-list",
    },
    delegationDepth: 1,
  };
}

// T01 — IdentityCredentialCache: not-yet-cached agent returns null
{
  const cache = new InMemoryIdentityCredentialCache();
  assert.equal(await cache.get("agent-087"), null);
  console.log("OK: T01 IdentityCredentialCache miss returns null");
}

// T02 — set()/get() round-trip, then delete()
{
  const cache = new InMemoryIdentityCredentialCache();
  const future = new Date(Date.now() + 60_000).toISOString();
  const credential = makeIdentityCredential(future);
  await cache.set("agent-087", credential);
  assert.deepEqual(await cache.get("agent-087"), credential);
  await cache.delete("agent-087");
  assert.equal(await cache.get("agent-087"), null);
  console.log("OK: T02 IdentityCredentialCache set/get/delete round-trip");
}

// T03 — set() is a no-op for an already-expired credential
{
  const cache = new InMemoryIdentityCredentialCache();
  const past = new Date(Date.now() - 60_000).toISOString();
  await cache.set("agent-087", makeIdentityCredential(past));
  assert.equal(await cache.get("agent-087"), null);
  console.log("OK: T03 IdentityCredentialCache rejects already-expired credential");
}

// T04 — a credential with no validUntil is cached with no expiry (this
// matches python/stls-identity's ACTUAL InMemoryIdentityCredentialCache.set()
// behavior, which only skips caching when validUntil IS set and already
// in the past — not when it's absent; that skip-when-absent behavior is
// only in RedisIdentityCredentialCache, which needs a concrete TTL. The
// abstract base class's docstring says "no validUntil" should also be a
// no-op, but the in-memory implementation doesn't actually do that — a
// pre-existing inconsistency in the Python source, faithfully ported
// as-is rather than silently "fixed" during the port.)
{
  const cache = new InMemoryIdentityCredentialCache();
  const credential = makeIdentityCredential(null);
  await cache.set("agent-087", credential);
  assert.deepEqual(await cache.get("agent-087"), credential);
  console.log("OK: T04 IdentityCredentialCache caches a credential with no validUntil (no expiry)");
}

// T05 — IdentityCredentialCacheFactory backend selection
{
  delete process.env.IDENTITY_CREDENTIAL_CACHE_BACKEND;
  assert.ok(IdentityCredentialCacheFactory.build() instanceof InMemoryIdentityCredentialCache);
  process.env.IDENTITY_CREDENTIAL_CACHE_BACKEND = "redis";
  assert.ok(IdentityCredentialCacheFactory.build() instanceof RedisIdentityCredentialCache);
  delete process.env.IDENTITY_CREDENTIAL_CACHE_BACKEND;
  console.log("OK: T05 IdentityCredentialCacheFactory backend selection");
}

// T06 — OperationalCredentialCache: a collection, listIds() self-cleans expired entries
{
  const cache = new InMemoryOperationalCredentialCache();
  const future = new Date(Date.now() + 60_000).toISOString();
  const past = new Date(Date.now() - 60_000).toISOString();

  await cache.set("agent-requestor", makeOperationalCredential("urn:uuid:op-1", future));
  await cache.set("agent-requestor", makeOperationalCredential("urn:uuid:op-2", past)); // no-op, already expired

  const ids = await cache.listIds("agent-requestor");
  assert.deepEqual(ids, ["urn:uuid:op-1"]);

  const got = await cache.get("agent-requestor", "urn:uuid:op-1");
  assert.ok(got);
  assert.equal(got!.id, "urn:uuid:op-1");

  await cache.delete("agent-requestor", "urn:uuid:op-1");
  assert.equal(await cache.get("agent-requestor", "urn:uuid:op-1"), null);
  console.log("OK: T06 OperationalCredentialCache collection semantics");
}

// T07 — OperationalCredentialCacheFactory backend selection
{
  delete process.env.OPERATIONAL_CREDENTIAL_CACHE_BACKEND;
  assert.ok(OperationalCredentialCacheFactory.build() instanceof InMemoryOperationalCredentialCache);
  process.env.OPERATIONAL_CREDENTIAL_CACHE_BACKEND = "redis";
  assert.ok(OperationalCredentialCacheFactory.build() instanceof RedisOperationalCredentialCache);
  delete process.env.OPERATIONAL_CREDENTIAL_CACHE_BACKEND;
  console.log("OK: T07 OperationalCredentialCacheFactory backend selection");
}

// T08 — real Redis round-trip for both caches (skipped if no TEST_REDIS_URL)
{
  const url = process.env.TEST_REDIS_URL;
  if (!url) {
    console.log("SKIP: T08 real Redis round-trip (TEST_REDIS_URL not set)");
  } else {
    const future = new Date(Date.now() + 60_000).toISOString();

    const idCache = new RedisIdentityCredentialCache({ url, namespace: "test:stls:identity:credential" });
    const credential = makeIdentityCredential(future);
    await idCache.set("agent-087", credential);
    assert.deepEqual(await idCache.get("agent-087"), credential);
    await idCache.delete("agent-087");
    await idCache.close();

    const opCache = new RedisOperationalCredentialCache({ url, namespace: "test:stls:identity:opcred" });
    const opCredential = makeOperationalCredential("urn:uuid:op-redis-1", future);
    await opCache.set("agent-requestor", opCredential);
    assert.deepEqual(await opCache.listIds("agent-requestor"), ["urn:uuid:op-redis-1"]);
    await opCache.delete("agent-requestor", "urn:uuid:op-redis-1");
    await opCache.close();

    console.log("OK: T08 real Redis round-trip for both caches");
  }
}
