-- Optional automatic first message sent to the agent right after connection
-- (production mode only).
--   intro_enabled : TRUE → the client sends the intro message after connecting
--   intro_message : custom text; NULL/empty → 'Please introduce yourself.'
ALTER TABLE sf_agents_config
  ADD COLUMN IF NOT EXISTS intro_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS intro_message TEXT;

-- Example:
-- UPDATE sf_agents_config SET intro_enabled = TRUE WHERE agent_name = 'translator';
-- UPDATE sf_agents_config SET intro_enabled = TRUE, intro_message = 'Ciao, presentati.' WHERE agent_name = 'foo';
