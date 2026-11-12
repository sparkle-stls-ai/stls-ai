/**
 * transport.ts
 * ------------
 * Client-side: handshake helpers and a custom `fetch` that transparently
 * encrypts/decrypts requests to the STLS path, for use with
 * StreamableHTTPClientTransport's `fetch` option.
 *
 * Usage flow:
 *   1. One-time STLS handshake against /stls/handshake (or the PQC
 *      variant), to obtain session_id + session_key. See
 *      performClientHandshake()/performPqcClientHandshake().
 *   2. Build the encrypted fetch with createStlsFetch().
 *   3. new StreamableHTTPClientTransport(url, { fetch: stlsFetch }) — the
 *      rest of the MCP client stack (Client, session management, SSE) has
 *      no idea it's talking to an encrypted endpoint.
 *
 * Interception happens at the `fetch` level, below the MCP application
 * layer — exactly the same design as python/stls-mcp's StlsHttpxTransport,
 * just as a plain function instead of a transport class, because the
 * TypeScript SDK's extension point (`fetch?: FetchLike` on
 * StreamableHTTPClientTransportOptions) is itself a plain function, not a
 * pluggable transport object like httpx's.
 *
 * Port of python/stls-mcp/src/stls_mcp/stls_mcp_transport.py.
 */

import {
  HEADER_SESSION_ID,
  StlsEnvelopeDict,
  StlsTransport,
} from "stls-core";
import type { FetchLike } from "@modelcontextprotocol/sdk/shared/transport.js";

import { MCP_STLS_PATH } from "./constants.js";

// performClientHandshake(), performPqcClientHandshake() and
// StlsHandshakeFailed moved to stls-core (client-handshake.ts) as part
// of issue #5 ("Restrict stls-core's public API surface") — getTransport()
// there needs to own resolve+handshake end to end so it never has to hand
// raw key material back to its caller. Re-exported here unchanged: stls-mcp's
// own public API (names, signatures, README samples) is unaffected.
export {
  StlsHandshakeFailed,
  performClientHandshake,
  performPqcClientHandshake,
} from "stls-core";
export type {
  StlsClientSession,
  PerformClientHandshakeOptions,
  PerformPqcClientHandshakeOptions,
} from "stls-core";

// ---------------------------------------------------------------------------
// createStlsFetch() — the FetchLike used by StreamableHTTPClientTransport
// ---------------------------------------------------------------------------

export interface CreateStlsFetchOptions {
  /** Path to intercept and encrypt (default MCP_STLS_PATH). Requests to any other path pass through unmodified. */
  stlsPath?: string;
  /** Underlying fetch to actually perform the (now encrypted) HTTP request. Defaults to the global fetch. */
  fetch?: FetchLike;
}

async function requestBodyToText(body: string | Uint8Array | null | undefined): Promise<string> {
  if (body === null || body === undefined) return "";
  if (typeof body === "string") return body;
  if (body instanceof Uint8Array) return Buffer.from(body).toString("utf-8");
  throw new Error(`Unsupported request body type for STLS encryption: ${Object.prototype.toString.call(body)}`);
}

/**
 * Builds a `fetch`-compatible function that transparently encrypts/decrypts
 * requests to the configured STLS path, leaving everything else unchanged
 * (including GET requests used for the SSE long-poll stream).
 *
 * Usage:
 *   const { sessionId, transport } = await performClientHandshake(baseUrl, sessionKey, { clientId });
 *   const stlsFetch = createStlsFetch(sessionId, transport);
 *   const client = new Client(...);
 *   const httpTransport = new StreamableHTTPClientTransport(new URL(`${baseUrl}/mcp-stls`), { fetch: stlsFetch });
 *   await client.connect(httpTransport);
 */
export function createStlsFetch(
  sessionId: string,
  transport: StlsTransport,
  options: CreateStlsFetchOptions = {},
): FetchLike {
  const stlsPath = options.stlsPath ?? MCP_STLS_PATH;
  const innerFetch = options.fetch ?? globalThis.fetch;

  return async (url: string | URL, init?: RequestInit): Promise<Response> => {
    const targetUrl = url instanceof URL ? url : new URL(url);
    const method = (init?.method ?? "GET").toUpperCase();

    if (targetUrl.pathname !== stlsPath || method !== "POST") {
      // Pass-through for anything not aimed at the STLS path (e.g. GET for
      // SSE, or requests to other endpoints).
      return innerFetch(url, init);
    }

    // ── Encrypt the outgoing body ─────────────────────────────────────────
    let plaintextPayload: unknown;
    try {
      const bodyText = await requestBodyToText(init?.body as string | Uint8Array | null | undefined);
      plaintextPayload = JSON.parse(bodyText);
    } catch (exc) {
      throw new Error(`STLS encryption error (request body): ${(exc as Error).message}`);
    }
    const envelope = transport.encryptJson(plaintextPayload);
    const encryptedBody = JSON.stringify(envelope);

    // Native MCP headers (mcp-session-id, accept, etc.) are preserved; we
    // only add the STLS session header and fix up content-length.
    const headers = new Headers(init?.headers);
    headers.set(HEADER_SESSION_ID, sessionId);
    headers.set("content-length", String(Buffer.byteLength(encryptedBody)));

    const response = await innerFetch(url, { ...init, headers, body: encryptedBody });

    // Error responses (4xx/5xx) are always sent in plaintext server-side
    // (see middleware.ts) — never try to decrypt them.
    if (response.status >= 400) {
      return response;
    }

    // ── Decrypt the incoming body ─────────────────────────────────────────
    let plaintextResponse: unknown;
    try {
      const responseEnvelope = (await response.json()) as StlsEnvelopeDict;
      plaintextResponse = transport.decryptJson(responseEnvelope);
    } catch (exc) {
      throw new Error(`STLS decryption error (response): ${(exc as Error).message}`);
    }

    const plaintextBody = JSON.stringify(plaintextResponse);
    const outHeaders = new Headers(response.headers);
    outHeaders.delete("content-length");
    outHeaders.delete("content-encoding");
    outHeaders.set("content-length", String(Buffer.byteLength(plaintextBody)));

    return new Response(plaintextBody, {
      status: response.status,
      statusText: response.statusText,
      headers: outHeaders,
    });
  };
}
