/**
 * peer.ts — 14_peer_a_core / 14_peer_b_core
 * -------------------------------------------
 * Companion to samples/{server,client}/13_{server,client}_core, testing
 * a DIFFERENT shape of bidirectionality than 13_'s.
 *
 * 13_ has ONE session/connection: a fixed client and a fixed server,
 * made bidirectional by labeling the two directions of that SAME
 * session with `direction` ("c2s"/"s2c", issue #12) so the request and
 * the response-carried-in-the-same-HTTP-response use independent keys.
 *
 * This sample instead has TWO fully independent channels, each
 * strictly one-way, and — the actual point of it — **both peers run
 * both roles**: each one exposes its own HTTP server AND can act as an
 * HTTP client toward the other. There is no fixed "the client"/"the
 * server": "peer_a" and "peer_b" run the EXACT SAME script, only their
 * .env differs (PORT, PEER_URL, INITIATE). This is the shape a NaaS
 * federation PoC between two genuinely symmetric peer nodes actually
 * needs — unlike 13_, which still has one side permanently cast as
 * "the client" and the other as "the server".
 *
 * Consequence worth stating explicitly: **`direction` is NOT used
 * here.** Each of the two channels only ever flows one way (its sender
 * always encrypts, its receiver always decrypts), so each channel's
 * StlsTransport is built with `direction` left unset — the same
 * single-shared-key mode stls-a2a/stls-mcp already use. `direction`
 * only earns its keep when ONE session carries traffic both ways, which
 * is 13_'s scenario, not this one.
 *
 * Flow, with PEER_A started with INITIATE=1 (PEER_B is not):
 *   1. Both peers start listening (/notify, /stls/handshake,
 *      /stls/pqc/handshake — all 3 routes exist on BOTH peers, since
 *      both can be contacted).
 *   2. peer_a, as a CLIENT, opens channel #1 toward peer_b (its own
 *      handshake, its own session_id/keys) and POSTs a DIDComm-shaped
 *      "ping" to peer_b's /notify.
 *   3. peer_b's /notify handler decrypts it on channel #1, replies with
 *      a bare technical ack on that SAME HTTP response (no app payload
 *      travels back here — that would just be 13_'s pattern again),
 *      then reacts by becoming a CLIENT ITSELF: opens channel #2
 *      (a brand-new, independent handshake/session) toward peer_a and
 *      POSTs the modified "pong" to peer_a's /notify.
 *   4. peer_a's /notify handler decrypts the pong on channel #2 (a
 *      DIFFERENT StlsTransport than the one it used to send the ping on
 *      channel #1) and the round trip is complete.
 *
 * Message bodies are DIDComm v2-shaped (id/type/from/to/created_time/
 * body) — format-compatibility exploration only, not real DIDComm
 * crypto (no JWE/JWS, no DID resolution). StlsTransport (AES-256-GCM +
 * HMAC) is the only encryption layer on both channels, same rationale
 * as 13_.
 *
 * Run: npx tsx --env-file=.env peer.ts
 * For KEY_PROVIDER_NAME=arqit_py, each peer needs TWO sidecars (one per
 * role it plays — SERVER for channel-as-listener, CLIENT for
 * channel-as-initiator) — see the README for the full 4-sidecar setup.
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
  getTransport,
  performHandshake,
  performPqcHandshake,
  KeySessionProviderFactory,
  KeySessionProviderType,
} from "stls-core";
import type { KeySessionProvider } from "stls-core";

const PEER_NAME = process.env.PEER_NAME ?? "peer-a";
const PORT = Number(process.env.PORT ?? "8401");
const PEER_URL = process.env.PEER_URL ?? "http://localhost:8402";
const INITIATE = process.env.INITIATE === "1";
const providerName = (process.env.KEY_PROVIDER_NAME ?? "base").trim().toLowerCase();
const SELF_DID = `did:example:${PEER_NAME}`;

interface DidCommMessage {
  id: string;
  type: string;
  from: string;
  to: string[];
  created_time: number;
  body: Record<string, unknown>;
  thid?: string;
}

function buildMessage(type: string, body: Record<string, unknown>, thid?: string): DidCommMessage {
  return {
    id: randomUUID(),
    type,
    from: SELF_DID,
    to: ["did:example:peer"],
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

  let keySessionProvider: KeySessionProvider | null = null;
  if (providerName !== "pqc") {
    keySessionProvider = await KeySessionProviderFactory.getSessionProvider({
      sessionType: KeySessionProviderType.SERVER,
      name: providerName,
      port: PORT,
    });
  }

  let resolveExchange: (() => void) | null = null;
  const exchangeDone = new Promise<void>((resolve) => {
    resolveExchange = resolve;
  });

  // Opens a BRAND NEW, independent channel toward the other peer — its
  // own handshake, its own session_id/keys. Never reuses whatever
  // transport this process might have used (or received a call on) for
  // anything else. No `direction`: this channel only ever flows this
  // one way (we encrypt, the other peer decrypts).
  async function sendOnNewChannel(message: DidCommMessage): Promise<void> {
    const { transport } = await getTransport({
      providerName,
      baseUrl: PEER_URL,
      clientId: PEER_NAME,
    });
    console.log(
      `[${PEER_NAME}] opened its own channel toward ${PEER_URL} ` +
        `(session_id=${transport.sessionId}) to send: ${JSON.stringify(message.body)}`,
    );
    const envelope = transport.encrypt(Buffer.from(JSON.stringify(message), "utf-8"));
    const response = await fetch(`${PEER_URL}/notify`, {
      method: "POST",
      headers: { "content-type": "application/json", [HEADER_SESSION_ID]: transport.sessionId },
      body: JSON.stringify(envelope.toDict()),
    });
    if (!response.ok) {
      throw new Error(`peer responded ${response.status}: ${await response.text()}`);
    }
  }

  const server = createServer(async (req, res) => {
    try {
      if (req.method === "POST" && req.url === HANDSHAKE_PATH) {
        const body = (await readJsonBody(req)) as HandshakeRequest;
        try {
          const response = await performHandshake(body, keySessionProvider!, cache);
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
          const response = await performPqcHandshake(body, cache);
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

      if (req.method === "POST" && req.url === "/notify") {
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
        let incoming: DidCommMessage;
        try {
          const plaintext = transport.decrypt(StlsEnvelope.fromDict(envelopeDict as never));
          incoming = JSON.parse(plaintext.toString("utf-8")) as DidCommMessage;
        } catch (exc) {
          sendJson(res, 400, { error: `Decrypt failed: ${(exc as Error).message}` });
          return;
        }

        console.log(`[${PEER_NAME}] received on this channel (session=${sessionId}): ${JSON.stringify(incoming.body)}`);

        // Bare technical ack on THIS same HTTP response — no app
        // payload travels back here. The actual reply, if any, travels
        // over the OTHER peer's own independent channel (below).
        sendJson(res, 200, { ack: true });

        if (incoming.type.endsWith("/ping")) {
          const originalText = String(incoming.body.text ?? "");
          const pong = buildMessage(
            "https://stls.example/protocols/ping/1.0/pong",
            { text: `${originalText.toUpperCase()} — ACK FROM ${PEER_NAME.toUpperCase()} (separate channel)` },
            incoming.id,
          );
          await sendOnNewChannel(pong);
          console.log(`[${PEER_NAME}] sent its reply on its own channel — role complete`);
          process.exit(0);
        } else if (incoming.type.endsWith("/pong")) {
          console.log(`[${PEER_NAME}] round trip complete via two independent channels`);
          console.log("RESULT: PASS");
          resolveExchange?.();
        }
        return;
      }

      sendJson(res, 404, { error: "Not found" });
    } catch (exc) {
      sendJson(res, 500, { error: (exc as Error).message });
    }
  });

  server.listen(PORT, "0.0.0.0", async () => {
    console.log(`${PEER_NAME} listening on port ${PORT} (KEY_PROVIDER_NAME=${providerName})`);
    if (INITIATE) {
      const ping = buildMessage(
        "https://stls.example/protocols/ping/1.0/ping",
        { text: `hello from ${PEER_NAME} (provider=${providerName})` },
      );
      await sendOnNewChannel(ping);
    }
  });

  if (INITIATE) {
    await exchangeDone;
    process.exit(0);
  }
}

void main();
