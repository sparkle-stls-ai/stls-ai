/**
 * buyer.ts — samples/peer/18_naas_federation, step 1 (issue #38)
 * -----------------------------------------------------------------------
 * Generic "buyer" company node: sends the SAME NaaS RFQ (request for
 * quote) to N seller companies -- each its own relationship, own STLS
 * session, own device identity for arqit_py -- then waits for each
 * seller's quote to arrive on its OWN separate reply channel (mirrors
 * seller.ts's own N-sources shape, roles reversed: here the buyer
 * RECEIVES N replies instead of N requests, same physically-separate-
 * channels shape as samples/peer/{14,17}_peer_*_core). Once every
 * quote is in, picks the most advantageous one (lowest price) and
 * exits.
 *
 * Step 1 of this sample's gradual build-up: exactly ONE target
 * (company-b), this process itself NOT replicated (only the seller
 * side is, in this step) -- see this sample's README once written.
 *
 * Provider-agnostic by design: base/pqc/arqit_py all work here --
 * arqit_py is what this sample exists to exercise on real K8s
 * replicas, but nothing here is arqit-specific.
 *
 * Routing (mirrors seller.ts, roles reversed):
 *   POST /from/<name>/stls/handshake       -- one path per TARGET, bound
 *   POST /from/<name>/stls/pqc/handshake       to that relationship's OWN
 *                                               KeySessionProvider (SERVER
 *                                               role) -- this is where a
 *                                               seller's REPLY handshake
 *                                               lands. <name> must equal
 *                                               that seller's own
 *                                               COMPANY_NAME (the sender
 *                                               on this channel).
 *   POST /notify                           -- ONE shared path for every
 *                                               relationship's reply;
 *                                               session_id disambiguates.
 *
 * The RFQ itself goes out over its own channel (its own handshake),
 * toward each target's own receiving endpoint, named after THIS
 * company (the sender on THAT channel) -- see sendRfq()'s own
 * handshakePath comment.
 *
 * Sends RFQs SEQUENTIALLY, not concurrently (even with one target
 * this doesn't matter yet, but keeping this shape from the start --
 * see this sample's README for why, once more than one target exists).
 *
 * Env:
 *   COMPANY_NAME, PORT, KEY_PROVIDER_NAME (default for all targets)
 *   RFQ_SERVICE, RFQ_QTY (what's being requested, for the log/body only)
 *   TARGETS_COUNT, then per i in 1..TARGETS_COUNT:
 *     TARGET_<i>_NAME (must equal that seller's own COMPANY_NAME),
 *     TARGET_<i>_URL, TARGET_<i>_PROVIDER (optional override)
 *     arqit_py only: TARGET_<i>_SOCKET_CLIENT/_TOKEN_CLIENT (owner sidecar,
 *       sending the RFQ) and TARGET_<i>_SOCKET_SERVER/_TOKEN_SERVER
 *       (owner sidecar, receiving that seller's quote)
 *
 * Run: npx tsx --env-file=.env buyer.ts
 */
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { randomUUID } from "node:crypto";
import { appendFileSync } from "node:fs";

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
  StlsTransport,
  getTransport,
  performHandshake,
  performPqcHandshake,
  KeySessionProviderFactory,
  KeySessionProviderType,
} from "stls-core";
import type { KeySessionProvider } from "stls-core";

// Debug aid for K8s crash-loop debugging -- see seller.ts's own
// identical header comment for the full rationale.
const CRASH_LOG_PATH = process.env.CRASH_LOG_PATH;
function logCrash(label: string, exc: unknown): void {
  const text = `[${new Date().toISOString()}] ${label}: ${exc instanceof Error ? (exc.stack ?? exc.message) : String(exc)}\n`;
  console.error(text);
  if (CRASH_LOG_PATH) {
    try {
      appendFileSync(CRASH_LOG_PATH, text);
    } catch {
      // best-effort only
    }
  }
}
process.on("uncaughtException", (exc) => {
  logCrash("uncaughtException", exc);
  process.exit(1);
});
process.on("unhandledRejection", (exc) => {
  logCrash("unhandledRejection", exc);
  process.exit(1);
});

/**
 * session_id -> which TARGET (seller) a reply channel belongs to --
 * see seller.ts's own identical SessionSourceStore comment for why
 * this can't be a plain in-memory Map once this process itself is
 * ever replicated (not yet, in this sample's current step, but kept
 * consistent with seller.ts from the start).
 */
interface SessionTargetStore {
  set(sessionId: string, targetName: string): Promise<void>;
  get(sessionId: string): Promise<string | undefined>;
}

async function buildSessionTargetStore(): Promise<SessionTargetStore> {
  if ((process.env.SESSION_CACHE_BACKEND ?? "memory").trim().toLowerCase() !== "redis") {
    const map = new Map<string, string>();
    return {
      async set(sessionId, targetName) {
        map.set(sessionId, targetName);
      },
      async get(sessionId) {
        return map.get(sessionId);
      },
    };
  }
  const { createClient } = await import("redis");
  const url = process.env.SESSION_CACHE_REDIS_URL ?? "redis://localhost:6379";
  const client = createClient({ url });
  await client.connect();
  const prefix = "stls18:session-target";
  return {
    async set(sessionId, targetName) {
      await client.set(`${prefix}:${sessionId}`, targetName, { EX: 3600 });
    },
    async get(sessionId) {
      return (await client.get(`${prefix}:${sessionId}`)) ?? undefined;
    },
  };
}

const COMPANY_NAME = process.env.COMPANY_NAME ?? "company-buyer";
const PORT = Number(process.env.PORT ?? "8600");
const DEFAULT_PROVIDER = (process.env.KEY_PROVIDER_NAME ?? "base").trim().toLowerCase();
const TARGETS_COUNT = Number(process.env.TARGETS_COUNT ?? "0");
const RFQ_SERVICE = process.env.RFQ_SERVICE ?? "naas-bandwidth-link";
const RFQ_QTY = Number(process.env.RFQ_QTY ?? "1");

interface Target {
  name: string;
  url: string;
  providerName: string;
  socketClient?: string;
  tokenClient?: string;
  socketServer?: string;
  tokenServer?: string;
}

function loadTargets(): Target[] {
  const targets: Target[] = [];
  for (let i = 1; i <= TARGETS_COUNT; i++) {
    const name = process.env[`TARGET_${i}_NAME`];
    const url = process.env[`TARGET_${i}_URL`];
    if (!name || !url) {
      throw new Error(`TARGET_${i}_NAME / TARGET_${i}_URL not set (TARGETS_COUNT=${TARGETS_COUNT})`);
    }
    targets.push({
      name,
      url,
      providerName: (process.env[`TARGET_${i}_PROVIDER`] ?? DEFAULT_PROVIDER).trim().toLowerCase(),
      socketClient: process.env[`TARGET_${i}_SOCKET_CLIENT`],
      tokenClient: process.env[`TARGET_${i}_TOKEN_CLIENT`],
      socketServer: process.env[`TARGET_${i}_SOCKET_SERVER`],
      tokenServer: process.env[`TARGET_${i}_TOKEN_SERVER`],
    });
  }
  return targets;
}

interface DidCommMessage {
  id: string;
  type: string;
  from: string;
  to: string[];
  created_time: number;
  body: Record<string, unknown>;
  thid?: string;
}

function buildMessage(from: string, to: string, type: string, body: Record<string, unknown>): DidCommMessage {
  return {
    id: randomUUID(),
    type,
    from: `did:example:${from}`,
    to: [`did:example:${to}`],
    created_time: Math.floor(Date.now() / 1000),
    body,
  };
}

async function readJsonBody(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  return JSON.parse(Buffer.concat(chunks).toString("utf-8"));
}

function sendJson(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}

/**
 * Builds the SERVER-role KeySessionProvider that receives ONE target's
 * quote -- a dedicated sidecar connection with THIS relationship's own
 * socket/token, same rationale as seller.ts's buildServerProvider().
 */
async function buildServerProvider(target: Target): Promise<KeySessionProvider | null> {
  if (target.providerName === "pqc") return null;
  if (target.providerName === "arqit_py" && (!target.socketServer || !target.tokenServer)) {
    throw new Error(`target ${target.name}: SOCKET_SERVER/TOKEN_SERVER not set for arqit_py`);
  }
  return KeySessionProviderFactory.getSessionProvider({
    sessionType: KeySessionProviderType.SERVER,
    name: target.providerName,
    port: PORT,
    socketPath: target.socketServer,
    token: target.tokenServer,
  });
}

/**
 * Opens a BRAND NEW channel toward `target`'s own receiving endpoint
 * and sends the RFQ. handshakePath routes to that seller's
 * /from/<name>/... endpoint, named after THIS company (the sender on
 * this channel) -- that seller's own SOURCE_<i>_NAME must be set to
 * this COMPANY_NAME.
 */
async function sendRfq(target: Target, message: DidCommMessage): Promise<void> {
  if (target.providerName === "arqit_py" && (!target.socketClient || !target.tokenClient)) {
    throw new Error(`target ${target.name}: SOCKET_CLIENT/TOKEN_CLIENT not set for arqit_py`);
  }
  const handshakeSuffix = target.providerName === "pqc" ? HANDSHAKE_PQC_PATH : HANDSHAKE_PATH;
  const { transport } = await getTransport({
    providerName: target.providerName,
    baseUrl: target.url,
    clientId: `${COMPANY_NAME}-to-${target.name}`,
    socketPath: target.socketClient,
    handshakePath: `/from/${COMPANY_NAME}${handshakeSuffix}`,
  });
  console.log(`[${COMPANY_NAME}] sending RFQ to ${target.name} on a new channel (session_id=${transport.sessionId})`);
  const envelope = transport.encrypt(Buffer.from(JSON.stringify(message), "utf-8"));
  await postEnvelope(target.url, transport.sessionId, envelope.toDict());
}

async function postEnvelope(baseUrl: string, sessionId: string, envelopeDict: unknown): Promise<void> {
  const response = await fetch(`${baseUrl}/notify`, {
    method: "POST",
    headers: { "content-type": "application/json", [HEADER_SESSION_ID]: sessionId },
    body: JSON.stringify(envelopeDict),
  });
  if (!response.ok) throw new Error(`target responded ${response.status}: ${await response.text()}`);
}

function pickBestOffer(offers: Map<string, number>): { seller: string; price: number } {
  let best: { seller: string; price: number } | null = null;
  for (const [seller, price] of offers) {
    if (best === null || price < best.price) best = { seller, price };
  }
  if (best === null) throw new Error("no offers received (bug)");
  return best;
}

// Spaces out device authentications when this process owns more than
// one arqit_py device (see this sample's README, "Notes on real-cloud
// device auth"). ARQIT_PY_DEVICE_AUTH_STAGGER_MS, default 3000; 0 disables it.
const DEVICE_AUTH_STAGGER_MS = Number(process.env.ARQIT_PY_DEVICE_AUTH_STAGGER_MS ?? "3000");
function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function main(): Promise<void> {
  const targets = loadTargets();
  const cache: SessionCache = await SessionCacheFactory.build();
  const serverProviders = new Map<string, KeySessionProvider | null>();
  for (const [i, t] of targets.entries()) {
    if (i > 0 && DEVICE_AUTH_STAGGER_MS > 0) await sleep(DEVICE_AUTH_STAGGER_MS);
    serverProviders.set(t.name, await buildServerProvider(t));
  }

  // session_id -> which TARGET this reply channel belongs to, set at
  // handshake time (that seller's own reply handshake), read at /notify
  // time to know which offer this is.
  const sessionTarget = await buildSessionTargetStore();
  const offers = new Map<string, number>();

  const server = createServer(async (req, res) => {
    try {
      const match = req.url?.match(/^\/from\/([^/]+)\/(stls\/handshake|stls\/pqc\/handshake)$/);
      if (req.method === "POST" && match) {
        const [, name, kind] = match;
        const target = targets.find((t) => t.name === name);
        if (!target) return sendJson(res, 404, { error: `unknown target: ${name}` });

        if (kind === "stls/pqc/handshake") {
          const body = (await readJsonBody(req)) as PQCHandshakeRequest;
          try {
            const response = await performPqcHandshake(body, cache);
            await sessionTarget.set(response.session_id, name);
            sendJson(res, 200, response);
          } catch (exc) {
            if (exc instanceof PQCHandshakeError) return sendJson(res, exc.code === -32600 ? 400 : 503, { error: exc.message, code: exc.code });
            throw exc;
          }
          return;
        }

        const body = (await readJsonBody(req)) as HandshakeRequest;
        try {
          const response = await performHandshake(body, serverProviders.get(name)!, cache);
          await sessionTarget.set(response.session_id, name);
          sendJson(res, 200, response);
        } catch (exc) {
          if (exc instanceof HandshakeError) return sendJson(res, exc.code === -32600 ? 400 : 503, { error: exc.message, code: exc.code });
          throw exc;
        }
        return;
      }

      if (req.method === "POST" && req.url === "/notify") {
        const sessionId = req.headers[HEADER_SESSION_ID.toLowerCase()] as string | undefined;
        if (!sessionId) return sendJson(res, 401, { error: `Missing ${HEADER_SESSION_ID} header` });
        const transport: StlsTransport | undefined = await cache.get(sessionId);
        if (!transport) return sendJson(res, 404, { error: "Unknown session_id" });
        const sellerName = await sessionTarget.get(sessionId);
        if (!sellerName) return sendJson(res, 500, { error: "session_id with no known target (bug)" });

        const envelopeDict = await readJsonBody(req);
        let incoming: DidCommMessage;
        try {
          const plaintext = transport.decrypt(StlsEnvelope.fromDict(envelopeDict as never));
          incoming = JSON.parse(plaintext.toString("utf-8")) as DidCommMessage;
        } catch (exc) {
          return sendJson(res, 400, { error: `Decrypt failed: ${(exc as Error).message}` });
        }

        const offer = (incoming.body as { offer?: { price: number } }).offer;
        if (!offer) return sendJson(res, 500, { error: "reply with no offer (bug)" });
        offers.set(sellerName, offer.price);
        console.log(`[${COMPANY_NAME}] quote from ${sellerName} (session=${sessionId}): ${offer.price} EUR`);
        sendJson(res, 200, { ack: true });

        if (offers.size === targets.length) {
          const best = pickBestOffer(offers);
          console.log(`[${COMPANY_NAME}] all ${targets.length} quotes received: ${JSON.stringify(Object.fromEntries(offers))}`);
          console.log(`[${COMPANY_NAME}] most advantageous offer: ${best.seller} at ${best.price} EUR`);
          console.log("RESULT: PASS");
          process.exit(0);
        }
        return;
      }

      sendJson(res, 404, { error: "Not found" });
    } catch (exc) {
      sendJson(res, 500, { error: (exc as Error).message });
    }
  });

  server.listen(PORT, "0.0.0.0", async () => {
    console.log(`${COMPANY_NAME} (buyer) listening on port ${PORT} -- targets: ${targets.map((t) => t.name).join(", ")}`);

    if (DEVICE_AUTH_STAGGER_MS > 0) await sleep(DEVICE_AUTH_STAGGER_MS);
    const rfq = buildMessage(COMPANY_NAME, "naas-sellers", "https://stls.example/protocols/naas-rfq/1.0/request", {
      service: RFQ_SERVICE,
      qty: RFQ_QTY,
    });
    // Sequential, not Promise.all -- see this sample's README for why
    // (empirically found, on an earlier K8s/arqit_py pass of this
    // sample, that authenticating two DIFFERENT devices under the
    // same Arqit account at the exact same instant was unreliable).
    for (const [i, target] of targets.entries()) {
      if (i > 0 && DEVICE_AUTH_STAGGER_MS > 0) await sleep(DEVICE_AUTH_STAGGER_MS);
      await sendRfq(target, rfq);
    }
  });
}

void main();
