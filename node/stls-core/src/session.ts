/**
 * session.ts
 * ----------
 * Port of python/stls-core/src/stls_core/stls_core_session.py.
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
}

/**
 * In-process LRU+TTL cache. Does not survive process restarts or
 * multi-instance scale-out — equivalent to InMemorySessionCache on the
 * Python side.
 */
export class InMemorySessionCache implements SessionCache {
  private readonly maxSize: number;
  private readonly ttlMs: number;
  private readonly store = new Map<string, CacheEntry>();

  constructor(options: { maxSize?: number; ttlSeconds?: number } = {}) {
    this.maxSize =
      options.maxSize ?? Number(process.env.STLS_SESSION_CACHE_MAX ?? "256");
    this.ttlMs =
      1000 *
      (options.ttlSeconds ??
        Number(
          process.env.SESSION_CACHE_TTL ?? process.env.STLS_SESSION_TTL ?? "3600",
        ));
  }

  async put(
    sessionId: string,
    kid: string,
    sessionKey: Buffer,
    direction?: StlsDirection,
  ): Promise<void> {
    const now = Date.now();
    const transport = new StlsTransport(sessionKey, sessionId, direction);

    if (this.store.has(sessionId)) {
      this.store.delete(sessionId);
      this.store.set(sessionId, { transport, kid, lastAccessMs: now });
      return;
    }

    if (this.store.size >= this.maxSize) {
      const oldestKey = this.store.keys().next().value;
      if (oldestKey !== undefined) {
        this.store.delete(oldestKey);
      }
    }

    this.store.set(sessionId, { transport, kid, lastAccessMs: now });
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

    // LRU refresh + sliding TTL: re-insert at the end (Map preserves
    // insertion order, equivalent to OrderedDict.move_to_end()).
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
      if (now - entry.lastAccessMs > this.ttlMs) {
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

    if (backend === "redis") {
      const { RedisSessionCache } = await import("./session-redis.js");
      const url = process.env.SESSION_CACHE_REDIS_URL ?? "redis://localhost:6379";
      const prefix = process.env.SESSION_CACHE_REDIS_NAMESPACE ?? "stls:sc";
      const ttlSeconds = Number(
        process.env.SESSION_CACHE_TTL ?? process.env.STLS_SESSION_TTL ?? "3600",
      );
      return RedisSessionCache.connect({ url, prefix, ttlSeconds });
    }

    if (backend === "memory") {
      return new InMemorySessionCache();
    }

    throw new Error(
      `SESSION_CACHE_BACKEND='${backend}' not recognized. Supported values: 'memory' (default), 'redis'.`,
    );
  }
}
