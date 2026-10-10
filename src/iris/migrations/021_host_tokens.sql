CREATE TABLE host_tokens (
    id TEXT PRIMARY KEY,
    host TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    salt TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE TABLE host_recalls (
    recall_id TEXT PRIMARY KEY REFERENCES recalls(id) ON DELETE CASCADE,
    token_id TEXT NOT NULL REFERENCES host_tokens(id)
);
-- Feedback already records the operation in its write transaction. Attribute
-- host HTTP recalls using their issuing credential without changing retrieval.
CREATE TRIGGER host_feedback_actor AFTER INSERT ON admin_operations
WHEN NEW.action='feedback' AND NEW.object_type='recall' AND NEW.actor='host'
BEGIN
    UPDATE admin_operations SET actor=COALESCE((
        SELECT t.host FROM host_recalls r JOIN host_tokens t ON t.id=r.token_id
        WHERE r.recall_id=NEW.object_id),NEW.actor) WHERE id=NEW.id;
END;
INSERT OR IGNORE INTO runtime_settings(key,value_json)
VALUES('host_tokens','{"rate_per_second":20,"burst":60}');

ALTER TABLE goals ADD COLUMN host TEXT;
-- Keep the accepted request's scope for asynchronous dedup review after restart.
ALTER TABLE goals ADD COLUMN host_scope_json TEXT NOT NULL DEFAULT '{"kind":"all"}';
DROP INDEX goals_host_key;
CREATE UNIQUE INDEX goals_host_key ON goals(COALESCE(host,''),host_key) WHERE host_key IS NOT NULL;
