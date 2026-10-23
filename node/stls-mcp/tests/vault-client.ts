/**
 * vault-client.ts
 * ----------------
 * Discovery client for the "Deep Sky Vault" example server
 * (vault-server.ts). Performs the STLS handshake, then a full MCP
 * session through the encrypted transport (/mcp-stls): discovery of
 * tools, resources and prompts, and exercising each at least once.
 *
 * Also meant as reading material: shows the full
 * "handshake -> createStlsFetch -> StreamableHTTPClientTransport -> Client"
 * pattern any stls-mcp-based Node client application should follow.
 *
 * Usage:
 *   npx tsx tests/vault-server.ts     # terminal 1
 *   npx tsx tests/vault-client.ts     # terminal 2
 */
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { KeySessionProviderFactory, KeySessionProviderType } from "stls-core";
import { createStlsFetch, performClientHandshake, performPqcClientHandshake } from "../src/index.js";

function section(title: string): void {
  console.log();
  console.log(`── ${title} ` + "─".repeat(Math.max(0, 60 - title.length)));
}

async function main(): Promise<void> {
  const baseUrl = process.env.VAULT_CLIENT_TARGET_URL ?? "http://127.0.0.1:8765";
  const stlsPath = process.env.MCP_STLS_PATH ?? "/mcp-stls";
  const providerName = (process.env.KEY_PROVIDER_NAME ?? "base").trim().toLowerCase();

  console.log("Deep Sky Vault — discovery client (Node)");
  console.log(`Target: ${baseUrl}`);

  section("Handshake");
  let sessionId: string;
  let transport: Awaited<ReturnType<typeof performClientHandshake>>["transport"];

  if (providerName === "pqc") {
    console.log("Provider: pqc — X25519 + ML-KEM-768, no pre-shared key");
    const result = await performPqcClientHandshake(baseUrl, { clientId: "vault-demo-client-node" });
    sessionId = result.sessionId;
    transport = result.transport;
  } else {
    const clientProvider = await KeySessionProviderFactory.getSessionProvider({
      sessionType: KeySessionProviderType.CLIENT,
      name: providerName,
    });
    const kid = await clientProvider.getSessionKeyIdentifier();
    const sessionKey = await clientProvider.getSessionKeyData(kid);
    console.log(`Provider: ${providerName} — kid=${kid}`);
    const result = await performClientHandshake(baseUrl, sessionKey, { clientId: "vault-demo-client-node", kid });
    sessionId = result.sessionId;
    transport = result.transport;
  }
  console.log(`session_id = ${sessionId}`);
  console.log("Proof of key possession verified — channel is ready.");

  const stlsFetch = createStlsFetch(sessionId, transport, { stlsPath });
  const client = new Client({ name: "vault-demo-client-node", version: "1.0.0" });
  const clientTransport = new StreamableHTTPClientTransport(new URL(`${baseUrl}${stlsPath}`), { fetch: stlsFetch });

  await client.connect(clientTransport);

  section("initialize");
  const serverVersion = client.getServerVersion();
  console.log(`server   = ${serverVersion?.name} v${serverVersion?.version}`);

  section("Discovery: tools");
  const { tools } = await client.listTools();
  for (const t of tools) console.log(`  - ${t.name}: ${t.description}`);

  section("Discovery: resources");
  const { resources } = await client.listResources();
  for (const r of resources) console.log(`  - ${r.uri}: ${r.description ?? r.name}`);
  const { resourceTemplates } = await client.listResourceTemplates();
  for (const rt of resourceTemplates) console.log(`  - ${rt.uriTemplate}: ${rt.description ?? rt.name}`);

  section("Discovery: prompts");
  const { prompts } = await client.listPrompts();
  for (const p of prompts) {
    const args = (p.arguments ?? []).map((a) => a.name).join(", ");
    console.log(`  - ${p.name}(${args}): ${p.description}`);
  }

  section("Tool: list_stations");
  const listResult = await client.callTool({ name: "list_stations", arguments: {} });
  console.log((listResult.content as Array<{ text?: string }>)[0]?.text);

  section("Tool: get_station_status('halcyon')");
  const statusResult = await client.callTool({ name: "get_station_status", arguments: { station_id: "halcyon" } });
  console.log((statusResult.content as Array<{ text?: string }>)[0]?.text);

  section("Tool: decode_transmission('kepler-9')");
  const decodeResult = await client.callTool({ name: "decode_transmission", arguments: { station_id: "kepler-9" } });
  console.log(`Decoded message: ${(decodeResult.content as Array<{ text?: string }>)[0]?.text}`);

  section("Tool: calculate_orbit(altitude_km=408)");
  const orbitResult = await client.callTool({ name: "calculate_orbit", arguments: { altitude_km: 408 } });
  console.log((orbitResult.content as Array<{ text?: string }>)[0]?.text);

  section("Tool: signal_strength_check('vega-relay')");
  const signalResult = await client.callTool({ name: "signal_strength_check", arguments: { station_id: "vega-relay" } });
  console.log((signalResult.content as Array<{ text?: string }>)[0]?.text);

  section("Resource: vault://manifest");
  const manifest = await client.readResource({ uri: "vault://manifest" });
  console.log((manifest.contents[0] as { text?: string }).text);

  section("Resource: vault://station/halcyon (template URI)");
  const stationDetail = await client.readResource({ uri: "vault://station/halcyon" });
  console.log((stationDetail.contents[0] as { text?: string }).text);

  section("Prompt: mission_briefing(station_id='halcyon', urgency='urgent')");
  const briefing = await client.getPrompt({ name: "mission_briefing", arguments: { station_id: "halcyon", urgency: "urgent" } });
  const briefingText = (briefing.messages[0]?.content as { text?: string } | undefined)?.text;
  console.log(`[${briefing.messages[0]?.role}] ${briefingText}`);

  section("Prompt: fleet_summary(focus='crew morale')");
  const summary = await client.getPrompt({ name: "fleet_summary", arguments: { focus: "crew morale" } });
  const summaryText = (summary.messages[0]?.content as { text?: string } | undefined)?.text;
  console.log(`[${summary.messages[0]?.role}] ${summaryText}`);

  section("Done");
  console.log("All tools, resources, and prompts exercised successfully over /mcp-stls.");

  await client.close();
}

await main();

// With KEY_PROVIDER_NAME=arqit_py, KeySessionProviderFactory.getSessionProvider()
// returns only the KeySessionProvider, not a handle to the underlying
// KeyProviderClientArqitSidecar — so there's no way to close its RPC
// connection to the sidecar from here, and the open socket keeps the
// process alive indefinitely even though all the real work above has
// already completed successfully. base/pqc have no such open handle and
// exit on their own; force it here too, same as a one-shot CLI script.
process.exit(0);
