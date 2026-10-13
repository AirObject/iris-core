-- Refusal trees retain the original frozen root and attempts. No historical replay.
ALTER TABLE batches ADD COLUMN parent_batch_id INTEGER REFERENCES batches(id);
ALTER TABLE batches ADD COLUMN root_batch_id INTEGER REFERENCES batches(id);
ALTER TABLE batches ADD COLUMN split_slot INTEGER CHECK(split_slot IN (0,1));
ALTER TABLE batches ADD COLUMN split_path TEXT NOT NULL DEFAULT '' CHECK(split_path NOT GLOB '*[^01]*');
ALTER TABLE batches ADD COLUMN context_mode TEXT NOT NULL DEFAULT 'full' CHECK(context_mode IN ('full','target_only'));
CREATE UNIQUE INDEX batches_child_slot ON batches(parent_batch_id,split_slot);
CREATE UNIQUE INDEX batches_root_path ON batches(root_batch_id,split_path);
CREATE INDEX batches_leaf_order ON batches(entry_id,COALESCE(root_batch_id,id),split_path,id)
 WHERE state IN ('waiting','running');
CREATE INDEX batches_roots ON batches(entry_id,id) WHERE parent_batch_id IS NULL;
CREATE TRIGGER batch_tree_insert BEFORE INSERT ON batches
WHEN (new.parent_batch_id IS NULL AND (new.root_batch_id IS NOT NULL OR new.split_slot IS NOT NULL OR new.split_path!=''))
 OR (new.parent_batch_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM batches p WHERE p.id=new.parent_batch_id AND p.entry_id=new.entry_id
   AND new.root_batch_id=COALESCE(p.root_batch_id,p.id)
   AND new.split_slot IN (0,1) AND new.split_path=p.split_path||CAST(new.split_slot AS TEXT)))
BEGIN SELECT RAISE(ABORT,'invalid batch tree identity'); END;
CREATE TRIGGER batch_tree_immutable BEFORE UPDATE OF parent_batch_id,root_batch_id,split_slot,split_path,entry_id ON batches
WHEN new.parent_batch_id IS NOT old.parent_batch_id OR new.root_batch_id IS NOT old.root_batch_id
 OR new.split_slot IS NOT old.split_slot OR new.split_path!=old.split_path OR new.entry_id!=old.entry_id
BEGIN SELECT RAISE(ABORT,'batch tree identity is immutable'); END;

-- NULL means unknown for old attempts and interruptions before material assembly.
ALTER TABLE batch_attempts ADD COLUMN context_mode TEXT CHECK(context_mode IN ('full','target_only'));
ALTER TABLE batch_attempts ADD COLUMN sent_context_ids TEXT;
ALTER TABLE batch_attempts ADD COLUMN prompt_version TEXT;
CREATE TABLE batch_memory_effects (
 root_batch_id INTEGER NOT NULL REFERENCES batches(id),
 memory_id INTEGER NOT NULL REFERENCES memories(id),
 effect TEXT NOT NULL CHECK(effect IN ('created','confirmed')),
 first_batch_id INTEGER NOT NULL REFERENCES batches(id),
 PRIMARY KEY(root_batch_id,memory_id)
) WITHOUT ROWID;
CREATE TRIGGER batch_split_result_audit AFTER UPDATE OF state ON batches
WHEN old.state='running' AND new.state='split' BEGIN
 INSERT INTO admin_operations(actor,action,object_type,object_id,details_json,created_at)
 VALUES('system','batch_result','batch',CAST(new.id AS TEXT),
  json_object('batch_id',new.id,'entry_id',new.entry_id,'state','split','attempt_count',new.attempt_count,
              'created_count',0,'updated_count',0,'confirmed_count',0),new.finished_at);
END;

-- Propagate redaction upwards, including late result/attempt writes after purge.
-- Siblings are unaffected unless independently implicated by erased messages.
CREATE TRIGGER purge_batch_ancestors AFTER INSERT ON purge_redactions
WHEN new.table_name='batches' BEGIN
 INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
 SELECT 'batches',CAST(id AS TEXT),new.cleared_at,'{}' FROM (
  WITH RECURSIVE ancestors(id) AS (
   SELECT parent_batch_id FROM batches WHERE id=CAST(new.row_id AS INTEGER)
   UNION ALL SELECT b.parent_batch_id FROM batches b JOIN ancestors a ON b.id=a.id
   WHERE b.parent_batch_id IS NOT NULL)
  SELECT id FROM ancestors WHERE id IS NOT NULL);
 UPDATE batches SET result_json=result_json WHERE id IN (
  WITH RECURSIVE ancestors(id) AS (
   SELECT parent_batch_id FROM batches WHERE id=CAST(new.row_id AS INTEGER)
   UNION ALL SELECT b.parent_batch_id FROM batches b JOIN ancestors a ON b.id=a.id
   WHERE b.parent_batch_id IS NOT NULL)
  SELECT id FROM ancestors WHERE id IS NOT NULL);
 UPDATE batch_attempts SET raw_output=raw_output WHERE batch_id IN (
  WITH RECURSIVE ancestors(id) AS (
   SELECT parent_batch_id FROM batches WHERE id=CAST(new.row_id AS INTEGER)
   UNION ALL SELECT b.parent_batch_id FROM batches b JOIN ancestors a ON b.id=a.id
   WHERE b.parent_batch_id IS NOT NULL)
  SELECT id FROM ancestors WHERE id IS NOT NULL);
 UPDATE model_calls SET error_summary=error_summary WHERE batch_id IN (
  WITH RECURSIVE ancestors(id) AS (
   SELECT parent_batch_id FROM batches WHERE id=CAST(new.row_id AS INTEGER)
   UNION ALL SELECT b.parent_batch_id FROM batches b JOIN ancestors a ON b.id=a.id
   WHERE b.parent_batch_id IS NOT NULL)
  SELECT id FROM ancestors WHERE id IS NOT NULL);
END;
