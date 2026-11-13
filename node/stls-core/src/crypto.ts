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
  HKDF_INFO_AES_C2S,
  HKDF_INFO_AES_S2C,
  HKDF_INFO_HMAC,
  HKDF_INFO_HMAC_C2S,
  HKDF_INFO_HMAC_S2C,
  STLS_VERSION,
} from "./constants.js";

export type StlsDirection = "c2s" | "s2c";

/**
 * The four keys a StlsTransport actually uses, already HKDF-derived from
 * a session key. Letting a caller hold onto these instead of the raw
 * session key — and reconstruct a StlsTransport from them later via
 * StlsTransport.fromDerivedKeys() — means a store sitting between
 * "derive" and "use" (e.g. a Redis-backed session cache) never needs to
 * persist the raw session key at all. See issue #20.
 */
export interface DerivedKeys {
  encAesKey: Buffer;
  decAesKey: Buffer;
  encHmacKey: Buffer;
  decHmacKey: Buffer;
}

// ---------------------------------------------------------------------------
// HKDF-Expand (RFC 5869 §2.3) — PRK passed in directly, no extract step.
// ---------------------------------------------------------------------------

export function hkdfExpand(prk: Buffer, info: Buffer, length: number): Buffer {
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
 *
 * Direction (`direction` ctor arg, optional): by default (undefined) both
 * peers derive a single AES/HMAC key pair used for both encrypt() and
 * decrypt() — the mode used by stls-mcp today, where the same transport
 * object serves both directions of one session. Passing "c2s" or "s2c"
 * instead derives four keys and splits them per direction, so the two
 * directions of a session never share key material; the two peers must
 * agree out of band on who is "c2s" and who is "s2c". See the Python
 * counterpart's docstring for the full rationale.
 */
export class StlsTransport {
  private readonly encAesKey: Buffer;
  private readonly decAesKey: Buffer;
  private readonly encHmacKey: Buffer;
  private readonly decHmacKey: Buffer;

  /**
   * @param sessionKeyOrDerived Either the raw session key (derived
   *   internally via deriveKeys()), or an already-derived DerivedKeys —
   *   see fromDerivedKeys() and issue #20.
   */
  constructor(
    sessionKeyOrDerived: Buffer | DerivedKeys,
    public readonly sessionId: string,
    public readonly direction?: StlsDirection,
  ) {
    if (direction !== undefined && direction !== "c2s" && direction !== "s2c") {
      throw new Error(`Invalid direction: ${direction} (expected "c2s", "s2c" or undefined)`);
    }

    const derived = Buffer.isBuffer(sessionKeyOrDerived)
      ? StlsTransport.deriveKeys(sessionKeyOrDerived, direction)
      : sessionKeyOrDerived;
    this.encAesKey = derived.encAesKey;
    this.decAesKey = derived.decAesKey;
    this.encHmacKey = derived.encHmacKey;
    this.decHmacKey = derived.decHmacKey;
  }

  /**
   * Reconstructs a StlsTransport from already-derived keys (e.g. as
   * returned by deriveKeys()), bypassing HKDF derivation from a raw
   * session key. See issue #20.
   */
  static fromDerivedKeys(
    keys: DerivedKeys,
    sessionId: string,
    direction?: StlsDirection,
  ): StlsTransport {
    return new StlsTransport(keys, sessionId, direction);
  }

  /**
   * Derives the four AES/HMAC keys from a raw session key via
   * HKDF-Expand, without building a full StlsTransport.
   *
   * Exposed so a caller can derive once, discard the raw session key,
   * and persist/transport only the derived keys — reconstructing the
   * transport later via fromDerivedKeys(). See issue #20.
   */
  static deriveKeys(sessionKey: Buffer, direction?: StlsDirection): DerivedKeys {
    if (direction === undefined) {
      const aesKey = hkdfExpand(sessionKey, HKDF_INFO_AES, 32);
      const hmacKey = hkdfExpand(sessionKey, HKDF_INFO_HMAC, 32);
      return { encAesKey: aesKey, decAesKey: aesKey, encHmacKey: hmacKey, decHmacKey: hmacKey };
    }

    const aesC2s = hkdfExpand(sessionKey, HKDF_INFO_AES_C2S, 32);
    const aesS2c = hkdfExpand(sessionKey, HKDF_INFO_AES_S2C, 32);
    const hmacC2s = hkdfExpand(sessionKey, HKDF_INFO_HMAC_C2S, 32);
    const hmacS2c = hkdfExpand(sessionKey, HKDF_INFO_HMAC_S2C, 32);
    if (direction === "c2s") {
      return { encAesKey: aesC2s, decAesKey: aesS2c, encHmacKey: hmacC2s, decHmacKey: hmacS2c };
    }
    return { encAesKey: aesS2c, decAesKey: aesC2s, encHmacKey: hmacS2c, decHmacKey: hmacC2s };
  }

  encrypt(plaintext: Buffer): StlsEnvelope {
    const iv = randomBytes(12); // 96-bit for AES-GCM
    const aad = buildAad(STLS_VERSION, this.sessionId);

    const cipher = createCipheriv("aes-256-gcm", this.encAesKey, iv);
    cipher.setAAD(aad);
    const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
    const tag = cipher.getAuthTag(); // 128-bit GCM authentication tag

    const mac = this.computeMac(this.encHmacKey, aad, iv, ciphertext, tag);

    return new StlsEnvelope(this.sessionId, iv, ciphertext, tag, mac, STLS_VERSION);
  }

  decrypt(envelope: StlsEnvelope): Buffer {
    if (envelope.version !== STLS_VERSION) {
      throw new Error(
        `Unsupported envelope version: ${envelope.version} (expected ${STLS_VERSION})`,
      );
    }

    if (envelope.sessionId !== this.sessionId) {
      throw new Error(
        `Session ID mismatch: expected ${this.sessionId}, got ${envelope.sessionId}`,
      );
    }

    const aad = buildAad(envelope.version, envelope.sessionId);

    const expectedMac = this.computeMac(
      this.decHmacKey, aad, envelope.iv, envelope.ciphertext, envelope.tag,
    );
    if (!constantTimeEqual(expectedMac, envelope.mac)) {
      throw new Error("MAC verification failed — message integrity compromised");
    }

    const decipher = createDecipheriv("aes-256-gcm", this.decAesKey, envelope.iv);
    decipher.setAAD(aad);
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

  private computeMac(
    hmacKey: Buffer, aad: Buffer, iv: Buffer, ciphertext: Buffer, tag: Buffer,
  ): Buffer {
    return createHmac("sha256", hmacKey)
      .update(aad)
      .update(iv)
      .update(ciphertext)
      .update(tag)
      .digest();
  }
}

/**
 * Self-delimiting AAD from (version, session_id) — length-prefixed fields
 * so no two distinct (version, session_id) pairs ever concatenate to the
 * same bytes. Must stay byte-compatible with the Python counterpart
 * (StlsTransport._build_aad).
 */
function buildAad(version: string, sessionId: string): Buffer {
  const versionBytes = Buffer.from(version, "utf-8");
  const sessionIdBytes = Buffer.from(sessionId, "utf-8");
  const versionLen = Buffer.from([versionBytes.length]);
  const sessionIdLen = Buffer.alloc(2);
  sessionIdLen.writeUInt16BE(sessionIdBytes.length);
  return Buffer.concat([versionLen, versionBytes, sessionIdLen, sessionIdBytes]);
}

function constantTimeEqual(a: Buffer, b: Buffer): boolean {
  if (a.length !== b.length) {
    return false;
  }
  return timingSafeEqual(a, b);
}
