/**
 * provider-factory-arqit-real.live.ts
 * -------------------------------------
 * Real end-to-end test of KeySessionProviderFactory's "arqit_py" path —
 * not the wiring only (provider-factory.smoke.ts's arqit_py checks all
 * fail on purpose, before any real config/network access, to verify the
 * role-aware socket resolution without needing credentials), but a real
 * bilocation key agreement with the real Arqit SKA-Platform™ cloud,
 * through the SAME KeySessionProviderFactory.getSessionProvider() call a
 * real application would use — not KeyProviderArqitSidecar directly
 * (already covered by arqit-sidecar-real.live.ts).
 *
 * Mirrors provider-factory.smoke.ts's "base" section (real handshake +
 * real encrypt/decrypt), just built via the factory's arqit_py path
 * instead of "base", against two already-running sidecar processes.
 *
 * Setup (otherwise throws immediately, same convention as the other
 * *.live.ts files in this project):
 *   - two ALREADY-RUNNING Python sidecar processes, each on its own
 *     socket (SERVER and CLIENT must be two separate processes — see
 *     python/stls-core/sidecar/ARQIT_PEERING_NOTES.md), each with
 *     ARQIT_PY_CLIENT_CONFIG/ARQIT_PY_SERVER_CONFIG pointing at a real,
 *     already-provisioned device pair:
 *       python sidecar/arqit_sidecar.py --socket /tmp/arqit-server.sock
 *       python sidecar/arqit_sidecar.py --socket /tmp/arqit-client.sock
 *   - ARQIT_SIDECAR_SOCKET_SERVER / ARQIT_SIDECAR_SOCKET_CLIENT set to
 *     those two sockets
 *   - each sidecar process can complete its SERVER-role "start" only
 *     ONCE per process lifetime (the Arqit SDK's DevicePolicyStore is a
 *     process-wide singleton — see ARQIT_PEERING_NOTES.md) — use freshly
 *     started sidecar processes for this test, not ones already used by
 *     another real-peering run
 *
 * Usage:
 *   ARQIT_SIDECAR_SOCKET_SERVER=/tmp/arqit-server.sock \
 *   ARQIT_SIDECAR_SOCKET_CLIENT=/tmp/arqit-client.sock \
 *   npx tsx tests/interop/provider-factory-arqit-real.live.ts
 */
import assert from "node:assert/strict";
import {
  InMemorySessionCache,
  KeySessionProviderFactory,
  KeySessionProviderType,
  generateHandshakeRequest,
  performHandshake,
  verifyHandshakeProof,
} from "../../src/index.js";

if (!process.env.ARQIT_SIDECAR_SOCKET_SERVER || !process.env.ARQIT_SIDECAR_SOCKET_CLIENT) {
  throw new Error(
    "ARQIT_SIDECAR_SOCKET_SERVER / ARQIT_SIDECAR_SOCKET_CLIENT not set — this test requires two " +
      "already-running Python sidecar processes (one per role), each configured with a real, " +
      "already-provisioned Arqit device pair. See the module comment above.",
  );
}

const serverProvider = await KeySessionProviderFactory.getSessionProvider({
  sessionType: KeySessionProviderType.SERVER,
  name: "arqit_py",
});
console.log("OK: SERVER-role arqit_py provider built via the factory (real bilocation negotiation started)");

const clientProvider = await KeySessionProviderFactory.getSessionProvider({
  sessionType: KeySessionProviderType.CLIENT,
  name: "arqit_py",
});
console.log("OK: CLIENT-role arqit_py provider built via the factory");

const sessionCache = new InMemorySessionCache();
const req = generateHandshakeRequest("factory-arqit-real-live");
const response = await performHandshake(req, serverProvider, sessionCache);
console.log(`OK: real handshake completed via the factory-built arqit_py provider, kid=${response.kid}`);

const clientKid = await clientProvider.getSessionKeyIdentifier();
const clientKey = await clientProvider.getSessionKeyData(clientKid);
assert.equal(
  verifyHandshakeProof(clientKey, req.nonce, response),
  true,
  "proof-of-key-possession failed — the two factory-built providers resolved different keys",
);
console.log("OK: proof-of-key-possession verified — client/server keys from the factory are byte-identical");

const transport = await sessionCache.get(response.session_id);
assert.ok(transport, "session not found in cache");
const payload = Buffer.from("secured-over-real-arqit-via-the-factory");
const envelope = transport!.encrypt(payload);
const recovered = transport!.decrypt(envelope);
assert.deepEqual(recovered, payload);
console.log("OK: real AES-256-GCM encrypt/decrypt round trip over the factory-built arqit_py session");

console.log("ALL PROVIDER FACTORY ARQIT_PY REAL CHECKS PASSED");

// getSessionProvider() returns only the KeySessionProvider — it has no
// stop()/close() of its own (that lives on the KeyProviderServer/Client
// wrapper one level up, which the factory doesn't expose to the caller).
// The sidecar RPC connections it opened stay open, so the process won't
// exit on its own; force it, same as a one-shot CLI script would.
process.exit(0);
