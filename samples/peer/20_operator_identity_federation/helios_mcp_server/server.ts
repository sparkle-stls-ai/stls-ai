/**
 * helios_mcp_server/server.ts — 20_operator_identity_federation
 * ----------------------------------------------------------------
 * Helios's own local tool server: logs the outcome of each call to
 * Meridian into Helios's (fake) incident ticketing system. Local,
 * same-operator call from helios_agent — the plain endpoint would be
 * an equally legitimate choice here, but helios_agent calls the
 * STLS-protected path instead, mirroring meridian_mcp_server's own
 * choice to exercise pqc end to end on both legs of this demo.
 *
 * Run:
 *   npx tsx --env-file=.env server.ts
 */
import { createServer } from "node:http";
import { randomUUID } from "node:crypto";
import { z } from "zod";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";

import { KeySessionProviderFactory, KeySessionProviderType } from "stls-core";
import { createSecureMcpApp, toNodeRequestListener } from "stls-mcp";

const PORT = Number(process.env.PORT ?? "8721");
const PLAIN_PATH = process.env.MCP_PLAIN_PATH ?? "/mcp";
const STLS_PATH = process.env.MCP_STLS_PATH ?? "/mcp-stls";

function getServer(): McpServer {
  const server = new McpServer({ name: "helios-incident-log", version: "1.0.0" });

  server.registerTool(
    "log_incident",
    {
      description: "Logs the outcome of a cross-operator call into Helios's incident ticketing system.",
      inputSchema: { outcome: z.string(), detail: z.string() },
    },
    async ({ outcome, detail }) => {
      const ticketId = `HEL-${randomUUID().slice(0, 8).toUpperCase()}`;
      console.log(`[helios_mcp_server] ticket ${ticketId} — ${outcome}: ${detail}`);
      return { content: [{ type: "text", text: JSON.stringify({ ticketId, outcome }) }] };
    },
  );

  return server;
}

async function main(): Promise<void> {
  const provider = await KeySessionProviderFactory.getSessionProvider({
    sessionType: KeySessionProviderType.SERVER,
    name: process.env.KEY_PROVIDER_NAME ?? "pqc",
    port: PORT,
  });

  const app = await createSecureMcpApp(getServer, provider, { plainPath: PLAIN_PATH, stlsPath: STLS_PATH });
  const httpServer = createServer(toNodeRequestListener(app));
  httpServer.listen(PORT, "0.0.0.0", () => {
    console.log(`helios_mcp_server started on port ${PORT}`);
    console.log(`  plain -> http://localhost:${PORT}${PLAIN_PATH}`);
  });
}

void main();
