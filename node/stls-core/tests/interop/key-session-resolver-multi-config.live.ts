/**
 * key-session-resolver-multi-config.live.ts
 * --------------------------------------------
 * Real end-to-end test of KeySessionResolver's per-target configPath
 * override (session-resolver.ts) — the Node twin of
 * python/stls-core/src/stls_core/providers/stls_core_providers_session_resolver.py,
 * and the Node-side consumer validating the sidecar RPC fix that
 * propagates configPath through to the Python side (arqit_sidecar.py's
 * _start(), see python/stls-core/CHANGELOG.md).
 *
 * Unlike arqit-sidecar-real.live.ts (which proves ONE device pair
 * works through the sidecar, same config for every client session),
 * this proves that ONE client-side sidecar PROCESS can resolve TWO
 * DIFFERENT device identities across two calls, via
 * KeySessionResolver's targetConfigs — the exact scenario
 * stls_a2a.SecureSessionManager's arqit_client_configs= and
 * stls_mcp.SecureToolsetManager's arqit_client_configs= already
 * validated sequentially on the Python side (see
 * samples/client/10_client_ms/ and samples/client/11_client_mcp_ms/).
 *
 * Setup (otherwise this throws immediately — no automatic skip, same
 * convention as the other *.live.ts tests in this project):
 *   - two already-running real arqit_py SERVER-role peers — reuses the
 *     already-provisioned devices/servers from
 *     samples/server/11_server_mcp_ms/server_mcp_ms_1 (port 8891) and
 *     server_mcp_ms_2 (port 8892):
 *       KEY_PROVIDER_NAME=arqit_py python samples/server/11_server_mcp_ms/server_mcp_ms_1/server.py
 *       KEY_PROVIDER_NAME=arqit_py python samples/server/11_server_mcp_ms/server_mcp_ms_2/server.py
 *   - ONE already-running Python sidecar process for the CLIENT role,
 *     on its own socket, with NO ARQIT_PY_CLIENT_CONFIG env var set —
 *     this test supplies a different config_path per call instead:
 *       python sidecar/arqit_sidecar.py --socket /tmp/arqit-resolver-client.sock
 *   - ARQIT_SIDECAR_SOCKET_CLIENT pointing at that socket, and
 *     ARQIT_PY_CLIENT_CONFIG_1 / ARQIT_PY_CLIENT_CONFIG_2 pointing at
 *     the two already-provisioned client device configs
 *     (samples/client/11_client_mcp_ms/aq-sf-mcp-client-ms-{1,2}.json)
 *   - rerunning this rapidly can hit real Arqit-side rate-limiting —
 *     space retries out rather than treating one back-to-back failure
 *     as a regression (see arqit-sidecar-real.live.ts's own note)
 *
 * Usage:
 *   ARQIT_SIDECAR_SOCKET_CLIENT=/tmp/arqit-resolver-client.sock \
 *   ARQIT_PY_CLIENT_CONFIG_1=.../aq-sf-mcp-client-ms-1.json \
 *   ARQIT_PY_CLIENT_CONFIG_2=.../aq-sf-mcp-client-ms-2.json \
 *   npx tsx tests/interop/key-session-resolver-multi-config.live.ts
 */
import assert from "node:assert/strict";
import { KeySessionResolver } from "../../src/providers/session-resolver.js";

const SOCKET = process.env.ARQIT_SIDECAR_SOCKET_CLIENT;
const CONFIG_1 = process.env.ARQIT_PY_CLIENT_CONFIG_1;
const CONFIG_2 = process.env.ARQIT_PY_CLIENT_CONFIG_2;

if (!SOCKET) {
  throw new Error(
    "ARQIT_SIDECAR_SOCKET_CLIENT not set — this test requires one already-running Python sidecar " +
      "process for the CLIENT role (see the module comment above).",
  );
}
if (!CONFIG_1 || !CONFIG_2) {
  throw new Error(
    "ARQIT_PY_CLIENT_CONFIG_1 / ARQIT_PY_CLIENT_CONFIG_2 not set — this test requires two real, " +
      "already-provisioned Arqit client device configs (see the module comment above).",
  );
}

const TARGET_1 = "mcp-server-1";
const TARGET_2 = "mcp-server-2";

const resolver = new KeySessionResolver({
  providerName: "arqit_py",
  targetConfigs: { [TARGET_1]: CONFIG_1, [TARGET_2]: CONFIG_2 },
});

const { kid: kid1, kdata: kdata1 } = await resolver.resolve(TARGET_1, { socketPath: SOCKET });
console.log(`OK: resolved ${TARGET_1} via configPath=${CONFIG_1}, kid=${kid1}, key_len=${kdata1.length}`);
assert.ok(kdata1.length > 0, `${TARGET_1}: empty key`);

const { kid: kid2, kdata: kdata2 } = await resolver.resolve(TARGET_2, { socketPath: SOCKET });
console.log(`OK: resolved ${TARGET_2} via configPath=${CONFIG_2}, kid=${kid2}, key_len=${kdata2.length}`);
assert.ok(kdata2.length > 0, `${TARGET_2}: empty key`);

assert.notEqual(kid1, kid2, "both targets resolved to the same kid — device identity override did not take effect");
assert.notEqual(kdata1.toString("base64"), kdata2.toString("base64"), "both targets resolved to the same key");

console.log(
  "ALL KEY SESSION RESOLVER MULTI-CONFIG CHECKS PASSED " +
    "(one sidecar process, two different real Arqit device identities via configPath, distinct kid/key each)",
);

// The arqit_py sidecar path returns only the KeySessionProvider, with
// no handle to close the underlying RPC socket connection(s) — the
// Node process never exits on its own (same known characteristic
// already documented for node/stls-mcp's vault-client.ts, see
// sidecar/ARQIT_PEERING_NOTES.md's 2026-10-18 follow-up). Exit
// explicitly now that every assertion above has already passed.
process.exit(0);
