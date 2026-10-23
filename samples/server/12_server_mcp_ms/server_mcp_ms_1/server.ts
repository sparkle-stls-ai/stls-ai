/**
 * server.ts — server_mcp_ms_1 (Node)
 * --------------------------------------
 * Node analogue of samples/server/11_server_mcp_ms/server_mcp_ms_1
 * (Python) — one of the two MCP servers used to test "1 client -> N
 * MCP servers" from samples/client/12_client_mcp_ms.
 *
 * Works, unmodified, with KEY_PROVIDER_NAME=base|pqc|arqit_py.
 *
 * Run:
 *   npx tsx --env-file=.env server.ts
 *
 * Relative Arqit config paths (DEVICE_METADATA_FILEPATH, etc.) are
 * resolved by the Python sidecar process against ITS OWN cwd, not this
 * script's — if you use KEY_PROVIDER_NAME=arqit_py, launch the sidecar
 * from this directory (see the README for the exact command).
 */
import { createServer } from "node:http";
import { z } from "zod";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";

import { KeySessionProviderFactory, KeySessionProviderType } from "stls-core";
import { createSecureMcpApp, toNodeRequestListener } from "stls-mcp";

const SERVER_LABEL = process.env.SERVER_LABEL ?? "server-mcp-ms-1-node";
const PORT = Number(process.env.PORT ?? "8211");
const PLAIN_PATH = process.env.MCP_PLAIN_PATH ?? "/mcp";
const STLS_PATH = process.env.MCP_STLS_PATH ?? "/mcp-stls";
const TOOL_NAME = `ping_${SERVER_LABEL.replace(/-/g, "_")}`;

function getServer(): McpServer {
  const server = new McpServer({ name: SERVER_LABEL, version: "1.0.0" });

  server.registerTool(
    TOOL_NAME,
    {
      description: "Returns an echo tagged with the label of the server that answered.",
      inputSchema: { message: z.string() },
    },
    async ({ message }) => ({
      content: [{ type: "text", text: JSON.stringify({ server: SERVER_LABEL, echo: message }) }],
    }),
  );

  return server;
}

async function main(): Promise<void> {
  const providerName = process.env.KEY_PROVIDER_NAME ?? "pqc";

  const provider = await KeySessionProviderFactory.getSessionProvider({
    sessionType: KeySessionProviderType.SERVER,
    name: providerName,
    port: PORT,
  });

  const app = await createSecureMcpApp(getServer, provider, { plainPath: PLAIN_PATH, stlsPath: STLS_PATH });
  const httpServer = createServer(toNodeRequestListener(app));
  httpServer.listen(PORT, "0.0.0.0", () => {
    console.log(`${SERVER_LABEL} started on port ${PORT} (KEY_PROVIDER_NAME=${providerName})`);
    console.log(`  plain -> http://localhost:${PORT}${PLAIN_PATH}`);
    console.log(`  stls  -> http://localhost:${PORT}${STLS_PATH}`);
    console.log(`  tool  -> ${TOOL_NAME}`);
  });
}

void main();
