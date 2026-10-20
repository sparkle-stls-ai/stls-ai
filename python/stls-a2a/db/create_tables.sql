-- create_tables.sql
-- -----------------
-- Schema PostgreSQL per il PostgresLoader di STLS-A2A.
-- Eseguire una sola volta sul database di destinazione.
--
-- Compatibile con: PostgreSQL 13+, GCP Cloud SQL, AWS RDS, Azure DB for PG
--
-- Esecuzione:
--   psql $AUTH_PG_DSN -f create_tables.sql
--
-- Per generare key_id da un raw_key Python:
--   python3 -c "from stls_a2a.stls_a2a_auth_models import generate_api_key; \
--               raw, kid = generate_api_key(); print('raw:', raw); print('kid:', kid)"

-- ── Tabella api keys ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sf_api_keys (
    key_id      TEXT        PRIMARY KEY,            -- SHA-256 hex del raw_key
    client_id   TEXT        NOT NULL,
    scopes      TEXT[]      NOT NULL DEFAULT '{}',
    endpoints   TEXT[]      NOT NULL DEFAULT '{}',  -- vuoto = wildcard
    expires_at  TIMESTAMPTZ,                        -- NULL = nessuna scadenza
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked     BOOLEAN     NOT NULL DEFAULT false,
    description TEXT        NOT NULL DEFAULT '',
    metadata    JSONB       NOT NULL DEFAULT '{}'
);

-- ── Indici ─────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_api_keys_client
    ON sf_api_keys (client_id);

-- Indice parziale: solo chiavi attive (ottimizza load_all nel loader)
CREATE INDEX IF NOT EXISTS idx_api_keys_active
    ON sf_api_keys (created_at DESC)
    WHERE revoked = false;

-- Indice GIN su metadata per query JSON (opzionale)
CREATE INDEX IF NOT EXISTS idx_api_keys_metadata
    ON sf_api_keys USING GIN (metadata);

-- Tabella per registrazione agente e heartbeat
CREATE TABLE IF NOT EXISTS sf_agents (
    agent_id        TEXT        PRIMARY KEY,         -- "calculator@host1:8001"
    agent_name      TEXT        NOT NULL,             -- "calculator"
    host            TEXT        NOT NULL,             -- hostname del processo
    port            INT,                              -- porta del server
    base_url        TEXT        NOT NULL,             -- URL base del server
    plain_url       TEXT,                             -- /agents/calculator/a2a
    secure_url      TEXT,                             -- /agents/calculator/secure-a2a
    framework       TEXT,                             -- custom | google-adk | langchain | ...
    capabilities    JSONB       NOT NULL DEFAULT '{}',
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_heartbeat  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT        NOT NULL DEFAULT 'active'  -- active | draining | offline
);

CREATE INDEX IF NOT EXISTS idx_sf_agents_status
    ON sf_agents (status);

CREATE INDEX IF NOT EXISTS idx_sf_agents_heartbeat
    ON sf_agents (last_heartbeat DESC);

CREATE INDEX IF NOT EXISTS idx_sf_agents_name
    ON sf_agents (agent_name);

COMMENT ON TABLE sf_agents IS 'STLS-A2A agent registry with liveness heartbeat';
COMMENT ON COLUMN sf_agents.agent_id       IS 'agent_name@hostname:port — unique per process';
COMMENT ON COLUMN sf_agents.last_heartbeat IS 'Updated every SF_HEARTBEAT_INTERVAL seconds';
COMMENT ON COLUMN sf_agents.status         IS 'active=running, draining=shutting down, offline=dead';

-- Vista comoda per monitoraggio liveness (threshold 360s = 3x heartbeat di 120s)
CREATE OR REPLACE VIEW sf_agents_live AS
SELECT
    agent_id,
    agent_name,
    host,
    port,
    base_url,
    plain_url,
    secure_url,
    framework,
    registered_at,
    last_heartbeat,
    EXTRACT(EPOCH FROM (now() - last_heartbeat))::int AS seconds_ago,
    status,
    CASE
        WHEN last_heartbeat <= now() - make_interval(secs => 360) THEN 'offline'
        WHEN status != 'active'                                    THEN status
        ELSE 'live'
    END AS liveness
FROM sf_agents
ORDER BY agent_name, host;

COMMENT ON VIEW sf_agents_live IS
    'Agent liveness view — liveness=live se last_heartbeat < 360s fa';

-- Query utili:
-- SELECT * FROM sf_agents_live;
-- SELECT * FROM sf_agents_live WHERE liveness = 'live';
-- SELECT * FROM sf_agents_live WHERE liveness = 'offline';
-- UPDATE sf_agents SET status='offline' WHERE last_heartbeat < now() - interval '10 minutes';