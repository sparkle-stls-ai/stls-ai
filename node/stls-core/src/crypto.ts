/**
 * crypto.ts
 * ---------
 * 1:1 port of python/stls-core/src/stls_core/stls_core_crypto.py.
 *
 * AES-256-GCM for confidentiality/authentication, an additional
 * HMAC-SHA256 over the whole envelope, and HKDF-Expand (RFC 5869 §3.2,
 * expand only — no extract) to derive distinct AES and HMAC keys from
 * the session key.
 *
 * Compatibility note: Node's `crypto.hkdfSync` implements full HKDF
 * (extract+expand), which differs byte-for-byte from `cryptography`'s
 * `HKDFExpand` used on the Python side. The expand-only step is
 * implemented manually below to stay bit-compatible with envelopes
 * produced by stls-core Python.
 */

import {
  createCipheriv,
  createDecipheriv,
  createHmac,
  randomBytes,
  timingSafeEqual,
} from "node:crypto";

import {
  ENVELOPE_CIPHERTEXT,
  ENVELOPE_IV,
  ENVELOPE_MAC,
  ENVELOPE_SESSION_ID,
  ENVELOPE_TAG,
  ENVELOPE_VERSION,
  HKDF_INFO_AES,
  HKDF_INFO_HMAC,
  STLS_VERSION,
} from "./constants.js";

// ---------------------------------------------------------------------------
// HKDF-Expand (RFC 5869 §2.3) — PRK passed in directly, no extract step.
// ---------------------------------------------------------------------------

function hkdfExpand(prk: Buffer, info: Buffer, length: number): Buffer {
  const hashLen = 32; // SHA-256
  const n = Math.ceil(length / hashLen);
  if (n > 255) {
    throw new RangeError("hkdfExpand: requested length is too large");
  }

  let previousBlock = Buffer.alloc(0);
  const blocks: Buffer[] = [];
  for (let i = 1; i <= n; i++) {
    const hmac = createHmac("sha256", prk);
    hmac.update(previousBlock);
    hmac.update(info);
    hmac.update(Buffer.from([i]));
    previousBlock = hmac.digest();
    blocks.push(previousBlock);
  }

  return Buffer.concat(blocks, n * hashLen).subarray(0, length);
}

// ---------------------------------------------------------------------------
// StlsEnvelope — structure of the encrypted envelope
// ---------------------------------------------------------------------------

export interface StlsEnvelopeDict {
  version: string;
  session_id: string;
  iv: string;
  ciphertext: string;
  tag: string;
  mac: string;
}

/**
 * Wire format:
 * {
 *   "version":    "1.0",
 *   "session_id": "<uuid>",
 *   "iv":         "<base64 96-bit>",
 *   "ciphertext": "<base64>",
 *   "tag":        "<base64 128-bit GCM tag>",
 *   "mac":        "<base64 SHA-256 HMAC>"
 * }
 */
export class StlsEnvelope {
  constructor(
    public readonly sessionId: string,
    public readonly iv: Buffer,
    public readonly ciphertext: Buffer,
    public readonly tag: Buffer,
    public readonly mac: Buffer,
    public readonly version: string = STLS_VERSION,
  ) {}

  toDict(): StlsEnvelopeDict {
    return {
      [ENVELOPE_VERSION]: this.version,
      [ENVELOPE_SESSION_ID]: this.sessionId,
      [ENVELOPE_IV]: this.iv.toString("base64"),
      [ENVELOPE_CIPHERTEXT]: this.ciphertext.toString("base64"),
      [ENVELOPE_TAG]: this.tag.toString("base64"),
      [ENVELOPE_MAC]: this.mac.toString("base64"),
    } as StlsEnvelopeDict;
  }

  static fromDict(data: StlsEnvelopeDict): StlsEnvelope {
    return new StlsEnvelope(
      data[ENVELOPE_SESSION_ID],
      Buffer.from(data[ENVELOPE_IV], "base64"),
      Buffer.from(data[ENVELOPE_CIPHERTEXT], "base64"),
      Buffer.from(data[ENVELOPE_TAG], "base64"),
      Buffer.from(data[ENVELOPE_MAC], "base64"),
      data[ENVELOPE_VERSION] ?? STLS_VERSION,
    );
  }
}

// ---------------------------------------------------------------------------
// StlsTransport — encrypt / decrypt over plain Buffers
// ---------------------------------------------------------------------------

/**
 * STLS transport: encrypts and decrypts arbitrary payloads. Protocol-agnostic.
 * Stateless after construction — safe for concurrent calls.
 */
export class StlsTransport {
  private readonly aesKey: Buffer;
  private readonly hmacKey: Buffer;

  constructor(
    sessionKey: Buffer,
    public readonly sessionId: string,
  ) {
    this.aesKey = hkdfExpand(sessionKey, HKDF_INFO_AES, 32);
    this.hmacKey = hkdfExpand(sessionKey, HKDF_INFO_HMAC, 32);
  }

  encrypt(plaintext: Buffer): StlsEnvelope {
    const iv = randomBytes(12); // 96-bit for AES-GCM

    const cipher = createCipheriv("aes-256-gcm", this.aesKey, iv);
    const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
    const tag = cipher.getAuthTag(); // 128-bit GCM authentication tag

    const mac = this.computeMac(iv, ciphertext, tag);

    return new StlsEnvelope(this.sessionId, iv, ciphertext, tag, mac);
  }

  decrypt(envelope: StlsEnvelope): Buffer {
    if (envelope.sessionId !== this.sessionId) {
      throw new Error(
        `Session ID mismatch: expected ${this.sessionId}, got ${envelope.sessionId}`,
      );
    }

    const expectedMac = this.computeMac(envelope.iv, envelope.ciphertext, envelope.tag);
    if (!constantTimeEqual(expectedMac, envelope.mac)) {
      throw new Error("MAC verification failed — message integrity compromised");
    }

    const decipher = createDecipheriv("aes-256-gcm", this.aesKey, envelope.iv);
    decipher.setAuthTag(envelope.tag);
    try {
      return Buffer.concat([decipher.update(envelope.ciphertext), decipher.final()]);
    } catch (exc) {
      throw new Error(
        `AES-GCM decryption failed — invalid tag or corrupted data: ${(exc as Error).message}`,
      );
    }
  }

  encryptJson(payload: unknown): StlsEnvelopeDict {
    const plaintext = Buffer.from(JSON.stringify(payload), "utf-8");
    return this.encrypt(plaintext).toDict();
  }

  decryptJson(envelopeDict: StlsEnvelopeDict): unknown {
    const envelope = StlsEnvelope.fromDict(envelopeDict);
    const plaintext = this.decrypt(envelope);
    return JSON.parse(plaintext.toString("utf-8"));
  }

  private computeMac(iv: Buffer, ciphertext: Buffer, tag: Buffer): Buffer {
    return createHmac("sha256", this.hmacKey)
      .update(iv)
      .update(ciphertext)
      .update(tag)
      .digest();
  }
}

function constantTimeEqual(a: Buffer, b: Buffer): boolean {
  if (a.length !== b.length) {
    return false;
  }
  return timingSafeEqual(a, b);
}
