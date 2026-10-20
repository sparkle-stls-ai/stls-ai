-- Initial state of the "suggested actions" chips (production mode).
-- The user can toggle it during the session; debug mode always shows them.
ALTER TABLE sf_agents_config
  ADD COLUMN IF NOT EXISTS show_suggestions BOOLEAN NOT NULL DEFAULT TRUE;
