CREATE TABLE IF NOT EXISTS sf_identity_did_documents (
    agent_id    TEXT        PRIMARY KEY,
    did         TEXT        NOT NULL UNIQUE,
    document    JSONB       NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sf_identity_status_list_cache (
    authority_id  TEXT        PRIMARY KEY,
    document      JSONB       NOT NULL,
    generated_at  TIMESTAMPTZ NOT NULL
);