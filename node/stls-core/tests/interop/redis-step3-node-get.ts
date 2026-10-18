import assert from "node:assert/strict";
import { RedisSessionCache } from "../../src/session-redis.js";

const cache = await RedisSessionCache.connect({
  url: process.env.TEST_REDIS_URL ?? "redis://localhost:6399",
  prefix: "stls:sc",
  ttlSeconds: 60,
});

const transport = await cache.get("python-to-node-session");
assert.ok(transport !== undefined, "session written by Python not found in Redis");

const envelope = transport!.encrypt(Buffer.from("roundtrip-check"));
const recovered = transport!.decrypt(envelope);
assert.equal(recovered.toString("utf-8"), "roundtrip-check");
console.log("OK: Node reads the session written by Python via Redis, session_id =", transport!.sessionId);

await cache.remove("node-to-python-session");
await cache.remove("python-to-node-session");
await cache.close();
console.log("OK: test sessions removed from Redis");
