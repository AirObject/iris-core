ALTER TABLE entries ADD COLUMN learn_requested_through INTEGER;
ALTER TABLE model_calls ADD COLUMN model_kind TEXT;
ALTER TABLE model_calls ADD COLUMN timed_out INTEGER NOT NULL DEFAULT 0;
CREATE INDEX model_calls_by_time ON model_calls(created_at);
CREATE INDEX batches_ready ON batches(state,next_retry_at,id);
CREATE INDEX memories_missing_vectors ON memories(id) WHERE embedding IS NULL AND lifecycle!='deleted';
