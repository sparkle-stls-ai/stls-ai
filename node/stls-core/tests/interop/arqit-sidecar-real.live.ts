/**
 * arqit-sidecar-real.live.ts
 * ----------------------------
 * Real end-to-end test against the Arqit SKA-Platform™ cloud — not a
 * mock, not provider_name="base". Unlike arqit-sidecar.smoke.ts (RPC
 * mechanics only, deterministic fixed key), this drives an actual
 * bilocation key exchange through the Python sidecar, then proves the
 * key is usable: a real STLS handshake (proof-of-key-possession) and a
 * real AES-256-GCM encrypt/decrypt round trip.
 *
 * Why this exists, and why it needs TWO sidecar processes, not one:
 * -----------------------------------------------------------------
 * python/stls-core/sidecar/ARQIT_PEERING_NOTES.md documents a real
 * finding from 2026-10-17: the Arqit SDK (or its backend validation)
 * rejects the bilocation key exchange with a genuine
 * `401 NotAuthorizedException` when the SERVER and CLIENT device
 * sessions of the same pair live in the same OS process — confirmed
 * with a controlled A/B in pure Python (no stls-mcp, no sidecar
 * involved at all). The original "blocked" investigation included the
 * Node sidecar specifically *because* a single `arqit_sidecar.py`
 * process can serve both an RPC "SERVER" and "CLIENT" session over two
 * connections — which is exactly the broken same-process pattern.
 *
 * `arqit_sidecar.py` now has a guard that refuses a second, different
 * role for provider_name="arqit_py" in the same process (see
 * arqit-sidecar-guard.smoke.ts) — so the only way to drive real
 * `arqit_py` peering through the sidecar at all is with two SEPARATE
 * sidecar processes, one per role. This file is also the executable
 * proof that doing so actually works end to end, through the full
 * JSON-RPC-over-Unix-socket layer, not just in pure Python.
 *
 * Covers two scenarios in one run (see ARQIT_N_CLIENTS below):
 *   - a single round trip (key agreement + handshake + encrypted echo)
 *   - N SEQUENTIAL client sessions against the SAME long-running SERVER
 *     sidecar session — the real production shape (a long-lived server,
 *     many separate handshakes over its lifetime) — asserting every
 *     kid/session_id comes out distinct.
 *
 * Setup (otherwise this throws immediately — no automatic skip, same
 * convention as the other *.live.ts tests in this project):
 *   - the [arqit_py] extra installed in python/stls-core/.venv (real
 *     Arqit SKA-Platform™ wheels)
 *   - two ALREADY-RUNNING Python sidecar processes, each on its own
 *     socket, each with ARQIT_PY_CLIENT_CONFIG/ARQIT_PY_SERVER_CONFIG
 *     pointing at a real, already-provisioned device pair:
 *       python sidecar/arqit_sidecar.py --socket /tmp/arqit-server.sock
 *       python sidecar/arqit_sidecar.py --socket /tmp/arqit-client.sock
 *   - ARQIT_SIDECAR_SOCKET_SERVER / ARQIT_SIDECAR_SOCKET_CLIENT pointing
 *     at those two sockets (NOT the same socket — that reintroduces the
 *     same-process problem this file exists to avoid)
 *   - relative paths inside the device JSON configs
 *     (DEVICE_METADATA_FILEPATH etc.) resolve against the Python
 *     process's cwd, not the JSON file's location — start both sidecar
 *     processes from whatever cwd makes those resolve correctly (see
 *     python/stls-core/tests/test_arqit_py_real_peering.py for the same
 *     caveat, hit for real during that investigation)
 *   - rerunning this rapidly can hit real Arqit-side rate-limiting —
 *     observed during development as a stray timeout; space retries out
 *     rather than treating one back-to-back failure as a regression
 *
 * Usage:
 *   ARQIT_SIDECAR_SOCKET_SERVER=/tmp/arqit-server.sock \
 *   ARQIT_SIDECAR_SOCKET_CLIENT=/tmp/arqit-client.sock \
 *   ARQIT_N_CLIENTS=3 \
 *   npx tsx tests/interop/arqit-sidecar-real.live.ts
 */
import assert from "node:assert/strict";
import {
  KeyProviderClientArqitSidecar,
  KeyProviderServerArqitSidecar,
} from "../../src/providers/arqit-sidecar.js";
import { InMemorySessionCache } from "../../src/session.js";
import { generateHandshakeRequest, performHandshake, verifyHandshakeProof } from "../../src/handshake.js";

const SERVER_SOCKET = process.env.ARQIT_SIDECAR_SOCKET_SERVER;
const CLIENT_SOCKET = process.env.ARQIT_SIDECAR_SOCKET_CLIENT;
const N_CLIENTS = Number(process.env.ARQIT_N_CLIENTS ?? "1");

if (!SERVER_SOCKET || !CLIENT_SOCKET) {
  throw new Error(
    "ARQIT_SIDECAR_SOCKET_SERVER / ARQIT_SIDECAR_SOCKET_CLIENT not set — this test requires two " +
      "already-running Python sidecar processes (one per role — see the module comment above), each " +
      "configured with a real, already-provisioned Arqit device pair.",
  );
}
if (SERVER_SOCKET === CLIENT_SOCKET) {
  throw new Error(
    "ARQIT_SIDECAR_SOCKET_SERVER and ARQIT_SIDECAR_SOCKET_CLIENT must be two DIFFERENT sidecar " +
      "processes — pointing both roles at the same socket reintroduces the same-process problem " +
      "this test exists to avoid (see the module comment above).",
  );
}

const sessionCache = new InMemorySessionCache();

// One persistent SERVER role for the whole run — exactly like
// vault_server.py reusing one long-running provider instance for every
// handshake it ever serves. Re-`start()`ing the SERVER role against the
// same sidecar process would try to re-bind the receiver's listening
// socket and fail — this is why only the CLIENT role is recreated per
// iteration below, never the server.
const serverProvider = new KeyProviderServerArqitSidecar({ socketPath: SERVER_SOCKET, providerName: "arqit_py" });
await serverProvider.start();
const serverSession = serverProvider.createProviderSession();
console.log("OK: SERVER role started on its own sidecar process (real arqit_py, receiver listening)");

const seenKids = new Set<string>();
const seenSessionIds = new Set<string>();

try {
  for (let i = 0; i < N_CLIENTS; i++) {
    const label = `client ${i + 1}/${N_CLIENTS}`;
    const clientProvider = new KeyProviderClientArqitSidecar({ socketPath: CLIENT_SOCKET, providerName: "arqit_py" });

    try {
      await clientProvider.start();
      const clientSession = clientProvider.createProviderSession();

      // Real key agreement FIRST, before the handshake is attempted
      // server-side. This is the order that actually works for
      // arqit_py: it's what drives/awaits the real bilocation
      // negotiation, so by the time the server resolves its own key a
      // moment later it's already there — resolving in the opposite
      // order (as the provider_name="base" smoke test does) only works
      // for "base" because that key is static, not negotiated.
      const clientKid = await clientSession.getSessionKeyIdentifier();
      const clientKey = await clientSession.getSessionKeyData(clientKid);
      console.log(`OK [${label}]: real Arqit bilocation key agreement, kid=${clientKid}`);

      // Real STLS handshake — proves the two keys, independently
      // obtained via two different sidecar processes, are
      // byte-identical, not just "both present".
      const req = generateHandshakeRequest(`node-arqit-e2e-${i}`);
      const response = await performHandshake(req, serverSession, sessionCache);
      const verified = verifyHandshakeProof(clientKey, req.nonce, response);
      assert.equal(verified, true, `[${label}] proof-of-key-possession failed — client/server keys do not match`);
      console.log(`OK [${label}]: handshake verified, session_id=${response.session_id}`);

      // Real AES-256-GCM + HMAC-SHA256 round trip over the session the
      // handshake just registered.
      const serverTransport = await sessionCache.get(response.session_id);
      assert.ok(serverTransport, `[${label}] session not found in cache`);
      const payload = Buffer.from(JSON.stringify({ ping: "secured-over-real-arqit-sidecar", n: i }));
      const envelope = serverTransport!.encrypt(payload);
      const recovered = serverTransport!.decrypt(envelope);
      assert.deepEqual(recovered, payload, `[${label}] decrypted payload mismatch`);
      console.log(`OK [${label}]: real AES-256-GCM encrypt/decrypt round trip through the sidecar`);

      assert.ok(!seenKids.has(clientKid), `[${label}] kid reused across clients — silent security bug: ${clientKid}`);
      assert.ok(
        !seenSessionIds.has(response.session_id),
        `[${label}] session_id reused across handshakes: ${response.session_id}`,
      );
      seenKids.add(clientKid);
      seenSessionIds.add(response.session_id);
    } finally {
      await clientProvider.stop();
    }
  }
} finally {
  await serverProvider.stop();
}

console.log(
  `ALL ${N_CLIENTS} ARQIT SIDECAR REAL-PEERING ROUND TRIP(S) PASSED ` +
    "(provider_name=arqit_py, two separate sidecar processes, distinct kid/session_id each time)",
);
