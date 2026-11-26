/**
 * credential-cache.ts
 * --------------------
 * 1:1 port of python/stls-identity/src/stls_identity/stls_id_credential_cache.py.
 *
 * Two caches for credentials an agent itself holds — same trust
 * perimeter reasoning as keystore.ts: whoever can already read an
 * agent's private key is already inside the right perimeter to also
 * read its own credentials, which travel publicly with every call
 * anyway.
 *
 * - IdentityCredentialCache: an agent's OWN current Identity Credential
 *   — a single slot (an agent has at most one at a time).
 * - OperationalCredentialCache: Operational Credentials an agent has
 *   RECEIVED via delegation — a COLLECTION (an agent can hold several
 *   at once, from different Grantors, all simultaneously valid).
 */

import { createClient, type RedisClientType } from "redis";
import type { NetworkAuthorityCredential, OperationalCredential } from "./models.js";

function ttlSecondsFrom(validUntil: string | null | undefined): number | null {
  if (!validUntil) {
    return null;
  }
  const ttl = Math.floor((new Date(validUntil).getTime() - Date.now()) / 1000);
  return ttl > 0 ? ttl : null;
}

// ---------------------------------------------------------------------------
// IdentityCredentialCache — single slot per agent
// ---------------------------------------------------------------------------

export interface IdentityCredentialCacheBase {
  /** Returns the cached credential, or null if absent/expired. */
  get(agentId: string): Promise<NetworkAuthorityCredential | null>;
  /**
   * Caches the credential with a TTL matching its own validUntil.
   * Silently a no-op if the credential has no validUntil or is
   * already expired — nothing safe to cache either way.
   */
  set(agentId: string, credential: NetworkAuthorityCredential): Promise<void>;
  /**
   * Removes whatever is cached for agentId, if anything — e.g. after a
   * revocation, so a stale cached credential doesn't keep looking
   * valid. A no-op if nothing was cached.
   */
  delete(agentId: string): Promise<void>;
}

/** Dev/test only — does not survive a process restart. */
export class InMemoryIdentityCredentialCache implements IdentityCredentialCacheBase {
  private readonly store = new Map<string, { credential: NetworkAuthorityCredential; expiresAt: number | null }>();

  async get(agentId: string): Promise<NetworkAuthorityCredential | null> {
    const entry = this.store.get(agentId);
    if (entry === undefined) {
      return null;
    }
    if (entry.expiresAt !== null && Date.now() > entry.expiresAt) {
      this.store.delete(agentId);
      return null;
    }
    return entry.credential;
  }

  async set(agentId: string, credential: NetworkAuthorityCredential): Promise<void> {
    const expiresAt = credential.validUntil ? new Date(credential.validUntil).getTime() : null;
    if (expiresAt !== null && Date.now() > expiresAt) {
      return;
    }
    this.store.set(agentId, { credential, expiresAt });
  }

  async delete(agentId: string): Promise<void> {
    this.store.delete(agentId);
  }
}

/**
 * Persistent — namespace isolated from the signing-key keystore and
 * from stls-core's own caches.
 */
export class RedisIdentityCredentialCache implements IdentityCredentialCacheBase {
  private client: RedisClientType | null = null;
  private readonly url: string;
  private readonly namespace: string;

  constructor(options: { url?: string; namespace?: string } = {}) {
    this.url = options.url ?? "redis://localhost:6379";
    this.namespace = options.namespace ?? "stls:identity:credential";
  }

  private key(agentId: string): string {
    return `${this.namespace}:${agentId}`;
  }

  private async ensureConnected(): Promise<RedisClientType> {
    if (this.client === null) {
      const client: RedisClientType = createClient({ url: this.url });
      await client.connect();
      this.client = client;
    }
    return this.client;
  }

  async get(agentId: string): Promise<NetworkAuthorityCredential | null> {
    const client = await this.ensureConnected();
    const raw = await client.get(this.key(agentId));
    return raw === null ? null : (JSON.parse(raw) as NetworkAuthorityCredential);
  }

  async set(agentId: string, credential: NetworkAuthorityCredential): Promise<void> {
    const ttl = ttlSecondsFrom(credential.validUntil);
    if (ttl === null) {
      return;
    }
    const client = await this.ensureConnected();
    await client.set(this.key(agentId), JSON.stringify(credential), { EX: ttl });
  }

  async delete(agentId: string): Promise<void> {
    const client = await this.ensureConnected();
    await client.del(this.key(agentId));
  }

  async close(): Promise<void> {
    if (this.client !== null) {
      await this.client.quit();
      this.client = null;
    }
  }
}

export class IdentityCredentialCacheFactory {
  /** Mirrors IdentityKeystoreFactory.build(). */
  static build(): IdentityCredentialCacheBase {
    const backend = (process.env.IDENTITY_CREDENTIAL_CACHE_BACKEND ?? "memory").trim().toLowerCase();

    if (backend === "redis") {
      // STATUS_LIST_REDIS_URL fallback: existing deployments already
      // point every identity-adjacent cache at the same Redis, no
      // second URL needed just to repeat the same value.
      const url =
        process.env.IDENTITY_CREDENTIAL_CACHE_REDIS_URL ??
        process.env.STATUS_LIST_REDIS_URL ??
        "redis://localhost:6379";
      const namespace =
        process.env.IDENTITY_CREDENTIAL_CACHE_REDIS_NAMESPACE ?? "stls:identity:credential";
      return new RedisIdentityCredentialCache({ url, namespace });
    }

    if (backend === "memory") {
      return new InMemoryIdentityCredentialCache();
    }

    throw new Error(
      `IDENTITY_CREDENTIAL_CACHE_BACKEND='${backend}' not recognized. ` +
        "Supported values: 'memory' (default), 'redis'.",
    );
  }
}

// ---------------------------------------------------------------------------
// OperationalCredentialCache — a collection per agent
// ---------------------------------------------------------------------------

export interface OperationalCredentialCacheBase {
  /** Returns the cached credential, or null if absent/expired. */
  get(agentId: string, credentialId: string): Promise<OperationalCredential | null>;
  /**
   * Caches the credential with a TTL matching its own validUntil.
   * Silently a no-op if the credential has no validUntil or is
   * already expired.
   */
  set(agentId: string, credential: OperationalCredential): Promise<void>;
  /**
   * Every currently-valid (not expired) cached credential id for this
   * agent. Self-cleaning: expired entries are dropped as a side effect.
   */
  listIds(agentId: string): Promise<string[]>;
  /** Removes one specific cached credential, if present. A no-op if not cached. */
  delete(agentId: string, credentialId: string): Promise<void>;
}

/** Dev/test only — does not survive a process restart. */
export class InMemoryOperationalCredentialCache implements OperationalCredentialCacheBase {
  private readonly store = new Map<
    string,
    Map<string, { credential: OperationalCredential; expiresAt: number }>
  >();

  async get(agentId: string, credentialId: string): Promise<OperationalCredential | null> {
    const bucket = this.store.get(agentId);
    const entry = bucket?.get(credentialId);
    if (entry === undefined) {
      return null;
    }
    if (Date.now() > entry.expiresAt) {
      bucket!.delete(credentialId);
      return null;
    }
    return entry.credential;
  }

  async set(agentId: string, credential: OperationalCredential): Promise<void> {
    if (!credential.validUntil) {
      return;
    }
    const expiresAt = new Date(credential.validUntil).getTime();
    if (Date.now() > expiresAt) {
      return;
    }
    if (!this.store.has(agentId)) {
      this.store.set(agentId, new Map());
    }
    this.store.get(agentId)!.set(credential.id, { credential, expiresAt });
  }

  async listIds(agentId: string): Promise<string[]> {
    const bucket = this.store.get(agentId);
    if (!bucket) {
      return [];
    }
    const now = Date.now();
    for (const [cid, entry] of bucket) {
      if (now > entry.expiresAt) {
        bucket.delete(cid);
      }
    }
    return [...bucket.keys()].sort();
  }

  async delete(agentId: string, credentialId: string): Promise<void> {
    this.store.get(agentId)?.delete(credentialId);
  }
}

/**
 * Persistent. The index is a sorted set (score = validUntil epoch
 * seconds), same self-cleaning pattern status.ts uses elsewhere in
 * this codebase.
 */
export class RedisOperationalCredentialCache implements OperationalCredentialCacheBase {
  private client: RedisClientType | null = null;
  private readonly url: string;
  private readonly namespace: string;

  constructor(options: { url?: string; namespace?: string } = {}) {
    this.url = options.url ?? "redis://localhost:6379";
    this.namespace = options.namespace ?? "stls:identity:opcred";
  }

  private key(agentId: string, credentialId: string): string {
    return `${this.namespace}:${agentId}:${credentialId}`;
  }

  private indexKey(agentId: string): string {
    return `${this.namespace}_index:${agentId}`;
  }

  private async ensureConnected(): Promise<RedisClientType> {
    if (this.client === null) {
      const client: RedisClientType = createClient({ url: this.url });
      await client.connect();
      this.client = client;
    }
    return this.client;
  }

  async get(agentId: string, credentialId: string): Promise<OperationalCredential | null> {
    const client = await this.ensureConnected();
    const raw = await client.get(this.key(agentId, credentialId));
    return raw === null ? null : (JSON.parse(raw) as OperationalCredential);
  }

  async set(agentId: string, credential: OperationalCredential): Promise<void> {
    if (!credential.validUntil) {
      return;
    }
    const ttl = ttlSecondsFrom(credential.validUntil);
    if (ttl === null) {
      return;
    }
    const client = await this.ensureConnected();
    await client.set(this.key(agentId, credential.id), JSON.stringify(credential), { EX: ttl });
    await client.zAdd(this.indexKey(agentId), {
      score: new Date(credential.validUntil).getTime() / 1000,
      value: credential.id,
    });
  }

  async listIds(agentId: string): Promise<string[]> {
    const client = await this.ensureConnected();
    const indexKey = this.indexKey(agentId);
    const nowEpoch = Date.now() / 1000;
    await client.zRemRangeByScore(indexKey, -Infinity, nowEpoch);
    return client.zRange(indexKey, 0, -1);
  }

  async delete(agentId: string, credentialId: string): Promise<void> {
    const client = await this.ensureConnected();
    await client.del(this.key(agentId, credentialId));
    await client.zRem(this.indexKey(agentId), credentialId);
  }

  async close(): Promise<void> {
    if (this.client !== null) {
      await this.client.quit();
      this.client = null;
    }
  }
}

export class OperationalCredentialCacheFactory {
  /** Mirrors IdentityCredentialCacheFactory.build(). */
  static build(): OperationalCredentialCacheBase {
    const backend = (process.env.OPERATIONAL_CREDENTIAL_CACHE_BACKEND ?? "memory").trim().toLowerCase();

    if (backend === "redis") {
      const url =
        process.env.OPERATIONAL_CREDENTIAL_CACHE_REDIS_URL ??
        process.env.STATUS_LIST_REDIS_URL ??
        "redis://localhost:6379";
      const namespace =
        process.env.OPERATIONAL_CREDENTIAL_CACHE_REDIS_NAMESPACE ?? "stls:identity:opcred";
      return new RedisOperationalCredentialCache({ url, namespace });
    }

    if (backend === "memory") {
      return new InMemoryOperationalCredentialCache();
    }

    throw new Error(
      `OPERATIONAL_CREDENTIAL_CACHE_BACKEND='${backend}' not recognized. ` +
        "Supported values: 'memory' (default), 'redis'.",
    );
  }
}
