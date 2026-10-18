import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { Client } from "pg";
import { PostgresLoader } from "../src/auth/loaders-postgres.js";

// Real end-to-end test against a live PostgreSQL instance (DSN from
// AUTH_PG_DSN). Unlike postgres-loader.smoke.ts (error paths only), this
// exercises the actual SQL query and the row -> KeyRecord conversion
// against a real sf_api_keys table. Self-contained: creates the table if
// missing, inserts its own fixture row, and removes it afterwards — safe
// to run against a shared/pre-populated database, and repeatable in CI.

const dsn = process.env.AUTH_PG_DSN;
if (!dsn) {
  throw new Error("AUTH_PG_DSN is not set — this test requires a real Postgres DSN.");
}

const TEST_KEY_ID = "0b43540e87025f4a97f0df7fd9a186d6d061949397dfbe23b2c79254f277da4d"; // sha256("node-postgres-test-key")

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
    ["secure-a2a", "admin"],
    ["/secure-a2a"],
    "Temporary row for stls-core (Node) PostgresLoader interop test",
    JSON.stringify({ tenant: "test", plan: "free" }),
  ],
);
console.log("OK: fixture row inserted (or already present) in sf_api_keys");

try {
  const loader = new PostgresLoader({ dsn });

  // 1) loadOne() — fetch the single test row by key_id
  const record = await loader.loadOne(TEST_KEY_ID);
  assert.ok(record !== null, "test row not found via loadOne()");
  assert.equal(record!.keyId, TEST_KEY_ID);
  assert.equal(record!.clientId, "stls-core-node-interop-test");
  assert.deepEqual(record!.scopes, ["secure-a2a", "admin"]);
  assert.deepEqual(record!.endpoints, ["/secure-a2a"]);
  assert.equal(record!.description, "Temporary row for stls-core (Node) PostgresLoader interop test");
  assert.deepEqual(record!.metadata, { tenant: "test", plan: "free" });
  assert.ok(record!.expiresAt instanceof Date, "expiresAt must be a real Date (TIMESTAMPTZ -> Date)");
  assert.ok(record!.expiresAt!.getTime() > Date.now(), "expiresAt must be in the future");
  assert.ok(record!.createdAt instanceof Date, "createdAt must be a real Date (TIMESTAMPTZ -> Date)");
  assert.equal(record!.isExpired(), false);
  assert.equal(record!.allowsEndpoint("/secure-a2a/stream"), true, "prefix-based endpoint match");
  assert.equal(record!.allowsEndpoint("/admin"), false);
  console.log("OK: loadOne() reads the real row — TEXT[], JSONB and TIMESTAMPTZ all convert correctly");

  // 2) loadAll() — the test row must be among the non-revoked rows
  const all = await loader.loadAll();
  assert.ok(all.some((r) => r.keyId === TEST_KEY_ID), "test row missing from loadAll()");
  console.log(`OK: loadAll() returns ${all.length} non-revoked rows, test row included`);

  // 3) loadOne() on a nonexistent key_id -> null, not an exception
  const missing = await loader.loadOne("does-not-exist");
  assert.equal(missing, null);
  console.log("OK: loadOne() on an unknown key_id -> null");

  await loader.close();
  console.log("ALL POSTGRES LOADER LIVE CHECKS PASSED (real PostgreSQL, not just error paths)");
} finally {
  await admin.query("DELETE FROM sf_api_keys WHERE key_id = $1", [TEST_KEY_ID]);
  await admin.end();
  console.log("OK: fixture row removed from sf_api_keys");
}
