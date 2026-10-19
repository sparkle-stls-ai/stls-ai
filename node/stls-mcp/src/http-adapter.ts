/**
 * http-adapter.ts
 * ---------------
 * Thin convenience wrapper: turns a Web-standard (Request) => Promise<Response>
 * handler (what createSecureMcpApp() returns) into a node:http
 * RequestListener, for immediate use with http.createServer().
 *
 * The handler itself stays runtime-portable (Node, Deno, Bun, Cloudflare
 * Workers) — this adapter is the one piece that is Node-http-specific,
 * mirroring the split the official MCP SDK itself makes between
 * WebStandardStreamableHTTPServerTransport (portable core) and
 * StreamableHTTPServerTransport (its own Node-http convenience wrapper).
 */

import type { IncomingMessage, ServerResponse } from "node:http";
import type { RequestHandler } from "./auth.js";

function collectBody(req: IncomingMessage): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on("data", (chunk: Buffer) => chunks.push(chunk));
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

function toWebHeaders(req: IncomingMessage): Headers {
  const headers = new Headers();
  for (const [key, value] of Object.entries(req.headers)) {
    if (value === undefined) continue;
    if (Array.isArray(value)) {
      for (const v of value) headers.append(key, v);
    } else {
      headers.set(key, value);
    }
  }
  return headers;
}

export function toNodeRequestListener(handler: RequestHandler): (req: IncomingMessage, res: ServerResponse) => void {
  return (req: IncomingMessage, res: ServerResponse): void => {
    void (async () => {
      const method = req.method ?? "GET";
      const body = method === "GET" || method === "HEAD" ? undefined : await collectBody(req);
      const url = new URL(req.url ?? "/", `http://${req.headers.host ?? "localhost"}`);

      const request = new Request(url, {
        method,
        headers: toWebHeaders(req),
        body: body && body.length > 0 ? new Uint8Array(body) : undefined,
      });

      const response = await handler(request);
      const responseBody = Buffer.from(await response.arrayBuffer());

      res.writeHead(response.status, Object.fromEntries(response.headers.entries()));
      res.end(responseBody);
    })().catch((err: unknown) => {
      const message = err instanceof Error ? err.message : String(err);
      if (!res.headersSent) {
        res.writeHead(500, { "content-type": "application/json" });
      }
      res.end(JSON.stringify({ jsonrpc: "2.0", error: { code: -32603, message: `Internal error: ${message}` }, id: null }));
    });
  };
}
