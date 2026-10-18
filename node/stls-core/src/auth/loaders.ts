/**
 * auth/loaders.ts
 * ---------------
 * Port of python/stls-core/src/stls_core/auth/stls_core_auth_loaders.py.
 *
 * Ports StaticLoader and FileLoader. PostgresLoader lives in its own
 * module (loaders-postgres.ts), imported lazily below.
 */

import { readFile, stat } from "node:fs/promises";
import path from "node:path";
import { hashKey, KeyRecord, LoaderBackend, type AuthConfig } from "./models.js";
import { PostgresLoader } from "./loaders-postgres.js";

export interface ApiKeyLoader {
  /**
   * Loads all available KeyRecords. Must return an empty list (not throw)
   * if the source is temporarily unavailable.
   */
  loadAll(): Promise<KeyRecord[]>;
  /** Loads a single KeyRecord by keyId (SHA-256 hex), or null. */
  loadOne(keyId: string): Promise<KeyRecord | null>;
  close(): Promise<void>;
}

// ---------------------------------------------------------------------------
// StaticLoader
// ---------------------------------------------------------------------------

export interface StaticKeyProps {
  clientId?: string;
  scopes?: string[];
  endpoints?: string[];
  description?: string;
  metadata?: Record<string, unknown>;
}

/** In-memory loader: keys are passed directly to the constructor. */
export class StaticLoader implements ApiKeyLoader {
  private readonly store: Map<string, KeyRecord>;

  constructor(records: KeyRecord[] = []) {
    this.store = new Map(records.map((r) => [r.keyId, r]));
  }

  /** Builds the loader from {rawKey: {clientId, scopes, ...}}. The plaintext is hashed and discarded. */
  static fromPlaintext(keys: Record<string, StaticKeyProps>): StaticLoader {
    const records: KeyRecord[] = [];
    for (const [rawKey, props] of Object.entries(keys)) {
      records.push(
        new KeyRecord({
          keyId: hashKey(rawKey),
          clientId: props.clientId ?? "unknown",
          scopes: props.scopes ?? [],
          endpoints: props.endpoints ?? [],
          description: props.description ?? "",
          metadata: props.metadata ?? {},
        }),
      );
    }
    return new StaticLoader(records);
  }

  /** Migrates keys from an existing AuthConfig (apiKeys/bearerTokens) to the new system. */
  static fromAuthConfig(authConfig: AuthConfig): StaticLoader {
    const records: KeyRecord[] = [];

    for (const rawKey of authConfig.apiKeys) {
      if (!rawKey) continue;
      records.push(
        new KeyRecord({
          keyId: hashKey(rawKey),
          clientId: "legacy-api-key",
          scopes: ["secure-a2a"],
          description: "Migrated from AuthConfig.apiKeys",
        }),
      );
    }

    for (const token of authConfig.bearerTokens) {
      if (!token) continue;
      records.push(
        new KeyRecord({
          keyId: hashKey(token),
          clientId: "legacy-bearer",
          scopes: ["secure-a2a"],
          description: "Migrated from AuthConfig.bearerTokens",
        }),
      );
    }

    return new StaticLoader(records);
  }

  async loadAll(): Promise<KeyRecord[]> {
    return Array.from(this.store.values());
  }

  async loadOne(keyId: string): Promise<KeyRecord | null> {
    return this.store.get(keyId) ?? null;
  }

  /** Adds or updates a record at runtime (handy in tests). */
  add(record: KeyRecord): void {
    this.store.set(record.keyId, record);
  }

  /** Removes a record at runtime. */
  remove(keyId: string): void {
    this.store.delete(keyId);
  }

  get size(): number {
    return this.store.size;
  }

  async close(): Promise<void> {}
}

// ---------------------------------------------------------------------------
// FileLoader — YAML (if available) or JSON, reloadable at runtime
// ---------------------------------------------------------------------------

/**
 * Loads API keys from a YAML or JSON file. Path from AUTH_KEYS_FILE.
 * Reloads at runtime: mtime is compared on every loadAll().
 *
 * JSON format (flat list) or YAML with a "keys" key:
 *   [{ "key_id": "...", "client_id": "...", "scopes": ["secure-a2a"] }, ...]
 *
 * If "raw_key" appears instead of "key_id", the loader hashes it and
 * discards the plaintext from memory.
 *
 * YAML parsing requires the optional `yaml` package (not a runtime
 * dependency of stls-core): if it is not installed, FileLoader falls
 * back to JSON — the same behavior as the Python side when PyYAML is
 * missing.
 */
export class FileLoader implements ApiKeyLoader {
  private readonly filePath: string;
  private lastMtimeMs = 0;
  private cache = new Map<string, KeyRecord>();

  constructor(filePath?: string) {
    this.filePath = filePath ?? process.env.AUTH_KEYS_FILE ?? "api_keys.yaml";
  }

  async loadAll(): Promise<KeyRecord[]> {
    let mtimeMs: number;
    try {
      mtimeMs = (await stat(this.filePath)).mtimeMs;
    } catch {
      return [];
    }

    if (mtimeMs !== this.lastMtimeMs || this.cache.size === 0) {
      const records = await this.parseFile();
      this.cache = new Map(records.map((r) => [r.keyId, r]));
      this.lastMtimeMs = mtimeMs;
    }

    return Array.from(this.cache.values());
  }

  async loadOne(keyId: string): Promise<KeyRecord | null> {
    await this.loadAll();
    return this.cache.get(keyId) ?? null;
  }

  private async parseFile(): Promise<KeyRecord[]> {
    const suffix = path.extname(this.filePath).toLowerCase();
    const text = await readFile(this.filePath, "utf-8");

    let raw: unknown;
    try {
      raw = suffix === ".yaml" || suffix === ".yml" ? await this.parseYaml(text) : JSON.parse(text);
    } catch {
      return [];
    }

    let items: unknown[];
    if (Array.isArray(raw)) {
      items = raw;
    } else if (raw && typeof raw === "object" && Array.isArray((raw as Record<string, unknown>).keys)) {
      items = (raw as Record<string, unknown>).keys as unknown[];
    } else {
      return [];
    }

    const records: KeyRecord[] = [];
    for (const item of items) {
      try {
        records.push(this.itemToRecord(item as Record<string, unknown>));
      } catch {
        // invalid record, skipped (same as the Python side)
      }
    }
    return records;
  }

  private itemToRecord(item: Record<string, unknown>): KeyRecord {
    if (typeof item.raw_key === "string" && !item.key_id) {
      const { raw_key: rawKey, ...rest } = item;
      return KeyRecord.fromDict({ ...rest, key_id: hashKey(rawKey) });
    }
    return KeyRecord.fromDict(item);
  }

  private async parseYaml(text: string): Promise<unknown> {
    try {
      const yamlModule = await import("yaml");
      return yamlModule.parse(text);
    } catch {
      return JSON.parse(text);
    }
  }

  async close(): Promise<void> {}
}

// ---------------------------------------------------------------------------
// Factory
// ---------------------------------------------------------------------------

export interface CreateLoaderOptions {
  records?: KeyRecord[];
  path?: string;
  dsn?: string;
  minPoolSize?: number;
  maxPoolSize?: number;
  table?: string;
}

/** Factory: instantiates the right loader. Default: env AUTH_LOADER_BACKEND, fallback "static". */
export function createLoader(
  backend?: string | LoaderBackend,
  options: CreateLoaderOptions = {},
): ApiKeyLoader {
  const resolved = (backend ?? process.env.AUTH_LOADER_BACKEND ?? LoaderBackend.STATIC)
    .toString()
    .toLowerCase()
    .trim();

  if (resolved === LoaderBackend.STATIC) {
    return new StaticLoader(options.records ?? []);
  }

  if (resolved === LoaderBackend.FILE) {
    return new FileLoader(options.path);
  }

  if (resolved === LoaderBackend.POSTGRES) {
    // PostgresLoader only imports `pg` lazily on first use of the pool
    // (see loaders-postgres.ts) — constructing it here does not require
    // `pg` to be installed until loadAll()/loadOne() is actually called,
    // the same lazy semantics as the Python side (asyncpg imported only
    // inside _get_pool()).
    return new PostgresLoader(options);
  }

  throw new Error(
    `Unrecognized loader backend: '${resolved}'. Valid values: static, file, postgres.`,
  );
}
