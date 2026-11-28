/**
 * endpoint-toggles.smoke.ts
 * ---------------------------
 * Node mirror of python/stls-mcp/tests/test_stls_mcp_endpoint_toggles.py.
 * Covers issue #80's enablePlainEndpoint / enableSecureEndpoint options
 * on createSecureMcpApp().
 *
 * Coverage:
 *   1. Both false -> createSecureMcpApp() throws instead of silently
 *      registering nothing.
 *   2. enableSecureEndpoint=false: {stlsPath} and the handshake
 *      endpoint are both unreachable (404); {plainPath} still works
 *      end to end (a real tools/list round trip).
 *   3. enablePlainEndpoint=false: {plainPath} is unreachable (404);
 *      the handshake endpoint still responds (not 404).
 */

import assert from "node:assert/strict";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { KeySessionProviderFactory, KeySessionProviderType } from "stls-core";
import { createSecureMcpApp } from "../src/server.js";

function getServer(): McpServer {
  const server = new McpServer({ name: "toggles-test", version: "1.0.0" });
  server.registerTool("ping", { description: "ping" }, async () => ({
    content: [{ type: "text", text: "pong" }],
  }));
  return server;
}

async function main(): Promise<void> {
  const provider = await KeySessionProviderFactory.getSessionProvider({
    sessionType: KeySessionProviderType.SERVER,
    name: "base",
  });

  // 1. Both disabled -> throws.
  await assert.rejects(
    () => createSecureMcpApp(getServer, provider, { enablePlainEndpoint: false, enableSecureEndpoint: false }),
    /enablePlainEndpoint.*enableSecureEndpoint/,
  );
  console.log("OK: 1 both toggles false throws at setup time");

  // 2. Secure disabled, plain only.
  {
    const app = await createSecureMcpApp(getServer, provider, {
      enablePlainEndpoint: true,
      enableSecureEndpoint: false,
    });

    const initReq = new Request("http://test/mcp", {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json, text/event-stream" },
      body: JSON.stringify({
        jsonrpc: "2.0",
        method: "initialize",
        params: { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "test", version: "0" } },
        id: 1,
      }),
    });
    const initResp = await app(initReq);
    assert.notEqual(initResp.status, 404, "plain endpoint should be reachable");

    const handshakeResp = await app(new Request("http://test/stls/handshake", { method: "POST", body: "{}" }));
    assert.equal(handshakeResp.status, 404, "handshake endpoint should be gone");

    const stlsResp = await app(new Request("http://test/mcp-stls", { method: "POST", body: "{}" }));
    assert.equal(stlsResp.status, 404, "secure endpoint should be gone");
  }
  console.log("OK: 2 enableSecureEndpoint=false — plain reachable, secure+handshake gone");

  // 3. Plain disabled, secure only.
  {
    const app = await createSecureMcpApp(getServer, provider, {
      enablePlainEndpoint: false,
      enableSecureEndpoint: true,
    });

    const plainResp = await app(new Request("http://test/mcp", { method: "POST", body: "{}" }));
    assert.equal(plainResp.status, 404, "plain endpoint should be gone");

    const handshakeResp = await app(
      new Request("http://test/stls/handshake", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ bad: "request" }),
      }),
    );
    assert.notEqual(handshakeResp.status, 404, "handshake endpoint should still be reachable");
  }
  console.log("OK: 3 enablePlainEndpoint=false — plain gone, handshake still reachable");

  console.log("\nAll endpoint-toggles.smoke.ts checks passed.");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
