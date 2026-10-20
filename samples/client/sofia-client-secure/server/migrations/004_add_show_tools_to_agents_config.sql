-- Initial visibility of tool calls (DataParts) in production mode.
-- The user can toggle it during the session; debug mode always shows them.
ALTER TABLE sf_agents_config
  ADD COLUMN IF NOT EXISTS show_tools BOOLEAN NOT NULL DEFAULT TRUE;

-- Example:
-- UPDATE sf_agents_config SET show_tools = FALSE WHERE agent_name = 'translator';
