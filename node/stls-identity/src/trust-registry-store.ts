/**
 * trust-registry-store.ts
 * --------------------------
 * 1:1 port of python/stls-identity/src/stls_identity/stls_id_trust_registry_store.py.
 *
 * Postgres-backed storage for the Trust Anchor Registry — Governance
 * Option A: a centralized registry operated by one anchor member,
 * fastest to stand up for a pilot. Options B (quorum signatures) and C
 * (transparency log) are future governance layers on top of this same
 * data shape, not a different schema.
 *
 * Key history: an Authority can have MULTIPLE rows over its lifetime,
 * one per key rotation. The primary key is therefore
 * (authorityId, verificationMethodId), not authorityId alone. Exactly
 * one row per authorityId should have validUntil IS NULL at any time
 * — that's the "current" key. Older rows are kept, not deleted,
 * specifically so a credential signed before a rotation still resolves
 * against the key that was actually valid when it was signed.
 *
 * Environment variables:
 *   IDENTITY_PG_DSN           (shared with postgres-store.ts — same DB)
 *   TRUST_REGISTRY_PG_TABLE   default: sf_identity_trust_anchors
 */

import { multibaseEncodeEd25519Pub } from "./did.js";
import { toPydanticIsoString, type TrustAnchorEntry } from "./models.js";

interface PgRow {
  document: unknown;
}

interface PgClientLike {
  query(text: string, params?: unknown[]): Promise<{ rows: PgRow[] }>;
  release(): void;
}

interface PgPoolLike {
  connect(): Promise<PgClientLike>;
  end(): Promise<void>;
}

interface PgModuleLike {
  Pool: new (config: {
    connectionString: string;
    max: number;
    statement_timeout: number;
  }) => PgPoolLike;
}

async function loadPgModule(): Promise<PgModuleLike> {
  try {
    return (await import("pg")) as unknown as PgModuleLike;
  } catch (exc) {
    throw new Error(
      `PostgresTrustAnchorRegistryStore requires the optional 'pg' package. ` +
        `Install it with: npm install pg\n(${(exc as Error).message})`,
    );
  }
}

function rowToEntry(raw: unknown): TrustAnchorEntry {
  return (typeof raw === "string" ? JSON.parse(raw) : raw) as TrustAnchorEntry;
}

/**
 * Onboarding/rotation helper: builds a TrustAnchorEntry from an
 * Authority's raw Ed25519 public key — the administrative counterpart
 * to TrustAnchorRegistryClient's read path. Not itself a network
 * call; the caller still has to persist the result via
 * TrustAnchorRegistryStoreBase.put() (Option A: a deliberate,
 * out-of-band governance action). Used identically for the FIRST
 * onboarding of an Authority and for every later key rotation — put()
 * tells them apart, this helper doesn't need to.
 */
export function buildTrustAnchorEntry(args: {
  authorityId: string;
  did: string;
  verificationMethodId: string;
  publicKey: Uint8Array;
  onboardedAt?: Date;
}): TrustAnchorEntry {
  return {
    authorityId: args.authorityId,
    did: args.did,
    verificationMethodId: args.verificationMethodId,
    publicKeyMultibase: multibaseEncodeEd25519Pub(args.publicKey),
    onboardedAt: toPydanticIsoString(args.onboardedAt ?? new Date()),
  };
}

export interface TrustAnchorRegistryStoreBase {
  /** The CURRENT key generation for this Authority (validUntil IS NULL). */
  get(authorityId: string): Promise<TrustAnchorEntry | null>;
  /**
   * The entry for this specific key, IF it was the trusted key for
   * this Authority at time `at` (onboardedAt <= at < validUntil-or-now)
   * AND not revoked before `at`.
   */
  getKeyValidAt(authorityId: string, verificationMethodId: string, at: Date): Promise<TrustAnchorEntry | null>;
  /**
   * Same lookup as getKeyValidAt(), but keyed by verificationMethodId
   * alone — what Sentinel actually needs: a credential's proof
   * carries a verificationMethod (a DID fragment), never the
   * registry's short authorityId.
   */
  getKeyValidAtByVerificationMethod(verificationMethodId: string, at: Date): Promise<TrustAnchorEntry | null>;
  /**
   * Upserts this key generation AND, if a different key was previously
   * current for the same authorityId, closes it (validUntil =
   * entry.onboardedAt) atomically.
   */
  put(entry: TrustAnchorEntry): Promise<void>;
  /** Every CURRENT entry (validUntil IS NULL, revokedAt IS NULL). */
  listActive(): Promise<TrustAnchorEntry[]>;
  /** Every key generation this Authority has ever had, oldest first. */
  getHistory(authorityId: string): Promise<TrustAnchorEntry[]>;
  /** Emergency revocation of ONE specific key generation. */
  revokeKey(authorityId: string, verificationMethodId: string, revokedAt: Date): Promise<void>;
  /** Offboards the Authority entirely. Does NOT delete any row (audit). */
  revokeAuthority(authorityId: string, revokedAt: Date): Promise<void>;
  close(): Promise<void>;
}

export interface PostgresTrustAnchorRegistryStoreOptions {
  dsn?: string;
  minPoolSize?: number;
  maxPoolSize?: number;
  table?: string;
}

export class PostgresTrustAnchorRegistryStore implements TrustAnchorRegistryStoreBase {
  static readonly DEFAULT_TABLE = "sf_identity_trust_anchors";

  private readonly dsn: string;
  private readonly maxPoolSize: number;
  private readonly tableName: string;
  private pool: PgPoolLike | null = null;

  constructor(options: PostgresTrustAnchorRegistryStoreOptions = {}) {
    this.dsn = options.dsn ?? process.env.IDENTITY_PG_DSN ?? "";
    this.maxPoolSize = options.maxPoolSize ?? 5;
    this.tableName =
      options.table ??
      process.env.TRUST_REGISTRY_PG_TABLE ??
      PostgresTrustAnchorRegistryStore.DEFAULT_TABLE;
  }

  /** Actual table name in use — respects TRUST_REGISTRY_PG_TABLE. */
  get table(): string {
    return this.tableName;
  }

  /**
   * DDL on this instance's ACTUAL table name. Primary key is
   * (authorityId, verificationMethodId): one row per key generation,
   * not one row per Authority — that's the schema change that makes
   * key history possible.
   */
  getDdl(): string {
    return `
      CREATE TABLE IF NOT EXISTS ${this.tableName} (
          authority_id           TEXT        NOT NULL,
          verification_method_id TEXT        NOT NULL,
          did                    TEXT        NOT NULL,
          document               JSONB       NOT NULL,
          onboarded_at           TIMESTAMPTZ NOT NULL,
          valid_until            TIMESTAMPTZ,
          revoked_at             TIMESTAMPTZ,
          PRIMARY KEY (authority_id, verification_method_id)
      );
      CREATE INDEX IF NOT EXISTS ${this.tableName}_current_idx
          ON ${this.tableName} (authority_id) WHERE valid_until IS NULL;
    `;
  }

  private async getPool(): Promise<PgPoolLike> {
    if (this.pool !== null) {
      return this.pool;
    }
    const pgModule = await loadPgModule();
    if (!this.dsn) {
      throw new Error("PostgresTrustAnchorRegistryStore: IDENTITY_PG_DSN is not configured.");
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

  async get(authorityId: string): Promise<TrustAnchorEntry | null> {
    try {
      const pool = await this.getPool();
      const client = await pool.connect();
      try {
        const { rows } = await client.query(
          `SELECT document FROM ${this.tableName} WHERE authority_id = $1 AND valid_until IS NULL`,
          [authorityId],
        );
        return rows.length === 0 ? null : rowToEntry(rows[0].document);
      } finally {
        client.release();
      }
    } catch (exc) {
      console.error(`PostgresTrustAnchorRegistryStore.get(${authorityId}) failed: ${(exc as Error).message}`);
      return null;
    }
  }

  async getKeyValidAt(
    authorityId: string,
    verificationMethodId: string,
    at: Date,
  ): Promise<TrustAnchorEntry | null> {
    try {
      const pool = await this.getPool();
      const client = await pool.connect();
      try {
        const { rows } = await client.query(
          `SELECT document FROM ${this.tableName}
           WHERE authority_id = $1 AND verification_method_id = $2
             AND onboarded_at <= $3
             AND (valid_until IS NULL OR valid_until > $3)
             AND (revoked_at IS NULL OR revoked_at > $3)`,
          [authorityId, verificationMethodId, at],
        );
        return rows.length === 0 ? null : rowToEntry(rows[0].document);
      } finally {
        client.release();
      }
    } catch (exc) {
      console.error(
        `PostgresTrustAnchorRegistryStore.getKeyValidAt(${authorityId}, ${verificationMethodId}, ${at.toISOString()}) failed: ${(exc as Error).message}`,
      );
      return null;
    }
  }

  async getKeyValidAtByVerificationMethod(
    verificationMethodId: string,
    at: Date,
  ): Promise<TrustAnchorEntry | null> {
    try {
      const pool = await this.getPool();
      const client = await pool.connect();
      try {
        const { rows } = await client.query(
          `SELECT document FROM ${this.tableName}
           WHERE verification_method_id = $1
             AND onboarded_at <= $2
             AND (valid_until IS NULL OR valid_until > $2)
             AND (revoked_at IS NULL OR revoked_at > $2)`,
          [verificationMethodId, at],
        );
        return rows.length === 0 ? null : rowToEntry(rows[0].document);
      } finally {
        client.release();
      }
    } catch (exc) {
      console.error(
        `PostgresTrustAnchorRegistryStore.getKeyValidAtByVerificationMethod(${verificationMethodId}, ${at.toISOString()}) failed: ${(exc as Error).message}`,
      );
      return null;
    }
  }

  /**
   * Read-modify-write for the row being closed, inside a REAL
   * transaction (BEGIN/COMMIT on a single checked-out client — not
   * pool.query() per statement, which could run each statement on a
   * different pooled connection and lose the atomicity this method
   * exists for). document (JSONB) and the valid_until column must move
   * together, in the same statement, or they silently desync — same
   * lesson the Python port's docstring documents from a real bug this
   * fixed there.
   */
  async put(entry: TrustAnchorEntry): Promise<void> {
    const payload = entry;
    const pool = await this.getPool();
    const client = await pool.connect();
    try {
      await client.query("BEGIN");
      try {
        const { rows: previousRows } = await client.query(
          `SELECT document FROM ${this.tableName}
           WHERE authority_id = $1 AND valid_until IS NULL
             AND verification_method_id != $2`,
          [entry.authorityId, entry.verificationMethodId],
        );
        if (previousRows.length > 0) {
          const previousEntry = rowToEntry(previousRows[0].document);
          const closed: TrustAnchorEntry = { ...previousEntry, validUntil: entry.onboardedAt };
          await client.query(
            `UPDATE ${this.tableName} SET document = $3::jsonb, valid_until = $4
             WHERE authority_id = $1 AND verification_method_id = $2`,
            [entry.authorityId, previousEntry.verificationMethodId, JSON.stringify(closed), entry.onboardedAt],
          );
        }

        await client.query(
          `INSERT INTO ${this.tableName}
               (authority_id, verification_method_id, did, document,
                onboarded_at, valid_until, revoked_at)
           VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
           ON CONFLICT (authority_id, verification_method_id) DO UPDATE
               SET did = EXCLUDED.did,
                   document = EXCLUDED.document,
                   valid_until = EXCLUDED.valid_until,
                   revoked_at = EXCLUDED.revoked_at`,
          [
            entry.authorityId,
            entry.verificationMethodId,
            entry.did,
            JSON.stringify(payload),
            entry.onboardedAt,
            entry.validUntil ?? null,
            entry.revokedAt ?? null,
          ],
        );
        await client.query("COMMIT");
      } catch (exc) {
        await client.query("ROLLBACK");
        throw exc;
      }
    } finally {
      client.release();
    }
  }

  async listActive(): Promise<TrustAnchorEntry[]> {
    const pool = await this.getPool();
    const client = await pool.connect();
    try {
      const { rows } = await client.query(
        `SELECT document FROM ${this.tableName} WHERE valid_until IS NULL AND revoked_at IS NULL`,
      );
      return rows.map((r) => rowToEntry(r.document));
    } finally {
      client.release();
    }
  }

  async getHistory(authorityId: string): Promise<TrustAnchorEntry[]> {
    const pool = await this.getPool();
    const client = await pool.connect();
    try {
      const { rows } = await client.query(
        `SELECT document FROM ${this.tableName} WHERE authority_id = $1 ORDER BY onboarded_at ASC`,
        [authorityId],
      );
      return rows.map((r) => rowToEntry(r.document));
    } finally {
      client.release();
    }
  }

  /**
   * Read-modify-write, document (JSONB) and the revoked_at column
   * updated together in the SAME statement, with revokedAt passed as
   * a real timestamptz parameter — not stuffed into the JSONB via a
   * text cast, which would mismatch the column's own type.
   */
  async revokeKey(authorityId: string, verificationMethodId: string, revokedAt: Date): Promise<void> {
    const pool = await this.getPool();
    const client = await pool.connect();
    try {
      const { rows } = await client.query(
        `SELECT document FROM ${this.tableName} WHERE authority_id = $1 AND verification_method_id = $2`,
        [authorityId, verificationMethodId],
      );
      if (rows.length === 0) {
        console.warn(
          `PostgresTrustAnchorRegistryStore.revokeKey: no row for authorityId=${authorityId} verificationMethodId=${verificationMethodId}`,
        );
        return;
      }
      const entry = rowToEntry(rows[0].document);
      const updated: TrustAnchorEntry = { ...entry, revokedAt: toPydanticIsoString(revokedAt) };
      await client.query(
        `UPDATE ${this.tableName} SET document = $3::jsonb, revoked_at = $4
         WHERE authority_id = $1 AND verification_method_id = $2`,
        [authorityId, verificationMethodId, JSON.stringify(updated), revokedAt],
      );
    } finally {
      client.release();
    }
  }

  /**
   * Revokes EVERY key generation for this Authority — implemented by
   * reading the full history and revoking each row individually via
   * revokeKey(), so the document/column sync fix revokeKey() already
   * gets is not duplicated here.
   */
  async revokeAuthority(authorityId: string, revokedAt: Date): Promise<void> {
    const history = await this.getHistory(authorityId);
    for (const entry of history) {
      if (!entry.revokedAt) {
        await this.revokeKey(authorityId, entry.verificationMethodId, revokedAt);
      }
    }
  }
}
