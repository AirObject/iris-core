-- Stable identity tombstones; memory prose/revisions are not identity revisions.
ALTER TABLE subjects ADD COLUMN merged_into TEXT REFERENCES subjects(id);
ALTER TABLE subjects ADD COLUMN merged_at TEXT;
ALTER TABLE subjects ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
CREATE INDEX subjects_merged_into ON subjects(merged_into);

-- Alias IDs remain usable in audit records after removal. Folded rows preserve
-- original evidence in the existing table, so maintenance still protects it.
CREATE TABLE people_alias_copy AS SELECT rowid AS id,* FROM subject_aliases;
DROP TABLE subject_aliases;
CREATE TABLE subject_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id TEXT NOT NULL REFERENCES subjects(id),
    alias TEXT NOT NULL,
    source_message_id INTEGER REFERENCES messages(id),
    folded_into INTEGER REFERENCES subject_aliases(id),
    UNIQUE(subject_id,alias)
);
INSERT INTO subject_aliases(id,subject_id,alias,source_message_id)
    SELECT id,subject_id,alias,source_message_id FROM people_alias_copy;
DROP TABLE people_alias_copy;
CREATE INDEX subject_aliases_by_message ON subject_aliases(source_message_id);
CREATE INDEX subject_aliases_folded ON subject_aliases(folded_into);
CREATE TABLE subject_alias_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id TEXT NOT NULL REFERENCES subjects(id),
    alias TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subject_id,alias)
);

ALTER TABLE subject_links ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE subject_links ADD COLUMN folded_into INTEGER REFERENCES subject_links(id);
ALTER TABLE subject_links ADD COLUMN resolved_at TEXT;
CREATE INDEX subject_links_folded ON subject_links(folded_into);
CREATE INDEX subject_links_a ON subject_links(subject_a);
CREATE INDEX subject_links_b ON subject_links(subject_b);

-- Old rows may use either orientation. Prefer the already canonical row as the
-- survivor to preserve the existing UPSERT target; keep every evidence row.
CREATE TEMP TABLE people_link_folds AS
SELECT l.id,(SELECT k.id FROM subject_links k WHERE k.kind='same_as'
    AND MIN(k.subject_a,k.subject_b)=MIN(l.subject_a,l.subject_b)
    AND MAX(k.subject_a,k.subject_b)=MAX(l.subject_a,l.subject_b)
    ORDER BY (k.subject_a<k.subject_b) DESC,k.id LIMIT 1) AS survivor,
    l.status,l.belief,ROW_NUMBER() OVER (
        PARTITION BY MIN(l.subject_a,l.subject_b),MAX(l.subject_a,l.subject_b)
        ORDER BY CASE l.status WHEN 'denied' THEN 2 WHEN 'confirmed' THEN 1 ELSE 0 END DESC,
            (l.subject_a<l.subject_b) DESC,l.id) AS judgment_order
FROM subject_links l WHERE l.kind='same_as';
UPDATE subject_links SET
    status=(SELECT status FROM people_link_folds WHERE survivor=subject_links.id AND judgment_order=1),
    belief=(SELECT belief FROM people_link_folds WHERE survivor=subject_links.id AND judgment_order=1)
    WHERE id IN (SELECT survivor FROM people_link_folds);
UPDATE subject_links SET folded_into=(SELECT survivor FROM people_link_folds WHERE id=subject_links.id)
    WHERE id IN (SELECT id FROM people_link_folds WHERE id!=survivor);
UPDATE subject_links SET subject_a=MIN(subject_a,subject_b),subject_b=MAX(subject_a,subject_b)
    WHERE kind='same_as' AND folded_into IS NULL;
DROP TABLE people_link_folds;
CREATE UNIQUE INDEX subject_links_unordered ON subject_links(MIN(subject_a,subject_b),MAX(subject_a,subject_b))
    WHERE kind='same_as' AND folded_into IS NULL AND resolved_at IS NULL;

CREATE TRIGGER subject_link_denied_insert BEFORE INSERT ON subject_links
WHEN new.kind='same_as' AND EXISTS(SELECT 1 FROM subject_links WHERE kind='same_as' AND status='denied'
    AND MIN(subject_a,subject_b)=MIN(new.subject_a,new.subject_b)
    AND MAX(subject_a,subject_b)=MAX(new.subject_a,new.subject_b))
BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER subject_link_denied_update BEFORE UPDATE ON subject_links
WHEN old.status='denied' AND (new.status!='denied' OR new.belief!=old.belief
    OR new.source_message_id IS NOT old.source_message_id OR new.world IS NOT old.world)
BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER subject_link_denied_delete BEFORE DELETE ON subject_links WHEN old.status='denied'
BEGIN SELECT RAISE(ABORT,'denied subject link is permanent'); END;
CREATE TRIGGER subject_link_canonical_insert BEFORE INSERT ON subject_links
WHEN new.kind='same_as' AND new.subject_a>new.subject_b AND new.folded_into IS NULL AND new.resolved_at IS NULL
BEGIN
    INSERT INTO subject_links(subject_a,subject_b,kind,belief,status,world,source_message_id,created_at)
    VALUES(new.subject_b,new.subject_a,new.kind,new.belief,new.status,new.world,new.source_message_id,new.created_at)
    ON CONFLICT(subject_a,subject_b,kind) DO UPDATE SET belief=excluded.belief;
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER subject_link_canonical_update BEFORE UPDATE OF subject_a,subject_b ON subject_links
WHEN new.kind='same_as' AND new.subject_a>=new.subject_b AND new.folded_into IS NULL AND new.resolved_at IS NULL
BEGIN SELECT RAISE(ABORT,'same_as needs canonical distinct subjects'); END;
CREATE TRIGGER subject_link_self_insert BEFORE INSERT ON subject_links WHEN new.subject_a=new.subject_b
BEGIN SELECT RAISE(IGNORE); END;

CREATE TRIGGER subject_alias_blocked_insert BEFORE INSERT ON subject_aliases
WHEN EXISTS(SELECT 1 FROM subject_alias_blocks WHERE subject_id=new.subject_id AND alias=new.alias)
BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER subject_alias_blocked_update BEFORE UPDATE OF subject_id,alias ON subject_aliases
WHEN new.folded_into IS NULL AND EXISTS(SELECT 1 FROM subject_alias_blocks WHERE subject_id=new.subject_id AND alias=new.alias)
BEGIN SELECT RAISE(ABORT,'alias was removed by administrator'); END;

CREATE TRIGGER subjects_merge_guard BEFORE UPDATE OF merged_into ON subjects
WHEN new.merged_into IS NOT old.merged_into AND (old.merged_into IS NOT NULL
    OR old.id IN ('self','scene') OR new.merged_into=old.id OR new.merged_into IN ('self','scene')
    OR NOT EXISTS(SELECT 1 FROM subjects WHERE id=new.merged_into AND merged_into IS NULL))
BEGIN SELECT RAISE(ABORT,'invalid or immutable subject merge'); END;
CREATE TRIGGER subject_parent_insert BEFORE INSERT ON subjects
WHEN EXISTS(SELECT 1 FROM subjects WHERE id=new.parent_id AND merged_into IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'parent subject is merged'); END;
CREATE TRIGGER subject_parent_update BEFORE UPDATE OF parent_id ON subjects
WHEN EXISTS(SELECT 1 FROM subjects WHERE id=new.parent_id AND merged_into IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'parent subject is merged'); END;

CREATE TRIGGER subject_memory_insert BEFORE INSERT ON memories
WHEN EXISTS(SELECT 1 FROM subjects WHERE id=new.speaker_subject_id AND merged_into IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'speaker subject is merged'); END;
CREATE TRIGGER subject_memory_update BEFORE UPDATE OF speaker_subject_id ON memories
WHEN EXISTS(SELECT 1 FROM subjects WHERE id=new.speaker_subject_id AND merged_into IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'speaker subject is merged'); END;
CREATE TRIGGER subject_about_insert BEFORE INSERT ON memory_subjects
WHEN EXISTS(SELECT 1 FROM subjects WHERE id=new.subject_id AND merged_into IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'about subject is merged'); END;
CREATE TRIGGER subject_about_update BEFORE UPDATE OF subject_id ON memory_subjects
WHEN EXISTS(SELECT 1 FROM subjects WHERE id=new.subject_id AND merged_into IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'about subject is merged'); END;
CREATE TRIGGER subject_alias_insert BEFORE INSERT ON subject_aliases
WHEN new.folded_into IS NULL AND EXISTS(SELECT 1 FROM subjects WHERE id=new.subject_id AND merged_into IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'alias subject is merged'); END;
CREATE TRIGGER subject_alias_update BEFORE UPDATE OF subject_id,folded_into ON subject_aliases
WHEN new.folded_into IS NULL AND EXISTS(SELECT 1 FROM subjects WHERE id=new.subject_id AND merged_into IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'alias subject is merged'); END;
CREATE TRIGGER subject_identity_insert BEFORE INSERT ON platform_identities
WHEN EXISTS(SELECT 1 FROM subjects WHERE id=new.subject_id AND merged_into IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'identity subject is merged'); END;
CREATE TRIGGER subject_identity_update BEFORE UPDATE OF subject_id ON platform_identities
WHEN EXISTS(SELECT 1 FROM subjects WHERE id=new.subject_id AND merged_into IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'identity subject is merged'); END;
CREATE TRIGGER subject_link_insert BEFORE INSERT ON subject_links
WHEN new.folded_into IS NULL AND new.resolved_at IS NULL AND EXISTS(
    SELECT 1 FROM subjects WHERE id IN (new.subject_a,new.subject_b) AND merged_into IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'link subject is merged'); END;
CREATE TRIGGER subject_link_update BEFORE UPDATE OF subject_a,subject_b,folded_into,resolved_at ON subject_links
WHEN new.folded_into IS NULL AND new.resolved_at IS NULL AND EXISTS(
    SELECT 1 FROM subjects WHERE id IN (new.subject_a,new.subject_b) AND merged_into IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'link subject is merged'); END;
CREATE TRIGGER subject_message_insert BEFORE INSERT ON messages
WHEN EXISTS(SELECT 1 FROM subjects WHERE id IN (new.sender_subject_id,new.quote_author_subject_id) AND merged_into IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'message subject is merged'); END;

CREATE TRIGGER subjects_revision AFTER UPDATE OF name,parent_id,merged_into ON subjects
WHEN new.name IS NOT old.name OR new.parent_id IS NOT old.parent_id OR new.merged_into IS NOT old.merged_into
BEGIN UPDATE subjects SET revision=revision+1 WHERE id=new.id; END;
CREATE TRIGGER alias_subject_insert_revision AFTER INSERT ON subject_aliases
BEGIN UPDATE subjects SET revision=revision+1 WHERE id=new.subject_id; END;
CREATE TRIGGER alias_subject_delete_revision AFTER DELETE ON subject_aliases
BEGIN UPDATE subjects SET revision=revision+1 WHERE id=old.subject_id; END;
CREATE TRIGGER alias_subject_update_revision AFTER UPDATE OF subject_id,folded_into ON subject_aliases
BEGIN UPDATE subjects SET revision=revision+1 WHERE id IN (old.subject_id,new.subject_id); END;
CREATE TRIGGER link_subject_insert_revision AFTER INSERT ON subject_links
BEGIN UPDATE subjects SET revision=revision+1 WHERE id IN (new.subject_a,new.subject_b); END;
CREATE TRIGGER link_revision AFTER UPDATE OF subject_a,subject_b,belief,status,world,source_message_id,folded_into,resolved_at ON subject_links
WHEN new.subject_a IS NOT old.subject_a OR new.subject_b IS NOT old.subject_b OR new.belief IS NOT old.belief
    OR new.status IS NOT old.status OR new.world IS NOT old.world OR new.source_message_id IS NOT old.source_message_id
    OR new.folded_into IS NOT old.folded_into OR new.resolved_at IS NOT old.resolved_at
BEGIN
    UPDATE subject_links SET revision=revision+1 WHERE id=new.id;
    UPDATE subjects SET revision=revision+1 WHERE id IN (old.subject_a,old.subject_b,new.subject_a,new.subject_b);
END;
CREATE TRIGGER identity_subject_update_revision AFTER UPDATE OF subject_id ON platform_identities
BEGIN UPDATE subjects SET revision=revision+1 WHERE id IN (old.subject_id,new.subject_id); END;
