/**
 * vault-server.ts
 * ----------------
 * Example MCP server for stls-mcp: "Deep Sky Vault", the same themed
 * archive of quantum-safe space stations as python/stls-mcp/tests/vault_server.py
 * (same tool/resource/prompt names and behavior, so a client speaking to
 * either implementation sees the same surface — useful for cross-language
 * interop tests).
 *
 * Exposed both in plain on MCP_PLAIN_PATH (default "/mcp", standard MCP,
 * zero modifications) and STLS-encrypted on MCP_STLS_PATH (default
 * "/mcp-stls").
 *
 * Optional API key protection: set AUTH_ENABLED=true and AUTH_API_KEYS
 * (comma-separated) in the environment to require an API key on BOTH
 * paths via a static AuthConfig — a simplified version of
 * python/stls-mcp's full ApiKeyManager.from_env() cascade (Redis/File/
 * Postgres), enough to validate createSecureMcpApp()'s auth wiring without
 * needing that external infrastructure for this example.
 *
 * Usage:
 *   npx tsx tests/vault-server.ts
 *   # then, in another terminal:
 *   npx tsx tests/vault-client.ts
 */
import { createServer } from "node:http";
import { z } from "zod";
import { McpServer, ResourceTemplate } from "@modelcontextprotocol/sdk/server/mcp.js";

import { AuthConfig, KeySessionProviderFactory, KeySessionProviderType } from "stls-core";
import { createSecureMcpApp, toNodeRequestListener } from "../src/index.js";

// ---------------------------------------------------------------------------
// "Database" — in-memory fleet, nothing persistent, it's a demo
// ---------------------------------------------------------------------------

interface Station {
  id: string;
  name: string;
  sector: string;
  crew: number;
  orbitAltitudeKm: number;
  status: string;
  lastTransmission: string;
}

const FLEET: Record<string, Station> = {
  "kepler-9": {
    id: "kepler-9",
    name: "Kepler Outpost",
    sector: "Sigma Draconis",
    crew: 12,
    orbitAltitudeKm: 408.0,
    status: "nominal",
    lastTransmission: "All quantum links green. Crew morale high.",
  },
  "vega-relay": {
    id: "vega-relay",
    name: "Vega Relay Station",
    sector: "Lyra Expanse",
    crew: 4,
    orbitAltitudeKm: 850.0,
    status: "nominal",
    lastTransmission: "Relay buffers nominal, no anomalies detected.",
  },
  halcyon: {
    id: "halcyon",
    name: "Halcyon Deep-Field Array",
    sector: "Orion Spur",
    crew: 7,
    orbitAltitudeKm: 1200.0,
    status: "degraded",
    lastTransmission: "Solar flare interference on array 3, rerouting.",
  },
};

// Toy substitution cipher for the decode_transmission demo tool only — no
// relation at all to the real STLS crypto (AES-256-GCM), just a text puzzle.
const CIPHER_SHIFT = 7;

function caesarEncode(text: string, shift = CIPHER_SHIFT): string {
  return text.replace(/[a-zA-Z]/g, (ch) => {
    const base = ch <= "Z" ? 65 : 97;
    return String.fromCharCode(((ch.charCodeAt(0) - base + shift) % 26 + 26) % 26 + base);
  });
}

function caesarDecode(text: string, shift = CIPHER_SHIFT): string {
  return caesarEncode(text, 26 - (shift % 26));
}

const INCOMING_TRANSMISSIONS: Record<string, string> = Object.fromEntries(
  Object.values(FLEET).map((s) => [s.id, caesarEncode(s.lastTransmission)]),
);

// ---------------------------------------------------------------------------
// MCP server — tools / resources / prompts
// ---------------------------------------------------------------------------

function getServer(): McpServer {
  const server = new McpServer({
    name: "Deep Sky Vault",
    version: "1.0.0",
  });

  // ── Tools ────────────────────────────────────────────────────────────

  server.registerTool(
    "list_stations",
    { description: "List all stations currently in the fleet, with their status." },
    async () => {
      const lines = Object.values(FLEET).map((s) => `${s.id}: ${s.name} (${s.sector}) — status: ${s.status}`);
      return { content: [{ type: "text", text: lines.join("\n") }] };
    },
  );

  server.registerTool(
    "get_station_status",
    {
      description: "Get detailed status for a single station by its id (e.g. 'kepler-9').",
      inputSchema: { station_id: z.string() },
    },
    async ({ station_id }) => {
      const station = FLEET[station_id];
      if (!station) {
        return {
          content: [{ type: "text", text: `Unknown station '${station_id}'. Known stations: ${Object.keys(FLEET).join(", ")}` }],
        };
      }
      const text =
        `${station.name} (${station.id})\n` +
        `Sector: ${station.sector}\n` +
        `Crew: ${station.crew}\n` +
        `Orbit altitude: ${station.orbitAltitudeKm} km\n` +
        `Status: ${station.status}`;
      return { content: [{ type: "text", text }] };
    },
  );

  server.registerTool(
    "decode_transmission",
    {
      description:
        "Decode the latest incoming transmission from a station. Transmissions arrive shifted " +
        "through a simple substitution cipher (a toy puzzle for this demo — unrelated to STLS's " +
        "real AES-256-GCM transport encryption). Returns the decoded plaintext message.",
      inputSchema: { station_id: z.string() },
    },
    async ({ station_id }) => {
      const ciphertext = INCOMING_TRANSMISSIONS[station_id];
      if (ciphertext === undefined) {
        return {
          content: [
            { type: "text", text: `No transmission on file for '${station_id}'. Known stations: ${Object.keys(INCOMING_TRANSMISSIONS).join(", ")}` },
          ],
        };
      }
      return { content: [{ type: "text", text: caesarDecode(ciphertext) }] };
    },
  );

  server.registerTool(
    "calculate_orbit",
    {
      description:
        "Estimate the orbital period (in minutes) for a circular orbit at the given altitude above " +
        "Earth's surface, using simplified Keplerian mechanics (mu_earth = 398600.4418 km^3/s^2, " +
        "R_earth = 6371 km).",
      inputSchema: { altitude_km: z.number() },
    },
    async ({ altitude_km }) => {
      const muEarth = 398600.4418;
      const rEarth = 6371.0;
      const radius = rEarth + altitude_km;
      const periodSeconds = 2 * Math.PI * Math.sqrt(radius ** 3 / muEarth);
      const periodMinutes = periodSeconds / 60.0;
      const text =
        `At ${altitude_km.toFixed(1)} km altitude (orbital radius ${radius.toFixed(1)} km), ` +
        `the estimated orbital period is ${periodMinutes.toFixed(2)} minutes (${(periodMinutes / 60).toFixed(2)} hours).`;
      return { content: [{ type: "text", text }] };
    },
  );

  server.registerTool(
    "signal_strength_check",
    {
      description:
        "Simulate a quantum-safe link signal strength check for a station. Returns a pseudo-random " +
        "but deterministic-per-call strength reading, purely for demo/testing purposes (no real " +
        "telemetry involved).",
      inputSchema: { station_id: z.string() },
    },
    async ({ station_id }) => {
      if (!(station_id in FLEET)) {
        return {
          content: [{ type: "text", text: `Unknown station '${station_id}'. Known stations: ${Object.keys(FLEET).join(", ")}` }],
        };
      }
      const strength = 72 + Math.floor(Math.random() * 28);
      const quality = strength >= 90 ? "excellent" : strength >= 80 ? "good" : "fair";
      return { content: [{ type: "text", text: `${station_id}: quantum link signal strength ${strength}% (${quality})` }] };
    },
  );

  // ── Resources ────────────────────────────────────────────────────────

  server.registerResource(
    "vault-manifest",
    "vault://manifest",
    { mimeType: "application/json", description: "Machine-readable manifest of the entire fleet (JSON)." },
    async (uri) => ({
      contents: [
        {
          uri: uri.toString(),
          mimeType: "application/json",
          text: JSON.stringify(
            {
              fleet: "Deep Sky Vault",
              station_count: Object.keys(FLEET).length,
              stations: Object.values(FLEET).map((s) => ({ id: s.id, name: s.name, sector: s.sector, status: s.status })),
            },
            null,
            2,
          ),
        },
      ],
    }),
  );

  server.registerResource(
    "vault-station-detail",
    new ResourceTemplate("vault://station/{station_id}", { list: undefined }),
    { mimeType: "application/json", description: "Detailed JSON record for a single station." },
    async (uri, variables) => {
      const stationId = String(variables.station_id);
      const station = FLEET[stationId];
      const body = station
        ? {
            id: station.id,
            name: station.name,
            sector: station.sector,
            crew: station.crew,
            orbit_altitude_km: station.orbitAltitudeKm,
            status: station.status,
            last_transmission: station.lastTransmission,
          }
        : { error: `Unknown station '${stationId}'` };
      return { contents: [{ uri: uri.toString(), mimeType: "application/json", text: JSON.stringify(body, null, 2) }] };
    },
  );

  // ── Prompts ──────────────────────────────────────────────────────────

  server.registerPrompt(
    "mission_briefing",
    {
      description: "Generate a mission briefing prompt for a given station and urgency level.",
      argsSchema: { station_id: z.string(), urgency: z.string().optional() },
    },
    async ({ station_id, urgency }) => {
      const station = FLEET[station_id];
      const stationName = station ? station.name : station_id;
      const level = urgency ?? "routine";
      return {
        messages: [
          {
            role: "user",
            content: {
              type: "text",
              text:
                `You are a fleet operations officer for Deep Sky Vault. Prepare a ${level} mission ` +
                `briefing for ${stationName}. Cover: current status, any anomalies in the latest ` +
                `transmission, and one recommended action for the crew.`,
            },
          },
        ],
      };
    },
  );

  server.registerPrompt(
    "fleet_summary",
    {
      description: "Generate a prompt asking for a fleet-wide summary, focused on a given aspect.",
      argsSchema: { focus: z.string().optional() },
    },
    async ({ focus }) => {
      const aspect = focus ?? "status";
      return {
        messages: [
          {
            role: "user",
            content: {
              type: "text",
              text:
                `Summarize the current state of the Deep Sky Vault fleet, focusing on ${aspect}. ` +
                `Use list_stations and get_station_status to gather data before writing the summary.`,
            },
          },
        ],
      };
    },
  );

  return server;
}

// ---------------------------------------------------------------------------
// Wiring + startup
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  const plainPath = process.env.MCP_PLAIN_PATH ?? "/mcp";
  const stlsPath = process.env.MCP_STLS_PATH ?? "/mcp-stls";
  const port = Number(process.env.MCP_SERVER_PORT ?? "8765");
  const host = process.env.MCP_SERVER_HOST ?? "127.0.0.1";

  const provider = await KeySessionProviderFactory.getSessionProvider({
    sessionType: KeySessionProviderType.SERVER,
    name: process.env.KEY_PROVIDER_NAME ?? "base",
  });

  let auth: AuthConfig | undefined;
  if ((process.env.AUTH_ENABLED ?? "").toLowerCase() === "true") {
    const apiKeys = new Set((process.env.AUTH_API_KEYS ?? "").split(",").map((k) => k.trim()).filter(Boolean));
    auth = new AuthConfig({ apiKeys });
    console.log(`auth      -> enabled (static AuthConfig, ${apiKeys.size} key(s))`);
  } else {
    console.log("auth      -> disabled (AUTH_ENABLED not set) — endpoints open");
  }

  const app = await createSecureMcpApp(getServer, provider, { plainPath, stlsPath, auth });

  const httpServer = createServer(toNodeRequestListener(app));
  httpServer.listen(port, host, () => {
    console.log("Deep Sky Vault started:");
    console.log(`  plain -> http://${host}:${port}${plainPath}`);
    console.log(`  stls  -> http://${host}:${port}${stlsPath}`);
    console.log(`  handshake -> http://${host}:${port}/stls/handshake`);
  });
}

void main();
