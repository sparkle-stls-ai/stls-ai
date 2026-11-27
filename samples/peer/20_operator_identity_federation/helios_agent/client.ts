/**
 * helios_agent/client.ts — 20_operator_identity_federation
 * ------------------------------------------------------------
 * Helios: the Requestor. There is no `node/stls-a2a` package — only
 * `python/stls-a2a` exists — so this is a minimal, hand-rolled A2A+PQC
 * client, not a general-purpose SDK. Same spirit as
 * samples/peer/18_naas_federation/app/buyer.ts, which already talks to
 * a Python stls_a2a server from Node without one: `stls-core`'s
 * performPqcClientHandshake() does the encrypted-channel handshake,
 * and the A2A JSON-RPC envelope (`{jsonrpc, method, params, id}`) is
 * built by hand — it's plain JSON, not something that needs a library.
 *
 * One call per process (run_demo.py invokes this once per guided
 * step). Prints a human-readable narration, then a final
 * `RESULT_JSON: {...}` line the Python orchestrator parses.
 *
 * Run:
 *   npx tsx --env-file=.env client.ts --credential=valid
 *   npx tsx --env-file=.env client.ts --credential=expired
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { performPqcClientHandshake, StlsEnvelope, type StlsTransport } from "stls-core";
import { createStlsFetch } from "stls-mcp";
import {
  attachIdentityPresentationHeader,
  buildVerifiablePresentation,
  type NetworkAuthorityCredential,
  type OperationalCredential,
} from "stls-identity";

const HERE = dirname(fileURLToPath(import.meta.url));
const SAMPLE_ROOT = join(HERE, "..");

const MERIDIAN_BASE_URL = process.env.MERIDIAN_BASE_URL ?? "http://localhost:8710";
const MERIDIAN_AGENT_PATH = "/agents/meridian/secure-a2a";
const SEGMENT = "MER-SEG-7";

// Matches python/stls-a2a/src/stls_a2a/stls_a2a_adapters_model.py's
// ERR_IDENTITY_CHALLENGE_REQUIRED — there's no Node constant for it
// since there's no node/stls-a2a; the value is part of stls_a2a's own
// wire contract, not implementation detail.
const ERR_IDENTITY_CHALLENGE_REQUIRED = -32005;

interface HeliosIdentity {
  holder_did: string;
  holder_verification_method_id: string;
  private_key_hex: string;
  operational_credential: OperationalCredential;
  operational_credential_expired: OperationalCredential;
  grantor_identity_credential: NetworkAuthorityCredential;
}

function loadIdentity(): HeliosIdentity {
  const raw = readFileSync(join(SAMPLE_ROOT, "helios_identity.json"), "utf-8");
  return JSON.parse(raw) as HeliosIdentity;
}

async function postEncrypted(
  transport: StlsTransport,
  sessionId: string,
  body: Record<string, unknown>,
  extraHeaders: Record<string, string> = {},
): Promise<{ status: number; json: any }> {
  const envelope = transport.encrypt(Buffer.from(JSON.stringify(body), "utf-8")).toDict();
  const response = await fetch(`${MERIDIAN_BASE_URL}${MERIDIAN_AGENT_PATH}`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "X-A2A-Session-ID": sessionId,
      ...extraHeaders,
    },
    body: JSON.stringify(envelope),
  });
  const json = await response.json();
  return { status: response.status, json };
}

async function callReroute(
  credentialChoice: "valid" | "expired",
): Promise<{ outcome: "success" | "rejected"; detail: unknown }> {
  const identity = loadIdentity();
  const operationalCredential =
    credentialChoice === "valid" ? identity.operational_credential : identity.operational_credential_expired;

  console.log(`[helios_agent] opening a PQC-encrypted channel to Meridian (${MERIDIAN_BASE_URL})...`);
  const { sessionId, transport } = await performPqcClientHandshake(MERIDIAN_BASE_URL, {
    clientId: "helios-agent",
  });
  console.log(`[helios_agent] channel established, session_id=${sessionId}`);

  const requestBody = {
    jsonrpc: "2.0",
    method: "messages/send",
    params: {
      message: {
        parts: [
          { type: "text", text: JSON.stringify({ segment: SEGMENT, reason: "incident on MER-SEG-7" }) },
        ],
      },
    },
    id: 1,
  };

  console.log(`[helios_agent] asking Meridian to reroute traffic on ${SEGMENT}...`);
  let { status, json } = await postEncrypted(transport, sessionId, requestBody);

  if (status === 401 && json?.error?.code === ERR_IDENTITY_CHALLENGE_REQUIRED) {
    const challenge = json.error.challenge as string;
    console.log(`[helios_agent] Meridian asks for proof of identity (challenge issued) — presenting credentials...`);

    const vp = buildVerifiablePresentation({
      holderDid: identity.holder_did,
      holderSigningKey: Buffer.from(identity.private_key_hex, "hex"),
      holderVerificationMethodId: identity.holder_verification_method_id,
      credentials: [operationalCredential, identity.grantor_identity_credential],
      challenge,
    });
    const presentationHeader = attachIdentityPresentationHeader(vp);

    ({ status, json } = await postEncrypted(transport, sessionId, requestBody, presentationHeader));
  }

  if (status !== 200) {
    const message = json?.error?.message ?? JSON.stringify(json);
    console.log(`[helios_agent] REJECTED — ${message}`);
    return { outcome: "rejected", detail: message };
  }

  const plaintext = transport.decrypt(StlsEnvelope.fromDict(json));
  const decrypted = JSON.parse(plaintext.toString("utf-8"));
  // The plain function-agent adapter wraps our {parts:[...]} into a full
  // A2A Task shape (history/artifacts/status) — the text we returned is
  // under the first artifact's first part, not directly on `result`.
  const resultText = decrypted.result?.artifacts?.[0]?.parts?.[0]?.text ?? "{}";
  const toolResult = JSON.parse(resultText);
  console.log(`[helios_agent] ACCEPTED — Meridian applied the reroute: ${resultText}`);
  return { outcome: "success", detail: toolResult };
}

async function logIncident(outcome: string, detail: unknown): Promise<string> {
  // Same-company, same-machine call — plain /mcp would be equally
  // legitimate here. Uses the STLS-protected path anyway, to exercise
  // pqc on this leg too, mirroring meridian_mcp_server's own choice.
  const heliosMcpBaseUrl = process.env.HELIOS_MCP_URL ?? "http://localhost:8721";
  const { Client } = await import("@modelcontextprotocol/sdk/client/index.js");
  const { StreamableHTTPClientTransport } = await import("@modelcontextprotocol/sdk/client/streamableHttp.js");

  const { sessionId, transport } = await performPqcClientHandshake(heliosMcpBaseUrl, {
    clientId: "helios-agent",
  });
  const stlsFetch = createStlsFetch(sessionId, transport);

  const client = new Client({ name: "helios-agent", version: "1.0.0" });
  await client.connect(
    new StreamableHTTPClientTransport(new URL(`${heliosMcpBaseUrl}/mcp-stls`), { fetch: stlsFetch }),
  );
  const result = await client.callTool({
    name: "log_incident",
    arguments: { outcome, detail: JSON.stringify(detail) },
  });
  const text = (result.content as Array<{ text?: string }>)[0]?.text ?? "{}";
  await client.close();
  return (JSON.parse(text) as { ticketId: string }).ticketId;
}

async function main(): Promise<void> {
  const credentialArg = process.argv.find((a) => a.startsWith("--credential="));
  const credentialChoice = (credentialArg?.split("=")[1] ?? "valid") as "valid" | "expired";

  const { outcome, detail } = await callReroute(credentialChoice);
  const ticketId = await logIncident(outcome, detail);
  console.log(`[helios_agent] logged as incident ticket ${ticketId}`);

  console.log(`RESULT_JSON: ${JSON.stringify({ outcome, detail, ticketId })}`);
}

void main();
