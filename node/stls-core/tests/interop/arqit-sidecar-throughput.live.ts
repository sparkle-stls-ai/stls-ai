/**
 * arqit-sidecar-throughput.live.ts
 * -----------------------------------
 * Issue #7: measures, against the real Arqit SKA-Platform™ cloud, what
 * "one socket connection = one STLS session, no pooling/reuse" (Option A,
 * see arqit-sidecar.ts's module docstring) actually costs for the
 * provider where it's documented to matter most — arqit_py. Does NOT
 * cover base/pqc (out of scope for the PoC this is feeding).
 *
 * Only times the key-establishment phase (clientProvider.start() +
 * getSessionKeyIdentifier() + getSessionKeyData()) — the real cost of
 * "a new socket per session" lives there (bilocation negotiation,
 * cloud round trip, the process-wide _ARQIT_PY_START_LOCK documented in
 * python/stls-core/sidecar/ARQIT_PEERING_NOTES.md). The STLS
 * handshake/encrypt-decrypt correctness proof is already covered by
 * arqit-sidecar-real.live.ts — deliberately not repeated here to keep
 * this test (and its real-cloud call count) lean.
 *
 * Two runs, same N, same long-running SERVER-role sidecar:
 *   A) SEQUENTIAL — N client sessions one after another. This is the
 *      "normal" shape (requests arriving one at a time).
 *   B) CONCURRENT — same N, all started together via Promise.all(). If
 *      _ARQIT_PY_START_LOCK is really serializing start() process-wide
 *      as documented, this should NOT be meaningfully faster than (A) —
 *      proving (or disproving) that claim with a number, not just by
 *      reading the code.
 *
 * Setup: identical to arqit-sidecar-real.live.ts — two already-running
 * sidecar processes (one per role), ARQIT_SIDECAR_SOCKET_SERVER/_CLIENT
 * + matching tokens. See that file for the full setup note.
 *
 * Usage:
 *   ARQIT_SIDECAR_SOCKET_SERVER=/tmp/arqit-server.sock \
 *   ARQIT_SIDECAR_SOCKET_CLIENT=/tmp/arqit-client.sock \
 *   ARQIT_SIDECAR_TOKEN_SERVER=server-secret \
 *   ARQIT_SIDECAR_TOKEN_CLIENT=client-secret \
 *   ARQIT_N_THROUGHPUT=3 \
 *   npx tsx tests/interop/arqit-sidecar-throughput.live.ts
 */
import {
  KeyProviderClientArqitSidecar,
  KeyProviderServerArqitSidecar,
} from "../../src/providers/arqit-sidecar.js";

const SERVER_SOCKET = process.env.ARQIT_SIDECAR_SOCKET_SERVER;
const CLIENT_SOCKET = process.env.ARQIT_SIDECAR_SOCKET_CLIENT;
// Unlike arqit-sidecar-real.live.ts (which relies on the flat
// ARQIT_SIDECAR_TOKEN and so only works if both sidecars happen to share
// one token), this passes each role's token explicitly — KeyProviderClient/
// ServerArqitSidecar's own resolveToken() only knows the flat var, the
// role-aware ARQIT_SIDECAR_TOKEN_SERVER/_CLIENT split lives in
// KeySessionProviderFactory, which this script deliberately bypasses (same
// as its sibling) to drive the sidecar classes directly.
const SERVER_TOKEN = process.env.ARQIT_SIDECAR_TOKEN_SERVER;
const CLIENT_TOKEN = process.env.ARQIT_SIDECAR_TOKEN_CLIENT;
const N = Number(process.env.ARQIT_N_THROUGHPUT ?? "3");

if (!SERVER_SOCKET || !CLIENT_SOCKET || !SERVER_TOKEN || !CLIENT_TOKEN) {
  throw new Error(
    "ARQIT_SIDECAR_SOCKET_SERVER / ARQIT_SIDECAR_SOCKET_CLIENT / ARQIT_SIDECAR_TOKEN_SERVER / " +
      "ARQIT_SIDECAR_TOKEN_CLIENT not set — see arqit-sidecar-real.live.ts's setup note.",
  );
}

async function oneClientSession(label: string): Promise<number> {
  const t0 = performance.now();
  const clientProvider = new KeyProviderClientArqitSidecar({
    socketPath: CLIENT_SOCKET!,
    providerName: "arqit_py",
    token: CLIENT_TOKEN,
  });
  try {
    await clientProvider.start();
    const session = clientProvider.createProviderSession();
    const kid = await session.getSessionKeyIdentifier();
    await session.getSessionKeyData(kid);
    const elapsedMs = performance.now() - t0;
    console.log(`  [${label}] kid=${kid} — ${elapsedMs.toFixed(0)}ms`);
    return elapsedMs;
  } finally {
    await clientProvider.stop();
  }
}

function summarize(label: string, totalMs: number, perSessionMs: number[]): void {
  const avg = perSessionMs.reduce((a, b) => a + b, 0) / perSessionMs.length;
  const min = Math.min(...perSessionMs);
  const max = Math.max(...perSessionMs);
  console.log(
    `\n${label}: N=${perSessionMs.length}, wall-clock=${totalMs.toFixed(0)}ms, ` +
      `throughput=${(perSessionMs.length / (totalMs / 1000)).toFixed(3)} sessions/sec, ` +
      `per-session avg=${avg.toFixed(0)}ms min=${min.toFixed(0)}ms max=${max.toFixed(0)}ms`,
  );
}

const serverProvider = new KeyProviderServerArqitSidecar({
  socketPath: SERVER_SOCKET,
  providerName: "arqit_py",
  token: SERVER_TOKEN,
});
await serverProvider.start();
console.log(`OK: SERVER role started (real arqit_py, receiver listening) — N=${N} per run\n`);

try {
  console.log(`=== A) SEQUENTIAL (${N} client sessions, one after another) ===`);
  const seqStart = performance.now();
  const seqTimes: number[] = [];
  for (let i = 0; i < N; i++) {
    seqTimes.push(await oneClientSession(`seq ${i + 1}/${N}`));
  }
  summarize("SEQUENTIAL", performance.now() - seqStart, seqTimes);

  console.log(`\n=== B) CONCURRENT (${N} client sessions, all started together) ===`);
  const concStart = performance.now();
  const concTimes = await Promise.all(
    Array.from({ length: N }, (_, i) => oneClientSession(`conc ${i + 1}/${N}`)),
  );
  summarize("CONCURRENT", performance.now() - concStart, concTimes);

  console.log(
    "\nIf CONCURRENT's wall-clock is close to SEQUENTIAL's (not ~1/N of it), that confirms " +
      "_ARQIT_PY_START_LOCK is the binding constraint, not the one-socket-per-session architecture — " +
      "connection pooling would not help throughput for this provider.",
  );
} finally {
  await serverProvider.stop();
}

process.exit(0);
