/**
 * handshake-pqc.ts
 * ----------------
 * Port of python/stls-core/src/stls_core/stls_core_handshake_pqc.py.
 *
 * STLS handshake with hybrid X25519 + ML-KEM-768 key agreement. A
 * dedicated endpoint (distinct from the standard handshake in
 * handshake.ts), active when KEY_PROVIDER_NAME=pqc. See the diagram in
 * the Python module for the full client/server flow.
 */

import { createHmac, randomBytes, randomUUID, timingSafeEqual } from "node:crypto";
import { STLS_VERSION } from "./constants.js";
import type { StlsDirection } from "./crypto.js";
import type { SessionCache } from "./session.js";
import {
  deriveHybridKey,
  mlkemDecap,
  mlkemEncap,
  x25519Exchange,
  x25519GenerateKeypair,
} from "./providers/pqc.js";

// ---------------------------------------------------------------------------
// Wire format
// ---------------------------------------------------------------------------

export interface PQCHandshakeRequest {
  client_id: string;
  /** 32 random bytes, base64 — HMAC proof + HKDF salt. Single-use. */
  nonce: string;
  /** Client's X25519 public key, 32 bytes base64. */
  x25519_public_key: string;
  /** Client's ML-KEM-768 public key, base64. */
  mlkem_public_key: string;
  protocol: string;
}

export function generatePqcHandshakeRequest(
  clientId: string,
  x25519PublicKeyBytes: Buffer,
  mlkemPublicKeyBytes: Buffer,
): PQCHandshakeRequest {
  return {
    client_id: clientId,
    nonce: randomBytes(32).toString("base64"),
    x25519_public_key: x25519PublicKeyBytes.toString("base64"),
    mlkem_public_key: mlkemPublicKeyBytes.toString("base64"),
    protocol: STLS_VERSION,
  };
}

export interface PQCHandshakeResponse {
  session_id: string;
  kid: string;
  /** HMAC-SHA256(session_key, nonce), base64. */
  proof: string;
  /** Server's X25519 public key, 32 bytes base64. */
  server_x25519_public_key: string;
  /** ML-KEM-768 ciphertext, base64. */
  mlkem_ciphertext: string;
  protocol: string;
  encryption: string;
  hmac: string;
  key_derivation: string;
}

export class PQCHandshakeError extends Error {
  constructor(
    message: string,
    public readonly code: number = -32603,
  ) {
    super(message);
    this.name = "PQCHandshakeError";
  }
}

// ---------------------------------------------------------------------------
// Server-side: performPqcHandshake()
// ---------------------------------------------------------------------------

/**
 * Unlike performHandshake(), this does not require a KeySessionProvider:
 * the session_key is derived locally via the KEM, with no external service.
 */
export async function performPqcHandshake(
  request: PQCHandshakeRequest,
  sessionCache: SessionCache,
  direction?: StlsDirection,
): Promise<PQCHandshakeResponse> {
  let nonce: Buffer;
  try {
    nonce = Buffer.from(request.nonce, "base64");
    if (nonce.length < 16) {
      throw new PQCHandshakeError(`Nonce too short: ${nonce.length} bytes (minimum 16)`, -32600);
    }
  } catch (exc) {
    if (exc instanceof PQCHandshakeError) throw exc;
    throw new PQCHandshakeError(`Nonce is not valid base64: ${(exc as Error).message}`, -32600);
  }

  let clientX25519Pub: Buffer;
  try {
    clientX25519Pub = Buffer.from(request.x25519_public_key, "base64");
    if (clientX25519Pub.length !== 32) {
      throw new PQCHandshakeError(
        `x25519_public_key must be 32 bytes, got ${clientX25519Pub.length}`,
        -32600,
      );
    }
  } catch (exc) {
    if (exc instanceof PQCHandshakeError) throw exc;
    throw new PQCHandshakeError(
      `x25519_public_key is not valid base64: ${(exc as Error).message}`,
      -32600,
    );
  }

  let clientMlkemPub: Buffer;
  try {
    clientMlkemPub = Buffer.from(request.mlkem_public_key, "base64");
  } catch (exc) {
    throw new PQCHandshakeError(
      `mlkem_public_key is not valid base64: ${(exc as Error).message}`,
      -32600,
    );
  }

  const sessionId = randomUUID();
  const kid = randomUUID(); // PQC kid = fresh UUID, distinct per session

  let serverX25519Priv: Buffer, serverX25519Pub: Buffer;
  try {
    ({ secretKey: serverX25519Priv, publicKey: serverX25519Pub } = x25519GenerateKeypair());
  } catch (exc) {
    throw new PQCHandshakeError(`Error generating the X25519 keypair: ${(exc as Error).message}`);
  }

  let x25519Secret: Buffer;
  try {
    x25519Secret = x25519Exchange(serverX25519Priv, clientX25519Pub);
  } catch (exc) {
    throw new PQCHandshakeError(`X25519 DH exchange failed: ${(exc as Error).message}`);
  }

  let mlkemCiphertext: Buffer, mlkemSecret: Buffer;
  try {
    ({ ciphertext: mlkemCiphertext, sharedSecret: mlkemSecret } = mlkemEncap(clientMlkemPub));
  } catch (exc) {
    throw new PQCHandshakeError(`ML-KEM encapsulation failed: ${(exc as Error).message}`);
  }

  let sessionKey: Buffer;
  try {
    sessionKey = deriveHybridKey(x25519Secret, mlkemSecret, nonce);
  } catch (exc) {
    throw new PQCHandshakeError(`HKDF derivation failed: ${(exc as Error).message}`);
  }

  const proof = createHmac("sha256", sessionKey).update(nonce).digest("base64");

  try {
    await sessionCache.put(sessionId, kid, sessionKey, direction);
  } catch (exc) {
    throw new PQCHandshakeError(
      `Error registering the session in the cache: ${(exc as Error).message}`,
    );
  }

  return {
    session_id: sessionId,
    kid,
    proof,
    server_x25519_public_key: serverX25519Pub.toString("base64"),
    mlkem_ciphertext: mlkemCiphertext.toString("base64"),
    protocol: STLS_VERSION,
    encryption: "AES-256-GCM",
    hmac: "HMAC-SHA256",
    key_derivation: "HKDF-SHA256+X25519+ML-KEM-768",
  };
}

// ---------------------------------------------------------------------------
// Client-side: deriveSessionKeyClient() + verifyPqcHandshakeProof()
// ---------------------------------------------------------------------------

export function deriveSessionKeyClient(
  clientX25519PrivateKeyBytes: Buffer,
  clientMlkemSecretKeyBytes: Buffer,
  response: PQCHandshakeResponse,
  nonce: string,
): Buffer {
  const nonceBytes = Buffer.from(nonce, "base64");
  const serverX25519Pub = Buffer.from(response.server_x25519_public_key, "base64");
  const mlkemCt = Buffer.from(response.mlkem_ciphertext, "base64");

  const x25519Secret = x25519Exchange(clientX25519PrivateKeyBytes, serverX25519Pub);
  const mlkemSecret = mlkemDecap(clientMlkemSecretKeyBytes, mlkemCt);

  return deriveHybridKey(x25519Secret, mlkemSecret, nonceBytes);
}

export function verifyPqcHandshakeProof(
  sessionKey: Buffer,
  nonce: string,
  response: PQCHandshakeResponse,
): boolean {
  const nonceBytes = Buffer.from(nonce, "base64");
  const expected = createHmac("sha256", sessionKey).update(nonceBytes).digest();
  const received = Buffer.from(response.proof, "base64");
  if (expected.length !== received.length) {
    return false;
  }
  return timingSafeEqual(expected, received);
}
