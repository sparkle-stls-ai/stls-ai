/**
 * trust-registry.smoke.ts
 * --------------------------
 * Node mirror of python/stls-identity/tests/test_trust_registry.py.
 * PostgresTrustAnchorRegistryStore's error paths only (no real
 * Postgres available — same caveat as postgres-store.smoke.ts).
 * TrustAnchorRegistryClient's cache/freshness logic is pure and fully
 * exercised here against a trivial in-memory store.
 */

import assert from "node:assert/strict";
import { ed25519 } from "@noble/curves/ed25519.js";
import { multibaseEncodeEd25519Pub } from "../src/did.js";
import type { TrustAnchorEntry } from "../src/models.js";
import { toPydanticIsoString } from "../src/models.js";
import { buildTrustAnchorEntry, PostgresTrustAnchorRegistryStore } from "../src/trust-registry-store.js";
import {
  TrustAnchorRegistryClient,
  TrustAnchorRegistryStaleError,
} from "../src/trust-registry-client.js";
import type { TrustAnchorRegistryStoreBase } from "../src/trust-registry-store.js";

class InMemoryTrustAnchorRegistryStore implements TrustAnchorRegistryStoreBase {
  entries: TrustAnchorEntry[] = [];

  async get(authorityId: string): Promise<TrustAnchorEntry | null> {
    return this.entries.find((e) => e.authorityId === authorityId && !e.validUntil) ?? null;
  }

  async getKeyValidAt(authorityId: string, verificationMethodId: string, at: Date): Promise<TrustAnchorEntry | null> {
    return (
      this.entries.find(
        (e) =>
          e.authorityId === authorityId &&
          e.verificationMethodId === verificationMethodId &&
          new Date(e.onboardedAt) <= at &&
          (!e.validUntil || new Date(e.validUntil) > at) &&
          (!e.revokedAt || new Date(e.revokedAt) > at),
      ) ?? null
    );
  }

  async getKeyValidAtByVerificationMethod(verificationMethodId: string, at: Date): Promise<TrustAnchorEntry | null> {
    return (
      this.entries.find(
        (e) =>
          e.verificationMethodId === verificationMethodId &&
          new Date(e.onboardedAt) <= at &&
          (!e.validUntil || new Date(e.validUntil) > at) &&
          (!e.revokedAt || new Date(e.revokedAt) > at),
      ) ?? null
    );
  }

  async put(entry: TrustAnchorEntry): Promise<void> {
    const previous = this.entries.find(
      (e) => e.authorityId === entry.authorityId && !e.validUntil && e.verificationMethodId !== entry.verificationMethodId,
    );
    if (previous) {
      previous.validUntil = entry.onboardedAt;
    }
    const existingIdx = this.entries.findIndex(
      (e) => e.authorityId === entry.authorityId && e.verificationMethodId === entry.verificationMethodId,
    );
    if (existingIdx >= 0) {
      this.entries[existingIdx] = entry;
    } else {
      this.entries.push(entry);
    }
  }

  async listActive(): Promise<TrustAnchorEntry[]> {
    return this.entries.filter((e) => !e.validUntil && !e.revokedAt);
  }

  async getHistory(authorityId: string): Promise<TrustAnchorEntry[]> {
    return this.entries
      .filter((e) => e.authorityId === authorityId)
      .sort((a, b) => a.onboardedAt.localeCompare(b.onboardedAt));
  }

  async revokeKey(authorityId: string, verificationMethodId: string, revokedAt: Date): Promise<void> {
    const entry = this.entries.find(
      (e) => e.authorityId === authorityId && e.verificationMethodId === verificationMethodId,
    );
    if (entry) {
      entry.revokedAt = revokedAt.toISOString();
    }
  }

  async revokeAuthority(authorityId: string, revokedAt: Date): Promise<void> {
    for (const entry of this.entries) {
      if (entry.authorityId === authorityId && !entry.revokedAt) {
        entry.revokedAt = revokedAt.toISOString();
      }
    }
  }

  async close(): Promise<void> {}
}

// T01 — buildTrustAnchorEntry() shape
{
  const kp = ed25519.keygen();
  const entry = buildTrustAnchorEntry({
    authorityId: "sparkle",
    did: "did:web:sofia.sparkle.example.com",
    verificationMethodId: "did:web:sofia.sparkle.example.com#key-1",
    publicKey: kp.publicKey,
  });
  assert.equal(entry.publicKeyMultibase, multibaseEncodeEd25519Pub(kp.publicKey));
  assert.equal(entry.validUntil, undefined);
  console.log("OK: T01 buildTrustAnchorEntry() shape");
}

// T02 — rotation: put() a second key closes the first (validUntil set), new one is current
{
  const store = new InMemoryTrustAnchorRegistryStore();
  const kp1 = ed25519.keygen();
  const kp2 = ed25519.keygen();
  const onboardedAt1 = new Date("2026-01-01T00:00:00.000Z");
  const rotatedAt = new Date("2026-02-01T00:00:00.000Z");

  await store.put(
    buildTrustAnchorEntry({
      authorityId: "sparkle",
      did: "did:web:sofia.sparkle.example.com",
      verificationMethodId: "did:web:sofia.sparkle.example.com#key-1",
      publicKey: kp1.publicKey,
      onboardedAt: onboardedAt1,
    }),
  );
  await store.put(
    buildTrustAnchorEntry({
      authorityId: "sparkle",
      did: "did:web:sofia.sparkle.example.com",
      verificationMethodId: "did:web:sofia.sparkle.example.com#key-2",
      publicKey: kp2.publicKey,
      onboardedAt: rotatedAt,
    }),
  );

  const current = await store.get("sparkle");
  assert.equal(current?.verificationMethodId, "did:web:sofia.sparkle.example.com#key-2");

  const history = await store.getHistory("sparkle");
  assert.equal(history.length, 2);
  assert.equal(history[0].validUntil, toPydanticIsoString(rotatedAt));

  // The OLD key still resolves for a timestamp before the rotation.
  const resolvedOld = await store.getKeyValidAt(
    "sparkle",
    "did:web:sofia.sparkle.example.com#key-1",
    new Date("2026-01-15T00:00:00.000Z"),
  );
  assert.ok(resolvedOld);
  // ...but not for a timestamp after.
  const resolvedOldAfter = await store.getKeyValidAt(
    "sparkle",
    "did:web:sofia.sparkle.example.com#key-1",
    new Date("2026-03-01T00:00:00.000Z"),
  );
  assert.equal(resolvedOldAfter, null);
  console.log("OK: T02 rotation closes the previous key, old key still resolves historically");
}

// T03 — TrustAnchorRegistryClient: asTrustedKeys()/resolve() throw before the first refresh()
{
  const store = new InMemoryTrustAnchorRegistryStore();
  const client = new TrustAnchorRegistryClient(store);
  assert.throws(() => client.asTrustedKeys(), TrustAnchorRegistryStaleError);
  assert.throws(() => client.resolve("whatever"), TrustAnchorRegistryStaleError);
  console.log("OK: T03 client fails closed before the first refresh()");
}

// T04 — refresh() populates the cache; a revoked authority disappears on the next refresh
{
  const store = new InMemoryTrustAnchorRegistryStore();
  const kp = ed25519.keygen();
  const vmId = "did:web:sofia.sparkle.example.com#key-1";
  await store.put(
    buildTrustAnchorEntry({
      authorityId: "sparkle",
      did: "did:web:sofia.sparkle.example.com",
      verificationMethodId: vmId,
      publicKey: kp.publicKey,
    }),
  );

  const client = new TrustAnchorRegistryClient(store);
  assert.equal(await client.refresh(), 1);
  assert.deepEqual(Buffer.from(client.resolve(vmId)!), Buffer.from(kp.publicKey));
  assert.ok(client.asTrustedKeys().has(vmId));

  await store.revokeAuthority("sparkle", new Date());
  assert.equal(await client.refresh(), 0);
  assert.equal(client.resolve(vmId), null);
  console.log("OK: T04 refresh() populates/evicts the cache on revocation");
}

// T05 — resolveHistorical() bypasses the cache/staleness check entirely
{
  const store = new InMemoryTrustAnchorRegistryStore();
  const kp = ed25519.keygen();
  const vmId = "did:web:sofia.sparkle.example.com#key-1";
  await store.put(
    buildTrustAnchorEntry({
      authorityId: "sparkle",
      did: "did:web:sofia.sparkle.example.com",
      verificationMethodId: vmId,
      publicKey: kp.publicKey,
      onboardedAt: new Date("2026-01-01T00:00:00.000Z"),
    }),
  );

  const client = new TrustAnchorRegistryClient(store);
  // Never called refresh() — asTrustedKeys()/resolve() would throw, but
  // resolveHistorical() must still work (goes straight to the store).
  const resolved = await client.resolveHistorical(vmId, new Date("2026-01-15T00:00:00.000Z"));
  assert.deepEqual(Buffer.from(resolved!), Buffer.from(kp.publicKey));
  console.log("OK: T05 resolveHistorical() works without a prior refresh()");
}

// T06 — PostgresTrustAnchorRegistryStore error paths (no real Postgres)
{
  const store = new PostgresTrustAnchorRegistryStore({ dsn: "" });
  assert.equal(await store.get("sparkle"), null);
  assert.equal(await store.getKeyValidAt("sparkle", "vm-1", new Date()), null);
  await assert.rejects(() =>
    store.put(
      buildTrustAnchorEntry({
        authorityId: "sparkle",
        did: "did:web:x",
        verificationMethodId: "did:web:x#key-1",
        publicKey: ed25519.keygen().publicKey,
      }),
    ),
  );
  await store.close();
  console.log("OK: T06 PostgresTrustAnchorRegistryStore soft-fail on get(), throws on put()");
}

console.log("ALL TRUST REGISTRY SMOKE CHECKS PASSED (Postgres error paths only — no real DB tested)");
