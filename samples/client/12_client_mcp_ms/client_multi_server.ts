/**
 * client_multi_server.ts — "1 client -> N MCP servers" (Node)
 * -----------------------------------------------------------------
 * Node analogue of samples/client/11_client_mcp_ms/client_multi_server.py.
 * Unlike the Python version (which goes through ADK's STLSMCPToolset/
 * SecureToolsetManager, since that's an ADK-integration helper), the
 * Node side has no ADK at all — stls-mcp's Node client API is already
 * ADK-free (raw @modelcontextprotocol/sdk Client + stls-core's
 * KeySessionResolver), so this calls each server's tool directly, no
 * agent framework needed. This is also the first real consumer of
 * KeySessionResolver + the sidecar configPath fix against two
 * DIFFERENT arqit_py device identities from one Node process (see
 * node/stls-core/CHANGELOG.md and
 * node/stls-core/tests/interop/key-session-resolver-multi-config.live.ts).
 *
 * Works, unmodified, with KEY_PROVIDER_NAME=base|pqc|arqit_py.
 *
 * Run (after starting both servers):
 *   npx tsx --env-file=.env client_multi_server.ts
 */
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { KeySessionResolver } from "stls-core";
import { createStlsFetch, performClientHandshake, performPqcClientHandshake } from "stls-mcp";

const PROVIDER_NAME = (process.env.KEY_PROVIDER_NAME ?? "pqc").trim().toLowerCase();

const SERVERS: Array<[string, string]> = [
  ["server-mcp-ms-1-node", process.env.BASE_URL_1 ?? "http://localhost:8211"],
  ["server-mcp-ms-2-node", process.env.BASE_URL_2 ?? "http://localhost:8212"],
];

// Only relevant for arqit_py: a different device identity per server,
// resolved by base_url — same shape as stls-a2a/stls-mcp's Python
// managers' arqit_client_configs=.
const ARQIT_CLIENT_CONFIGS: Record<string, string> = {
  [SERVERS[0][1]]: process.env.ARQIT_PY_CLIENT_CONFIG_1 ?? "./aq-sf-node-mcp-client-ms-1.json",
  [SERVERS[1][1]]: process.env.ARQIT_PY_CLIENT_CONFIG_2 ?? "./aq-sf-node-mcp-client-ms-2.json",
};

const resolver = new KeySessionResolver({ providerName: PROVIDER_NAME, targetConfigs: ARQIT_CLIENT_CONFIGS });

async function callServer(label: string, baseUrl: string): Promise<boolean> {
  const toolName = `ping_${label.replace(/-/g, "_")}`;
  try {
    let sessionId: string;
    let transport: Awaited<ReturnType<typeof performClientHandshake>>["transport"];

    if (PROVIDER_NAME === "pqc") {
      const result = await performPqcClientHandshake(baseUrl, { clientId: "client-12-mcp-ms" });
      sessionId = result.sessionId;
      transport = result.transport;
    } else {
      const { kid, kdata } = await resolver.resolve(baseUrl, {
        socketPath: process.env.ARQIT_SIDECAR_SOCKET_CLIENT,
      });
      const result = await performClientHandshake(baseUrl, kdata, { clientId: "client-12-mcp-ms", kid });
      sessionId = result.sessionId;
      transport = result.transport;
    }

    const stlsFetch = createStlsFetch(sessionId, transport);
    const mcpClient = new Client({ name: "client-12-mcp-ms", version: "1.0.0" });
    const clientTransport = new StreamableHTTPClientTransport(new URL(`${baseUrl}/mcp-stls`), { fetch: stlsFetch });
    await mcpClient.connect(clientTransport);

    const result = await mcpClient.callTool({
      name: toolName,
      arguments: { message: "hello from the Node client" },
    });
    const text = (result.content as Array<{ text?: string }>)[0]?.text;
    console.log(`  [${label}] OK — ${text}`);
    await mcpClient.close();
    return true;
  } catch (exc) {
    console.log(`  [${label}] FAILED — ${(exc as Error).message}`);
    return false;
  }
}

async function main(): Promise<void> {
  console.log(`\n=== MCP multi-server test (Node) — provider=${PROVIDER_NAME} ===`);
  let nOk = 0;
  for (const [label, baseUrl] of SERVERS) {
    if (await callServer(label, baseUrl)) nOk++;
  }
  console.log(`=== Done — ${nOk}/${SERVERS.length} servers OK ===\n`);

  // arqit_py leaves an open sidecar RPC socket with no close handle —
  // same known characteristic as vault-client.ts; harmless to force
  // for base/pqc too.
  process.exit(0);
}

void main();
