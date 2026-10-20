-- Sofia Client — initial schema
-- Run once: psql -d <dbname> -f 001_init.sql

-- Users / apps that can authenticate
CREATE TABLE IF NOT EXISTS sf_user (
  user_id   TEXT PRIMARY KEY,
  pwd_hash  TEXT NOT NULL,
  type      TEXT NOT NULL CHECK (type IN ('user', 'app')),
  app_name  TEXT,           -- populated only for type='app'
  active    BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Agent configurations (url + optional api key)
CREATE TABLE IF NOT EXISTS sf_agents_config (
  agent_name  TEXT PRIMARY KEY,
  url         TEXT NOT NULL,
  api_key     TEXT            -- NULL means no key required
);

-- Access control: which users can use which agents, and with which profile
CREATE TABLE IF NOT EXISTS sf_agents_access (
  agent_name  TEXT NOT NULL REFERENCES sf_agents_config(agent_name) ON DELETE CASCADE,
  user_id     TEXT NOT NULL REFERENCES sf_user(user_id) ON DELETE CASCADE,
  profile     JSONB,          -- arbitrary profile data propagated to the agent
  PRIMARY KEY (agent_name, user_id)
);

-- One-time tokens for app-to-app authentication
CREATE TABLE IF NOT EXISTS sf_app_tokens (
  token       TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL REFERENCES sf_user(user_id) ON DELETE CASCADE,
  profile     JSONB,
  created_at  TIMESTAMPTZ DEFAULT NOW(),
  expires_at  TIMESTAMPTZ NOT NULL,
  used        BOOLEAN NOT NULL DEFAULT FALSE
);

-- Session store (managed automatically by connect-pg-simple)
-- connect-pg-simple will create this if createTableIfMissing=true,
-- but included here for reference.
CREATE TABLE IF NOT EXISTS sf_sessions (
  sid     TEXT PRIMARY KEY,
  sess    JSONB NOT NULL,
  expire  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sf_sessions_expire ON sf_sessions(expire);

-- Example: insert a test user (password: 'changeme')
-- SELECT '$2b$10$...' from running: node -e "const b=require('bcryptjs');b.hash('changeme',10).then(console.log)"
-- INSERT INTO sf_user (user_id, pwd_hash, type, active)
-- VALUES ('admin', '<hash>', 'user', true);
