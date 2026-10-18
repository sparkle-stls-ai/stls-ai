import assert from "node:assert/strict";
import { PostgresLoader } from "../src/auth/loaders-postgres.js";

// NB: no real Postgres available in this environment — this test only
// verifies the error paths (missing DSN, connection refused), not the SQL
// query or the row->KeyRecord conversion against a real database. It must
// be further validated against a real Postgres instance before use in
// production (see CHANGELOG.md and README.md).

// 1) No DSN configured -> loadAll()/loadOne() return empty, no throw
{
  const loader = new PostgresLoader({ dsn: "" });
  const all = await loader.loadAll();
  assert.deepEqual(all, []);
  const one = await loader.loadOne("whatever");
  assert.equal(one, null);
  console.log("OK: PostgresLoader with no DSN -> empty fallback (no exception)");
}

// 2) DSN pointing at a nonexistent server -> same soft-fail behavior
{
  const loader = new PostgresLoader({
    dsn: "postgresql://nouser:nopass@127.0.0.1:1/nonexistent",
    maxPoolSize: 1,
  });
  const all = await loader.loadAll();
  assert.deepEqual(all, []);
  const one = await loader.loadOne("whatever");
  assert.equal(one, null);
  await loader.close();
  console.log("OK: PostgresLoader with a refused connection -> empty fallback (no exception)");
}

// 3) close() without ever having created the pool must not throw
{
  const loader = new PostgresLoader({ dsn: "" });
  await loader.close();
  console.log("OK: close() with no initialized pool does not throw");
}

console.log("ALL POSTGRES LOADER SMOKE CHECKS PASSED (error paths only — no real DB tested)");
