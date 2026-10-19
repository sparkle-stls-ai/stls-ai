/**
 * middleware.ts
 * -------------
 * Server-side encrypted handler for MCP_STLS_PATH.
 *
 * Design: "same request logic, byte-level wrap" — there is no second
 * McpServer and no router re-dispatch involved. Given `innerHandle`, the
 * exact same (Request) => Promise<Response> function the plain endpoint
 * calls directly, this module:
 *
 *   1. Reads the incoming encrypted request.
 *   2. Resolves the STLS session (handshake already happened, transport
 *      cached).
 *   3. Decrypts the JSON-RPC body.
 *   4. Builds a synthetic Request with the decrypted body and calls
 *      innerHandle() with it — DIRECTLY, as a function call, not through
 *      any router. Unlike python/stls-mcp's StlsMcpMiddleware (which has
 *      to rewrite an ASGI scope and work around a Starlette `Mount`
 *      quirk because it re-invokes a whole external framework app), this
 *      is just calling a function — there's no routing layer that could
 *      ever send the decrypted request to the wrong place.
 *   5. Encrypts innerHandle's response and returns it on the STLS path.
 *
 * Because both the plain and STLS exposures call the SAME innerHandle,
 * the tool/resource/prompt surface can never drift between them.
 *
 * v0.1 limitation, documented not silent (same as python/stls-mcp): only
 * POST with a single JSON body is supported on MCP_STLS_PATH — no
 * chunk-by-chunk encrypted SSE streaming yet. Not an issue with the
 * recommended stateless/JSON-response configuration this package targets,
 * where every response is a single JSON block anyway.
 *
 * Port of python/stls-mcp/src/stls_mcp/stls_mcp_middleware.py.
 */

import { HEADER_SESSION_ID } from "stls-core";
import type { SessionCache, StlsEnvelopeDict } from "stls-core";

import { MCP_PLAIN_PATH } from "./constants.js";
import type { RequestHandler } from "./auth.js";

export class StlsMcpError extends Error {
  constructor(
    message: string,
    public readonly statusCode: number = 400,
  ) {
    super(message);
    this.name = "StlsMcpError";
  }
}

export interface EncryptedHandlerOptions {
  /**
   * Path the inner handler logically represents, used only to build a
   * sensible synthetic URL (innerHandle itself doesn't route on path —
   * see module docstring). Default MCP_PLAIN_PATH.
   */
  plainPath?: string;
}

function sendJsonError(statusCode: number, message: string): Response {
  const body = JSON.stringify({
    jsonrpc: "2.0",
    error: { code: statusCode < 500 ? -32600 : -32603, message },
    id: null,
  });
  return new Response(body, { status: statusCode, headers: { "content-type": "application/json" } });
}

export function encryptedHandler(
  innerHandle: RequestHandler,
  sessionCache: SessionCache,
  options: EncryptedHandlerOptions = {},
): RequestHandler {
  const plainPath = options.plainPath ?? MCP_PLAIN_PATH;

  return async (request: Request): Promise<Response> => {
    try {
      return await handle(request);
    } catch (exc) {
      if (exc instanceof StlsMcpError) {
        return sendJsonError(exc.statusCode, exc.message);
      }
      return sendJsonError(500, `Internal STLS-MCP error: ${(exc as Error).message}`);
    }
  };

  async function handle(request: Request): Promise<Response> {
    if (request.method !== "POST") {
      throw new StlsMcpError(
        "Only POST is supported on the encrypted MCP endpoint " +
          "(chunk-by-chunk encrypted SSE streaming is not yet supported).",
        405,
      );
    }

    const sessionId = request.headers.get(HEADER_SESSION_ID);
    if (!sessionId) {
      throw new StlsMcpError(
        `Missing header ${HEADER_SESSION_ID} — STLS session not established. ` +
          "Perform the handshake against /stls/handshake first.",
        401,
      );
    }

    const transport = await sessionCache.get(sessionId);
    if (!transport) {
      throw new StlsMcpError("Unknown or expired STLS session — repeat the handshake.", 401);
    }

    // ── 1. Read and decrypt the inbound body ──────────────────────────────
    let plaintextPayload: unknown;
    try {
      const envelope = (await request.json()) as StlsEnvelopeDict;
      plaintextPayload = transport.decryptJson(envelope);
    } catch (exc) {
      throw new StlsMcpError(`Decrypt failed: ${(exc as Error).message}`, 400);
    }
    const plaintextBytes = Buffer.from(JSON.stringify(plaintextPayload), "utf-8");

    // ── 2. Build a synthetic request pointing at the plain logical path ───
    const innerUrl = new URL(request.url);
    innerUrl.pathname = plainPath;
    const innerHeaders = new Headers(request.headers);
    innerHeaders.delete(HEADER_SESSION_ID); // STLS-only header, meaningless to innerHandle
    innerHeaders.set("content-length", String(plaintextBytes.length));

    const innerRequest = new Request(innerUrl, {
      method: "POST",
      headers: innerHeaders,
      body: plaintextBytes,
    });

    // ── 3. Invoke the SAME request logic the plain endpoint uses ──────────
    const innerResponse = await innerHandle(innerRequest);

    // ── 4. Encrypt the response and return it on the STLS path ────────────
    // HTTP-level errors (e.g. the SDK's own 400/406/415 for a malformed
    // request, not wrapped in StlsMcpError) are returned as-is, in
    // plaintext — the client (transport.ts) assumes exactly this for any
    // status >= 400 and skips decryption. Only a genuine application
    // response (status < 400; a JSON-RPC error *inside* a 200 is still
    // encrypted normally) goes through the encrypt step below.
    if (innerResponse.status >= 400) {
      return innerResponse;
    }

    let responsePayload: unknown;
    try {
      const text = await innerResponse.text();
      responsePayload = text ? JSON.parse(text) : {};
    } catch (exc) {
      throw new StlsMcpError(`Error reading the inner response: ${(exc as Error).message}`, 500);
    }

    let responseEnvelope: unknown;
    try {
      responseEnvelope = transport.encryptJson(responsePayload);
    } catch (exc) {
      throw new StlsMcpError(`Error encrypting the response: ${(exc as Error).message}`, 500);
    }

    const responseBytes = Buffer.from(JSON.stringify(responseEnvelope), "utf-8");
    return new Response(responseBytes, {
      status: innerResponse.status,
      headers: {
        "content-type": "application/json",
        "content-length": String(responseBytes.length),
      },
    });
  }
}
