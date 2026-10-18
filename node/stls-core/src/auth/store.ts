/**
 * auth/store.ts
 * -------------
 * Port of python/stls-core/src/stls_core/auth/stls_core_auth_store.py.
 *
 * Redis key layout identical to the Python side (configurable namespace):
 *   {ns}:key:{key_id}   -> JSON(KeyRecord), TTL = expiresAt or AUTH_KEY_TTL
 *   {ns}:revoked        -> SET of revoked key_ids
 *   {ns}:keys:index     -> SET of all known key_ids
 */

import { createClient, type RedisClientType } from "redis";
import { DEFAULT_KEY_TTL_SECONDS, KeyRecord } from "./models.js";

const DEFAULT_NAMESPACE = "stls";

export class RedisKeyCache {
  private client: RedisClientType | null = null;
  private readonly url: string;
  private readonly keyTtl: number;
  private readonly namespace: string;
  private readonly keyPrefix: string;
  private readonly revokedSet: string;
  private readonly indexSet: string;

  constructor(
    options: {
      url?: string;
      keyTtl?: number;
      namespace?: string;
    } = {},
  ) {
    this.url = options.url ?? process.env.REDIS_URL ?? "redis://localhost:6379";
    this.keyTtl =
      options.keyTtl ?? Number(process.env.AUTH_KEY_TTL ?? DEFAULT_KEY_TTL_SECONDS);
    this.namespace = options.namespace ?? process.env.AUTH_REDIS_NS ?? DEFAULT_NAMESPACE;
    this.keyPrefix = `${this.namespace}:key:`;
    this.revokedSet = `${this.namespace}:revoked`;
    this.indexSet = `${this.namespace}:keys:index`;
  }

  // ---- Lifecycle ----------------------------------------------------------

  async initialize(): Promise<void> {
    if (this.client !== null) return;
    const client: RedisClientType = createClient({ url: this.url });
    await client.connect();
    await client.ping();
    this.client = client;
  }

  async close(): Promise<void> {
    if (this.client !== null) {
      await this.client.quit();
      this.client = null;
    }
  }

  private async ensureConnected(): Promise<RedisClientType> {
    if (this.client === null) {
      await this.initialize();
    }
    return this.client!;
  }

  // ---- Single-record operations --------------------------------------------------

  async get(keyId: string): Promise<KeyRecord | null> {
    const client = await this.ensureConnected();
    try {
      const data = await client.get(`${this.keyPrefix}${keyId}`);
      if (data === null) return null;
      return KeyRecord.fromDict(JSON.parse(data));
    } catch {
      return null;
    }
  }

  async set(record: KeyRecord): Promise<void> {
    const client = await this.ensureConnected();
    const ttl = this.computeTtl(record);
    if (ttl <= 0) return; // already expired: do not write to the cache

    try {
      const multi = client.multi();
      multi.setEx(`${this.keyPrefix}${record.keyId}`, ttl, JSON.stringify(record.toDict()));
      multi.sAdd(this.indexSet, record.keyId);
      await multi.exec();
    } catch {
      // best-effort, same as the Python side (log and ignore)
    }
  }

  async delete(keyId: string): Promise<void> {
    const client = await this.ensureConnected();
    try {
      const multi = client.multi();
      multi.del(`${this.keyPrefix}${keyId}`);
      multi.sRem(this.indexSet, keyId);
      await multi.exec();
    } catch {
      // best-effort
    }
  }

  // ---- Revocation -----------------------------------------------------------

  async revoke(keyId: string): Promise<void> {
    const client = await this.ensureConnected();
    try {
      const multi = client.multi();
      multi.sAdd(this.revokedSet, keyId);
      multi.del(`${this.keyPrefix}${keyId}`);
      multi.sRem(this.indexSet, keyId);
      await multi.exec();
    } catch {
      // best-effort
    }
  }

  async isRevoked(keyId: string): Promise<boolean> {
    const client = await this.ensureConnected();
    try {
      return (await client.sIsMember(this.revokedSet, keyId)) === 1;
    } catch {
      // fail-open: do not block the service if Redis errors out
      return false;
    }
  }

  async unrevoke(keyId: string): Promise<void> {
    const client = await this.ensureConnected();
    try {
      await client.sRem(this.revokedSet, keyId);
    } catch {
      // best-effort
    }
  }

  // ---- Bulk operations (used by BackgroundRefresher) ------------------------

  async bulkReload(records: KeyRecord[]): Promise<number> {
    const client = await this.ensureConnected();
    if (records.length === 0) return 0;

    const newIds = new Set(records.map((r) => r.keyId));

    try {
      const oldIds = new Set(await client.sMembers(this.indexSet));

      const multi = client.multi();
      for (const record of records) {
        const ttl = this.computeTtl(record);
        if (ttl <= 0) continue;
        multi.setEx(`${this.keyPrefix}${record.keyId}`, ttl, JSON.stringify(record.toDict()));
        multi.sAdd(this.indexSet, record.keyId);
      }

      const staleIds = [...oldIds].filter((id) => !newIds.has(id));
      for (const staleId of staleIds) {
        multi.del(`${this.keyPrefix}${staleId}`);
        multi.sRem(this.indexSet, staleId);
      }

      await multi.exec();
      return records.length;
    } catch {
      return 0;
    }
  }

  async listAllIds(): Promise<Set<string>> {
    const client = await this.ensureConnected();
    try {
      return new Set(await client.sMembers(this.indexSet));
    } catch {
      return new Set();
    }
  }

  async listRevokedIds(): Promise<Set<string>> {
    const client = await this.ensureConnected();
    try {
      return new Set(await client.sMembers(this.revokedSet));
    } catch {
      return new Set();
    }
  }

  async flushAllKeys(): Promise<void> {
    const client = await this.ensureConnected();
    try {
      const ids = await client.sMembers(this.indexSet);
      const multi = client.multi();
      for (const id of ids) {
        multi.del(`${this.keyPrefix}${id}`);
      }
      multi.del(this.indexSet);
      await multi.exec();
    } catch {
      // best-effort
    }
  }

  // ---- Diagnostics ------------------------------------------------------------

  async ping(): Promise<boolean> {
    try {
      const client = await this.ensureConnected();
      return (await client.ping()) === "PONG";
    } catch {
      return false;
    }
  }

  async stats(): Promise<Record<string, unknown>> {
    await this.ensureConnected();
    const cached = (await this.listAllIds()).size;
    const revoked = (await this.listRevokedIds()).size;
    return {
      cached_keys: cached,
      revoked_keys: revoked,
      redis_url: this.sanitizeUrl(this.url),
      key_ttl: this.keyTtl,
      namespace: this.namespace,
    };
  }

  // ---- Distributed lock (used by BackgroundRefresher) -----------------------

  /** SET {lockKey} {nodeId} NX EX {ttlSeconds} — atomic, no race condition. */
  async tryAcquireLock(lockKey: string, nodeId: string, ttlSeconds: number): Promise<boolean> {
    const client = await this.ensureConnected();
    try {
      const result = await client.set(lockKey, nodeId, {
        condition: "NX",
        expiration: { type: "EX", value: ttlSeconds },
      });
      return result !== null;
    } catch {
      // fail-open: proceed without the lock if Redis is unreachable
      return true;
    }
  }

  /** Releases the lock only if still held by this node (atomic check + delete via Lua). */
  async releaseLock(lockKey: string, nodeId: string): Promise<void> {
    const client = await this.ensureConnected();
    const luaScript = `
      if redis.call('get', KEYS[1]) == ARGV[1] then
        return redis.call('del', KEYS[1])
      else
        return 0
      end
    `;
    try {
      await client.eval(luaScript, { keys: [lockKey], arguments: [nodeId] });
    } catch {
      // best-effort
    }
  }

  // ---- Internal helpers ----------------------------------------------------

  private computeTtl(record: KeyRecord): number {
    if (record.expiresAt === null) return this.keyTtl;
    const remaining = Math.floor((record.expiresAt.getTime() - Date.now()) / 1000);
    return Math.min(remaining, this.keyTtl);
  }

  private sanitizeUrl(url: string): string {
    try {
      const parsed = new URL(url);
      if (parsed.password) {
        parsed.password = "***";
      }
      return parsed.toString();
    } catch {
      return url;
    }
  }
}
