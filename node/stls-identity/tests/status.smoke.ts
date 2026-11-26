/**
 * status.smoke.ts
 * ----------------
 * Node mirror of python/stls-identity/tests/test_status_list_redis.py +
 * test_status_fetch.py. Needs a real Redis (same reasoning as the
 * Python suite: the Lua atomic-bit-set script and SETBIT/GETBIT
 * semantics can't be faithfully exercised by an in-memory stub) —
 * skipped with a clear message if TEST_REDIS_URL isn't set.
 */

import assert from "node:assert/strict";
import { createClient } from "redis";
import { StatusListManager, fetchStatusBit } from "../src/status.js";

const url = process.env.TEST_REDIS_URL;

if (!url) {
  console.log("SKIP: status.smoke.ts (TEST_REDIS_URL not set)");
} else {
  const client = createClient({ url });
  await client.connect();
  const authorityId = `test-authority-${Date.now()}`;
  const manager = new StatusListManager(client, authorityId);

  // T01 — allocateIndex() is monotonically increasing from 0
  {
    const i0 = await manager.allocateIndex();
    const i1 = await manager.allocateIndex();
    const i2 = await manager.allocateIndex();
    assert.deepEqual([i0, i1, i2], [0, 1, 2]);
    console.log("OK: T01 allocateIndex() increments from 0");
  }

  // T02 — isRevoked() is false before revoke(), true after
  {
    assert.equal(await manager.isRevoked(5), false);
    await manager.revoke(5);
    assert.equal(await manager.isRevoked(5), true);
    console.log("OK: T02 revoke()/isRevoked() round-trip");
  }

  // T03 — revoke() grows the bitstring correctly for a high index
  // (exercises the Lua script's math.floor(index/8) SETRANGE growth path)
  {
    await manager.revoke(100);
    assert.equal(await manager.isRevoked(100), true);
    // bits other than the ones explicitly set must remain 0
    assert.equal(await manager.isRevoked(99), false);
    assert.equal(await manager.isRevoked(101), false);
    console.log("OK: T03 revoke() grows the bitstring for a high index");
  }

  // T04 — publish() produces a gzip+base64url bitstring that
  // fetchStatusBit() (via a throwaway local HTTP server) decodes
  // correctly for both a set and an unset bit.
  {
    const { encodedList } = await manager.publish();

    const http = await import("node:http");
    const server = http.createServer((req, res) => {
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ encodedList }));
    });
    await new Promise<void>((resolve) => server.listen(0, resolve));
    const address = server.address();
    const port = typeof address === "object" && address ? address.port : 0;
    const base = `http://127.0.0.1:${port}/status-list`;

    assert.equal(await fetchStatusBit(base, 5), true); // revoked in T02
    assert.equal(await fetchStatusBit(base, 100), true); // revoked in T03
    assert.equal(await fetchStatusBit(base, 6), false); // never touched
    assert.equal(await fetchStatusBit(base, 9999), false); // beyond the bitstring's length

    await new Promise<void>((resolve) => server.close(() => resolve()));
    console.log("OK: T04 publish() + fetchStatusBit() round-trip over HTTP");
  }

  // Clean up only the two keys this test created — never flush the
  // whole DB, which could be a shared dev Redis with unrelated data.
  await client.del([
    `stls:identity:statuslist:${authorityId}`,
    `stls:identity:statuslist:${authorityId}:next_index`,
  ]);
  await client.quit();
}
