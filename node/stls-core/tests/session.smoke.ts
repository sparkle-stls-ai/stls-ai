/**
 * session.smoke.ts
 * -----------------
 * Covers the SESSION_KEY_MAX_AGE_SECONDS absolute key-rotation check added
 * to InMemorySessionCache/RedisSessionCache for issue #6. Node mirror of
 * the M13-M16/R15-R18 cases in
 * python/stls-core/tests/test_stls_core_session_cache.py.
 *
 * Pre-existing TTL/LRU/factory coverage for session.ts has no Node unit
 * test today (unlike the Python side) — out of scope here, this file only
 * adds coverage for the new max-age behavior.
 *
 * RedisSessionCache cases need a real Redis (no in-memory Redis mock is
 * used in this package, see tests/interop/redis-step1-node-put.ts) —
 * TEST_REDIS_URL, default redis://localhost:6399, same convention as the
 * existing interop smoke tests.
 *
 * Coverage:
 *   M13 — maxAgeSeconds unset/0 (default): no absolute expiry, idle TTL only
 *   M14 — max-age expired: get returns undefined even with a generous idle TTL
 *   M15 — an update put() does NOT reset the max-age clock
 *   M16 — max-age does not renew on access (unlike the idle TTL)
 *   R15 — maxAgeSeconds unset/0 (default): no absolute expiry, idle TTL only
 *   R16 — max-age expired: get returns undefined and removes the key from Redis
 *   R17 — max-age does not renew on access (unlike GETEX)
 *   R18 — no cross-instance coordination needed: two separate
 *         RedisSessionCache instances on the same Redis agree on max-age
 *         expiry by reading the same shared created_at
 */

import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { InMemorySessionCache } from "../src/session.js";
import { RedisSessionCache } from "../src/session-redis.js";

function freshKey(): Buffer {
  return randomBytes(32);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

const SID1 = "session-1111-aaaa";
const KID1 = "kid-aaa-111";
const KID2 = "kid-bbb-222";

async function main(): Promise<void> {
  // M13 — disabled by default
  {
    const cache = new InMemorySessionCache({ maxSize: 10, ttlSeconds: 60 });
    await cache.put(SID1, KID1, freshKey());
    assert.notEqual(await cache.get(SID1), undefined);
    console.log("OK: M13 max-age disabled by default (InMemory)");
  }

  // M14 — max-age expiry
  {
    const cache = new InMemorySessionCache({ maxSize: 10, ttlSeconds: 60, maxAgeSeconds: 1 });
    await cache.put(SID1, KID1, freshKey());
    assert.notEqual(await cache.get(SID1), undefined);
    await sleep(1100);
    assert.equal(await cache.get(SID1), undefined);
    console.log("OK: M14 max-age expiry (InMemory)");
  }

  // M15 — not reset by an update put()
  {
    const cache = new InMemorySessionCache({ maxSize: 10, ttlSeconds: 60, maxAgeSeconds: 1 });
    await cache.put(SID1, KID1, freshKey());
    await sleep(600);
    await cache.put(SID1, KID2, freshKey()); // idempotent "update", same sessionId
    await sleep(600);
    // ~1.2s since the original creation, > maxAgeSeconds=1
    assert.equal(await cache.get(SID1), undefined);
    console.log("OK: M15 max-age not reset by update put() (InMemory)");
  }

  // M16 — not reset by get()
  {
    const cache = new InMemorySessionCache({ maxSize: 10, ttlSeconds: 60, maxAgeSeconds: 1 });
    await cache.put(SID1, KID1, freshKey());
    for (let i = 0; i < 3; i++) {
      await sleep(400);
      await cache.get(SID1); // repeated access, mirroring the idle-TTL sliding test
    }
    assert.equal(await cache.get(SID1), undefined);
    console.log("OK: M16 max-age not reset by access (InMemory)");
  }

  const redisUrl = process.env.TEST_REDIS_URL ?? "redis://localhost:6399";
  const prefix = `stls:sc:smoke-max-age-${Date.now()}`;

  // R15 — disabled by default
  {
    const cache = await RedisSessionCache.connect({ url: redisUrl, prefix, ttlSeconds: 300 });
    await cache.put(SID1, KID1, freshKey());
    assert.notEqual(await cache.get(SID1), undefined);
    await cache.remove(SID1);
    await cache.close();
    console.log("OK: R15 max-age disabled by default (Redis)");
  }

  // R16 — max-age expiry, and the Redis key is actually removed
  {
    const cache = await RedisSessionCache.connect({
      url: redisUrl, prefix, ttlSeconds: 300, maxAgeSeconds: 1,
    });
    await cache.put(SID1, KID1, freshKey());
    assert.notEqual(await cache.get(SID1), undefined);
    await sleep(1100);
    assert.equal(await cache.get(SID1), undefined);
    assert.equal(await cache.has(SID1), false);
    await cache.close();
    console.log("OK: R16 max-age expiry removes the Redis key");
  }

  // R17 — not reset by get()
  {
    const cache = await RedisSessionCache.connect({
      url: redisUrl, prefix, ttlSeconds: 300, maxAgeSeconds: 1,
    });
    await cache.put(SID1, KID1, freshKey());
    for (let i = 0; i < 3; i++) {
      await sleep(400);
      await cache.get(SID1);
    }
    assert.equal(await cache.get(SID1), undefined);
    await cache.close();
    console.log("OK: R17 max-age not reset by access (Redis GETEX)");
  }

  // R18 — no cross-instance coordination needed
  {
    const instanceA = await RedisSessionCache.connect({
      url: redisUrl, prefix, ttlSeconds: 300, maxAgeSeconds: 1,
    });
    const instanceB = await RedisSessionCache.connect({
      url: redisUrl, prefix, ttlSeconds: 300, maxAgeSeconds: 1,
    });

    await instanceA.put(SID1, KID1, freshKey());
    // Both instances see the session as valid before expiry.
    assert.notEqual(await instanceA.get(SID1), undefined);
    assert.notEqual(await instanceB.get(SID1), undefined);

    await sleep(1100);

    // Both instances, independently, see it as expired — no explicit
    // coordination between instanceA and instanceB.
    assert.equal(await instanceB.get(SID1), undefined);
    assert.equal(await instanceA.get(SID1), undefined);

    await instanceA.close();
    await instanceB.close();
    console.log("OK: R18 no cross-instance coordination needed (Redis)");
  }

  console.log("\nAll session.smoke.ts checks passed.");
}

main().catch((err) => {
  console.error("FAILED:", err);
  process.exit(1);
});
