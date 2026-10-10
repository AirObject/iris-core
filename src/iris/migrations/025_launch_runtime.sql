-- Rejection and backoff belong to a memory revision, never to the whole queue.
ALTER TABLE memories ADD COLUMN embedding_blocked_revision INTEGER;
ALTER TABLE memories ADD COLUMN embedding_retry_revision INTEGER;
ALTER TABLE memories ADD COLUMN embedding_retry_at TEXT;
ALTER TABLE memories ADD COLUMN embedding_failures INTEGER NOT NULL DEFAULT 0;
-- Cover eligibility with small metadata, including blob length, not blob reads.
CREATE INDEX memories_vector_candidates ON memories(
    embedding_model,length(embedding),id,revision,embedding_retry_revision,embedding_retry_at)
    WHERE lifecycle!='deleted' AND (embedding_blocked_revision IS NULL OR embedding_blocked_revision!=revision);

-- Preserve instant-based accounting for imported timestamps with UTC offsets.
CREATE INDEX model_calls_usage_time ON model_calls(julianday(created_at),
    COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0));
CREATE TABLE model_usage_revision(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL);
INSERT INTO model_usage_revision VALUES(1,0);
CREATE TRIGGER model_usage_insert AFTER INSERT ON model_calls BEGIN
    UPDATE model_usage_revision SET revision=revision+1 WHERE id=1;
END;
CREATE TRIGGER model_usage_delete AFTER DELETE ON model_calls BEGIN
    UPDATE model_usage_revision SET revision=revision+1 WHERE id=1;
END;
CREATE TRIGGER model_usage_update AFTER UPDATE OF prompt_tokens,completion_tokens,created_at ON model_calls BEGIN
    UPDATE model_usage_revision SET revision=revision+1 WHERE id=1;
END;
CREATE INDEX recall_items_by_memory ON recall_items(memory_id);
CREATE INDEX recalls_retention_time ON recalls(julianday(created_at));
CREATE INDEX batch_attempts_raw_retention ON batch_attempts(julianday(finished_at))
    WHERE raw_output IS NOT NULL OR repair_output IS NOT NULL;
CREATE INDEX consolidation_calls_raw_retention ON consolidation_calls(julianday(created_at))
    WHERE raw_output IS NOT NULL;
