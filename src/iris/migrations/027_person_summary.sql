-- Person summaries contain only globally visible learned memories.
INSERT OR IGNORE INTO runtime_settings(key,value_json) VALUES('person_summary',
'{"enabled":true,"publish_mode":"check_auto","memory_tokens":3000,"min_memories":8,"min_changed":5,"update_days":14,"max_subjects_per_run":10,"max_items":5,"max_chars":200,"max_tokens":250,"generate_timeout_seconds":120,"check_timeout_seconds":120}');

CREATE TABLE person_summary_settings (
 subject_id TEXT PRIMARY KEY REFERENCES subjects(id),
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 publish_mode TEXT NOT NULL DEFAULT 'check_auto' CHECK(publish_mode IN ('check_auto','all_manual')),
 revision INTEGER NOT NULL DEFAULT 1,
 current_version INTEGER NOT NULL DEFAULT 0,
 needs_regeneration INTEGER NOT NULL DEFAULT 0 CHECK(needs_regeneration IN (0,1)),
 updated_at TEXT NOT NULL
);
CREATE TABLE person_summary_versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 subject_id TEXT NOT NULL REFERENCES subjects(id),
 version INTEGER NOT NULL,
 base_version INTEGER NOT NULL,
 content TEXT NOT NULL,
 sentences_json TEXT NOT NULL,
 checks_json TEXT NOT NULL,
 change_degree TEXT NOT NULL CHECK(change_degree IN ('small','medium','large')),
 status TEXT NOT NULL CHECK(status IN ('candidate','published','rejected')),
 author TEXT NOT NULL CHECK(author IN ('model','admin')),
 source TEXT NOT NULL CHECK(source IN ('periodic','regenerate','edit','rollback')),
 generated_at TEXT NOT NULL,
 published_at TEXT,
 settings_revision INTEGER NOT NULL,
 subject_revision INTEGER NOT NULL,
 material_json TEXT NOT NULL DEFAULT '{}',
 memory_state_json TEXT NOT NULL DEFAULT '{}',
 invalidated_reason TEXT,
 cleared_at TEXT,
 UNIQUE(subject_id,version)
);
CREATE INDEX person_summary_versions_subject ON person_summary_versions(subject_id,version DESC);
CREATE TABLE person_summary_attempts (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 subject_id TEXT NOT NULL REFERENCES subjects(id),
 run_id INTEGER REFERENCES maintenance_runs(id),
 base_version INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('running','published','candidate','rejected','failed','skipped','conflict')),
 reason TEXT,
 started_at TEXT NOT NULL,
 finished_at TEXT,
 version INTEGER,
 material_json TEXT NOT NULL DEFAULT '{}',
 outputs_json TEXT NOT NULL DEFAULT '{}',
 calls_json TEXT NOT NULL DEFAULT '[]',
 cleared_at TEXT,
 UNIQUE(subject_id,run_id)
);
CREATE UNIQUE INDEX person_summary_one_running ON person_summary_attempts(subject_id) WHERE state='running';
-- Numeric evidence identities do not pin memories or retain their prose.
CREATE TABLE person_summary_version_memories (
 version_id INTEGER NOT NULL REFERENCES person_summary_versions(id) ON DELETE CASCADE,
 memory_id INTEGER NOT NULL,
 PRIMARY KEY(version_id,memory_id)
);
CREATE INDEX person_summary_version_memory ON person_summary_version_memories(memory_id,version_id);
CREATE TABLE person_summary_attempt_memories (
 attempt_id INTEGER NOT NULL REFERENCES person_summary_attempts(id) ON DELETE CASCADE,
 memory_id INTEGER NOT NULL,
 PRIMARY KEY(attempt_id,memory_id)
);
CREATE INDEX person_summary_attempt_memory ON person_summary_attempt_memories(memory_id,attempt_id);

-- Keep merge behavior atomic with the existing same_as operation, without a new
-- call site in people.py. Historical versions remain inspectable but unusable.
CREATE TRIGGER person_summary_subject_merge AFTER UPDATE OF merged_into ON subjects
WHEN new.merged_into IS NOT NULL AND old.merged_into IS NULL
BEGIN
 UPDATE person_summary_versions SET invalidated_reason='subject_merged' WHERE subject_id=new.id;
 UPDATE person_summary_versions SET status='rejected' WHERE subject_id=new.id AND status='candidate';
 UPDATE person_summary_settings SET revision=revision+1,needs_regeneration=1,updated_at=new.merged_at
 WHERE subject_id IN (new.id,new.merged_into);
 UPDATE person_summary_attempts SET state='conflict',reason='subject_merged',finished_at=new.merged_at
 WHERE subject_id IN (new.id,new.merged_into) AND state='running';
END;
