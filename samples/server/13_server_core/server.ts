/**
 * server.ts — 13_server_core
 * ---------------------------
 * Minimal "node B" of a 2-peer-node scenario using ONLY node/stls-core —
 * no stls-a2a, no stls-mcp. This is the NaaS-federation-style shape:
 * two nodes label their own transport direction ("c2s"/"s2c") and talk
 * to each other bidirectionally over one request/response, instead of
 * the request/response-only pattern stls-a2a/stls-mcp use (which don't
 * need direction at all — see issues #10/#11/#12).
 *
 * Wire-level routes, hand-rolled with node:http (no framework — "core
 * only" means no stls-mcp/stls-a2a route-wiring helpers either):
 *   POST /stls/handshake      — base/arqit_py (performHandshake)
 *   POST /stls/pqc/handshake  — pqc (performPqcHandshake)
 *   POST /ping                — the actual bidirectional exchange
 *
 * Message body shape is DIDComm v2's plaintext message envelope
 * (id/type/from/to/created_time/body) — format-compatibility only, NOT
 * real DIDComm crypto (no JWE/JWS, no DID resolution): STLS's own
 * AES-256-GCM+HMAC (via StlsTransport, direction="s2c" here) is the
 * only encryption layer. Real DIDComm is explicitly out of scope for
 * stls-core (see the "DIDComm evolution" backlog note) — this only
 * explores whether the two framings compose.
 *
 * Run: npx tsx --env-file=.env server.ts
 * For KEY_PROVIDER_NAME=arqit_py, launch the Python sidecar first — see
 * the README (same pattern as samples/server/12_server_mcp_ms).
 */
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { randomUUID } from "node:crypto";

import {
  HANDSHAKE_PATH,
  HANDSHAKE_PQC_PATH,
  HEADER_SESSION_ID,
  HandshakeError,
  HandshakeRequest,
  PQCHandshakeError,
  PQCHandshakeRequest,
  SessionCache,
  SessionCacheFactory,
  StlsEnvelope,
  performHandshake,
  performPqcHandshake,
  KeySessionProviderFactory,
  KeySessionProviderType,
} from "stls-core";
import type { KeySessionProvider } from "stls-core";

const PORT = Number(process.env.PORT ?? "8301");
const SERVER_DID = "did:example:server-core";
const CLIENT_DID = "did:example:client-core";
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
  thid?: string,
): DidCommMessage {
  return {
    id: randomUUID(),
    type,
    from,
    to: [to],
    created_time: Math.floor(Date.now() / 1000),
    body,
    ...(thid ? { thid } : {}),
  };
}

async function readJsonBody(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) {
    chunks.push(chunk as Buffer);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf-8"));
}

function sendJson(res: ServerResponse, status: number, body: unknown): void {
  const data = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json" });
  res.end(data);
}

async function main(): Promise<void> {
  const cache: SessionCache = await SessionCacheFactory.build();

  // pqc derives its own session key via the KEM — no KeySessionProvider
  // involved. base/arqit_py need one, exactly like performHandshake()
  // expects (see handshake.ts).
  let keySessionProvider: KeySessionProvider | null = null;
  if (providerName !== "pqc") {
    keySessionProvider = await KeySessionProviderFactory.getSessionProvider({
      sessionType: KeySessionProviderType.SERVER,
      name: providerName,
      port: PORT,
    });
  }

  const server = createServer(async (req, res) => {
    try {
      if (req.method === "POST" && req.url === HANDSHAKE_PATH) {
        const body = (await readJsonBody(req)) as HandshakeRequest;
        try {
          // direction="s2c": this node encrypts with the s2c key and
          // decrypts with the c2s key — the client (direction="c2s")
          // does the opposite. See StlsTransport's docstring.
          const response = await performHandshake(body, keySessionProvider!, cache, "s2c");
          sendJson(res, 200, response);
        } catch (exc) {
          if (exc instanceof HandshakeError) {
            sendJson(res, exc.code === -32600 ? 400 : 503, { error: exc.message, code: exc.code });
            return;
          }
          throw exc;
        }
        return;
      }

      if (req.method === "POST" && req.url === HANDSHAKE_PQC_PATH) {
        const body = (await readJsonBody(req)) as PQCHandshakeRequest;
        try {
          const response = await performPqcHandshake(body, cache, "s2c");
          sendJson(res, 200, response);
        } catch (exc) {
          if (exc instanceof PQCHandshakeError) {
            sendJson(res, exc.code === -32600 ? 400 : 503, { error: exc.message, code: exc.code });
            return;
          }
          throw exc;
        }
        return;
      }

      if (req.method === "POST" && req.url === "/ping") {
        const sessionId = req.headers[HEADER_SESSION_ID.toLowerCase()] as string | undefined;
        if (!sessionId) {
          sendJson(res, 401, { error: `Missing ${HEADER_SESSION_ID} header` });
          return;
        }
        const transport = await cache.get(sessionId);
        if (!transport) {
          sendJson(res, 404, { error: "Unknown session_id" });
          return;
        }

        const envelopeDict = await readJsonBody(req);
        let ping: DidCommMessage;
        try {
          const plaintext = transport.decrypt(StlsEnvelope.fromDict(envelopeDict as never));
          ping = JSON.parse(plaintext.toString("utf-8")) as DidCommMessage;
        } catch (exc) {
          sendJson(res, 400, { error: `Decrypt failed: ${(exc as Error).message}` });
          return;
        }

        const originalText = String(ping.body?.text ?? "");
        console.log(`[server-core] received (session=${sessionId}): ${JSON.stringify(ping.body)}`);

        const pong = buildMessage(
          "https://stls.example/protocols/ping/1.0/pong",
          SERVER_DID,
          CLIENT_DID,
          { text: `${originalText.toUpperCase()} — ACK FROM SERVER-CORE` },
          ping.id,
        );
        // Encrypted on the OTHER direction: the server's encrypt() uses
        // its s2c key, which the client decrypts with its own s2c key —
        // the actual point of this sample.
        const pongEnvelope = transport.encrypt(Buffer.from(JSON.stringify(pong), "utf-8"));
        sendJson(res, 200, pongEnvelope.toDict());
        return;
      }

      sendJson(res, 404, { error: "Not found" });
    } catch (exc) {
      sendJson(res, 500, { error: (exc as Error).message });
    }
  });

  server.listen(PORT, "0.0.0.0", () => {
    console.log(`13_server_core started on port ${PORT} (KEY_PROVIDER_NAME=${providerName})`);
  });
}

void main();
