/**
 * postgres-store.smoke.ts
 * --------------------------
 * Node mirror of python/stls-identity/tests/test_postgres_store.py's
 * error-path coverage (no real Postgres available in this environment
 * — same caveat as node/stls-core's own postgres-loader.smoke.ts. Must
 * be further validated against a real Postgres instance before
 * production use).
 */

import assert from "node:assert/strict";
import { PostgresDIDDocumentStore, PostgresStatusListCacheStore } from "../src/postgres-store.js";

// T01 — DIDDocumentStore.get() with no DSN configured -> null, no throw
{
  const store = new PostgresDIDDocumentStore({ dsn: "" });
  assert.equal(await store.get("agent-087"), null);
  console.log("OK: T01 DIDDocumentStore.get() with no DSN -> null (no throw)");
}

// T02 — DIDDocumentStore.put() with no DSN configured DOES throw (matches
// Python: get()/get_key_valid_at() soft-fail, put()/listAgentIds() do not)
{
  const store = new PostgresDIDDocumentStore({ dsn: "" });
  await assert.rejects(() =>
    store.put("agent-087", {
      "@context": [],
      id: "did:web:x",
      controller: "did:web:x",
      verificationMethod: [],
      authentication: [],
      assertionMethod: [],
      created: new Date().toISOString(),
    }),
  );
  console.log("OK: T02 DIDDocumentStore.put() with no DSN -> throws");
}

// T03 — table/getDdl() respect the env-var override, not just the fixed default
{
  const store = new PostgresDIDDocumentStore({ dsn: "", table: "custom_did_table" });
  assert.equal(store.table, "custom_did_table");
  assert.ok(store.getDdl().includes("custom_did_table"));
  assert.ok(!store.getDdl().includes(PostgresDIDDocumentStore.DEFAULT_TABLE));
  console.log("OK: T03 DIDDocumentStore table/getDdl() respect override");
}

// T04 — a refused connection (unreachable host) soft-fails the same way as no DSN
{
  const store = new PostgresDIDDocumentStore({
    dsn: "postgresql://nouser:nopass@127.0.0.1:1/nonexistent",
    maxPoolSize: 1,
  });
  assert.equal(await store.get("agent-087"), null);
  await store.close();
  console.log("OK: T04 DIDDocumentStore.get() with refused connection -> null (no throw)");
}

// T05 — close() without ever having created the pool must not throw
{
  const store = new PostgresDIDDocumentStore({ dsn: "" });
  await store.close();
  console.log("OK: T05 DIDDocumentStore.close() with no initialized pool does not throw");
}

// T06 — StatusListCacheStore mirrors the same soft-fail/override behavior
{
  const store = new PostgresStatusListCacheStore({ dsn: "", table: "custom_status_table" });
  assert.equal(await store.get("authority-1"), null);
  assert.equal(store.table, "custom_status_table");
  await store.close();
  console.log("OK: T06 StatusListCacheStore soft-fail + table override");
}

console.log("ALL POSTGRES STORE SMOKE CHECKS PASSED (error paths only — no real DB tested)");
