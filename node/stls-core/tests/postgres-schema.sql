-- postgres-schema.sql
-- --------------------
-- Schema for PostgresLoader (auth/loaders-postgres.ts), identical to the
-- one documented in python/stls-core's stls_core_auth_loaders.py. Used by:
--   - tests/postgres-loader.live.ts / tests/apikeymanager-postgres.live.ts
--     (CREATE TABLE IF NOT EXISTS, so it's safe to run against a real,
--     already-populated database too)
--   - .github/workflows/stls-core-node-interop.yml (against a throwaway
--     Postgres service container)

CREATE TABLE IF NOT EXISTS sf_api_keys (
    key_id      TEXT        PRIMARY KEY,
    client_id   TEXT        NOT NULL,
    scopes      TEXT[]      NOT NULL DEFAULT '{}',
    endpoints   TEXT[]      NOT NULL DEFAULT '{}',
    expires_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked     BOOLEAN     NOT NULL DEFAULT false,
    description TEXT        NOT NULL DEFAULT '',
    metadata    JSONB       NOT NULL DEFAULT '{}'
);
