/**
 * client-handshake.ts
 * --------------------
 * Client-side STLS handshake: generic over the STLS protocol itself
 * (base/arqit_py pre-shared-key handshake, and the PQC KEM handshake),
 * with no knowledge of any particular application protocol (MCP, A2A,
 * ...) built on top of it — that's why this lives in stls-core rather
 * than stls-mcp, where it used to live (moved here as part of issue #5,
 * "Restrict stls-core's public API surface": getTransport(), in
 * providers/session-establish.ts, needs to own resolve+handshake end to
 * end so it never has to hand raw key material back to its caller).
 *
 * node/stls-mcp's transport.ts now just re-exports these two functions
 * for backward compatibility — its own public API is unchanged.
 *
 * Port of python/stls-core/src/stls_core/stls_core_client_handshake.py.
 */

import {
  HANDSHAKE_PATH,
  HANDSHAKE_PQC_PATH,
} from "./constants.js";
import { StlsDirection, StlsTransport } from "./crypto.js";
import { HandshakeResponse, generateHandshakeRequest, verifyHandshakeProof } from "./handshake.js";
import {
  PQCHandshakeResponse,
  generatePqcHandshakeRequest,
  verifyPqcHandshakeProof,
} from "./handshake-pqc.js";
import { KeyProviderClientPQC, KeySessionProviderPQC } from "./providers/pqc.js";

/** Structurally identical to @modelcontextprotocol/sdk's FetchLike — kept local so stls-core has no dependency on it. */
export type FetchLike = (url: string | URL, init?: RequestInit) => Promise<Response>;

export class StlsHandshakeFailed extends Error {
  constructor(message: string) {
    super(message);
    this.name = "StlsHandshakeFailed";
  }
}

export interface StlsClientSession {
  sessionId: string;
  /** Key identifier confirmed by the server's handshake response. */
  kid: string;
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
  /**
   * Optional: builds the resulting StlsTransport with this direction
   * instead of the default single-shared-key mode. Must match what the
   * server was given (opposite value) for the two to interoperate —
   * see StlsTransport's own docstring.
   */
  direction?: StlsDirection;
}

/**
 * Performs the STLS handshake against the server and returns a ready
 * session. The session key is never transmitted: the handshake only
 * proves mutual possession via an HMAC proof.
 *
 * Low-level primitive: the caller must already hold `sessionKey` (e.g.
 * resolved from its own KeySessionProvider). Most callers should use
 * getTransport() instead, which resolves the key AND performs this
 * handshake without ever handing the raw key back to ITS caller.
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

  const transport = new StlsTransport(sessionKey, handshakeResponse.session_id, options.direction);
  return { sessionId: handshakeResponse.session_id, kid: handshakeResponse.kid, transport };
}

// ---------------------------------------------------------------------------
// performPqcClientHandshake() — X25519 + ML-KEM-768
// ---------------------------------------------------------------------------

export interface PerformPqcClientHandshakeOptions {
  clientId: string;
  handshakePath?: string;
  fetch?: FetchLike;
  /** See PerformClientHandshakeOptions.direction. */
  direction?: StlsDirection;
}

/**
 * Performs the PQC STLS handshake against the server. Unlike
 * performClientHandshake(), no pre-shared session key is needed: keypairs
 * are generated client-side, sent to the server, and the session key is
 * derived locally via the KEM exchange (X25519 DH + ML-KEM-768 decap) —
 * entirely inside this function; the ephemeral KEM private key material
 * never leaves KeySessionProviderPQC (see its deriveSessionKey()).
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

    // 4. Derive the session key via the KEM (X25519 DH + ML-KEM decap, then HKDF) —
    //    entirely inside KeySessionProviderPQC, never touching its private fields directly.
    const session = pqcClient.createProviderSession() as KeySessionProviderPQC;
    let sessionKey: Buffer;
    try {
      sessionKey = session.deriveSessionKey(
        Buffer.from(pqcResponse.server_x25519_public_key, "base64"),
        Buffer.from(pqcResponse.mlkem_ciphertext, "base64"),
        Buffer.from(nonce, "base64"),
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

    const transport = new StlsTransport(sessionKey, pqcResponse.session_id, options.direction);
    return { sessionId: pqcResponse.session_id, kid: pqcResponse.kid, transport };
  } finally {
    pqcClient.stop(); // best-effort zeroing of private keys
  }
}
