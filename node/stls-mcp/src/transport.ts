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
  HANDSHAKE_PATH,
  HANDSHAKE_PQC_PATH,
  HandshakeResponse,
  HEADER_SESSION_ID,
  KeyProviderClientPQC,
  KeySessionProviderPQC,
  PQCHandshakeResponse,
  StlsEnvelopeDict,
  StlsTransport,
  deriveSessionKeyClient,
  generateHandshakeRequest,
  generatePqcHandshakeRequest,
  verifyHandshakeProof,
  verifyPqcHandshakeProof,
} from "stls-core";
import type { FetchLike } from "@modelcontextprotocol/sdk/shared/transport.js";

import { MCP_STLS_PATH } from "./constants.js";

export class StlsHandshakeFailed extends Error {
  constructor(message: string) {
    super(message);
    this.name = "StlsHandshakeFailed";
  }
}

export interface StlsClientSession {
  sessionId: string;
  transport: StlsTransport;
}

// ---------------------------------------------------------------------------
// performClientHandshake() — standard (base / arqit_py)
// ---------------------------------------------------------------------------

export interface PerformClientHandshakeOptions {
  clientId: string;
  /**
   * Optional kid the caller already resolved alongside sessionKey
   * (e.g. via KeySessionProvider.getSessionKeyIdentifier() for
   * arqit_py) — passed through as a hint so the server resolves THIS
   * exact key directly for arqit_py, instead of a plain "next
   * available" lookup, which is not safe once more than one handshake
   * can be in flight concurrently against the same long-running
   * server-side provider instance. Leave unset for base (nothing to
   * disambiguate) or when unavailable.
   */
  kid?: string;
  handshakePath?: string;
  fetch?: FetchLike;
}

/**
 * Performs the STLS handshake against the server and returns a ready
 * session. The session key is never transmitted: the handshake only
 * proves mutual possession via an HMAC proof.
 *
 * @param baseUrl Server base URL (e.g. "https://my-server.example.com").
 * @param sessionKey Session key the client already holds (resolved from
 *   its own KeySessionProvider — base or arqit_py — exactly like the
 *   server side).
 * @throws StlsHandshakeFailed if the HTTP handshake fails, or if the
 *   proof returned by the server doesn't match the client's session key
 *   (the server doesn't hold the same key — possible attack or
 *   misconfiguration).
 */
export async function performClientHandshake(
  baseUrl: string,
  sessionKey: Buffer,
  options: PerformClientHandshakeOptions,
): Promise<StlsClientSession> {
  const handshakePath = options.handshakePath ?? HANDSHAKE_PATH;
  const doFetch = options.fetch ?? globalThis.fetch;
  const request = generateHandshakeRequest(options.clientId, options.kid);
  const url = baseUrl.replace(/\/$/, "") + handshakePath;

  let response: Response;
  try {
    response = await doFetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(request),
    });
  } catch (exc) {
    throw new StlsHandshakeFailed(`Handshake HTTP request failed against ${url}: ${(exc as Error).message}`);
  }

  if (!response.ok) {
    throw new StlsHandshakeFailed(
      `Handshake rejected by the server (status=${response.status}): ${await response.text()}`,
    );
  }

  let handshakeResponse: HandshakeResponse;
  try {
    handshakeResponse = (await response.json()) as HandshakeResponse;
  } catch (exc) {
    throw new StlsHandshakeFailed(`Invalid handshake response: ${(exc as Error).message}`);
  }

  if (!verifyHandshakeProof(sessionKey, request.nonce, handshakeResponse)) {
    throw new StlsHandshakeFailed(
      "Proof of possession invalid — the server does not appear to hold the same " +
        "session key as the client. Handshake rejected for safety.",
    );
  }

  const transport = new StlsTransport(sessionKey, handshakeResponse.session_id);
  return { sessionId: handshakeResponse.session_id, transport };
}

// ---------------------------------------------------------------------------
// performPqcClientHandshake() — X25519 + ML-KEM-768
// ---------------------------------------------------------------------------

export interface PerformPqcClientHandshakeOptions {
  clientId: string;
  handshakePath?: string;
  fetch?: FetchLike;
}

/**
 * Performs the PQC STLS handshake against the server. Unlike
 * performClientHandshake(), no pre-shared session key is needed: keypairs
 * are generated client-side, sent to the server, and the session key is
 * derived locally via the KEM exchange (X25519 DH + ML-KEM-768 decap).
 */
export async function performPqcClientHandshake(
  baseUrl: string,
  options: PerformPqcClientHandshakeOptions,
): Promise<StlsClientSession> {
  const handshakePath = options.handshakePath ?? HANDSHAKE_PQC_PATH;
  const doFetch = options.fetch ?? globalThis.fetch;

  // 1. Generate X25519 + ML-KEM-768 keypairs client-side.
  const pqcClient = new KeyProviderClientPQC();
  pqcClient.start();

  try {
    // 2. Build the request with the client's public keys.
    const request = generatePqcHandshakeRequest(options.clientId, pqcClient.x25519PublicKey, pqcClient.mlkemPublicKey);
    const nonce = request.nonce;

    // 3. POST to the server.
    const url = baseUrl.replace(/\/$/, "") + handshakePath;
    let response: Response;
    try {
      response = await doFetch(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(request),
      });
    } catch (exc) {
      throw new StlsHandshakeFailed(`PQC handshake HTTP request failed against ${url}: ${(exc as Error).message}`);
    }

    if (!response.ok) {
      throw new StlsHandshakeFailed(
        `PQC handshake rejected by the server (status=${response.status}): ${await response.text()}`,
      );
    }

    let pqcResponse: PQCHandshakeResponse;
    try {
      pqcResponse = (await response.json()) as PQCHandshakeResponse;
    } catch (exc) {
      throw new StlsHandshakeFailed(`Invalid PQCHandshakeResponse: ${(exc as Error).message}`);
    }

    // 4. Derive the session key via the KEM (X25519 DH + ML-KEM decap, then HKDF).
    const session = pqcClient.createProviderSession() as KeySessionProviderPQC;
    let sessionKey: Buffer;
    try {
      sessionKey = deriveSessionKeyClient(
        session.x25519PrivateKeyBytes as Buffer,
        session.mlkemSecretKeyBytes as Buffer,
        pqcResponse,
        nonce,
      );
    } catch (exc) {
      throw new StlsHandshakeFailed(`PQC session_key derivation failed: ${(exc as Error).message}`);
    }

    // 5. Verify the server's proof — mutual proof of possession.
    if (!verifyPqcHandshakeProof(sessionKey, nonce, pqcResponse)) {
      throw new StlsHandshakeFailed(
        "PQC proof of possession invalid — the server did not derive the same " +
          "session_key. Possible attack or misconfiguration. Handshake rejected for safety.",
      );
    }

    const transport = new StlsTransport(sessionKey, pqcResponse.session_id);
    return { sessionId: pqcResponse.session_id, transport };
  } finally {
    pqcClient.stop(); // best-effort zeroing of private keys
  }
}

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
