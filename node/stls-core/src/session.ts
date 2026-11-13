/**
 * session.ts
 * ----------
 * Port of python/stls-core/src/stls_core/stls_core_session.py.
 *
 * Environment variable SESSION_KEY_MAX_AGE_SECONDS (both backends): absolute
 * key-rotation max-age in seconds. Unset or <=0 (default): disabled, no
 * behavior change from before this existed. >0: get() also expires a
 * session once its AGE (since creation, not since last access) exceeds
 * this value, on top of, not instead of, the idle TTL. Server-side only,
 * never client-settable — see issue #6.
 *
 * Design note — why SessionCache is async here but sync in Python:
 * the Redis client used on the Python side (redis-py) is
 * synchronous/blocking, so SessionCacheBase can have sync methods even
 * for the Redis backend. There is no mature synchronous Redis client for
 * Node: every Redis backend is Promise-based. To have a single
 * SessionCache interface valid for both InMemorySessionCache and
 * RedisSessionCache, all methods are async — including
 * InMemorySessionCache, where the implementation stays synchronous
 * internally and the Promise resolves immediately.
 */

import { StlsDirection, StlsTransport } from "./crypto.js";

export interface StlsSession {
  sessionId: string;
  kid: string;
  transport: StlsTransport;
  createdAt: Date;
  peerIdentity?: string | null;
  ageSeconds(): number;
}

export function createStlsSession(
  sessionId: string,
  kid: string,
  transport: StlsTransport,
  peerIdentity: string | null = null,
): StlsSession {
  const createdAt = new Date();
  return {
    sessionId,
    kid,
    transport,
    createdAt,
    peerIdentity,
    ageSeconds: () => (Date.now() - createdAt.getTime()) / 1000,
  };
}

export interface SessionCache {
  /**
   * Registers a new STLS session after a completed handshake.
   * `direction` is optional and additive: leave it unset for the
   * existing single-shared-key mode (used by stls-mcp today); pass
   * "c2s"/"s2c" only for callers that label the two ends of a session
   * themselves — see StlsTransport's own docstring.
   */
  put(sessionId: string, kid: string, sessionKey: Buffer, direction?: StlsDirection): Promise<void>;
  /** Retrieves the transport for an active session, renewing the idle TTL. */
  get(sessionId: string): Promise<StlsTransport | undefined>;
  /** Explicitly removes a session. Returns true if it existed. */
  remove(sessionId: string): Promise<boolean>;
  /** Removes expired sessions. Returns the number removed (no-op on Redis: native TTL). */
  purgeExpired(): Promise<number>;
  size(): Promise<number>;
  has(sessionId: string): Promise<boolean>;
}

interface CacheEntry {
  transport: StlsTransport;
  kid: string;
  lastAccessMs: number;
  /**
   * Set once, at first insertion, and never refreshed by get()/put()-on-
   * update — unlike lastAccessMs, which slides. Drives the max-age check
   * below. Both clocks use Date.now(), consistent with the rest of this
   * class.
   */
  createdAtMs: number;
}

/**
 * In-process LRU+TTL cache. Does not survive process restarts or
 * multi-instance scale-out — equivalent to InMemorySessionCache on the
 * Python side.
 */
export class InMemorySessionCache implements SessionCache {
  private readonly maxSize: number;
  private readonly ttlMs: number;
  /**
   * Absolute key-rotation max-age, in ms. 0 (default, from an unset or
   * <=0 env var) disables the check entirely — no behavior change from
   * before this existed. See issue #6.
   */
  private readonly maxAgeMs: number;
  private readonly store = new Map<string, CacheEntry>();

  constructor(options: { maxSize?: number; ttlSeconds?: number; maxAgeSeconds?: number } = {}) {
    this.maxSize =
      options.maxSize ?? Number(process.env.STLS_SESSION_CACHE_MAX ?? "256");
    this.ttlMs =
      1000 *
      (options.ttlSeconds ??
        Number(
          process.env.SESSION_CACHE_TTL ?? process.env.STLS_SESSION_TTL ?? "3600",
        ));
    this.maxAgeMs =
      1000 *
      (options.maxAgeSeconds ?? Number(process.env.SESSION_KEY_MAX_AGE_SECONDS ?? "0"));
  }

  async put(
    sessionId: string,
    kid: string,
    sessionKey: Buffer,
    direction?: StlsDirection,
  ): Promise<void> {
    const now = Date.now();
    const transport = new StlsTransport(sessionKey, sessionId, direction);

    const existing = this.store.get(sessionId);
    if (existing !== undefined) {
      // Preserves the original createdAtMs — a put() on an already known
      // sessionId is a redundant/idempotent re-registration, not a brand
      // new session, so its max-age clock must not reset.
      this.store.delete(sessionId);
      this.store.set(sessionId, { transport, kid, lastAccessMs: now, createdAtMs: existing.createdAtMs });
      return;
    }

    if (this.store.size >= this.maxSize) {
      const oldestKey = this.store.keys().next().value;
      if (oldestKey !== undefined) {
        this.store.delete(oldestKey);
      }
    }

    this.store.set(sessionId, { transport, kid, lastAccessMs: now, createdAtMs: now });
  }

  async get(sessionId: string): Promise<StlsTransport | undefined> {
    const entry = this.store.get(sessionId);
    if (entry === undefined) {
      return undefined;
    }

    const now = Date.now();
    if (now - entry.lastAccessMs > this.ttlMs) {
      this.store.delete(sessionId);
      return undefined;
    }

    if (this.maxAgeMs > 0 && now - entry.createdAtMs > this.maxAgeMs) {
      this.store.delete(sessionId);
      return undefined;
    }

    // LRU refresh + sliding TTL: re-insert at the end (Map preserves
    // insertion order, equivalent to OrderedDict.move_to_end()).
    // createdAtMs is NOT refreshed here.
    this.store.delete(sessionId);
    entry.lastAccessMs = now;
    this.store.set(sessionId, entry);
    return entry.transport;
  }

  async remove(sessionId: string): Promise<boolean> {
    return this.store.delete(sessionId);
  }

  async purgeExpired(): Promise<number> {
    const now = Date.now();
    let removed = 0;
    for (const [sessionId, entry] of this.store) {
      if (
        now - entry.lastAccessMs > this.ttlMs ||
        (this.maxAgeMs > 0 && now - entry.createdAtMs > this.maxAgeMs)
      ) {
        this.store.delete(sessionId);
        removed++;
      }
    }
    return removed;
  }

  async size(): Promise<number> {
    return this.store.size;
  }

  async has(sessionId: string): Promise<boolean> {
    return this.store.has(sessionId);
  }
}

export { RedisSessionCache } from "./session-redis.js";

export class SessionCacheFactory {
  /**
   * Builds the SessionCache backend based on SESSION_CACHE_BACKEND.
   * Async (unlike the Python SessionCacheFactory.build(), which is sync)
   * because the Redis backend must connect before use.
   */
  static async build(): Promise<SessionCache> {
    const backend = (process.env.SESSION_CACHE_BACKEND ?? "memory").trim().toLowerCase();
    const maxAgeSeconds = Number(process.env.SESSION_KEY_MAX_AGE_SECONDS ?? "0");

    if (backend === "redis") {
      const { RedisSessionCache } = await import("./session-redis.js");
      const url = process.env.SESSION_CACHE_REDIS_URL ?? "redis://localhost:6379";
      const prefix = process.env.SESSION_CACHE_REDIS_NAMESPACE ?? "stls:sc";
      const ttlSeconds = Number(
        process.env.SESSION_CACHE_TTL ?? process.env.STLS_SESSION_TTL ?? "3600",
      );
      return RedisSessionCache.connect({ url, prefix, ttlSeconds, maxAgeSeconds });
    }

    if (backend === "memory") {
      return new InMemorySessionCache({ maxAgeSeconds });
    }

    throw new Error(
      `SESSION_CACHE_BACKEND='${backend}' not recognized. Supported values: 'memory' (default), 'redis'.`,
    );
  }
}
