/**
 * session-redis.ts
 * ----------------
 * Port of the RedisSessionCache backend from stls_core_session.py.
 *
 * Redis layout identical to the Python side, to stay interoperable on a
 * Redis instance shared between Python and Node clients/servers:
 *
 *   {prefix}:{session_id}  ->  JSON string, optionally AES-256-GCM-sealed
 *                              as a whole (see below)
 *                              TTL = ttlSeconds (idle, sliding)
 *
 *   JSON:
 *     {
 *       "enc_aes_key":  "<base64>",
 *       "dec_aes_key":  "<base64>",
 *       "enc_hmac_key": "<base64>",
 *       "dec_hmac_key": "<base64>",
 *       "kid":          "<kid>",
 *       "created_at":   <unix timestamp float>,
 *       "direction":    "c2s" | "s2c" (omitted for the single-shared-key mode)
 *     }
 *
 * Only the already-HKDF-derived AES/HMAC keys are stored — the raw
 * session key passed to put() is never persisted. See issue #20.
 *
 * "direction" is optional on read: a payload written before it existed,
 * or by a peer that never sets it, is read back as undefined — the
 * correct default (single shared key for both directions).
 *
 * If STLS_CORE_ATREST_KEY is set, this JSON is AES-256-GCM-sealed as a
 * whole (AtRestCipher, see atrest.ts) before SET and opened after GET —
 * independent of Redis's own at-rest settings. If unset, the JSON above
 * is stored in the clear, as before encryption-at-rest existed (a
 * warning is logged once, at connect()). See issue #20.
 *
 * The TTL is sliding (renewed on every get() via GETEX), same as Python.
 *
 * created_at also drives the max-age check (SESSION_KEY_MAX_AGE_SECONDS,
 * see session.ts and issue #6) when enabled: every instance in a
 * multi-replica deployment reads the same shared record, so no
 * cross-instance coordination is needed for this check.
 */

import { createClient, type RedisClientType } from "redis";
import { AtRestCipher } from "./atrest.js";
import { StlsDirection, StlsTransport, type DerivedKeys } from "./crypto.js";
import type { SessionCache } from "./session.js";

interface RedisSessionPayload {
  enc_aes_key: string;
  dec_aes_key: string;
  enc_hmac_key: string;
  dec_hmac_key: string;
  kid: string;
  created_at: number;
  direction?: StlsDirection;
}

export class RedisSessionCache implements SessionCache {
  private constructor(
    private readonly client: RedisClientType,
    private readonly prefix: string,
    private readonly ttlSeconds: number,
    /**
     * Absolute key-rotation max-age, in seconds. 0 (default, from an
     * unset or <=0 env var/option) disables the check entirely — no
     * behavior change from before this existed. See issue #6.
     */
    private readonly maxAgeSeconds: number = 0,
    /** Encryption-at-rest cipher, or undefined when STLS_CORE_ATREST_KEY is unset. See issue #20. */
    private readonly cipher?: AtRestCipher,
  ) {}

  static async connect(options: {
    url?: string;
    prefix?: string;
    ttlSeconds?: number;
    maxAgeSeconds?: number;
  } = {}): Promise<RedisSessionCache> {
    const client: RedisClientType = createClient({
      url: options.url ?? "redis://localhost:6379",
    });
    await client.connect();

    const cipher = AtRestCipher.fromEnv("stls-core/session-cache");
    if (cipher === undefined) {
      console.warn(
        "RedisSessionCache: STLS_CORE_ATREST_KEY is not set — session " +
          "cache entries are stored in plaintext (see issue #20).",
      );
    }

    return new RedisSessionCache(
      client,
      options.prefix ?? "stls:sc",
      options.ttlSeconds ?? 3600,
      options.maxAgeSeconds ?? 0,
      cipher,
    );
  }

  /** Closes the Redis connection. Call on process shutdown. */
  async close(): Promise<void> {
    await this.client.quit();
  }

  private key(sessionId: string): string {
    return `${this.prefix}:${sessionId}`;
  }

  async put(
    sessionId: string,
    kid: string,
    sessionKey: Buffer,
    direction?: StlsDirection,
  ): Promise<void> {
    // Derives the AES/HMAC keys once and discards sessionKey — only the
    // derived keys below are ever written to Redis (issue #20).
    const derived = StlsTransport.deriveKeys(sessionKey, direction);
    const payload: RedisSessionPayload = {
      enc_aes_key: derived.encAesKey.toString("base64"),
      dec_aes_key: derived.decAesKey.toString("base64"),
      enc_hmac_key: derived.encHmacKey.toString("base64"),
      dec_hmac_key: derived.decHmacKey.toString("base64"),
      kid,
      created_at: Date.now() / 1000,
      ...(direction !== undefined ? { direction } : {}),
    };
    const plaintext = Buffer.from(JSON.stringify(payload), "utf-8");
    const value = this.cipher !== undefined
      ? this.cipher.seal(plaintext).toString("base64")
      : plaintext.toString("utf-8");

    await this.client.set(this.key(sessionId), value, {
      expiration: { type: "EX", value: this.ttlSeconds },
    });
  }

  async get(sessionId: string): Promise<StlsTransport | undefined> {
    // GETEX: returns the value AND renews the TTL (sliding idle timeout).
    const raw = await this.client.getEx(this.key(sessionId), {
      type: "EX",
      value: this.ttlSeconds,
    });
    if (raw === null) {
      return undefined;
    }

    let data: RedisSessionPayload;
    try {
      const plaintext = this.cipher !== undefined
        ? this.cipher.open(Buffer.from(raw, "base64"))
        : Buffer.from(raw, "utf-8");
      data = JSON.parse(plaintext.toString("utf-8")) as RedisSessionPayload;
    } catch {
      return undefined;
    }

    if (this.maxAgeSeconds > 0) {
      const age = data.created_at !== undefined ? Date.now() / 1000 - data.created_at : 0;
      if (age > this.maxAgeSeconds) {
        await this.client.del(this.key(sessionId));
        return undefined;
      }
    }

    const derived: DerivedKeys = {
      encAesKey: Buffer.from(data.enc_aes_key, "base64"),
      decAesKey: Buffer.from(data.dec_aes_key, "base64"),
      encHmacKey: Buffer.from(data.enc_hmac_key, "base64"),
      decHmacKey: Buffer.from(data.dec_hmac_key, "base64"),
    };
    return StlsTransport.fromDerivedKeys(derived, sessionId, data.direction);
  }

  async remove(sessionId: string): Promise<boolean> {
    const deleted = await this.client.del(this.key(sessionId));
    return deleted > 0;
  }

  async purgeExpired(): Promise<number> {
    // Redis handles eviction automatically via TTL — no-op, same as Python.
    return 0;
  }

  async size(): Promise<number> {
    // KEYS is O(N) — acceptable for diagnostics, not for the hot path (same as Python).
    const keys = await this.client.keys(`${this.prefix}:*`);
    return keys.length;
  }

  async has(sessionId: string): Promise<boolean> {
    const exists = await this.client.exists(this.key(sessionId));
    return exists > 0;
  }
}
