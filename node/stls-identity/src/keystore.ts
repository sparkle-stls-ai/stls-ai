/**
 * keystore.ts
 * -----------
 * 1:1 port of python/stls-identity/src/stls_identity/stls_id_keystore.py.
 *
 * Persistent store for the Ed25519 *signing* keypair of an agent's DID.
 *
 * This is NOT stls-core's ArqitKeyStoreBase, deliberately: that store
 * is scoped to ephemeral SKA session-key material with an idle-TTL
 * lifecycle. A DID signing key is written once at bootstrap and read
 * forever after, until explicit rotation — no TTL, no eviction.
 *
 * Environment variables:
 *   IDENTITY_KEYSTORE_BACKEND          memory (default) | redis
 *   IDENTITY_KEYSTORE_REDIS_URL        redis://localhost:6379
 *   IDENTITY_KEYSTORE_REDIS_NAMESPACE  stls:identity:signingkey (prefix)
 *
 * Redis structure (backend=redis):
 *   {namespace}:{agentId}  ->  STRING, raw Ed25519 private key bytes
 *                               (32 bytes) if STLS_CORE_ATREST_KEY is
 *                               unset, or an AtRestCipher-sealed blob
 *                               (nonce || ciphertext || tag) if set.
 *                               No TTL — persistent by design.
 *
 * Encryption at rest (issue #49): opt-in via STLS_CORE_ATREST_KEY — the
 * SAME KEK node/stls-core's own RedisKeyCache already reads — reusing
 * stls-core's own AtRestCipher directly, not reinventing it, with its
 * own domain-separation label so the two stores never share a derived
 * key even on a shared KEK.
 */

import { createClient, type RedisClientType } from "redis";
import { ed25519 } from "@noble/curves/ed25519.js";
import { AtRestCipher } from "stls-core";

const ATREST_LABEL = "stls-identity/keystore";

export interface IdentityKeystoreBase {
  /** Returns the stored key, or null if this agent has never bootstrapped an identity yet. */
  get(agentId: string): Promise<Uint8Array | null>;
  /** Persists the key. No TTL, no expiry — overwritten only on explicit rotation. */
  put(agentId: string, privateKey: Uint8Array): Promise<void>;
  /**
   * Convenience used at agent bootstrap: returns { privateKey, created }.
   * created=true the first time an agent is seen, false on every
   * subsequent restart — so did.ts can decide whether to also
   * (re)publish the DID Document or just serve the existing one.
   */
  getOrCreate(agentId: string): Promise<{ privateKey: Uint8Array; created: boolean }>;
}

abstract class BaseIdentityKeystore implements IdentityKeystoreBase {
  abstract get(agentId: string): Promise<Uint8Array | null>;
  abstract put(agentId: string, privateKey: Uint8Array): Promise<void>;

  async getOrCreate(agentId: string): Promise<{ privateKey: Uint8Array; created: boolean }> {
    const existing = await this.get(agentId);
    if (existing !== null) {
      return { privateKey: existing, created: false };
    }
    const newKey = ed25519.keygen().secretKey;
    await this.put(agentId, newKey);
    return { privateKey: newKey, created: true };
  }
}

/** Dev/test only — does not survive a process restart. */
export class InMemoryIdentityKeystore extends BaseIdentityKeystore {
  private readonly store = new Map<string, Uint8Array>();

  async get(agentId: string): Promise<Uint8Array | null> {
    return this.store.get(agentId) ?? null;
  }

  async put(agentId: string, privateKey: Uint8Array): Promise<void> {
    this.store.set(agentId, privateKey);
  }
}

export interface RedisIdentityKeystoreOptions {
  url?: string;
  namespace?: string;
}

/** Persistent, namespace-isolated from both session caches and ephemeral Arqit keys. */
export class RedisIdentityKeystore extends BaseIdentityKeystore {
  private client: RedisClientType | null = null;
  private readonly url: string;
  private readonly namespace: string;
  private readonly cipher: AtRestCipher | undefined;
  private warnedPlaintext = false;

  constructor(options: RedisIdentityKeystoreOptions = {}) {
    super();
    this.url = options.url ?? "redis://localhost:6379";
    this.namespace = options.namespace ?? "stls:identity:signingkey";
    this.cipher = AtRestCipher.fromEnv(ATREST_LABEL);
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
    if (this.cipher === undefined && !this.warnedPlaintext) {
      console.warn(
        "[RedisIdentityKeystore] STLS_CORE_ATREST_KEY is not set — " +
          "signing private keys are stored in plaintext (see issue #49).",
      );
      this.warnedPlaintext = true;
    }
    return this.client;
  }

  async get(agentId: string): Promise<Uint8Array | null> {
    const client = await this.ensureConnected();
    // Stored base64-encoded (not raw bytes) — same convention
    // node/stls-core's own RedisSessionCache already uses for binary
    // values, since node-redis's plain string get/set is UTF-8 by
    // default and this module has no need to fight that for a value
    // that only ever round-trips through this same Node process (the
    // signing key never crosses the wire — see this file's porting
    // note on cross-runtime Redis sharing).
    const raw = await client.get(this.key(agentId));
    if (raw === null) {
      return null;
    }
    const buf = Buffer.from(raw, "base64");
    if (this.cipher === undefined) {
      return new Uint8Array(buf);
    }
    try {
      return new Uint8Array(this.cipher.open(buf));
    } catch (exc) {
      // Same fail-closed-but-logged posture as stls-core's own
      // RedisKeyCache: a wrong/rotated STLS_CORE_ATREST_KEY must not
      // throw out to the caller or return garbage key material, just
      // "no key found".
      console.error(
        `[RedisIdentityKeystore] get: decryption failed for agentId=${agentId}: ${(exc as Error).message}`,
      );
      return null;
    }
  }

  async put(agentId: string, privateKey: Uint8Array): Promise<void> {
    const client = await this.ensureConnected();
    const raw = Buffer.from(privateKey);
    const stored = this.cipher === undefined ? raw : this.cipher.seal(raw);
    // Plain SET — deliberately no EX: persistent, not a cache entry.
    await client.set(this.key(agentId), stored.toString("base64"));
  }

  async close(): Promise<void> {
    if (this.client !== null) {
      await this.client.quit();
      this.client = null;
    }
  }
}

export class IdentityKeystoreFactory {
  /** Mirrors stls-core's SessionCacheFactory.build() / ArqitKeyStoreFactory.build(). */
  static build(): IdentityKeystoreBase {
    const backend = (process.env.IDENTITY_KEYSTORE_BACKEND ?? "memory").trim().toLowerCase();

    if (backend === "redis") {
      const url = process.env.IDENTITY_KEYSTORE_REDIS_URL ?? "redis://localhost:6379";
      const namespace =
        process.env.IDENTITY_KEYSTORE_REDIS_NAMESPACE ?? "stls:identity:signingkey";
      return new RedisIdentityKeystore({ url, namespace });
    }

    if (backend === "memory") {
      return new InMemoryIdentityKeystore();
    }

    throw new Error(
      `IDENTITY_KEYSTORE_BACKEND='${backend}' not recognized. ` +
        "Supported values: 'memory' (default), 'redis'.",
    );
  }
}
