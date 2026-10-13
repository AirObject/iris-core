-- Structural transition only: model rebuilding runs in the entry task.
CREATE TABLE entry_profile_memory_state (
 entry_id TEXT PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE,
 cursor INTEGER NOT NULL DEFAULT 0,
 anchors_json TEXT NOT NULL DEFAULT '[]',
 version INTEGER NOT NULL DEFAULT 0,
 projection_json TEXT NOT NULL DEFAULT '[]',
 generated_at TEXT NOT NULL,
 legacy_pending INTEGER NOT NULL DEFAULT 0,
 rebuild_pending INTEGER NOT NULL DEFAULT 0,
 diagnostics_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE entry_profile_projection_dirty (
 entry_id TEXT PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE
);
CREATE TABLE entry_profile_memory_plans (
 entry_id TEXT NOT NULL REFERENCES entries(id),
 plan_id TEXT NOT NULL,
 receipt_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(entry_id,plan_id)
);
CREATE TABLE entry_profile_legacy_migration (
 entry_id TEXT NOT NULL REFERENCES entries(id),
 legacy_version INTEGER NOT NULL,
 sentence_index INTEGER NOT NULL,
 disposition TEXT NOT NULL,
 memory_id INTEGER REFERENCES memories(id),
 category TEXT,
 created_at TEXT NOT NULL,
 PRIMARY KEY(entry_id,legacy_version,sentence_index)
);
INSERT INTO entry_profile_memory_state(entry_id,version,generated_at,legacy_pending,rebuild_pending)
 SELECT e.id,COALESCE(s.current_version,0),strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),1,
 EXISTS(SELECT 1 FROM entry_profile_versions v WHERE v.entry_id=e.id)
 FROM entries e LEFT JOIN entry_profile_settings s ON s.entry_id=e.id;
INSERT INTO entry_profile_projection_dirty SELECT id FROM entries;
CREATE TRIGGER entry_profile_memory_state_create AFTER INSERT ON entries BEGIN
 INSERT INTO entry_profile_memory_state(entry_id,generated_at)
 VALUES(new.id,strftime('%Y-%m-%dT%H:%M:%f+00:00','now'));
 INSERT OR IGNORE INTO entry_profile_projection_dirty VALUES(new.id);
END;
CREATE TRIGGER ep_projection_memories_0 AFTER INSERT ON memories BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_memories_1 AFTER DELETE ON memories BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_memories_2 AFTER UPDATE OF content,kind,revision,lifecycle,purged_at,merged_into ON memories BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_sources_0 AFTER INSERT ON sources BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_sources_1 AFTER UPDATE ON sources BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_sources_2 AFTER DELETE ON sources BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_entry_profile_memories_0 AFTER INSERT ON entry_profile_memories BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_entry_profile_memories_1 AFTER UPDATE ON entry_profile_memories BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_entry_profile_memories_2 AFTER DELETE ON entry_profile_memories BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_entry_profile_revision_sources_0 AFTER INSERT ON entry_profile_revision_sources BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_entry_profile_revision_sources_1 AFTER UPDATE ON entry_profile_revision_sources BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_entry_profile_revision_sources_2 AFTER DELETE ON entry_profile_revision_sources BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_messages_0 AFTER UPDATE OF content,entry_id,kind,sender_subject_id,quote_content,quote_author_subject_id,occurred_at ON messages BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_messages_1 AFTER DELETE ON messages BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_memory_owner_scopes_0 AFTER INSERT ON memory_owner_scopes BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_memory_owner_scopes_1 AFTER DELETE ON memory_owner_scopes BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_entries_0 AFTER UPDATE OF kind,visibility,visible_in_json ON entries BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_entry_profile_settings_0 AFTER INSERT ON entry_profile_settings BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_entry_profile_settings_1 AFTER UPDATE ON entry_profile_settings BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_entry_profile_settings_2 AFTER DELETE ON entry_profile_settings BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_runtime_settings_0 AFTER INSERT ON runtime_settings WHEN new.key='entry_profile' BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_runtime_settings_1 AFTER UPDATE ON runtime_settings WHEN new.key='entry_profile' BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_runtime_settings_2 AFTER DELETE ON runtime_settings WHEN old.key='entry_profile' BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT DISTINCT entry_id FROM entry_profile_memories;
END;
CREATE TRIGGER ep_projection_removed AFTER DELETE ON entry_profile_memories BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty
 SELECT old.entry_id WHERE EXISTS(SELECT 1 FROM entries WHERE id=old.entry_id);
END;
