/**
 * arqit-sidecar-guard.smoke.ts
 * ------------------------------
 * Verifies, through the actual TypeScript client classes (not a raw
 * hand-rolled socket script), that arqit_sidecar.py's role guard works:
 * a single sidecar process refuses to serve both a SERVER and a CLIENT
 * session for provider_name="arqit_py" — see the guard in
 * SidecarSession._start() and python/stls-core/sidecar/ARQIT_PEERING_NOTES.md
 * for why mixing both roles in one OS process makes the real Arqit
 * bilocation key exchange fail with a genuine 401, and arqit-sidecar-real.live.ts
 * for the correct two-processes pattern.
 *
 * No real Arqit credentials needed: the guard rejects the second,
 * different role BEFORE the sidecar ever tries to read
 * ARQIT_PY_*_CONFIG or talk to the real cloud — this is why the test
 * can run in CI like the other *.smoke.ts files, unlike
 * arqit-sidecar-real.live.ts.
 *
 * Usage (sidecar process started externally, same convention as
 * arqit-sidecar.smoke.ts):
 *   python sidecar/arqit_sidecar.py --socket /tmp/stls-arqit-guard.sock &
 *   ARQIT_SIDECAR_SOCKET=/tmp/stls-arqit-guard.sock npx tsx tests/interop/arqit-sidecar-guard.smoke.ts
 */
import assert from "node:assert/strict";
import {
  KeyProviderClientArqitSidecar,
  KeyProviderServerArqitSidecar,
} from "../../src/providers/arqit-sidecar.js";

const socketPath = process.env.ARQIT_SIDECAR_SOCKET ?? "/tmp/stls-arqit-sidecar-guard-test.sock";

// First connection: claims the SERVER role for this sidecar process.
// No real ARQIT_PY_SERVER_CONFIG is set in this smoke test either, so
// it fails for its own, unrelated reason right after — the guard still
// claims the role regardless of whether the underlying real provider
// call later succeeds (by design, see the Python module docstring: the
// point is to prevent the role from ever being mixed in this process,
// not to gate on success).
const serverProvider = new KeyProviderServerArqitSidecar({ socketPath, providerName: "arqit_py" });
try {
  await assert.rejects(serverProvider.start(), /ARQIT_PY_SERVER_CONFIG/);
  console.log("OK: first connection claimed the SERVER role (failed only on its own missing config, as expected)");
} finally {
  // start() rejecting still leaves the underlying RPC socket open
  // (set before the failing "start" call) — stop() closes it even
  // though start() never completed; without this the socket keeps
  // the Node process alive indefinitely after the test's assertions
  // already passed.
  await serverProvider.stop();
}

// Second connection, same sidecar process, the OTHER role: must be
// rejected immediately by the guard, not left to fail mysteriously
// after a real (or in this case, nonexistent) 30s negotiation attempt.
const clientProvider = new KeyProviderClientArqitSidecar({ socketPath, providerName: "arqit_py" });
try {
  await assert.rejects(clientProvider.start(), /already serves the 'SERVER' role/);
  console.log("OK: second connection, different role, rejected immediately by the sidecar's guard");
} finally {
  await clientProvider.stop();
}

console.log("ALL ARQIT SIDECAR GUARD CHECKS PASSED");
