/**
 * auth/loaders-postgres.ts
 * ------------------------
 * Port of PostgresLoader from python/stls-core/src/stls_core/auth/stls_core_auth_loaders.py.
 *
 * Uses the optional `pg` package (node-postgres) via a dynamic import,
 * mirroring how the Python side only imports `asyncpg` when the
 * "postgres" backend is selected.
 *
 * Expected table schema (identical to the Python side):
 *
 *   CREATE TABLE sf_api_keys (
 *       key_id      TEXT        PRIMARY KEY,
 *       client_id   TEXT        NOT NULL,
 *       scopes      TEXT[]      DEFAULT '{}',
 *       endpoints   TEXT[]      DEFAULT '{}',
 *       expires_at  TIMESTAMPTZ,
 *       created_at  TIMESTAMPTZ DEFAULT now(),
 *       revoked     BOOLEAN     DEFAULT false,
 *       description TEXT        DEFAULT '',
 *       metadata    JSONB       DEFAULT '{}'
 *   );
 *
 * The loader automatically filters out rows with revoked = true. Actual
 * revocation is handled by RedisKeyCache (the stls:revoked SET), not here.
 *
 * WARNING: not verified against a real PostgreSQL instance in this
 * development environment (no Postgres server available, no root/Docker
 * access to start one). The SQL query and the row -> KeyRecord
 * conversion were written to match PostgresLoader on the Python side
 * directly, but must be validated against a real database before use in
 * production.
 */

import type { KeyRecord as KeyRecordType } from "./models.js";
import { KeyRecord } from "./models.js";
import type { ApiKeyLoader } from "./loaders.js";

// Minimal types for `pg`, without requiring it statically (lazy dynamic import).
interface PgPoolLike {
  query(text: string, params?: unknown[]): Promise<{ rows: Record<string, unknown>[] }>;
  end(): Promise<void>;
}

interface PgModuleLike {
  Pool: new (config: {
    connectionString: string;
    max: number;
    statement_timeout: number;
  }) => PgPoolLike;
}

export interface PostgresLoaderOptions {
  dsn?: string;
  minPoolSize?: number;
  maxPoolSize?: number;
  table?: string;
}

export class PostgresLoader implements ApiKeyLoader {
  static readonly DEFAULT_TABLE = "sf_api_keys";

  private readonly dsn: string;
  private readonly maxPoolSize: number;
  private readonly table: string;
  private pool: PgPoolLike | null = null;

  constructor(options: PostgresLoaderOptions = {}) {
    this.dsn = options.dsn ?? process.env.AUTH_PG_DSN ?? "";
    this.maxPoolSize = options.maxPoolSize ?? 5;
    this.table = options.table ?? process.env.SF_API_KEYS_TABLE ?? PostgresLoader.DEFAULT_TABLE;
  }

  // ---- Pool lifecycle -------------------------------------------------------

  private async getPool(): Promise<PgPoolLike> {
    if (this.pool !== null) return this.pool;

    let pgModule: PgModuleLike;
    try {
      pgModule = (await import("pg")) as unknown as PgModuleLike;
    } catch (exc) {
      throw new Error(
        `PostgresLoader requires the optional 'pg' package. Install it with: npm install pg\n(${(exc as Error).message})`,
      );
    }

    if (!this.dsn) {
      throw new Error("PostgresLoader: AUTH_PG_DSN is not configured.");
    }

    this.pool = new pgModule.Pool({
      connectionString: this.dsn,
      max: this.maxPoolSize,
      statement_timeout: 10_000,
    });
    return this.pool;
  }

  async close(): Promise<void> {
    if (this.pool !== null) {
      await this.pool.end();
      this.pool = null;
    }
  }

  // ---- ApiKeyLoader impl ------------------------------------------------------

  async loadAll(): Promise<KeyRecordType[]> {
    try {
      const pool = await this.getPool();
      const { rows } = await pool.query(
        `SELECT key_id, client_id, scopes, endpoints,
                expires_at, created_at, description, metadata
         FROM ${this.table}
         WHERE revoked = false
         ORDER BY created_at DESC`,
      );
      return rows.map((row) => this.rowToRecord(row));
    } catch {
      // best-effort: source temporarily unavailable -> empty list
      return [];
    }
  }

  async loadOne(keyId: string): Promise<KeyRecordType | null> {
    try {
      const pool = await this.getPool();
      const { rows } = await pool.query(
        `SELECT key_id, client_id, scopes, endpoints,
                expires_at, created_at, description, metadata
         FROM ${this.table}
         WHERE key_id = $1 AND revoked = false`,
        [keyId],
      );
      if (rows.length === 0) return null;
      return this.rowToRecord(rows[0]);
    } catch {
      return null;
    }
  }

  // ---- Row -> KeyRecord conversion ---------------------------------------------

  private rowToRecord(row: Record<string, unknown>): KeyRecordType {
    // node-postgres converts TEXT[] -> JS array and JSONB -> JS object,
    // TIMESTAMPTZ -> Date by default — no manual conversion needed,
    // unlike the Python side where asyncpg can return JSONB as a string
    // in some combinations (see _safe_dict in stls_core_auth_loaders.py).
    return new KeyRecord({
      keyId: row.key_id as string,
      clientId: row.client_id as string,
      scopes: (row.scopes as string[] | null) ?? [],
      endpoints: (row.endpoints as string[] | null) ?? [],
      expiresAt: (row.expires_at as Date | null) ?? null,
      createdAt: (row.created_at as Date | undefined) ?? new Date(),
      description: (row.description as string | null) ?? "",
      metadata: (row.metadata as Record<string, unknown> | null) ?? {},
    });
  }
}
