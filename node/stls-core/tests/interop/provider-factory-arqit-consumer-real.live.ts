/**
 * provider-factory-arqit-consumer-real.live.ts
 * -----------------------------------------------
 * Real end-to-end test of ARQIT_PY_DEVICE_ROLE=consumer (issue #38)
 * through the Node sidecar path -- the one gap flagged after building
 * that mode: reasoned about via code inspection (the sidecar's "start"
 * RPC calls straight into KeySessionProviderFactory.get_session_provider()
 * on the Python side, which already has the consumer branch), never
 * actually run. This runs it, against a real Python sidecar process and
 * the real Redis-backed key store a real device-agent already fed.
 *
 * Unlike provider-factory-arqit-real.live.ts (the OWNER-role sibling of
 * this file), this sidecar never authenticates any device itself -- its
 * own process environment has ARQIT_PY_DEVICE_ROLE=consumer set when it
 * was launched (this script neither sets nor reads that var: it's a
 * Python-process-only concern, see KeySessionProviderFactory.get_session_provider()
 * on the Python side). It only resolves a kid a real CLIENT already
 * negotiated against a real, separately-running device-agent
 * (python/stls-core/sidecar/arqit_device_agent.py) -- confirmed
 * end-to-end for Python callers in samples/server/16_server_device_agent/;
 * this is the same mechanism through the Node sidecar instead.
 *
 * Setup (otherwise throws immediately, same convention as the other
 * *.live.ts files in this project):
 *   1. A real device-agent already running against a real, provisioned
 *      device pair, feeding a real Redis (KEY_STORE_BACKEND=redis) --
 *      see samples/server/16_server_device_agent/README.md.
 *   2. A real CLIENT session already resolved against that same device
 *      (e.g. samples/client/16_client_device_agent/client_session.py's
 *      step 1, or any other real arqit_py client) -- pass its kid/key
 *      via EXPECTED_KID/EXPECTED_KEY_B64 below.
 *   3. ONE Python sidecar process, SERVER role, consumer mode, pointed
 *      at the SAME Redis namespace the device-agent uses:
 *        ARQIT_SIDECAR_TOKEN=<token> KEY_PROVIDER_NAME=arqit_py \
 *        ARQIT_PY_DEVICE_ROLE=consumer KEY_STORE_BACKEND=redis \
 *        KEY_STORE_REDIS_URL=redis://localhost:6379 \
 *        KEY_STORE_REDIS_NAMESPACE=<same as the device-agent> \
 *        python sidecar/arqit_sidecar.py --socket /tmp/some.sock
 *
 * Usage:
 *   ARQIT_SIDECAR_SOCKET_SERVER=/tmp/some.sock \
 *   ARQIT_SIDECAR_TOKEN_SERVER=<token> \
 *   EXPECTED_KID=<kid from step 2> \
 *   EXPECTED_KEY_B64=<base64 key from step 2> \
 *   npx tsx tests/interop/provider-factory-arqit-consumer-real.live.ts
 */
import assert from "node:assert/strict";
import { KeySessionProviderFactory, KeySessionProviderType } from "../../src/index.js";

if (!process.env.ARQIT_SIDECAR_SOCKET_SERVER) {
  throw new Error(
    "ARQIT_SIDECAR_SOCKET_SERVER not set -- this test requires an already-running Python sidecar " +
      "process, SERVER role, ARQIT_PY_DEVICE_ROLE=consumer on ITS OWN env. See the module comment above.",
  );
}
const expectedKid = process.env.EXPECTED_KID;
const expectedKeyB64 = process.env.EXPECTED_KEY_B64;
if (!expectedKid || !expectedKeyB64) {
  throw new Error(
    "EXPECTED_KID / EXPECTED_KEY_B64 not set -- resolve a real CLIENT session against the real " +
      "device-agent first and pass its kid/key here. See the module comment above.",
  );
}

const provider = await KeySessionProviderFactory.getSessionProvider({
  sessionType: KeySessionProviderType.SERVER,
  name: "arqit_py",
});
console.log("OK: SERVER-role arqit_py provider built via the factory (consumer mode -- no real device auth here)");

const resolvedKid = await provider.getSessionKeyIdentifier(expectedKid);
assert.equal(resolvedKid, expectedKid, "resolved kid does not match the hint");
console.log(`OK: resolved kid matches hint: ${resolvedKid}`);

const keyBytes = await provider.getSessionKeyData(resolvedKid);
const expectedBytes = Buffer.from(expectedKeyB64, "base64");
assert.deepEqual(Buffer.from(keyBytes), expectedBytes, "resolved key bytes do not match");
console.log("OK: resolved key bytes are byte-identical to the real client's negotiated key");

console.log("ALL NODE CONSUMER-MODE CHECKS PASSED");
process.exit(0);
