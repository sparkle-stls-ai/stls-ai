/**
 * reply_agent.ts — samples/peer/18_naas_federation, step 1 (issue #38)
 * -----------------------------------------------------------------------
 * Singleton companion to seller.ts's REPLY_MODE=delegate: owns the
 * CLIENT-role reply device(s) for a seller that itself runs as >1
 * replica. Exists because issue #38's device-agent/consumer split only
 * covers the SERVER role -- negotiating a brand-new CLIENT session
 * inherently means authenticating the device, and two independent
 * replicas doing that concurrently for the SAME device is exactly the
 * incident that motivated #38 in the first place (the owner lock now
 * makes that merely fail loudly instead of bricking the device again,
 * but a dedicated singleton avoids hitting it at all).
 *
 * Nothing else about this process is specific to "seller" semantics --
 * it is a thin, generic "send this DIDComm-shaped message to this
 * named target, on its own brand-new channel" worker, addressed by
 * the seller replicas over one internal endpoint per relationship:
 *
 *   POST /reply/<name>   { "message": <DidCommMessage> }
 *
 * Reached by the seller replicas over plain in-cluster Service DNS
 * (intra-company, not cross-company traffic).
 *
 * Env:
 *   COMPANY_NAME, PORT
 *   TARGETS_COUNT, then per i in 1..TARGETS_COUNT:
 *     TARGET_<i>_NAME, TARGET_<i>_REPLY_URL, TARGET_<i>_PROVIDER (optional override)
 *     arqit_py only: TARGET_<i>_SOCKET_CLIENT/_TOKEN_CLIENT (owner sidecar)
 *
 * Run: npx tsx --env-file=.env reply_agent.ts
 */
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { appendFileSync } from "node:fs";

import { HANDSHAKE_PATH, HANDSHAKE_PQC_PATH, HEADER_SESSION_ID, getTransport } from "stls-core";

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

const COMPANY_NAME = process.env.COMPANY_NAME ?? "company-reply-agent";
const PORT = Number(process.env.PORT ?? "8800");
const DEFAULT_PROVIDER = (process.env.KEY_PROVIDER_NAME ?? "base").trim().toLowerCase();
const TARGETS_COUNT = Number(process.env.TARGETS_COUNT ?? "0");

interface DidCommMessage {
  id: string;
  type: string;
  from: string;
  to: string[];
  created_time: number;
  body: Record<string, unknown>;
  thid?: string;
}

interface Target {
  name: string;
  replyUrl: string;
  providerName: string;
  socketClient?: string;
  tokenClient?: string;
}

function loadTargets(): Map<string, Target> {
  const targets = new Map<string, Target>();
  for (let i = 1; i <= TARGETS_COUNT; i++) {
    const name = process.env[`TARGET_${i}_NAME`];
    const replyUrl = process.env[`TARGET_${i}_REPLY_URL`];
    if (!name || !replyUrl) {
      throw new Error(`TARGET_${i}_NAME / TARGET_${i}_REPLY_URL not set (TARGETS_COUNT=${TARGETS_COUNT})`);
    }
    targets.set(name, {
      name,
      replyUrl,
      providerName: (process.env[`TARGET_${i}_PROVIDER`] ?? DEFAULT_PROVIDER).trim().toLowerCase(),
      socketClient: process.env[`TARGET_${i}_SOCKET_CLIENT`],
      tokenClient: process.env[`TARGET_${i}_TOKEN_CLIENT`],
    });
  }
  return targets;
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

/** Same shape as seller.ts's replyDirect() -- this process IS that logic, extracted to a singleton. */
async function sendReply(target: Target, message: DidCommMessage): Promise<string> {
  if (target.providerName === "arqit_py" && (!target.socketClient || !target.tokenClient)) {
    throw new Error(`target ${target.name}: SOCKET_CLIENT/TOKEN_CLIENT not set for arqit_py`);
  }
  const handshakeSuffix = target.providerName === "pqc" ? HANDSHAKE_PQC_PATH : HANDSHAKE_PATH;
  const { transport } = await getTransport({
    providerName: target.providerName,
    baseUrl: target.replyUrl,
    clientId: `${COMPANY_NAME}-reply-to-${target.name}`,
    socketPath: target.socketClient,
    handshakePath: `/from/${COMPANY_NAME}${handshakeSuffix}`,
  });
  const envelope = transport.encrypt(Buffer.from(JSON.stringify(message), "utf-8"));
  const response = await fetch(`${target.replyUrl}/notify`, {
    method: "POST",
    headers: { "content-type": "application/json", [HEADER_SESSION_ID]: transport.sessionId },
    body: JSON.stringify(envelope.toDict()),
  });
  if (!response.ok) throw new Error(`reply target responded ${response.status}: ${await response.text()}`);
  return transport.sessionId;
}

async function main(): Promise<void> {
  const targets = loadTargets();

  const server = createServer(async (req, res) => {
    try {
      const match = req.url?.match(/^\/reply\/([^/]+)$/);
      if (req.method === "POST" && match) {
        const [, name] = match;
        const target = targets.get(name);
        if (!target) return sendJson(res, 404, { error: `unknown target: ${name}` });

        const { message } = (await readJsonBody(req)) as { message: DidCommMessage };
        const sessionId = await sendReply(target, message);
        console.log(`[${COMPANY_NAME}] sent reply to ${name} on its own channel (session_id=${sessionId})`);
        sendJson(res, 200, { ok: true, session_id: sessionId });
        return;
      }

      sendJson(res, 404, { error: "Not found" });
    } catch (exc) {
      sendJson(res, 500, { error: (exc as Error).message });
    }
  });

  server.listen(PORT, "0.0.0.0", () => {
    console.log(`${COMPANY_NAME} (reply agent) listening on port ${PORT} -- targets: ${[...targets.keys()].join(", ")}`);
  });
}

void main();
