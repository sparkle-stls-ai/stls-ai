/**
 * plain-endpoint.smoke.ts
 * ------------------------
 * Verifies that MCP_PLAIN_PATH ("/mcp") is indistinguishable from a
 * standard, "pure" MCP server — the central design requirement of
 * stls-mcp, ported from python/stls-mcp's README claim (validated there
 * against a real Claude Desktop connection).
 *
 * Deliberately uses the OFFICIAL, completely unmodified
 * StreamableHTTPClientTransport + Client from @modelcontextprotocol/sdk,
 * with NO stls-mcp code involved anywhere on the client side — no
 * handshake, no custom fetch, nothing. If createSecureMcpApp()'s plain
 * path wiring were subtly broken (wrong headers, wrong routing, auth
 * leaking onto the wrong path, etc.), this is the test that would catch
 * it, before any STLS complexity enters the picture.
 *
 * Requires tests/vault-server.ts already running:
 *   npx tsx tests/vault-server.ts            # terminal 1
 *   npx tsx tests/interop/plain-endpoint.smoke.ts   # terminal 2
 */
import assert from "node:assert/strict";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const baseUrl = process.env.VAULT_SERVER_URL ?? "http://127.0.0.1:8765";
const plainPath = process.env.MCP_PLAIN_PATH ?? "/mcp";

const client = new Client({ name: "plain-endpoint-smoke-client", version: "1.0.0" });
const transport = new StreamableHTTPClientTransport(new URL(`${baseUrl}${plainPath}`));

await client.connect(transport);
console.log("OK: connected to the plain MCP endpoint with the official, unmodified client transport");

const serverVersion = client.getServerVersion();
assert.ok(serverVersion, "server did not report its version during initialize()");
console.log(`OK: initialize() succeeded — server = ${serverVersion?.name} v${serverVersion?.version}`);

// ── Discovery ──────────────────────────────────────────────────────────
const { tools } = await client.listTools();
const toolNames = tools.map((t) => t.name).sort();
assert.deepEqual(
  toolNames,
  ["calculate_orbit", "decode_transmission", "get_station_status", "list_stations", "signal_strength_check"].sort(),
  "unexpected tool set on the plain endpoint",
);
console.log(`OK: discovered ${tools.length} tools: ${toolNames.join(", ")}`);

const { resources } = await client.listResources();
assert.ok(resources.some((r) => r.uri === "vault://manifest"), "vault://manifest resource not found");
console.log(`OK: discovered ${resources.length} resource(s)`);

const { prompts } = await client.listPrompts();
const promptNames = prompts.map((p) => p.name).sort();
assert.deepEqual(promptNames, ["fleet_summary", "mission_briefing"].sort(), "unexpected prompt set");
console.log(`OK: discovered ${prompts.length} prompt(s): ${promptNames.join(", ")}`);

// ── Exercise at least one tool, one resource, one prompt ────────────────
const listResult = await client.callTool({ name: "list_stations", arguments: {} });
const listText = (listResult.content as Array<{ type: string; text?: string }>)[0]?.text ?? "";
assert.ok(listText.includes("kepler-9"), "list_stations output missing expected station");
console.log("OK: list_stations tool call succeeded");

const orbitResult = await client.callTool({ name: "calculate_orbit", arguments: { altitude_km: 408 } });
const orbitText = (orbitResult.content as Array<{ type: string; text?: string }>)[0]?.text ?? "";
assert.ok(orbitText.includes("orbital period"), "calculate_orbit output missing expected phrase");
console.log("OK: calculate_orbit tool call succeeded");

const manifest = await client.readResource({ uri: "vault://manifest" });
const manifestText = (manifest.contents[0] as { text?: string }).text ?? "";
const manifestJson = JSON.parse(manifestText) as { station_count: number };
assert.equal(manifestJson.station_count, 3, "vault://manifest reported an unexpected station_count");
console.log("OK: vault://manifest resource read succeeded");

const prompt = await client.getPrompt({ name: "fleet_summary", arguments: { focus: "crew morale" } });
assert.ok(prompt.messages.length > 0, "fleet_summary prompt returned no messages");
console.log("OK: fleet_summary prompt retrieval succeeded");

await client.close();
console.log(
  "ALL PLAIN-ENDPOINT CHECKS PASSED — /mcp behaves exactly like a standard MCP server, " +
    "with zero stls-mcp awareness on the client side",
);
