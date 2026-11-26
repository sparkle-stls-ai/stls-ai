/**
 * keystore.smoke.ts
 * ------------------
 * Node mirror of python/stls-identity/tests/test_keystore_redis.py's
 * in-memory coverage, plus a real-Redis round-trip when TEST_REDIS_URL
 * is set (CI sets it; local runs without a Redis fall back to a soft
 * skip, same posture as node/stls-core's own postgres-loader.smoke.ts).
 */

import assert from "node:assert/strict";
import { createClient } from "redis";
import {
  IdentityKeystoreFactory,
  InMemoryIdentityKeystore,
  RedisIdentityKeystore,
} from "../src/keystore.js";

// T01 — get() on an unknown agent returns null
{
  const ks = new InMemoryIdentityKeystore();
  assert.equal(await ks.get("unknown-agent"), null);
  console.log("OK: T01 get() on unknown agent returns null");
}

// T02 — getOrCreate(): created=true on first call, false afterward, same key both times
{
  const ks = new InMemoryIdentityKeystore();
  const first = await ks.getOrCreate("agent-087");
  assert.equal(first.created, true);
  const second = await ks.getOrCreate("agent-087");
  assert.equal(second.created, false);
  assert.deepEqual(Buffer.from(first.privateKey), Buffer.from(second.privateKey));
  console.log("OK: T02 getOrCreate() created flag + key stability");
}

// T03 — put() then get() round-trips the exact bytes
{
  const ks = new InMemoryIdentityKeystore();
  const key = new Uint8Array(32).fill(7);
  await ks.put("agent-x", key);
  const got = await ks.get("agent-x");
  assert.deepEqual(Buffer.from(got!), Buffer.from(key));
  console.log("OK: T03 put()/get() round-trip");
}

// T04 — IdentityKeystoreFactory.build() defaults to memory, honors env override
{
  delete process.env.IDENTITY_KEYSTORE_BACKEND;
  assert.ok(IdentityKeystoreFactory.build() instanceof InMemoryIdentityKeystore);

  process.env.IDENTITY_KEYSTORE_BACKEND = "redis";
  assert.ok(IdentityKeystoreFactory.build() instanceof RedisIdentityKeystore);
  delete process.env.IDENTITY_KEYSTORE_BACKEND;

  process.env.IDENTITY_KEYSTORE_BACKEND = "bogus";
  assert.throws(() => IdentityKeystoreFactory.build());
  delete process.env.IDENTITY_KEYSTORE_BACKEND;
  console.log("OK: T04 IdentityKeystoreFactory backend selection");
}

// T05 — real Redis round-trip (skipped if no TEST_REDIS_URL)
{
  const url = process.env.TEST_REDIS_URL;
  if (!url) {
    console.log("SKIP: T05 real Redis round-trip (TEST_REDIS_URL not set)");
  } else {
    const ks = new RedisIdentityKeystore({ url, namespace: "test:stls:identity:signingkey" });
    const key = new Uint8Array(32).fill(9);
    await ks.put("redis-agent", key);
    const got = await ks.get("redis-agent");
    assert.deepEqual(Buffer.from(got!), Buffer.from(key));
    await ks.close();

    // IdentityKeystoreBase has no delete() (persistent by design, same
    // as the Python port — rotation is the only intended way to
    // overwrite an entry) — clean up this test's own key directly so
    // repeated runs don't accumulate stale entries in a shared Redis.
    const cleanup = createClient({ url });
    await cleanup.connect();
    await cleanup.del("test:stls:identity:signingkey:redis-agent");
    await cleanup.quit();

    console.log("OK: T05 real Redis round-trip");
  }
}
