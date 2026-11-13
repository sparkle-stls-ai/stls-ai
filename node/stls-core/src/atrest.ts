/**
 * atrest.ts
 * ---------
 * Port of python/stls-core/src/stls_core/stls_core_atrest.py.
 *
 * Encryption-at-rest for values stls-core persists on Redis: the
 * derived keys in RedisSessionCache's payload (RedisArqitKeyStore has
 * no Node port — Node always proxies Arqit through the Python sidecar).
 * See issue #20.
 *
 * AtRestCipher wraps a single value with AES-256-GCM under a key
 * derived (via the same expand-only HKDF used by crypto.ts, for
 * byte-compatibility with the Python port) from a locally-held wrapping
 * key (KEK) plus a per-component domain-separation label.
 *
 * Opt-in: if the KEK env var is unset, AtRestCipher.fromEnv() returns
 * undefined and callers fall back to storing the value in the clear
 * (logging a warning). If the env var IS set but malformed, fromEnv()
 * throws — a misconfigured KEK should fail loudly, unlike an absent one.
 */

import { createCipheriv, createDecipheriv, randomBytes } from "node:crypto";
import { hkdfExpand } from "./crypto.js";

// Fixed AAD, shared with the Python port — see that module's docstring.
const AAD = Buffer.from("stls-core/at-rest/v1");
const KEK_LENGTH = 32;
const NONCE_LENGTH = 12; // 96-bit, standard for AES-GCM
const TAG_LENGTH = 16; // 128-bit GCM tag

export class AtRestCipher {
  private readonly aesKey: Buffer;

  /**
   * @param kek   The wrapping key, exactly 32 bytes.
   * @param label Domain-separation label (e.g. "stls-core/session-cache")
   *              so the same KEK can be reused across independently
   *              sealed stores.
   */
  constructor(kek: Buffer, label: string) {
    if (kek.length !== KEK_LENGTH) {
      throw new Error(`AtRestCipher: kek must be ${KEK_LENGTH} bytes, got ${kek.length}`);
    }
    this.aesKey = hkdfExpand(kek, Buffer.from(label), KEK_LENGTH);
  }

  /**
   * Builds an AtRestCipher from a base64-encoded 32-byte KEK read from
   * the environment.
   *
   * Returns undefined if the env var is unset (caller decides the
   * plaintext-fallback-with-warning behavior from that).
   *
   * Throws if the env var is set but isn't valid base64, or doesn't
   * decode to exactly 32 bytes.
   */
  static fromEnv(label: string, varName = "STLS_CORE_ATREST_KEY"): AtRestCipher | undefined {
    const raw = process.env[varName];
    if (raw === undefined || raw === "") {
      return undefined;
    }

    const kek = Buffer.from(raw, "base64");
    if (kek.length !== KEK_LENGTH) {
      throw new Error(
        `${varName} decodes to ${kek.length} bytes, expected ${KEK_LENGTH} ` +
          `(must be base64-encoded ${KEK_LENGTH} random bytes, e.g. ` +
          `\`openssl rand -base64 ${KEK_LENGTH}\`)`,
      );
    }

    return new AtRestCipher(kek, label);
  }

  /**
   * Encrypts plaintext with AES-256-GCM under a fresh random nonce.
   *
   * Returns: nonce (12 bytes) || ciphertext || GCM tag (16 bytes) —
   * same layout as the Python port's seal().
   */
  seal(plaintext: Buffer): Buffer {
    const nonce = randomBytes(NONCE_LENGTH);
    const cipher = createCipheriv("aes-256-gcm", this.aesKey, nonce);
    cipher.setAAD(AAD);
    const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
    const tag = cipher.getAuthTag();
    return Buffer.concat([nonce, ciphertext, tag]);
  }

  /**
   * Decrypts a blob produced by seal() (Node or Python port).
   *
   * Throws if the blob is too short, the tag is invalid, or the data is
   * otherwise corrupted/tampered with.
   */
  open(blob: Buffer): Buffer {
    if (blob.length < NONCE_LENGTH + TAG_LENGTH) {
      throw new Error(`AtRestCipher.open: blob too short (${blob.length} bytes)`);
    }
    const nonce = blob.subarray(0, NONCE_LENGTH);
    const tag = blob.subarray(blob.length - TAG_LENGTH);
    const ciphertext = blob.subarray(NONCE_LENGTH, blob.length - TAG_LENGTH);

    const decipher = createDecipheriv("aes-256-gcm", this.aesKey, nonce);
    decipher.setAAD(AAD);
    decipher.setAuthTag(tag);
    try {
      return Buffer.concat([decipher.update(ciphertext), decipher.final()]);
    } catch (exc) {
      throw new Error(
        `AtRestCipher.open: AES-GCM decryption failed — invalid tag or corrupted data: ${(exc as Error).message}`,
      );
    }
  }
}
