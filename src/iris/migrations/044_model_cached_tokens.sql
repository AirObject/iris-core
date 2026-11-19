-- Unknown cache use stays NULL, including rows from before this migration.
ALTER TABLE model_calls ADD COLUMN cached_tokens INTEGER CHECK (cached_tokens >= 0);
