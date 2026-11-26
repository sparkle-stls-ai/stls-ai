/**
 * postgres-store.ts
 * -------------------
 * 1:1 port of python/stls-identity/src/stls_identity/stls_id_postgres_store.py.
 *
 * Postgres-backed storage for what a future bridge.ts would serve
 * publicly (bridge.ts itself is out of scope for this port — see
 * issue #47):
 *   - one DID Document per agent (the "Agent DID Catalog")
 *   - the last published StatusListCredential snapshot, cached — never
 *     recomputed per HTTP request
 *
 * Mirrors node/stls-core's auth/loaders-postgres.ts conventions: the
 * optional `pg` package via a dynamic import, minimal duck-typed
 * Pool/Module interfaces instead of a hard dependency on `pg`'s own
 * types at the call site.
 *
 * Environment variables:
 *   IDENTITY_PG_DSN                asyncpg/pg DSN
 *   IDENTITY_PG_DID_TABLE          default: stls_identity_did_documents
 *   IDENTITY_PG_STATUS_LIST_TABLE  default: stls_identity_status_list_cache
 *
 * Expected schema (identical to the Python side):
 *
 *   CREATE TABLE stls_identity_did_documents (
 *       agent_id    TEXT        PRIMARY KEY,
 *       did         TEXT        NOT NULL UNIQUE,
 *       document    JSONB       NOT NULL,
 *       created_at  TIMESTAMPTZ DEFAULT now(),
 *       updated_at  TIMESTAMPTZ DEFAULT now()
 *   );
 *
 *   CREATE TABLE stls_identity_status_list_cache (
 *       authority_id  TEXT        PRIMARY KEY,
 *       document      JSONB       NOT NULL,
 *       generated_at  TIMESTAMPTZ NOT NULL
 *   );
 */

import type { DIDDocument, StatusListCredential } from "./models.js";

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

async function loadPgModule(callerName: string): Promise<PgModuleLike> {
  try {
    return (await import("pg")) as unknown as PgModuleLike;
  } catch (exc) {
    throw new Error(
      `${callerName} requires the optional 'pg' package. Install it with: npm install pg\n(${(exc as Error).message})`,
    );
  }
}

function rowDocument<T>(raw: unknown): T {
  // node-postgres (unlike asyncpg) always deserializes JSONB -> a JS
  // object, so there is no "still a string" fallback needed here —
  // kept only as a defensive parse in case a future driver config
  // changes that.
  return (typeof raw === "string" ? JSON.parse(raw) : raw) as T;
}

// ---------------------------------------------------------------------------
// DID Document store
// ---------------------------------------------------------------------------

export interface DIDDocumentStoreBase {
  get(agentId: string): Promise<DIDDocument | null>;
  put(agentId: string, document: DIDDocument): Promise<void>;
  /**
   * Every agentId with a published DID Document — the roster of
   * platform agents that have an identity, not of their credentials
   * (those live elsewhere, per-issuer). Meant for an operational
   * inventory/console, not for the verification hot path.
   */
  listAgentIds(): Promise<string[]>;
  close(): Promise<void>;
}

export interface PostgresStoreOptions {
  dsn?: string;
  minPoolSize?: number;
  maxPoolSize?: number;
  table?: string;
}

/** Same lazy-pool-init / env-override pattern as node/stls-core's PostgresLoader. */
export class PostgresDIDDocumentStore implements DIDDocumentStoreBase {
  static readonly DEFAULT_TABLE = "stls_identity_did_documents";

  private readonly dsn: string;
  private readonly maxPoolSize: number;
  private readonly tableName: string;
  private pool: PgPoolLike | null = null;

  constructor(options: PostgresStoreOptions = {}) {
    this.dsn = options.dsn ?? process.env.IDENTITY_PG_DSN ?? "";
    this.maxPoolSize = options.maxPoolSize ?? 5;
    this.tableName =
      options.table ?? process.env.IDENTITY_PG_DID_TABLE ?? PostgresDIDDocumentStore.DEFAULT_TABLE;
  }

  /** The ACTUAL table name in use — respects IDENTITY_PG_DID_TABLE if set. */
  get table(): string {
    return this.tableName;
  }

  /** DDL on this instance's ACTUAL table name — see this.table's note. */
  getDdl(): string {
    return `
      CREATE TABLE IF NOT EXISTS ${this.tableName} (
          agent_id    TEXT        PRIMARY KEY,
          did         TEXT        NOT NULL UNIQUE,
          document    JSONB       NOT NULL,
          created_at  TIMESTAMPTZ DEFAULT now(),
          updated_at  TIMESTAMPTZ DEFAULT now()
      );
    `;
  }

  private async getPool(): Promise<PgPoolLike> {
    if (this.pool !== null) {
      return this.pool;
    }
    const pgModule = await loadPgModule("PostgresDIDDocumentStore");
    if (!this.dsn) {
      throw new Error("PostgresDIDDocumentStore: IDENTITY_PG_DSN is not configured.");
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

  async get(agentId: string): Promise<DIDDocument | null> {
    try {
      const pool = await this.getPool();
      const { rows } = await pool.query(
        `SELECT document FROM ${this.tableName} WHERE agent_id = $1`,
        [agentId],
      );
      if (rows.length === 0) {
        return null;
      }
      return rowDocument<DIDDocument>(rows[0].document);
    } catch (exc) {
      console.error(`PostgresDIDDocumentStore.get(${agentId}) failed: ${(exc as Error).message}`);
      return null;
    }
  }

  async put(agentId: string, document: DIDDocument): Promise<void> {
    const pool = await this.getPool();
    await pool.query(
      `INSERT INTO ${this.tableName} (agent_id, did, document, updated_at)
       VALUES ($1, $2, $3::jsonb, now())
       ON CONFLICT (agent_id) DO UPDATE
           SET document = EXCLUDED.document, updated_at = now()`,
      [agentId, document.id, JSON.stringify(document)],
    );
  }

  async listAgentIds(): Promise<string[]> {
    const pool = await this.getPool();
    const { rows } = await pool.query(`SELECT agent_id FROM ${this.tableName} ORDER BY agent_id ASC`);
    return rows.map((r) => r.agent_id as string);
  }
}

// ---------------------------------------------------------------------------
// Status list cache store
// ---------------------------------------------------------------------------

export interface StatusListCacheStoreBase {
  get(authorityId: string): Promise<StatusListCredential | null>;
  put(authorityId: string, document: StatusListCredential): Promise<void>;
  close(): Promise<void>;
}

export class PostgresStatusListCacheStore implements StatusListCacheStoreBase {
  static readonly DEFAULT_TABLE = "stls_identity_status_list_cache";

  private readonly dsn: string;
  private readonly maxPoolSize: number;
  private readonly tableName: string;
  private pool: PgPoolLike | null = null;

  constructor(options: PostgresStoreOptions = {}) {
    this.dsn = options.dsn ?? process.env.IDENTITY_PG_DSN ?? "";
    this.maxPoolSize = options.maxPoolSize ?? 5;
    this.tableName =
      options.table ??
      process.env.IDENTITY_PG_STATUS_LIST_TABLE ??
      PostgresStatusListCacheStore.DEFAULT_TABLE;
  }

  /** The ACTUAL table name in use — respects IDENTITY_PG_STATUS_LIST_TABLE if set. */
  get table(): string {
    return this.tableName;
  }

  getDdl(): string {
    return `
      CREATE TABLE IF NOT EXISTS ${this.tableName} (
          authority_id  TEXT        PRIMARY KEY,
          document      JSONB       NOT NULL,
          generated_at  TIMESTAMPTZ NOT NULL
      );
    `;
  }

  private async getPool(): Promise<PgPoolLike> {
    if (this.pool !== null) {
      return this.pool;
    }
    const pgModule = await loadPgModule("PostgresStatusListCacheStore");
    if (!this.dsn) {
      throw new Error("PostgresStatusListCacheStore: IDENTITY_PG_DSN is not configured.");
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

  async get(authorityId: string): Promise<StatusListCredential | null> {
    try {
      const pool = await this.getPool();
      const { rows } = await pool.query(
        `SELECT document FROM ${this.tableName} WHERE authority_id = $1`,
        [authorityId],
      );
      if (rows.length === 0) {
        return null;
      }
      return rowDocument<StatusListCredential>(rows[0].document);
    } catch (exc) {
      console.error(`PostgresStatusListCacheStore.get(${authorityId}) failed: ${(exc as Error).message}`);
      return null;
    }
  }

  async put(authorityId: string, document: StatusListCredential): Promise<void> {
    const pool = await this.getPool();
    await pool.query(
      `INSERT INTO ${this.tableName} (authority_id, document, generated_at)
       VALUES ($1, $2::jsonb, now())
       ON CONFLICT (authority_id) DO UPDATE
           SET document = EXCLUDED.document, generated_at = now()`,
      [authorityId, JSON.stringify(document)],
    );
  }
}

/**
 * DDL helper — handy for a one-off migration on the DEFAULT names. If
 * you overrode IDENTITY_PG_DID_TABLE / IDENTITY_PG_STATUS_LIST_TABLE,
 * this constant creates the tables with the WRONG name — use the
 * single instance's own getDdl() method instead, which respects the
 * override.
 */
export const DDL = `
CREATE TABLE IF NOT EXISTS ${PostgresDIDDocumentStore.DEFAULT_TABLE} (
    agent_id    TEXT        PRIMARY KEY,
    did         TEXT        NOT NULL UNIQUE,
    document    JSONB       NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ${PostgresStatusListCacheStore.DEFAULT_TABLE} (
    authority_id  TEXT        PRIMARY KEY,
    document      JSONB       NOT NULL,
    generated_at  TIMESTAMPTZ NOT NULL
);
`;
