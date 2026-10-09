ALTER TABLE memories ADD COLUMN merged_into INTEGER REFERENCES memories(id);
CREATE INDEX memories_merged_into ON memories(merged_into) WHERE merged_into IS NOT NULL;
CREATE TRIGGER consolidation_no_resurrection BEFORE UPDATE OF lifecycle,merged_into ON memories
WHEN old.merged_into IS NOT NULL AND (new.lifecycle!='deleted' OR new.merged_into IS NOT old.merged_into)
BEGIN SELECT RAISE(ABORT,'merged memory is a permanent placeholder'); END;
INSERT OR IGNORE INTO runtime_settings(key,value_json) VALUES('consolidation',
'{"enabled":true,"max_calls":50,"method":"broad"}');
CREATE TABLE consolidation_runs (
 run_id INTEGER PRIMARY KEY REFERENCES maintenance_runs(id), settings_json TEXT NOT NULL,
 planned INTEGER NOT NULL DEFAULT 0, finished INTEGER NOT NULL DEFAULT 0, skip_reason TEXT
);
CREATE TABLE consolidation_work (
 id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
 payload_json TEXT NOT NULL, importance INTEGER NOT NULL, changed_at TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', result_json TEXT, created_at TEXT NOT NULL
);
CREATE INDEX consolidation_pending ON consolidation_work(state,importance DESC,changed_at DESC,id);
CREATE TABLE consolidation_run_work (
 run_id INTEGER NOT NULL REFERENCES maintenance_runs(id), work_id INTEGER NOT NULL REFERENCES consolidation_work(id),
 outcome TEXT, reason TEXT, details_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(run_id,work_id)
);
CREATE TABLE consolidation_receipts (fingerprint TEXT PRIMARY KEY);
CREATE TABLE consolidation_scanned (memory_id INTEGER PRIMARY KEY REFERENCES memories(id), fingerprint TEXT NOT NULL);
CREATE TABLE consolidation_calls (
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES maintenance_runs(id), work_id INTEGER NOT NULL REFERENCES consolidation_work(id),
 purpose TEXT NOT NULL, created_at TEXT NOT NULL, result TEXT NOT NULL DEFAULT 'interrupted',
 duration_ms INTEGER NOT NULL DEFAULT 0, prompt_tokens INTEGER, completion_tokens INTEGER,
 finish_reason TEXT, reasoning_effort TEXT, error TEXT, raw_output TEXT
);
CREATE INDEX consolidation_calls_run ON consolidation_calls(run_id,id);
CREATE TABLE consolidation_annotations (
 id INTEGER PRIMARY KEY, memory_id INTEGER NOT NULL REFERENCES memories(id), work_id INTEGER NOT NULL REFERENCES consolidation_work(id),
 text TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(memory_id,work_id)
);
