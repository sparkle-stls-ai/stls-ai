/**
 * auth/models.ts
 * --------------
 * Port of python/stls-core/src/stls_core/auth/stls_core_auth_models.py.
 *
 * No external dependencies — only node:crypto.
 */

import { createHash, randomBytes, timingSafeEqual } from "node:crypto";

// ---------------------------------------------------------------------------
// Enums
// ---------------------------------------------------------------------------

export enum AuthResult {
  OK = "ok",
  INVALID = "invalid",
  REVOKED = "revoked",
  EXPIRED = "expired",
  SCOPE_DENIED = "scope_denied",
  INTERNAL_ERROR = "internal_error",
}

export enum LoaderBackend {
  STATIC = "static",
  FILE = "file",
  POSTGRES = "postgres",
}

// ---------------------------------------------------------------------------
// KeyRecord
// ---------------------------------------------------------------------------

export interface KeyRecordInit {
  keyId: string;
  clientId: string;
  scopes?: string[];
  endpoints?: string[];
  expiresAt?: Date | null;
  createdAt?: Date;
  description?: string;
  metadata?: Record<string, unknown>;
}

/**
 * `keyId` always holds the SHA-256 hash of the raw key value — the
 * plaintext is never persisted or logged.
 */
export class KeyRecord {
  readonly keyId: string;
  readonly clientId: string;
  readonly scopes: string[];
  readonly endpoints: string[];
  readonly expiresAt: Date | null;
  readonly createdAt: Date;
  readonly description: string;
  readonly metadata: Record<string, unknown>;

  constructor(init: KeyRecordInit) {
    this.keyId = init.keyId;
    this.clientId = init.clientId;
    this.scopes = init.scopes ?? [];
    this.endpoints = init.endpoints ?? [];
    this.expiresAt = init.expiresAt ?? null;
    this.createdAt = init.createdAt ?? new Date();
    this.description = init.description ?? "";
    this.metadata = init.metadata ?? {};
  }

  isExpired(): boolean {
    if (this.expiresAt === null) return false;
    return Date.now() >= this.expiresAt.getTime();
  }

  /**
   * True if this key is authorized for the requested endpoint.
   * Prefix-based: endpoints=["/secure-a2a"] also authorizes "/secure-a2a/stream".
   * An empty list means wildcard.
   */
  allowsEndpoint(endpoint: string): boolean {
    if (this.endpoints.length === 0) return true;
    const norm = endpoint.replace(/\/+$/, "");
    return this.endpoints.some((e) => {
      const ne = e.replace(/\/+$/, "");
      return norm === ne || norm.startsWith(`${ne}/`);
    });
  }

  hasScope(scope: string): boolean {
    return this.scopes.includes("*") || this.scopes.includes(scope);
  }

  toDict(): Record<string, unknown> {
    return {
      key_id: this.keyId,
      client_id: this.clientId,
      scopes: this.scopes,
      endpoints: this.endpoints,
      expires_at: this.expiresAt ? this.expiresAt.toISOString() : null,
      created_at: this.createdAt.toISOString(),
      description: this.description,
      metadata: this.metadata,
    };
  }

  static fromDict(data: Record<string, unknown>): KeyRecord {
    const expiresAtRaw = data.expires_at as string | null | undefined;
    const createdAtRaw = data.created_at as string | undefined;
    return new KeyRecord({
      keyId: data.key_id as string,
      clientId: data.client_id as string,
      scopes: (data.scopes as string[] | undefined) ?? [],
      endpoints: (data.endpoints as string[] | undefined) ?? [],
      expiresAt: expiresAtRaw ? new Date(expiresAtRaw) : null,
      createdAt: createdAtRaw ? new Date(createdAtRaw) : new Date(),
      description: (data.description as string | undefined) ?? "",
      metadata: (data.metadata as Record<string, unknown> | undefined) ?? {},
    });
  }
}

// ---------------------------------------------------------------------------
// AuditEntry
// ---------------------------------------------------------------------------

export interface AuditEntryInit {
  timestamp: Date;
  keyId: string | null;
  clientId: string | null;
  endpoint: string;
  result: AuthResult;
  remoteAddr?: string;
  requestId?: string;
}

export class AuditEntry {
  readonly timestamp: Date;
  readonly keyId: string | null;
  readonly clientId: string | null;
  readonly endpoint: string;
  readonly result: AuthResult;
  readonly remoteAddr: string;
  readonly requestId: string;

  constructor(init: AuditEntryInit) {
    this.timestamp = init.timestamp;
    this.keyId = init.keyId;
    this.clientId = init.clientId;
    this.endpoint = init.endpoint;
    this.result = init.result;
    this.remoteAddr = init.remoteAddr ?? "";
    this.requestId = init.requestId ?? "";
  }

  toDict(): Record<string, unknown> {
    return {
      timestamp: this.timestamp.toISOString(),
      key_id: this.keyId,
      client_id: this.clientId,
      endpoint: this.endpoint,
      result: this.result,
      remote_addr: this.remoteAddr,
      request_id: this.requestId,
    };
  }
}

// ---------------------------------------------------------------------------
// ValidationResult
// ---------------------------------------------------------------------------

export class ValidationResult {
  constructor(
    public readonly result: AuthResult,
    public readonly record: KeyRecord | null,
    public readonly audit: AuditEntry,
  ) {}

  get ok(): boolean {
    return this.result === AuthResult.OK;
  }

  /** Value for the WWW-Authenticate header on 401 responses (RFC 7235). */
  wwwAuthenticateHeader(realm = "STLS"): string {
    if (this.result === AuthResult.EXPIRED) {
      return `ApiKey realm="${realm}", error="token_expired"`;
    }
    if (this.result === AuthResult.REVOKED) {
      return `ApiKey realm="${realm}", error="token_revoked"`;
    }
    if (this.result === AuthResult.SCOPE_DENIED) {
      return `ApiKey realm="${realm}", error="insufficient_scope"`;
    }
    return `ApiKey realm="${realm}"`;
  }
}

// ---------------------------------------------------------------------------
// Cryptographic utilities
// ---------------------------------------------------------------------------

/** SHA-256 hex digest of the raw API key value. The only function that sees the plaintext. */
export function hashKey(rawKey: string): string {
  return createHash("sha256").update(rawKey, "utf-8").digest("hex");
}

/** Timing-safe comparison between two strings. Prevents timing attacks on validation. */
export function constantTimeEqual(a: string, b: string): boolean {
  const bufA = Buffer.from(a, "utf-8");
  const bufB = Buffer.from(b, "utf-8");
  if (bufA.length !== bufB.length) return false;
  return timingSafeEqual(bufA, bufB);
}

/**
 * Generates a new, secure API key. Format: `{prefix}_{32 random hex bytes}` (256 bits).
 * Returns: [rawKey, keyId] — the plaintext to hand to the client, and the keyId (SHA-256) to persist.
 */
export function generateApiKey(prefix = "stls"): [string, string] {
  const raw = `${prefix}_${randomBytes(32).toString("hex")}`;
  return [raw, hashKey(raw)];
}

// ---------------------------------------------------------------------------
// AuthConfig — lightweight static validation, no loader/cache/refresh
// ---------------------------------------------------------------------------

export interface AuthConfigInit {
  bearerTokens?: Set<string>;
  apiKeys?: Set<string>;
  realm?: string;
}

/**
 * Static authorization configuration, with no loader/Redis/refresh.
 * Does not replace ApiKeyManager: where key rotation without restarts,
 * structured audit, or per-key scope/endpoint checks are needed, use
 * ApiKeyManager instead.
 */
export class AuthConfig {
  readonly bearerTokens: Set<string>;
  readonly apiKeys: Set<string>;
  readonly realm: string;

  constructor(init: AuthConfigInit = {}) {
    this.bearerTokens = init.bearerTokens ?? new Set();
    this.apiKeys = init.apiKeys ?? new Set();
    this.realm = init.realm ?? "STLS";
  }

  get enabled(): boolean {
    return this.bearerTokens.size > 0 || this.apiKeys.size > 0;
  }

  /** Verifies the supplied credentials. True if at least one scheme is valid. */
  check(authorization: string | null | undefined, apiKey: string | null | undefined): boolean {
    if (this.bearerTokens.size > 0 && authorization) {
      const parts = authorization.split(" ");
      if (parts.length >= 2 && parts[0].toLowerCase() === "bearer") {
        const token = parts.slice(1).join(" ");
        for (const candidate of this.bearerTokens) {
          if (constantTimeEqual(token, candidate)) return true;
        }
      }
    }

    if (this.apiKeys.size > 0 && apiKey) {
      for (const candidate of this.apiKeys) {
        if (constantTimeEqual(apiKey, candidate)) return true;
      }
    }

    return false;
  }
}

// ---------------------------------------------------------------------------
// Configuration constants
// ---------------------------------------------------------------------------

export const DEFAULT_KEY_TTL_SECONDS = 3600;
export const DEFAULT_REFRESH_INTERVAL_SECONDS = 300;

export const REDIS_KEY_PREFIX = "stls:key:";
export const REDIS_REVOKED_SET = "stls:revoked";
export const REDIS_INDEX_SET = "stls:keys:index";
