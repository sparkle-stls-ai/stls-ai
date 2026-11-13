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
 *   R19 — the raw session key is never stored, only the derived keys (issue #20 (a))
 *   R20 — with STLS_CORE_ATREST_KEY set, the stored payload is sealed and
 *         still round-trips through get() (issue #20 (b))
 *   R21 — get() with the wrong KEK fails closed (returns undefined), doesn't throw
 */

import assert from "node:assert/strict";
import { createClient } from "redis";
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

  // R19 — the raw session key is never stored, only the derived keys
  {
    const raw = createClient({ url: redisUrl });
    await raw.connect();
    const cache = await RedisSessionCache.connect({ url: redisUrl, prefix, ttlSeconds: 300 });
    const sessionKey = freshKey();
    await cache.put(SID1, KID1, sessionKey);

    const stored = await raw.get(`${prefix}:${SID1}`);
    assert.ok(stored !== null);
    assert.ok(!stored!.includes(sessionKey.toString("base64")));
    const parsed = JSON.parse(stored!);
    assert.equal("session_key" in parsed, false);
    assert.ok("enc_aes_key" in parsed);

    await cache.remove(SID1);
    await cache.close();
    await raw.quit();
    console.log("OK: R19 raw session key never stored, only derived keys");
  }

  // R20 — STLS_CORE_ATREST_KEY set: stored payload is sealed, still round-trips
  {
    const kek = randomBytes(32).toString("base64");
    process.env.STLS_CORE_ATREST_KEY = kek;
    try {
      const raw = createClient({ url: redisUrl });
      await raw.connect();
      const cache = await RedisSessionCache.connect({ url: redisUrl, prefix, ttlSeconds: 300 });
      await cache.put(SID1, KID1, freshKey());

      const stored = await raw.get(`${prefix}:${SID1}`);
      assert.ok(stored !== null);
      assert.throws(() => JSON.parse(stored!));

      const transport = await cache.get(SID1);
      assert.notEqual(transport, undefined);
      const envelope = transport!.encrypt(Buffer.from("sealed round-trip"));
      assert.deepEqual(transport!.decrypt(envelope), Buffer.from("sealed round-trip"));

      await cache.remove(SID1);
      await cache.close();
      await raw.quit();
      console.log("OK: R20 STLS_CORE_ATREST_KEY set: payload sealed and round-trips");
    } finally {
      delete process.env.STLS_CORE_ATREST_KEY;
    }
  }

  // R21 — wrong KEK fails closed (returns undefined), doesn't throw
  {
    process.env.STLS_CORE_ATREST_KEY = randomBytes(32).toString("base64");
    const writer = await RedisSessionCache.connect({ url: redisUrl, prefix, ttlSeconds: 300 });
    try {
      process.env.STLS_CORE_ATREST_KEY = randomBytes(32).toString("base64");
      const reader = await RedisSessionCache.connect({ url: redisUrl, prefix, ttlSeconds: 300 });
      try {
        await writer.put(SID1, KID1, freshKey());
        assert.equal(await reader.get(SID1), undefined);
      } finally {
        await reader.remove(SID1);
        await reader.close();
      }
    } finally {
      delete process.env.STLS_CORE_ATREST_KEY;
      await writer.close();
    }
    console.log("OK: R21 wrong KEK fails closed, doesn't throw");
  }

  console.log("\nAll session.smoke.ts checks passed.");
}

main().catch((err) => {
  console.error("FAILED:", err);
  process.exit(1);
});
