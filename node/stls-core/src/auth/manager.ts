/**
 * auth/manager.ts
 * ---------------
 * Port of python/stls-core/src/stls_core/auth/stls_core_auth_manager.py.
 *
 * Design difference from the Python side: validateRequest() in Python
 * accepts a Starlette/FastAPI Request directly. Node has no single
 * canonical HTTP framework (Express, Fastify, native http all have
 * different shapes), so the entry point here is a small RequestLike
 * adapter that any framework can implement in a few lines — see the
 * interface below.
 */

import { hostname } from "node:os";
import {
  AuditEntry,
  AuthResult,
  DEFAULT_REFRESH_INTERVAL_SECONDS,
  type KeyRecord,
  ValidationResult,
  hashKey,
} from "./models.js";
import type { ApiKeyLoader } from "./loaders.js";
import { StaticLoader, createLoader } from "./loaders.js";
import { RedisKeyCache } from "./store.js";
import type { AuthConfig } from "./models.js";

export type AuditHook = (entry: AuditEntry) => Promise<void>;

/** Minimal adapter that any HTTP framework can implement to use validateRequest(). */
export interface RequestLike {
  header(name: string): string | undefined;
  queryParam(name: string): string | undefined;
  path: string;
  remoteAddr?: string;
}

// ---------------------------------------------------------------------------
// Default audit hook
// ---------------------------------------------------------------------------

/** Factory: returns a standard audit hook based on a console/log function. */
export function defaultAuditHook(
  log: (level: "info" | "warn", message: string) => void = (level, msg) =>
    // eslint-disable-next-line no-console
    console[level === "info" ? "log" : "warn"](msg),
): AuditHook {
  return async (entry: AuditEntry) => {
    const level = entry.result === AuthResult.OK ? "info" : "warn";
    log(
      level,
      `[AUDIT] result=${entry.result} client=${entry.clientId ?? "—"} ` +
        `endpoint=${entry.endpoint} addr=${entry.remoteAddr || "—"} req_id=${entry.requestId || "—"}`,
    );
  };
}

function namespaceForLoader(loader: ApiKeyLoader): string {
  const name = loader.constructor.name;
  const map: Record<string, string> = {
    StaticLoader: "stls:static",
    FileLoader: "stls:file",
    PostgresLoader: "stls:pg",
  };
  return map[name] ?? "stls";
}

// ---------------------------------------------------------------------------
// ApiKeyManager
// ---------------------------------------------------------------------------

export interface ApiKeyManagerOptions {
  refreshInterval?: number;
  auditHook?: AuditHook | null;
  enableCache?: boolean;
}

export class ApiKeyManager {
  private readonly loader: ApiKeyLoader;
  private readonly cache: RedisKeyCache;
  private readonly refreshInterval: number;
  private readonly auditHook: AuditHook | null;
  private readonly enableCache: boolean;
  private refresher: BackgroundRefresher | null = null;

  constructor(loader: ApiKeyLoader, cache: RedisKeyCache, options: ApiKeyManagerOptions = {}) {
    this.loader = loader;
    this.cache = cache;
    this.refreshInterval =
      options.refreshInterval ??
      Number(process.env.AUTH_REFRESH_INTERVAL ?? DEFAULT_REFRESH_INTERVAL_SECONDS);
    this.auditHook = options.auditHook !== undefined ? options.auditHook : defaultAuditHook();
    this.enableCache = options.enableCache ?? true;
  }

  // ---- Factory methods ------------------------------------------------------

  static fromLoader(
    loader: ApiKeyLoader,
    options: ApiKeyManagerOptions & { redisUrl?: string; namespace?: string } = {},
  ): ApiKeyManager {
    const redisUrl = options.redisUrl ?? process.env.REDIS_URL ?? "redis://localhost:6379";
    const enableCache =
      options.enableCache ??
      !["false", "0", "no"].includes((process.env.AUTH_KM_USE_REDIS ?? "true").toLowerCase());
    const namespace = options.namespace ?? namespaceForLoader(loader);
    const cache = new RedisKeyCache({ url: redisUrl, namespace });
    return new ApiKeyManager(loader, cache, { ...options, enableCache });
  }

  static fromEnv(options: ApiKeyManagerOptions = {}): ApiKeyManager {
    const loader = createLoader();
    return ApiKeyManager.fromLoader(loader, options);
  }

  /** Quick factory for test/dev with in-memory keys. */
  static fromStatic(
    keys: Record<string, { clientId?: string; scopes?: string[]; endpoints?: string[] }>,
    useRedis = true,
    options: ApiKeyManagerOptions & { redisUrl?: string; namespace?: string } = {},
  ): ApiKeyManager {
    const loader = StaticLoader.fromPlaintext(keys);
    return ApiKeyManager.fromLoader(loader, { ...options, enableCache: useRedis });
  }

  /** Compatibility factory: migrates keys from an existing AuthConfig. */
  static fromAuthConfig(
    authConfig: AuthConfig,
    useRedis = true,
    options: ApiKeyManagerOptions & { redisUrl?: string; namespace?: string } = {},
  ): ApiKeyManager {
    const loader = StaticLoader.fromAuthConfig(authConfig);
    return ApiKeyManager.fromLoader(loader, { ...options, enableCache: useRedis });
  }

  // ---- Lifecycle --------------------------------------------------------------

  async start(): Promise<void> {
    if (this.enableCache) {
      await this.cache.initialize();
      await this.initialLoad();
    }

    const distributed = !["false", "0", "no"].includes(
      (process.env.AUTH_REFRESH_DISTRIBUTED ?? "true").toLowerCase(),
    );
    const namespace = this.enableCache ? namespaceForLoader(this.loader) : "stls";
    this.refresher = new BackgroundRefresher(
      this.loader,
      this.enableCache ? this.cache : null,
      { interval: this.refreshInterval, distributed, namespace },
    );
    await this.refresher.start();
  }

  async stop(): Promise<void> {
    if (this.refresher) await this.refresher.stop();
    if (this.enableCache) await this.cache.close();
    await this.loader.close();
  }

  // ---- Main validation ---------------------------------------------------

  async validate(
    rawKey: string,
    endpoint: string,
    remoteAddr = "",
    requestId = "",
  ): Promise<ValidationResult> {
    const ts = new Date();
    const keyId = hashKey(rawKey);

    if (this.enableCache && (await this.cache.isRevoked(keyId))) {
      return this.buildResult(AuthResult.REVOKED, null, keyId, null, endpoint, remoteAddr, requestId, ts);
    }

    const record = await this.lookup(keyId);
    if (record === null) {
      return this.buildResult(AuthResult.INVALID, null, keyId, null, endpoint, remoteAddr, requestId, ts);
    }

    if (record.isExpired()) {
      return this.buildResult(
        AuthResult.EXPIRED, null, keyId, record.clientId, endpoint, remoteAddr, requestId, ts,
      );
    }

    if (!record.allowsEndpoint(endpoint)) {
      return this.buildResult(
        AuthResult.SCOPE_DENIED, null, keyId, record.clientId, endpoint, remoteAddr, requestId, ts,
      );
    }

    return this.buildResult(AuthResult.OK, record, keyId, record.clientId, endpoint, remoteAddr, requestId, ts);
  }

  /** Convenience overload: extracts the key from a RequestLike (X-API-Key header or ?api_key=). */
  async validateRequest(request: RequestLike): Promise<ValidationResult> {
    const rawKey = request.header("x-api-key") || request.queryParam("api_key") || "";
    const endpoint = request.path;
    const remoteAddr =
      request.remoteAddr ?? (request.header("x-forwarded-for")?.split(",")[0].trim() ?? "");
    const requestId = request.header("x-request-id") ?? "";

    if (!rawKey) {
      const ts = new Date();
      const audit = new AuditEntry({
        timestamp: ts,
        keyId: null,
        clientId: null,
        endpoint,
        result: AuthResult.INVALID,
        remoteAddr,
        requestId,
      });
      await this.emitAudit(audit);
      return new ValidationResult(AuthResult.INVALID, null, audit);
    }

    return this.validate(rawKey, endpoint, remoteAddr, requestId);
  }

  // ---- Manual key management (admin API) --------------------------------

  async revoke(keyId: string): Promise<void> {
    if (this.enableCache) await this.cache.revoke(keyId);
  }

  async addKey(record: KeyRecord): Promise<void> {
    if (this.enableCache) await this.cache.set(record);
  }

  async health(): Promise<Record<string, unknown>> {
    const redisOk = this.enableCache ? await this.cache.ping() : null;
    const cacheStats = this.enableCache ? await this.cache.stats() : {};
    const redisStatus = redisOk === null ? "disabled" : redisOk ? "ok" : "error";
    const refresherRunning = this.refresher?.running ?? false;
    const overall =
      (redisStatus === "ok" || redisStatus === "disabled") && refresherRunning ? "ok" : "degraded";
    return {
      status: overall,
      auth_manager: "ok",
      redis: redisStatus,
      loader: this.loader.constructor.name,
      refresher_running: refresherRunning,
      ...cacheStats,
    };
  }

  // ---- Internal helpers ------------------------------------------------------------

  private async initialLoad(): Promise<void> {
    try {
      const records = await this.loader.loadAll();
      if (records.length > 0) {
        await this.cache.bulkReload(records);
      }
    } catch {
      // best-effort, same as the Python side
    }
  }

  private async lookup(keyId: string): Promise<KeyRecord | null> {
    if (this.enableCache) {
      const cached = await this.cache.get(keyId);
      if (cached !== null) return cached;
    }

    const record = await this.loader.loadOne(keyId);
    if (record !== null && this.enableCache) {
      await this.cache.set(record);
    }
    return record;
  }

  private buildResult(
    result: AuthResult,
    record: KeyRecord | null,
    keyId: string,
    clientId: string | null,
    endpoint: string,
    remoteAddr: string,
    requestId: string,
    ts: Date,
  ): ValidationResult {
    const audit = new AuditEntry({
      timestamp: ts,
      keyId,
      clientId,
      endpoint,
      result,
      remoteAddr,
      requestId,
    });

    // fire-and-forget, does not block the response
    void this.emitAudit(audit);

    return new ValidationResult(result, record, audit);
  }

  private async emitAudit(audit: AuditEntry): Promise<void> {
    if (this.auditHook) {
      try {
        await this.auditHook(audit);
      } catch {
        // isolated — an error in the hook must not propagate
      }
    }
  }
}

// ---------------------------------------------------------------------------
// ApiKeyManagerGroup
// ---------------------------------------------------------------------------

/** Manages the lifecycle of a set of ApiKeyManagers with a single call. */
export class ApiKeyManagerGroup {
  private readonly managers = new Map<string, ApiKeyManager>();

  /** Adds a manager to the group. manager=null is silently ignored. */
  add(name: string, manager: ApiKeyManager | null): this {
    if (manager !== null) {
      this.managers.set(name, manager);
    }
    return this;
  }

  get(name: string): ApiKeyManager | undefined {
    return this.managers.get(name);
  }

  async start(): Promise<void> {
    for (const [, manager] of this.managers) {
      await manager.start();
    }
  }

  async stop(): Promise<void> {
    for (const [, manager] of [...this.managers].reverse()) {
      await manager.stop();
    }
  }

  async health(): Promise<Record<string, unknown>> {
    const results: Record<string, unknown> = {};
    for (const [name, manager] of this.managers) {
      try {
        results[name] = await manager.health();
      } catch (exc) {
        results[name] = { status: "error", auth_manager: "error", error: (exc as Error).message };
      }
    }
    const allOk = Object.values(results).every(
      (r) => (r as Record<string, unknown>).status === "ok",
    );
    return { status: allOk ? "ok" : "degraded", ...results };
  }

  get size(): number {
    return this.managers.size;
  }

  has(name: string): boolean {
    return this.managers.has(name);
  }
}

// ---------------------------------------------------------------------------
// BackgroundRefresher
// ---------------------------------------------------------------------------

export interface BackgroundRefresherOptions {
  interval?: number;
  distributed?: boolean;
  namespace?: string;
}

/** Periodic task that reloads keys from the loader into Redis. Per-namespace distributed lock. */
export class BackgroundRefresher {
  private readonly loader: ApiKeyLoader;
  private readonly cache: RedisKeyCache | null;
  private readonly interval: number;
  private readonly distributed: boolean;
  private readonly namespace: string;
  private readonly lockKey: string;
  private readonly nodeId: string;
  private timer: NodeJS.Timeout | null = null;
  private stopped = false;

  constructor(loader: ApiKeyLoader, cache: RedisKeyCache | null, options: BackgroundRefresherOptions = {}) {
    this.loader = loader;
    this.cache = cache;
    this.interval = options.interval ?? DEFAULT_REFRESH_INTERVAL_SECONDS;
    this.distributed = options.distributed ?? true;
    this.namespace = options.namespace ?? "stls";
    this.lockKey = `stls:refresh:lock:${this.namespace}`;
    this.nodeId = `${hostname()}:${process.pid}`;
  }

  get running(): boolean {
    return this.timer !== null;
  }

  async start(): Promise<void> {
    if (this.running) return;
    this.stopped = false;
    this.scheduleNext();
  }

  async stop(): Promise<void> {
    this.stopped = true;
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  /** Forces an immediate reload, bypassing the distributed lock. */
  async trigger(): Promise<number> {
    return this.doRefresh(true);
  }

  private scheduleNext(): void {
    this.timer = setTimeout(() => {
      void this.doRefresh().finally(() => {
        if (!this.stopped) this.scheduleNext();
      });
    }, this.interval * 1000);
  }

  private async doRefresh(force = false): Promise<number> {
    let acquired = false;

    if (this.distributed && this.cache !== null && !force) {
      acquired = await this.cache.tryAcquireLock(this.lockKey, this.nodeId, this.interval + 30);
      if (!acquired) return 0;
    }

    try {
      const records = await this.loader.loadAll();
      if (this.cache !== null) {
        return await this.cache.bulkReload(records);
      }
      return records.length;
    } catch {
      return 0;
    } finally {
      if (acquired && this.cache !== null) {
        await this.cache.releaseLock(this.lockKey, this.nodeId);
      }
    }
  }
}
