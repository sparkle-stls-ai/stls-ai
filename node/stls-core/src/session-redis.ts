/**
 * session-redis.ts
 * ----------------
 * Port of the RedisSessionCache backend from stls_core_session.py.
 *
 * Redis layout identical to the Python side, to stay interoperable on a
 * Redis instance shared between Python and Node clients/servers:
 *
 *   {prefix}:{session_id}  ->  JSON string   TTL = ttlSeconds (idle, sliding)
 *
 *   JSON:
 *     {
 *       "session_key": "<base64(32 bytes)>",
 *       "kid":         "<kid>",
 *       "created_at":  <unix timestamp float>,
 *       "direction":   "c2s" | "s2c" (omitted for the single-shared-key mode)
 *     }
 *
 * "direction" is optional on read: a payload written before it existed,
 * or by a peer that never sets it, is read back as undefined — the
 * correct default (single shared key for both directions).
 *
 * The TTL is sliding (renewed on every get() via GETEX), same as Python.
 */

import { createClient, type RedisClientType } from "redis";
import { StlsDirection, StlsTransport } from "./crypto.js";
import type { SessionCache } from "./session.js";

interface RedisSessionPayload {
  session_key: string;
  kid: string;
  created_at: number;
  direction?: StlsDirection;
}

export class RedisSessionCache implements SessionCache {
  private constructor(
    private readonly client: RedisClientType,
    private readonly prefix: string,
    private readonly ttlSeconds: number,
  ) {}

  static async connect(options: {
    url?: string;
    prefix?: string;
    ttlSeconds?: number;
  } = {}): Promise<RedisSessionCache> {
    const client: RedisClientType = createClient({
      url: options.url ?? "redis://localhost:6379",
    });
    await client.connect();
    return new RedisSessionCache(
      client,
      options.prefix ?? "stls:sc",
      options.ttlSeconds ?? 3600,
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
    const payload: RedisSessionPayload = {
      session_key: sessionKey.toString("base64"),
      kid,
      created_at: Date.now() / 1000,
      ...(direction !== undefined ? { direction } : {}),
    };
    await this.client.set(this.key(sessionId), JSON.stringify(payload), {
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

    try {
      const data = JSON.parse(raw) as RedisSessionPayload;
      const sessionKey = Buffer.from(data.session_key, "base64");
      return new StlsTransport(sessionKey, sessionId, data.direction);
    } catch {
      return undefined;
    }
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
