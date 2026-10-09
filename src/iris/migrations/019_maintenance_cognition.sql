-- Snapshot new cognitive switches at request/upgrade; old completed runs stay closed.
UPDATE runtime_settings SET value_json=json_patch(
    '{"enabled":true,"max_calls":50,"method":"broad","resolution":"report_only_v1","merge_enabled":true,"conflict_enabled":true,"dependency_enabled":true,"persona_enabled":true,"goal_review_enabled":true}',value_json)
WHERE key='consolidation';
INSERT OR IGNORE INTO consolidation_runs(run_id,settings_json)
SELECT id,(SELECT value_json FROM runtime_settings WHERE key='consolidation')
FROM maintenance_runs WHERE state='running';
ALTER TABLE maintenance_runs ADD COLUMN goal_through INTEGER NOT NULL DEFAULT 0;
UPDATE maintenance_runs SET goal_through=(SELECT COALESCE(MAX(id),0) FROM goals) WHERE state='running';

CREATE TABLE maintenance_persona (
    run_id INTEGER PRIMARY KEY REFERENCES maintenance_runs(id),
    attempt_id INTEGER UNIQUE REFERENCES persona_attempts(id),
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT,
    due_json TEXT NOT NULL DEFAULT '{}',
    details_json TEXT NOT NULL DEFAULT '{}'
);

-- Persona calls share the same durable per-run budget and usage ledger.
-- They belong to a persona attempt instead of a consolidation pair/dependency.
CREATE TABLE maintenance_model_calls_new (
    id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES maintenance_runs(id),
    work_id INTEGER REFERENCES consolidation_work(id),
    purpose TEXT NOT NULL, created_at TEXT NOT NULL, result TEXT NOT NULL DEFAULT 'interrupted',
    duration_ms INTEGER NOT NULL DEFAULT 0, prompt_tokens INTEGER, completion_tokens INTEGER,
    finish_reason TEXT, reasoning_effort TEXT, error TEXT, raw_output TEXT
);
INSERT INTO maintenance_model_calls_new SELECT * FROM consolidation_calls;
DROP TABLE consolidation_calls;
ALTER TABLE maintenance_model_calls_new RENAME TO consolidation_calls;
CREATE INDEX consolidation_calls_run ON consolidation_calls(run_id,id);
