/**
 * client.ts — 13_client_core
 * ----------------------------
 * "Node A" of the 2-peer-node core-only scenario — see
 * samples/server/13_server_core/server.ts for the full rationale
 * (direction labeling, DIDComm-shaped messages, no stls-a2a/stls-mcp).
 *
 * Flow: resolves/handshakes a session via getTransport() with
 * direction="c2s" (the server uses "s2c" — opposite ends of the same
 * session, independent key material each way), sends one DIDComm-shaped
 * ping over POST /ping, and decrypts the pong that comes back on the
 * OTHER direction's key.
 *
 * Run: npx tsx --env-file=.env client.ts
 * KEY_PROVIDER_NAME=base|pqc|arqit_py — same switch as every other
 * sample. For arqit_py, launch the Python sidecar first (see the
 * README) and point ARQIT_PY_CLIENT_CONFIG/ARQIT_SIDECAR_SOCKET_CLIENT/
 * ARQIT_SIDECAR_TOKEN_CLIENT at it.
 */
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";

import { HEADER_SESSION_ID, StlsEnvelope, getTransport } from "stls-core";

const BASE_URL = process.env.BASE_URL ?? "http://localhost:8301";
const CLIENT_DID = "did:example:client-core";
const SERVER_DID = "did:example:server-core";
const providerName = (process.env.KEY_PROVIDER_NAME ?? "base").trim().toLowerCase();

interface DidCommMessage {
  id: string;
  type: string;
  from: string;
  to: string[];
  created_time: number;
  body: Record<string, unknown>;
  thid?: string;
}

function buildMessage(
  type: string,
  from: string,
  to: string,
  body: Record<string, unknown>,
): DidCommMessage {
  return {
    id: randomUUID(),
    type,
    from,
    to: [to],
    created_time: Math.floor(Date.now() / 1000),
    body,
  };
}

async function main(): Promise<void> {
  console.log(`=== 13_ core-only bidirectional ping/pong (Node) — provider=${providerName} ===`);

  const { transport } = await getTransport({
    providerName,
    baseUrl: BASE_URL,
    clientId: "client-core",
    direction: "c2s",
  });
  console.log(`  session established: session_id=${transport.sessionId}`);

  const ping = buildMessage(
    "https://stls.example/protocols/ping/1.0/ping",
    CLIENT_DID,
    SERVER_DID,
    { text: `hello from client-core (provider=${providerName})` },
  );
  console.log(`  sending (c2s): ${JSON.stringify(ping.body)}`);

  const envelope = transport.encrypt(Buffer.from(JSON.stringify(ping), "utf-8"));
  const response = await fetch(`${BASE_URL}/ping`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      [HEADER_SESSION_ID]: transport.sessionId,
    },
    body: JSON.stringify(envelope.toDict()),
  });
  if (!response.ok) {
    throw new Error(`server responded ${response.status}: ${await response.text()}`);
  }

  const pongEnvelopeDict = await response.json();
  // Decrypted with the SAME transport, but on the other direction's key
  // internally (transport.decrypt() always uses the dec key — s2c, for
  // a "c2s" transport like this one).
  const pongPlaintext = transport.decrypt(StlsEnvelope.fromDict(pongEnvelopeDict));
  const pong = JSON.parse(pongPlaintext.toString("utf-8")) as DidCommMessage;
  console.log(`  received (s2c): ${JSON.stringify(pong.body)}`);

  const expected = `${ping.body.text}`.toUpperCase() + " — ACK FROM SERVER-CORE";
  assert.equal(pong.thid, ping.id);
  assert.equal(pong.body.text, expected);

  console.log("RESULT: PASS");

  // arqit_py keeps its sidecar socket connection open (no explicit
  // close() path from this one-shot script) — same as every other
  // sample's client_multi_*.ts, exit explicitly instead of hanging on
  // that lingering handle.
  process.exit(0);
}

void main();
