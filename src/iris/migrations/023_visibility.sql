-- Visibility is evaluated from current evidence and current entry settings.
ALTER TABLE entries ADD COLUMN visibility TEXT NOT NULL DEFAULT 'shared'
    CHECK(visibility IN ('shared','entry_only','entries'));
ALTER TABLE entries ADD COLUMN visible_in_json TEXT NOT NULL DEFAULT '[]'
    CHECK(json_valid(visible_in_json) AND json_type(visible_in_json)='array');
CREATE TABLE visibility_version(id INTEGER PRIMARY KEY CHECK(id=1), token TEXT NOT NULL);
INSERT INTO visibility_version VALUES(1,hex(randomblob(16)));
-- A purge removes prose and sources, but must not publish surviving descendants.
-- Retain only entry IDs; their current settings remain authoritative.
CREATE TABLE memory_visibility_roots (
    memory_id INTEGER NOT NULL REFERENCES memories(id),
    entry_id TEXT NOT NULL REFERENCES entries(id),
    PRIMARY KEY(memory_id,entry_id)
);
CREATE INDEX visibility_roots_entry ON memory_visibility_roots(entry_id,memory_id);
CREATE TRIGGER visibility_entries_insert AFTER INSERT ON entries
BEGIN UPDATE visibility_version SET token=hex(randomblob(16)) WHERE id=1; END;
CREATE TRIGGER visibility_entries_delete AFTER DELETE ON entries
BEGIN UPDATE visibility_version SET token=hex(randomblob(16)) WHERE id=1; END;
CREATE TRIGGER visibility_entries_update AFTER UPDATE OF visibility,visible_in_json ON entries
BEGIN UPDATE visibility_version SET token=hex(randomblob(16)) WHERE id=1; END;
CREATE TRIGGER visibility_sources_insert AFTER INSERT ON sources
BEGIN UPDATE visibility_version SET token=hex(randomblob(16)) WHERE id=1; END;
CREATE TRIGGER visibility_sources_delete AFTER DELETE ON sources
BEGIN UPDATE visibility_version SET token=hex(randomblob(16)) WHERE id=1; END;
CREATE TRIGGER visibility_sources_update AFTER UPDATE OF memory_id,kind,message_id,source_memory_id ON sources
BEGIN UPDATE visibility_version SET token=hex(randomblob(16)) WHERE id=1; END;
CREATE TRIGGER visibility_messages_update AFTER UPDATE OF entry_id ON messages
BEGIN UPDATE visibility_version SET token=hex(randomblob(16)) WHERE id=1; END;
CREATE TRIGGER visibility_goal_sources_insert AFTER INSERT ON goal_sources
BEGIN UPDATE visibility_version SET token=hex(randomblob(16)) WHERE id=1; END;
CREATE TRIGGER visibility_goal_sources_delete AFTER DELETE ON goal_sources
BEGIN UPDATE visibility_version SET token=hex(randomblob(16)) WHERE id=1; END;
CREATE TRIGGER visibility_goal_sources_update AFTER UPDATE ON goal_sources
BEGIN UPDATE visibility_version SET token=hex(randomblob(16)) WHERE id=1; END;
CREATE TRIGGER visibility_goals_insert AFTER INSERT ON goals
BEGIN UPDATE visibility_version SET token=hex(randomblob(16)) WHERE id=1; END;
CREATE TRIGGER visibility_goals_delete AFTER DELETE ON goals
BEGIN UPDATE visibility_version SET token=hex(randomblob(16)) WHERE id=1; END;
CREATE TRIGGER visibility_goals_update AFTER UPDATE OF entry_id ON goals
BEGIN UPDATE visibility_version SET token=hex(randomblob(16)) WHERE id=1; END;
CREATE TRIGGER visibility_memory_visibility_roots_insert AFTER INSERT ON memory_visibility_roots
BEGIN UPDATE visibility_version SET token=hex(randomblob(16)) WHERE id=1; END;
CREATE TRIGGER visibility_memory_visibility_roots_delete AFTER DELETE ON memory_visibility_roots
BEGIN UPDATE visibility_version SET token=hex(randomblob(16)) WHERE id=1; END;
CREATE TRIGGER visibility_memory_visibility_roots_update AFTER UPDATE ON memory_visibility_roots
BEGIN UPDATE visibility_version SET token=hex(randomblob(16)) WHERE id=1; END;
