import { RedisSessionCache } from "../../src/session-redis.js";

const cache = await RedisSessionCache.connect({
  url: process.env.TEST_REDIS_URL ?? "redis://localhost:6399",
  prefix: "stls:sc",
  ttlSeconds: 60,
});

const sessionKey = Buffer.from(Array.from({ length: 32 }, (_, i) => i));
await cache.put("node-to-python-session", "kid-from-node", sessionKey);
console.log("Node: session written to Redis (prefix=stls:sc)");

await cache.close();
