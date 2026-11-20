/**
 * seller.ts — samples/peer/18_naas_federation, step 1 (issue #38)
 * -----------------------------------------------------------------------
 * Generic "seller" company node: receives a NaaS RFQ (request for
 * quote) from N buyer companies -- each its own relationship, own STLS
 * session, own device identity for arqit_py -- and replies to each on
 * a SEPARATE, physically independent channel (its own handshake, its
 * own session_id/keys, its own device pair), same two-independent-
 * channels shape as samples/peer/{14,17}_peer_*_core.
 *
 * Step 1 of this sample's gradual build-up: exactly ONE source
 * (company-a), this process meant to run as N replicas (device-agent/
 * consumer split, issue #38's actual subject) -- see this sample's
 * README once written.
 *
 * Provider-agnostic by design: base/pqc/arqit_py all work here --
 * arqit_py is what this sample exists to exercise on real K8s
 * replicas, but nothing here is arqit-specific.
 *
 * Routing:
 *   POST /from/<name>/stls/handshake       -- one path per SOURCE,
 *   POST /from/<name>/stls/pqc/handshake       bound to that relationship's
 *                                               OWN KeySessionProvider
 *                                               (SERVER role).
 *   POST /notify                           -- ONE shared path for every
 *                                               relationship: session_id
 *                                               disambiguates. Which
 *                                               SOURCE a given session_id
 *                                               belongs to is recorded at
 *                                               handshake time.
 *
 * The reply goes out over a BRAND NEW channel (its own handshake)
 * toward that source's own receiving endpoint, using that
 * relationship's OWN reply device (CLIENT role). This device has no
 * consumer-mode equivalent (issue #38's split only covers SERVER
 * role), which matters here specifically for this company's own
 * REPLY_MODE:
 *   REPLY_MODE=direct (default): this process itself owns the reply
 *     device and sends the reply inline -- fine for a singleton
 *     seller.
 *   REPLY_MODE=delegate: this process has NO reply device of its own
 *     at all -- it POSTs {sourceName, message} to REPLY_AGENT_URL and
 *     a separate, singleton reply_agent.ts process (which DOES own
 *     the reply devices) sends it. Required whenever this seller
 *     itself runs as >1 replica: two replicas independently calling
 *     start() on the SAME reply device is exactly the incident that
 *     motivated issue #38's owner lock -- the lock makes that merely
 *     fail loudly instead of bricking the device again, but a
 *     dedicated singleton reply_agent avoids the failure outright.
 *
 * Env:
 *   COMPANY_NAME, PORT, KEY_PROVIDER_NAME (default for all sources)
 *   OFFER_PRICE (fixed quote) or OFFER_BASE_PRICE (deterministic per-buyer spread)
 *   REPLY_MODE (direct|delegate), REPLY_AGENT_URL (delegate only)
 *   SOURCES_COUNT, then per i in 1..SOURCES_COUNT:
 *     SOURCE_<i>_NAME (must equal that buyer's own COMPANY_NAME --
 *       it's the path segment that buyer's own outbound call uses),
 *     SOURCE_<i>_REPLY_URL, SOURCE_<i>_PROVIDER (optional override)
 *     arqit_py only: SOURCE_<i>_SOCKET_SERVER/_TOKEN_SERVER (owner or
 *       consumer sidecar, receiving this source's RFQ) and, REPLY_MODE=direct
 *       only, SOURCE_<i>_SOCKET_CLIENT/_TOKEN_CLIENT (owner sidecar,
 *       sending the reply back to this source)
 *
 * Run: npx tsx --env-file=.env seller.ts
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

// Debug aid for K8s crash-loop debugging -- a crash inside this
// process's own top-level handlers gets a fresh container filesystem
// on every OnFailure/Deployment restart, so kubectl logs --previous
// only ever shows the LATEST crash. CRASH_LOG_PATH (if set, e.g. into
// a pod's own shared /sockets emptyDir) survives restarts.
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
 * session_id -> which SOURCE it belongs to. Separate from (and just as
 * important as) stls-core's own SessionCache: SessionCache shares the
 * established StlsTransport across replicas via SESSION_CACHE_BACKEND=
 * redis (see this sample's README), but this app-level mapping -- which
 * named source a given session belongs to, needed at /notify time to
 * know which device/reply target to use -- is this sample's own state,
 * and a plain in-memory Map has the exact same cross-replica gap
 * SessionCache had before that fix: a handshake landing on one replica,
 * its /notify landing on another, leaves the second with no idea which
 * source it is. Reuses the SAME Redis instance as SESSION_CACHE_BACKEND
 * (own key prefix), only when that backend is actually `redis` --
 * otherwise (local/single-process runs) a plain Map is fine.
 */
interface SessionSourceStore {
  set(sessionId: string, sourceName: string): Promise<void>;
  get(sessionId: string): Promise<string | undefined>;
}

async function buildSessionSourceStore(): Promise<SessionSourceStore> {
  if ((process.env.SESSION_CACHE_BACKEND ?? "memory").trim().toLowerCase() !== "redis") {
    const map = new Map<string, string>();
    return {
      async set(sessionId, sourceName) {
        map.set(sessionId, sourceName);
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
  const prefix = "stls18:session-source";
  return {
    async set(sessionId, sourceName) {
      await client.set(`${prefix}:${sessionId}`, sourceName, { EX: 3600 });
    },
    async get(sessionId) {
      return (await client.get(`${prefix}:${sessionId}`)) ?? undefined;
    },
  };
}

const COMPANY_NAME = process.env.COMPANY_NAME ?? "company-seller";
const PORT = Number(process.env.PORT ?? "8700");
const DEFAULT_PROVIDER = (process.env.KEY_PROVIDER_NAME ?? "base").trim().toLowerCase();
const SOURCES_COUNT = Number(process.env.SOURCES_COUNT ?? "0");
const REPLY_MODE = (process.env.REPLY_MODE ?? "direct").trim().toLowerCase();
const REPLY_AGENT_URL = process.env.REPLY_AGENT_URL;
// Fixed quote (OFFER_PRICE), or a deterministic small per-buyer spread
// off OFFER_BASE_PRICE -- either way, no randomness: the same run
// always produces the same winner, so the sample stays scriptable.
const OFFER_BASE_PRICE = Number(process.env.OFFER_BASE_PRICE ?? "100");

if (REPLY_MODE === "delegate" && !REPLY_AGENT_URL) {
  throw new Error("REPLY_MODE=delegate requires REPLY_AGENT_URL");
}

function quoteOffer(sourceName: string): { price: number } {
  if (process.env.OFFER_PRICE) return { price: Number(process.env.OFFER_PRICE) };
  let hash = 0;
  for (const ch of sourceName) hash = (hash * 31 + ch.charCodeAt(0)) % 97;
  return { price: OFFER_BASE_PRICE + (hash % 20) };
}

interface Source {
  name: string;
  replyUrl: string;
  providerName: string;
  socketServer?: string;
  tokenServer?: string;
  socketClient?: string;
  tokenClient?: string;
}

function loadSources(): Source[] {
  const sources: Source[] = [];
  for (let i = 1; i <= SOURCES_COUNT; i++) {
    const name = process.env[`SOURCE_${i}_NAME`];
    const replyUrl = process.env[`SOURCE_${i}_REPLY_URL`];
    if (!name || !replyUrl) {
      throw new Error(`SOURCE_${i}_NAME / SOURCE_${i}_REPLY_URL not set (SOURCES_COUNT=${SOURCES_COUNT})`);
    }
    sources.push({
      name,
      replyUrl,
      providerName: (process.env[`SOURCE_${i}_PROVIDER`] ?? DEFAULT_PROVIDER).trim().toLowerCase(),
      socketServer: process.env[`SOURCE_${i}_SOCKET_SERVER`],
      tokenServer: process.env[`SOURCE_${i}_TOKEN_SERVER`],
      socketClient: process.env[`SOURCE_${i}_SOCKET_CLIENT`],
      tokenClient: process.env[`SOURCE_${i}_TOKEN_CLIENT`],
    });
  }
  return sources;
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

function buildMessage(from: string, to: string, type: string, body: Record<string, unknown>, thid?: string): DidCommMessage {
  return {
    id: randomUUID(),
    type,
    from: `did:example:${from}`,
    to: [`did:example:${to}`],
    created_time: Math.floor(Date.now() / 1000),
    body,
    ...(thid ? { thid } : {}),
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
 * Builds the SERVER-role KeySessionProvider for one relationship.
 * arqit_py: a dedicated sidecar connection with THIS relationship's own
 * socket/token (bypasses the single-socket-per-process env resolution
 * KeySessionProviderFactory does, which can't serve N different
 * relationships from one process on its own). Whether the sidecar on
 * the other end is running in owner or ARQIT_PY_DEVICE_ROLE=consumer
 * mode is an infra-level decision -- invisible here.
 */
async function buildServerProvider(source: Source): Promise<KeySessionProvider | null> {
  if (source.providerName === "pqc") return null;
  if (source.providerName === "arqit_py" && (!source.socketServer || !source.tokenServer)) {
    throw new Error(`source ${source.name}: SOCKET_SERVER/TOKEN_SERVER not set for arqit_py`);
  }
  return KeySessionProviderFactory.getSessionProvider({
    sessionType: KeySessionProviderType.SERVER,
    name: source.providerName,
    port: PORT,
    socketPath: source.socketServer,
    token: source.tokenServer,
  });
}

/**
 * Opens a BRAND NEW channel toward `source`'s own receiving endpoint
 * and sends `message` -- REPLY_MODE=direct only (this process owns the
 * reply device itself). handshakePath routes to the buyer's OWN
 * /from/<name>/... endpoint for this relationship, named after THIS
 * company (the sender on this channel) -- the buyer's matching
 * TARGET_<i>_NAME must be set to this same COMPANY_NAME.
 */
async function replyDirect(source: Source, message: DidCommMessage): Promise<void> {
  if (source.providerName === "arqit_py" && (!source.socketClient || !source.tokenClient)) {
    throw new Error(`source ${source.name}: SOCKET_CLIENT/TOKEN_CLIENT not set for arqit_py reply`);
  }
  const handshakeSuffix = source.providerName === "pqc" ? HANDSHAKE_PQC_PATH : HANDSHAKE_PATH;
  const { transport } = await getTransport({
    providerName: source.providerName,
    baseUrl: source.replyUrl,
    clientId: `${COMPANY_NAME}-reply-to-${source.name}`,
    socketPath: source.socketClient,
    handshakePath: `/from/${COMPANY_NAME}${handshakeSuffix}`,
  });
  console.log(`[${COMPANY_NAME}] replying to ${source.name} on a new channel (session_id=${transport.sessionId})`);
  const envelope = transport.encrypt(Buffer.from(JSON.stringify(message), "utf-8"));
  await postEnvelope(source.replyUrl, transport.sessionId, envelope.toDict());
}

/** REPLY_MODE=delegate only -- hands the reply off to the singleton reply_agent.ts. */
async function replyDelegate(source: Source, message: DidCommMessage): Promise<void> {
  const response = await fetch(`${REPLY_AGENT_URL}/reply/${source.name}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!response.ok) throw new Error(`reply_agent responded ${response.status}: ${await response.text()}`);
  console.log(`[${COMPANY_NAME}] delegated reply to ${source.name} to reply_agent`);
}

async function postEnvelope(baseUrl: string, sessionId: string, envelopeDict: unknown): Promise<void> {
  const response = await fetch(`${baseUrl}/notify`, {
    method: "POST",
    headers: { "content-type": "application/json", [HEADER_SESSION_ID]: sessionId },
    body: JSON.stringify(envelopeDict),
  });
  if (!response.ok) throw new Error(`reply target responded ${response.status}: ${await response.text()}`);
}

async function main(): Promise<void> {
  const sources = loadSources();
  const cache: SessionCache = await SessionCacheFactory.build();
  const serverProviders = new Map<string, KeySessionProvider | null>();
  for (const s of sources) serverProviders.set(s.name, await buildServerProvider(s));

  const sessionSource = await buildSessionSourceStore();

  const server = createServer(async (req, res) => {
    try {
      const match = req.url?.match(/^\/from\/([^/]+)\/(stls\/handshake|stls\/pqc\/handshake)$/);
      if (req.method === "POST" && match) {
        const [, name, kind] = match;
        const source = sources.find((s) => s.name === name);
        if (!source) return sendJson(res, 404, { error: `unknown source: ${name}` });

        if (kind === "stls/pqc/handshake") {
          const body = (await readJsonBody(req)) as PQCHandshakeRequest;
          try {
            const response = await performPqcHandshake(body, cache);
            await sessionSource.set(response.session_id, name);
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
          await sessionSource.set(response.session_id, name);
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
        const sourceName = await sessionSource.get(sessionId);
        if (!sourceName) return sendJson(res, 500, { error: "session_id with no known source (bug)" });
        const source = sources.find((s) => s.name === sourceName)!;

        const envelopeDict = await readJsonBody(req);
        let rfq: DidCommMessage;
        try {
          const plaintext = transport.decrypt(StlsEnvelope.fromDict(envelopeDict as never));
          rfq = JSON.parse(plaintext.toString("utf-8")) as DidCommMessage;
        } catch (exc) {
          return sendJson(res, 400, { error: `Decrypt failed: ${(exc as Error).message}` });
        }

        console.log(`[${COMPANY_NAME}] received RFQ from ${sourceName} (session=${sessionId}): ${JSON.stringify(rfq.body)}`);
        sendJson(res, 200, { ack: true });

        const offer = quoteOffer(sourceName);
        const reply = buildMessage(COMPANY_NAME, sourceName, "https://stls.example/protocols/naas-rfq/1.0/offer", {
          offer: { price: offer.price, currency: "EUR", seller: COMPANY_NAME, service: (rfq.body as { service?: string }).service },
        }, rfq.id);
        if (REPLY_MODE === "delegate") {
          await replyDelegate(source, reply);
        } else {
          await replyDirect(source, reply);
        }
        console.log(`[${COMPANY_NAME}] quoted ${sourceName}: ${offer.price} EUR (sent on its own reply channel)`);
        return;
      }

      sendJson(res, 404, { error: "Not found" });
    } catch (exc) {
      sendJson(res, 500, { error: (exc as Error).message });
    }
  });

  server.listen(PORT, "0.0.0.0", () => {
    console.log(`${COMPANY_NAME} (seller) listening on port ${PORT} -- sources: ${sources.map((s) => s.name).join(", ")}, reply_mode=${REPLY_MODE}`);
  });
}

void main();
