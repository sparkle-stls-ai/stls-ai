/**
 * client_multi_session.ts — N concurrent MCP sessions, 1 device vs 1 server (Node)
 * -------------------------------------------------------------------------------------
 * Node analogue of samples/client/11_client_mcp_ms/client_multi_session.py
 * and samples/client/10_client_ms/client_multi_session.py. Opens
 * GROUP_SIZE x NUM_GROUPS independent MCP sessions toward ONE server,
 * concurrently (Promise.all, not sequential), asserting each gets a
 * distinct server-issued session_id — the MCP protocol always does an
 * explicit handshake round trip per session (unlike stls-a2a's lazy
 * base/arqit_py session_id scheme), so unlike the A2A version, `base`
 * is NOT expected to collide here: the server mints a fresh session_id
 * on every handshake regardless of the underlying key being the same
 * fixed one.
 *
 * Works, unmodified, with KEY_PROVIDER_NAME=base|pqc|arqit_py.
 *
 * Run (after starting ONE of the two servers):
 *   npx tsx --env-file=.env client_multi_session.ts
 */
import assert from "node:assert/strict";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { KeySessionProviderFactory, KeySessionProviderType } from "stls-core";
import { createStlsFetch, performClientHandshake, performPqcClientHandshake } from "stls-mcp";

const PROVIDER_NAME = (process.env.KEY_PROVIDER_NAME ?? "pqc").trim().toLowerCase();
const BASE_URL = process.env.BASE_URL ?? "http://localhost:8211";
const TOOL_NAME = process.env.TOOL_NAME ?? "ping_server_mcp_ms_1_node";

const GROUP_SIZE = Number(process.env.GROUP_SIZE ?? "3");
const NUM_GROUPS = Number(process.env.NUM_GROUPS ?? "2");

interface SessionResult {
  sessionNum: number;
  sessionId: string | null;
  text: string;
  elapsedMs: number;
  error: Error | null;
}

async function openSession(
  sessionNum: number,
): Promise<{ sessionId: string; transport: Awaited<ReturnType<typeof performClientHandshake>>["transport"] }> {
  if (PROVIDER_NAME === "pqc") {
    return performPqcClientHandshake(BASE_URL, { clientId: `pqc-mcp-multi-session-node-${sessionNum}` });
  }
  const provider = await KeySessionProviderFactory.getSessionProvider({
    sessionType: KeySessionProviderType.CLIENT,
    name: PROVIDER_NAME,
  });
  const kid = await provider.getSessionKeyIdentifier();
  const kdata = await provider.getSessionKeyData(kid);
  return performClientHandshake(BASE_URL, kdata, {
    clientId: `${PROVIDER_NAME}-mcp-multi-session-node-${sessionNum}`,
    kid,
  });
}

async function runSession(sessionNum: number, message: string): Promise<SessionResult> {
  const t0 = Date.now();
  try {
    const { sessionId, transport } = await openSession(sessionNum);
    const stlsFetch = createStlsFetch(sessionId, transport);
    const mcpClient = new Client({ name: `multi-session-node-${sessionNum}`, version: "1.0.0" });
    const clientTransport = new StreamableHTTPClientTransport(new URL(`${BASE_URL}/mcp-stls`), { fetch: stlsFetch });
    await mcpClient.connect(clientTransport);
    const result = await mcpClient.callTool({ name: TOOL_NAME, arguments: { message } });
    const text = (result.content as Array<{ text?: string }>)[0]?.text ?? "";
    await mcpClient.close();
    return { sessionNum, sessionId, text, elapsedMs: Date.now() - t0, error: null };
  } catch (exc) {
    return { sessionNum, sessionId: null, text: "", elapsedMs: Date.now() - t0, error: exc as Error };
  }
}

async function main(): Promise<void> {
  const total = GROUP_SIZE * NUM_GROUPS;
  console.log(`\n=== client_multi_session (Node) — provider=${PROVIDER_NAME} ===`);
  console.log(`  BASE_URL = ${BASE_URL}, tool = ${TOOL_NAME}`);
  console.log(`  ${GROUP_SIZE} x ${NUM_GROUPS} = ${total} sessions\n`);

  const allResults: SessionResult[] = [];
  let counter = 0;

  for (let round = 1; round <= NUM_GROUPS; round++) {
    console.log(`-- Round ${round}/${NUM_GROUPS} (sessions ${counter + 1}-${counter + GROUP_SIZE})`);
    const group = Array.from({ length: GROUP_SIZE }, (_, i) => {
      const n = counter + i + 1;
      return runSession(n, `ping session ${String(n).padStart(2, "0")}`);
    });
    const results = await Promise.all(group);
    for (const r of results) {
      if (r.error) {
        console.log(`  FAIL Session-${r.sessionNum}: ${r.error.message}`);
      } else {
        console.log(`  OK   Session-${r.sessionNum}  session_id=${r.sessionId}  response=${r.text.slice(0, 60)}`);
      }
    }
    allResults.push(...results);
    counter += GROUP_SIZE;
  }

  console.log("\n=== Summary ===");
  const errors = allResults.filter((r) => r.error);
  const sessionIds = allResults.filter((r) => r.sessionId).map((r) => r.sessionId as string);
  const uniqueIds = new Set(sessionIds);
  console.log(`  Total sessions     : ${allResults.length}`);
  console.log(`  Errors             : ${errors.length}`);
  console.log(`  Unique session_ids : ${uniqueIds.size}/${sessionIds.length}`);

  assert.equal(errors.length, 0, "some sessions failed — see FAIL lines above");
  assert.equal(uniqueIds.size, sessionIds.length, "duplicate session_id across sessions — server-side bug");

  console.log("\n  RESULT: PASS\n");
  process.exit(0);
}

void main();
