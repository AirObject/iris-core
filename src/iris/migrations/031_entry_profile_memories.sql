-- Store runs this table rebuild with foreign_keys disabled outside the transaction,
-- restores subjects' existing indexes/triggers, and checks all FKs before commit.
CREATE TABLE subjects_new (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('self','person','entry')),
    name TEXT NOT NULL,
    parent_id TEXT REFERENCES subjects(id),
    created_at TEXT NOT NULL,
    merged_into TEXT REFERENCES subjects(id),
    merged_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    CHECK(kind!='entry' OR (parent_id IS NULL AND merged_into IS NULL))
);
INSERT INTO subjects_new SELECT id,kind,name,parent_id,created_at,merged_into,merged_at,revision FROM subjects;
DROP TABLE subjects;
ALTER TABLE subjects_new RENAME TO subjects;

CREATE TABLE entry_subjects (
    entry_id TEXT PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE,
    subject_id TEXT NOT NULL UNIQUE REFERENCES subjects(id) DEFERRABLE INITIALLY DEFERRED
);
INSERT INTO entry_subjects SELECT id,'entry:'||lower(hex(randomblob(16))) FROM entries;
INSERT INTO subjects(id,kind,name,created_at)
    SELECT es.subject_id,'entry',e.name,strftime('%Y-%m-%dT%H:%M:%f+00:00','now')
    FROM entry_subjects es JOIN entries e ON e.id=es.entry_id;
CREATE TRIGGER entry_subject_create AFTER INSERT ON entries BEGIN
    INSERT INTO entry_subjects VALUES(new.id,'entry:'||lower(hex(randomblob(16))));
    INSERT INTO subjects(id,kind,name,created_at)
        SELECT subject_id,'entry',new.name,strftime('%Y-%m-%dT%H:%M:%f+00:00','now')
        FROM entry_subjects WHERE entry_id=new.id;
END;
CREATE TRIGGER entry_subject_rename AFTER UPDATE OF name ON entries BEGIN
    UPDATE subjects SET name=new.name WHERE id=(SELECT subject_id FROM entry_subjects WHERE entry_id=new.id);
END;
CREATE TRIGGER entry_subject_immutable BEFORE UPDATE ON entry_subjects
WHEN new.entry_id IS NOT old.entry_id OR new.subject_id IS NOT old.subject_id
BEGIN SELECT RAISE(ABORT,'entry subject mapping is immutable'); END;
CREATE TRIGGER entry_subject_keep BEFORE DELETE ON entry_subjects
WHEN EXISTS(SELECT 1 FROM entries WHERE id=old.entry_id)
BEGIN SELECT RAISE(ABORT,'entry subject mapping is permanent'); END;
CREATE TRIGGER entry_subject_kind BEFORE UPDATE OF kind,parent_id,merged_into ON subjects
WHEN (old.kind='entry' OR new.kind='entry') AND
    (old.kind!=new.kind OR new.parent_id IS NOT NULL OR new.merged_into IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'entry subject cannot become a person or merge'); END;

ALTER TABLE memories ADD COLUMN owner_entry_id TEXT REFERENCES entries(id);
CREATE INDEX memories_owner_entry ON memories(owner_entry_id,id) WHERE owner_entry_id IS NOT NULL;
CREATE INDEX memories_profile_kind ON memories(kind,id) WHERE kind='入口画像';
CREATE TABLE memory_owner_scopes (
    memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    entry_id TEXT NOT NULL REFERENCES entries(id),
    PRIMARY KEY(memory_id,entry_id)
);
-- Scope rows remember inherited restrictions after provenance links disappear.
-- Multiple owners mean the intersection of singleton sets, hence no host access.
CREATE TRIGGER memory_owner_insert AFTER INSERT ON memories WHEN new.owner_entry_id IS NOT NULL BEGIN
    INSERT OR IGNORE INTO memory_owner_scopes VALUES(new.id,new.owner_entry_id);
END;
CREATE TRIGGER memory_owner_update AFTER UPDATE OF owner_entry_id ON memories
WHEN new.owner_entry_id IS NOT NULL AND new.owner_entry_id IS NOT old.owner_entry_id BEGIN
    INSERT OR IGNORE INTO memory_owner_scopes VALUES(new.id,new.owner_entry_id);
END;
CREATE TRIGGER memory_owner_immutable BEFORE UPDATE OF owner_entry_id ON memories
WHEN old.owner_entry_id IS NOT NULL AND new.owner_entry_id IS NOT old.owner_entry_id
BEGIN SELECT RAISE(ABORT,'hard entry owner is immutable'); END;
CREATE TRIGGER memory_owner_propagate AFTER INSERT ON memory_owner_scopes BEGIN
    INSERT OR IGNORE INTO memory_owner_scopes(memory_id,entry_id)
    SELECT id,new.entry_id FROM (WITH RECURSIVE children(id) AS (
        SELECT new.memory_id UNION SELECT s.memory_id FROM children c JOIN sources s
        ON s.source_memory_id=c.id WHERE s.kind='memory') SELECT id FROM children);
    UPDATE memories SET owner_entry_id=new.entry_id WHERE owner_entry_id IS NULL AND id IN (
        SELECT memory_id FROM memory_owner_scopes WHERE entry_id=new.entry_id);
    UPDATE visibility_version SET token=lower(hex(randomblob(16))) WHERE id=1;
END;
CREATE TRIGGER memory_owner_keep BEFORE DELETE ON memory_owner_scopes
WHEN EXISTS(SELECT 1 FROM memories WHERE id=old.memory_id AND purged_at IS NULL)
BEGIN SELECT RAISE(ABORT,'inherited hard scope cannot be removed'); END;
CREATE TRIGGER memory_owner_no_update BEFORE UPDATE ON memory_owner_scopes
BEGIN SELECT RAISE(ABORT,'inherited hard scope is immutable'); END;
CREATE TRIGGER memory_owner_delete AFTER DELETE ON memory_owner_scopes BEGIN
    UPDATE visibility_version SET token=lower(hex(randomblob(16))) WHERE id=1;
END;
CREATE TRIGGER source_owner_insert AFTER INSERT ON sources WHEN new.kind='memory' BEGIN
    INSERT OR IGNORE INTO memory_owner_scopes SELECT new.memory_id,entry_id
        FROM memory_owner_scopes WHERE memory_id=new.source_memory_id;
END;
CREATE TRIGGER source_owner_update AFTER UPDATE OF memory_id,kind,source_memory_id ON sources WHEN new.kind='memory' BEGIN
    INSERT OR IGNORE INTO memory_owner_scopes SELECT new.memory_id,entry_id
        FROM memory_owner_scopes WHERE memory_id=new.source_memory_id;
END;

CREATE TABLE entry_profile_memories (
    memory_id INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    entry_id TEXT NOT NULL REFERENCES entries(id),
    category TEXT CHECK(category IN ('topic','saying','rule')),
    manual_created INTEGER NOT NULL DEFAULT 0 CHECK(manual_created IN (0,1)),
    CHECK(category IS NOT NULL OR manual_created=1)
);
CREATE INDEX entry_profile_memories_entry ON entry_profile_memories(entry_id,category,memory_id);
CREATE TRIGGER entry_profile_memory_insert BEFORE INSERT ON memories WHEN new.kind='入口画像' AND
    (new.owner_entry_id IS NULL OR new.speaker_subject_id!='self' OR new.event_time IS NOT NULL OR new.world!='real')
BEGIN SELECT RAISE(ABORT,'invalid entry profile representation'); END;
CREATE TRIGGER entry_profile_memory_update BEFORE UPDATE ON memories WHEN new.kind='入口画像' AND
    (new.owner_entry_id IS NULL OR new.speaker_subject_id!='self' OR new.event_time IS NOT NULL OR new.world!='real'
    OR (new.importance>69 AND EXISTS(SELECT 1 FROM entry_profile_memories WHERE memory_id=new.id AND manual_created=0)))
BEGIN SELECT RAISE(ABORT,'invalid entry profile representation'); END;
CREATE TRIGGER entry_profile_metadata_insert BEFORE INSERT ON entry_profile_memories WHEN NOT EXISTS(
    SELECT 1 FROM memories WHERE id=new.memory_id AND kind='入口画像' AND owner_entry_id=new.entry_id
    AND (new.manual_created=1 OR importance<=69))
BEGIN SELECT RAISE(ABORT,'entry profile metadata does not match memory'); END;
CREATE TRIGGER entry_profile_metadata_update BEFORE UPDATE ON entry_profile_memories
WHEN new.memory_id!=old.memory_id OR new.entry_id!=old.entry_id OR new.manual_created!=old.manual_created
BEGIN SELECT RAISE(ABORT,'entry profile ownership is immutable'); END;
CREATE TABLE entry_profile_evidence_receipts (
    memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    identity TEXT NOT NULL,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(memory_id,identity)
);
CREATE TABLE entry_profile_revision_sources (
    memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    PRIMARY KEY(memory_id,revision,source_id)
);

-- Entry subjects are never identities, aliases, contacts, or message authors.
CREATE TRIGGER entry_no_alias_insert BEFORE INSERT ON subject_aliases
WHEN EXISTS(SELECT 1 FROM subjects WHERE id=new.subject_id AND kind='entry')
BEGIN SELECT RAISE(ABORT,'entry subject is not a person'); END;
CREATE TRIGGER entry_no_alias_update BEFORE UPDATE OF subject_id ON subject_aliases
WHEN EXISTS(SELECT 1 FROM subjects WHERE id=new.subject_id AND kind='entry')
BEGIN SELECT RAISE(ABORT,'entry subject is not a person'); END;
CREATE TRIGGER entry_no_identity_insert BEFORE INSERT ON platform_identities
WHEN EXISTS(SELECT 1 FROM subjects WHERE id=new.subject_id AND kind='entry')
BEGIN SELECT RAISE(ABORT,'entry subject is not a person'); END;
CREATE TRIGGER entry_no_identity_update BEFORE UPDATE OF subject_id ON platform_identities
WHEN EXISTS(SELECT 1 FROM subjects WHERE id=new.subject_id AND kind='entry')
BEGIN SELECT RAISE(ABORT,'entry subject is not a person'); END;
CREATE TRIGGER entry_no_link_insert BEFORE INSERT ON subject_links
WHEN EXISTS(SELECT 1 FROM subjects WHERE id IN (new.subject_a,new.subject_b) AND kind='entry')
BEGIN SELECT RAISE(ABORT,'entry subject is not a person'); END;
CREATE TRIGGER entry_no_link_update BEFORE UPDATE OF subject_a,subject_b ON subject_links
WHEN EXISTS(SELECT 1 FROM subjects WHERE id IN (new.subject_a,new.subject_b) AND kind='entry')
BEGIN SELECT RAISE(ABORT,'entry subject is not a person'); END;
CREATE TRIGGER entry_no_message_insert BEFORE INSERT ON messages
WHEN EXISTS(SELECT 1 FROM subjects WHERE id IN (new.sender_subject_id,new.quote_author_subject_id) AND kind='entry')
BEGIN SELECT RAISE(ABORT,'entry subject is not a message author'); END;
CREATE TRIGGER entry_no_message_update BEFORE UPDATE OF sender_subject_id,quote_author_subject_id ON messages
WHEN EXISTS(SELECT 1 FROM subjects WHERE id IN (new.sender_subject_id,new.quote_author_subject_id) AND kind='entry')
BEGIN SELECT RAISE(ABORT,'entry subject is not a message author'); END;
CREATE INDEX memory_owner_scopes_entry ON memory_owner_scopes(entry_id,memory_id);
CREATE TRIGGER entry_subject_insert_guard BEFORE INSERT ON entry_subjects
WHEN EXISTS(SELECT 1 FROM entry_subjects WHERE entry_id=new.entry_id OR subject_id=new.subject_id)
 OR EXISTS(SELECT 1 FROM subjects WHERE id=new.subject_id AND kind!='entry')
BEGIN SELECT RAISE(ABORT,'entry subject mapping is unique and typed'); END;
CREATE TRIGGER memory_merge_owner AFTER UPDATE OF merged_into ON memories
WHEN new.merged_into IS NOT NULL AND new.merged_into IS NOT old.merged_into BEGIN
    INSERT OR IGNORE INTO memory_owner_scopes SELECT new.merged_into,entry_id
        FROM memory_owner_scopes WHERE memory_id=new.id;
END;
CREATE TRIGGER profile_merge_partition BEFORE UPDATE OF merged_into ON memories
WHEN new.merged_into IS NOT NULL AND new.merged_into IS NOT old.merged_into
AND (old.kind='入口画像' OR EXISTS(SELECT 1 FROM memories WHERE id=new.merged_into AND kind='入口画像'))
AND NOT EXISTS(SELECT 1 FROM entry_profile_memories a JOIN entry_profile_memories b
    ON a.entry_id=b.entry_id AND a.category IS b.category
    WHERE a.memory_id=new.id AND b.memory_id=new.merged_into)
BEGIN SELECT RAISE(ABORT,'entry profile merge partition mismatch'); END;
CREATE TRIGGER profile_merge_receipts AFTER UPDATE OF merged_into ON memories
WHEN old.kind='入口画像' AND new.merged_into IS NOT NULL AND new.merged_into IS NOT old.merged_into BEGIN
    INSERT OR IGNORE INTO entry_profile_evidence_receipts
        SELECT new.merged_into,identity,source_id,created_at FROM entry_profile_evidence_receipts WHERE memory_id=new.id;
    INSERT OR IGNORE INTO entry_profile_revision_sources(memory_id,revision,source_id)
        SELECT k.memory_id,m.revision,k.id FROM sources k JOIN memories m ON m.id=k.memory_id
        WHERE k.memory_id=new.merged_into AND k.kind='message' AND EXISTS(
            SELECT 1 FROM entry_profile_revision_sources r JOIN sources s ON s.id=r.source_id
            WHERE (r.memory_id=new.merged_into AND r.revision=m.revision-1 AND s.id=k.id)
               OR (r.memory_id=new.id AND r.revision=old.revision
                   AND s.kind=k.kind AND s.message_id IS k.message_id AND s.note IS k.note));
END;

CREATE TRIGGER entry_subject_cleanup AFTER DELETE ON entry_subjects
WHEN NOT EXISTS(SELECT 1 FROM entries WHERE id=old.entry_id) BEGIN
    DELETE FROM subjects WHERE id=old.subject_id;
END;
CREATE TRIGGER profile_unchanged_body_sources AFTER UPDATE OF revision ON memories
WHEN old.kind='入口画像' AND new.kind=old.kind AND new.purged_at IS NULL
 AND new.revision>old.revision AND new.content=old.content AND new.stance=old.stance BEGIN
    INSERT OR IGNORE INTO entry_profile_revision_sources(memory_id,revision,source_id)
        SELECT new.id,new.revision,source_id FROM entry_profile_revision_sources
        WHERE memory_id=new.id AND revision=old.revision;
END;
