import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { Client } from "pg";
import { ApiKeyManager, AuthResult, PostgresLoader, RedisKeyCache } from "../src/auth/index.js";

// Full-stack live test: PostgresLoader -> RedisKeyCache -> ApiKeyManager,
// against a real Postgres (AUTH_PG_DSN) and a reachable Redis
// (TEST_REDIS_URL). Validates the whole chain, not just the loader in
// isolation (see postgres-loader.live.ts). Self-contained: manages its
// own fixture row.

const dsn = process.env.AUTH_PG_DSN;
if (!dsn) {
  throw new Error("AUTH_PG_DSN is not set — this test requires a real Postgres DSN.");
}
const RAW_KEY = "node-postgres-test-key"; // hashes to the key_id inserted below
const TEST_KEY_ID = "0b43540e87025f4a97f0df7fd9a186d6d061949397dfbe23b2c79254f277da4d";
const REDIS_URL = process.env.TEST_REDIS_URL ?? "redis://localhost:6399";

const admin = new Client({ connectionString: dsn });
await admin.connect();
await admin.query(readFileSync(new URL("./postgres-schema.sql", import.meta.url), "utf-8"));
await admin.query(
  `INSERT INTO sf_api_keys (key_id, client_id, scopes, endpoints, expires_at, description, metadata)
   VALUES ($1, $2, $3, $4, now() + interval '1 day', $5, $6)
   ON CONFLICT (key_id) DO NOTHING`,
  [
    TEST_KEY_ID,
    "stls-core-node-interop-test",
    ["secure-a2a"],
    ["/secure-a2a"],
    "Temporary row for stls-core (Node) ApiKeyManager+Postgres interop test",
    JSON.stringify({}),
  ],
);

try {
  const loader = new PostgresLoader({ dsn });
  const manager = ApiKeyManager.fromLoader(loader, { redisUrl: REDIS_URL, namespace: "stls:pg-live-test" });
  await manager.start(); // bulk-loads all non-revoked rows from Postgres into Redis

  const ok = await manager.validate(RAW_KEY, "/secure-a2a");
  assert.equal(ok.result, AuthResult.OK);
  assert.equal(ok.record?.clientId, "stls-core-node-interop-test");
  console.log("OK: ApiKeyManager.validate() -> OK, key loaded from real Postgres via the BackgroundRefresher's initial bulk load");

  const scopeDenied = await manager.validate(RAW_KEY, "/admin");
  assert.equal(scopeDenied.result, AuthResult.SCOPE_DENIED);
  console.log("OK: ApiKeyManager.validate() -> SCOPE_DENIED for an endpoint not in the Postgres row's endpoints[]");

  const health = await manager.health();
  assert.equal(health.redis, "ok");
  assert.equal(health.loader, "PostgresLoader");
  console.log("OK: health() reports loader=PostgresLoader, redis=ok");

  await manager.stop();
  console.log("ALL APIKEYMANAGER+POSTGRES LIVE CHECKS PASSED");
} finally {
  await admin.query("DELETE FROM sf_api_keys WHERE key_id = $1", [TEST_KEY_ID]);
  await admin.end();

  const cleanupCache = new RedisKeyCache({ url: REDIS_URL, namespace: "stls:pg-live-test" });
  await cleanupCache.flushAllKeys();
  await cleanupCache.close();
}
