import assert from "node:assert/strict";
import {
  ApiKeyLoader,
  BackgroundRefresher,
  KeyRecord,
  RedisKeyCache,
} from "../src/auth/index.js";

// Exercises the distributed-lock path of BackgroundRefresher against a
// real Redis — previously only start()/stop() had been exercised (in
// auth.smoke.ts and the live Postgres tests), but the refresh interval
// never actually elapsed during those short-lived tests, so
// tryAcquireLock()/releaseLock() (and the scheduled doRefresh() that
// calls them) had never really run.

const REDIS_URL = process.env.TEST_REDIS_URL ?? "redis://localhost:6399";
const NAMESPACE = "stls:bgrefresher-live-test";
const LOCK_KEY = `stls:refresh:lock:${NAMESPACE}`;

// 1) RedisKeyCache.tryAcquireLock()/releaseLock() — the atomic primitives
//    BackgroundRefresher relies on, tested directly with two distinct
//    simulated node ids (BackgroundRefresher itself can't be made to use
//    two different node ids from a single process, since nodeId is
//    derived from hostname+pid internally).
{
  const cache = new RedisKeyCache({ url: REDIS_URL, namespace: NAMESPACE });
  await cache.initialize();

  const a1 = await cache.tryAcquireLock(LOCK_KEY, "node-A", 30);
  assert.equal(a1, true, "first SET NX must succeed on an unheld lock");

  const a2 = await cache.tryAcquireLock(LOCK_KEY, "node-B", 30);
  assert.equal(a2, false, "second SET NX must fail while node-A still holds the lock");
  console.log("OK: tryAcquireLock() is mutually exclusive — only the first caller acquires the lock");

  // A non-holder must not be able to release someone else's lock.
  await cache.releaseLock(LOCK_KEY, "node-B");
  const stillHeld = await cache.tryAcquireLock(LOCK_KEY, "node-B", 30);
  assert.equal(stillHeld, false, "releaseLock() from a non-holder must be a no-op (Lua check-and-delete)");
  console.log("OK: releaseLock() from a non-holder does not release node-A's lock");

  // The actual holder releases it, and it becomes acquirable again.
  await cache.releaseLock(LOCK_KEY, "node-A");
  const releasedNow = await cache.tryAcquireLock(LOCK_KEY, "node-B", 30);
  assert.equal(releasedNow, true, "lock must be acquirable again after the real holder releases it");
  console.log("OK: releaseLock() from the actual holder frees the lock atomically");

  await cache.releaseLock(LOCK_KEY, "node-B"); // cleanup
  await cache.close();
}

// 2) BackgroundRefresher — a real scheduled refresh, interval short enough
//    to actually elapse during the test, against a fake loader with a
//    call counter so we can observe doRefresh() really running.
{
  let loadAllCalls = 0;
  const fakeRecord = new KeyRecord({ keyId: "bgrefresher-test-key", clientId: "bgrefresher-test" });
  const fakeLoader: ApiKeyLoader = {
    async loadAll() {
      loadAllCalls += 1;
      return [fakeRecord];
    },
    async loadOne(keyId: string) {
      return keyId === fakeRecord.keyId ? fakeRecord : null;
    },
    async close() {},
  };

  const cache = new RedisKeyCache({ url: REDIS_URL, namespace: NAMESPACE });
  await cache.initialize();

  const refresher = new BackgroundRefresher(fakeLoader, cache, {
    interval: 1, // seconds — short enough for the scheduled refresh to actually fire
    distributed: true,
    namespace: NAMESPACE,
  });

  await refresher.start();
  assert.equal(refresher.running, true);

  await new Promise((resolve) => setTimeout(resolve, 1400)); // let the 1s interval elapse

  assert.ok(loadAllCalls >= 1, "the scheduled doRefresh() never called loader.loadAll() — the lock path did not run");
  console.log(`OK: scheduled refresh ran for real (loadAll called ${loadAllCalls}x) via the Redis-backed lock path`);

  const record = await cache.get(fakeRecord.keyId);
  assert.ok(record !== null, "bulkReload() did not actually write the refreshed record to Redis");
  console.log("OK: the refreshed record landed in Redis via bulkReload()");

  // The lock must have been released after the refresh completed.
  const lockFree = await cache.tryAcquireLock(LOCK_KEY, "probe", 5);
  assert.equal(lockFree, true, "lock was not released after doRefresh() completed");
  await cache.releaseLock(LOCK_KEY, "probe");
  console.log("OK: the distributed lock is released after the scheduled refresh completes");

  await refresher.stop();
  assert.equal(refresher.running, false);

  await cache.flushAllKeys();
  await cache.close();
}

console.log("ALL BACKGROUND REFRESHER LIVE CHECKS PASSED (distributed lock exercised for real against Redis)");
