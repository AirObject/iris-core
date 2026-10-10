-- Profiles are entry-local derivatives; evidence IDs deliberately have no FKs
-- so message cleanup and memory purges can mark snapshots stale without pinning data.
INSERT OR IGNORE INTO runtime_settings(key,value_json) VALUES('entry_profile',
'{"enabled":true,"publish_mode":"check_auto","window_days":14,"message_tokens":6000,"memory_tokens":2000,"min_messages":50,"update_days":7,"min_chars":150,"max_chars":500,"generate_timeout_seconds":120,"check_timeout_seconds":120}');
UPDATE runtime_settings SET value_json=json_patch('{"entry_profile_enabled":true}',value_json)
WHERE key='consolidation';

CREATE TABLE entry_profile_settings (
 entry_id TEXT PRIMARY KEY REFERENCES entries(id),
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 publish_mode TEXT NOT NULL DEFAULT 'check_auto' CHECK(publish_mode IN ('check_auto','all_manual')),
 revision INTEGER NOT NULL DEFAULT 1,
 current_version INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL
);
CREATE TABLE entry_profile_versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 entry_id TEXT NOT NULL REFERENCES entries(id),
 version INTEGER NOT NULL,
 base_version INTEGER NOT NULL,
 content TEXT NOT NULL,
 sentences_json TEXT NOT NULL,
 checks_json TEXT NOT NULL,
 change_degree TEXT NOT NULL CHECK(change_degree IN ('small','medium','large')),
 status TEXT NOT NULL CHECK(status IN ('candidate','published','rejected')),
 generated_at TEXT NOT NULL,
 published_at TEXT,
 author TEXT NOT NULL CHECK(author IN ('model','admin')),
 source TEXT NOT NULL CHECK(source IN ('periodic','regenerate','edit','rollback')),
 message_through INTEGER NOT NULL,
 material_json TEXT NOT NULL DEFAULT '{}',
 settings_revision INTEGER NOT NULL,
 UNIQUE(entry_id,version)
);
CREATE INDEX entry_profile_versions_entry ON entry_profile_versions(entry_id,version DESC);
CREATE TABLE entry_profile_attempts (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 entry_id TEXT NOT NULL REFERENCES entries(id),
 run_id INTEGER REFERENCES maintenance_runs(id),
 base_version INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('running','published','candidate','rejected','failed','skipped','conflict')),
 reason TEXT,
 local_day TEXT NOT NULL,
 started_at TEXT NOT NULL,
 finished_at TEXT,
 version INTEGER,
 material_json TEXT NOT NULL DEFAULT '{}',
 outputs_json TEXT NOT NULL DEFAULT '{}',
 calls_json TEXT NOT NULL DEFAULT '[]',
 UNIQUE(entry_id,run_id)
);
CREATE UNIQUE INDEX entry_profile_one_running ON entry_profile_attempts(entry_id) WHERE state='running';
CREATE INDEX entry_profile_attempts_day ON entry_profile_attempts(entry_id,local_day);
