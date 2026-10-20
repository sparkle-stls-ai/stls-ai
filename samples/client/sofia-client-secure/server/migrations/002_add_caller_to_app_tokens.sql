-- Sofia Client — migration 002
-- Adds app_name and caller_user_id to sf_app_tokens.
--
-- app_name        : the authenticating app (mirrors sf_user.app_name)
-- caller_user_id  : the end-user in the calling app (NOT a FK — external identity)
--
-- Run once:
--   psql -d <dbname> -f server/migrations/002_add_caller_to_app_tokens.sql

ALTER TABLE sf_app_tokens ADD COLUMN IF NOT EXISTS app_name       TEXT;
ALTER TABLE sf_app_tokens ADD COLUMN IF NOT EXISTS caller_user_id TEXT;
