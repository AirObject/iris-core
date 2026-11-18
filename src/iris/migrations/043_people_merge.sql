-- The current identity remains canonical for live reads. Original account
-- attribution survives new merges and lets a split reassign later messages.
ALTER TABLE platform_identities ADD COLUMN original_subject_id TEXT REFERENCES subjects(id);
ALTER TABLE messages ADD COLUMN original_sender_subject_id TEXT REFERENCES subjects(id);
ALTER TABLE messages ADD COLUMN original_quote_author_subject_id TEXT REFERENCES subjects(id);
CREATE INDEX messages_original_sender ON messages(original_sender_subject_id, id)
    WHERE original_sender_subject_id IS NOT NULL;
CREATE INDEX messages_original_quote_author ON messages(original_quote_author_subject_id, id)
    WHERE original_quote_author_subject_id IS NOT NULL;

CREATE TABLE subject_merges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id INTEGER NOT NULL REFERENCES subject_links(id),
    source_id TEXT NOT NULL REFERENCES subjects(id),
    target_id TEXT NOT NULL REFERENCES subjects(id),
    actor TEXT NOT NULL CHECK(actor IN ('admin','maintenance')),
    created_at TEXT NOT NULL,
    split_at TEXT,
    memory_through INTEGER NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    changes_json TEXT NOT NULL
);
CREATE UNIQUE INDEX subject_merges_active_source ON subject_merges(source_id) WHERE split_at IS NULL;
CREATE INDEX subject_merges_target ON subject_merges(target_id, id);

-- Old merges have no inverse. A new merge can be cleared only by a split of
-- the exact recorded transition, in the same write as its restoration.
DROP TRIGGER subjects_merge_guard;
CREATE TRIGGER subjects_merge_guard BEFORE UPDATE OF merged_into ON subjects
WHEN new.merged_into IS NOT old.merged_into AND (
    (old.merged_into IS NULL AND (old.id IN ('self','scene') OR new.merged_into=old.id
        OR new.merged_into IN ('self','scene')
        OR NOT EXISTS(SELECT 1 FROM subjects WHERE id=new.merged_into AND merged_into IS NULL)))
    OR (old.merged_into IS NOT NULL AND (new.merged_into IS NOT NULL OR NOT EXISTS(
        SELECT 1 FROM subject_merges WHERE source_id=old.id AND target_id=old.merged_into
          AND created_at=old.merged_at AND split_at IS NOT NULL)))
)
BEGIN SELECT RAISE(ABORT,'invalid or immutable subject merge'); END;

-- A denial copied from a folded link can cease to apply after that link
-- returns to its own pair. The actual denied row is still permanent.
ALTER TABLE subject_links ADD COLUMN inherited_denial_from INTEGER REFERENCES subject_links(id);
DROP TRIGGER subject_link_denied_update;
CREATE TRIGGER subject_link_denied_update BEFORE UPDATE ON subject_links
WHEN old.status='denied' AND (new.status!='denied' OR new.belief!=old.belief
    OR new.source_message_id IS NOT old.source_message_id OR new.world IS NOT old.world)
    AND (old.inherited_denial_from IS NULL OR EXISTS(
        SELECT 1 FROM subject_links WHERE id=old.inherited_denial_from AND folded_into=old.id))
BEGIN SELECT RAISE(IGNORE); END;

CREATE INDEX people_review_history ON maintenance_items(object_id, run_id DESC)
    WHERE phase='people';

INSERT OR IGNORE INTO runtime_settings(key,value_json) VALUES('lifecycle','{}');
UPDATE runtime_settings
SET value_json=json_insert(value_json,'$.people_auto_merge_enabled',json('false'))
WHERE key='lifecycle' AND json_valid(value_json);
