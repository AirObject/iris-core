-- Undoing an automatic merge is the one sanctioned way back for a placeholder.
-- The guard row exists only inside the undo transaction; everything else stays permanent.
CREATE TABLE consolidation_undo_guard (memory_id INTEGER PRIMARY KEY);
DROP TRIGGER consolidation_no_resurrection;
CREATE TRIGGER consolidation_no_resurrection BEFORE UPDATE OF lifecycle,merged_into ON memories
WHEN old.merged_into IS NOT NULL AND (new.lifecycle!='deleted' OR new.merged_into IS NOT old.merged_into)
 AND NOT EXISTS(SELECT 1 FROM consolidation_undo_guard WHERE memory_id=old.id)
BEGIN SELECT RAISE(ABORT,'merged memory is a permanent placeholder'); END;
-- 1 only for annotations written after auto_v1 rules checked and executed a judgment.
-- Existing rows are report_only model suggestions and keep that meaning.
ALTER TABLE consolidation_annotations ADD COLUMN automatic INTEGER NOT NULL DEFAULT 0;
