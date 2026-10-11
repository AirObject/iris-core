-- Indexed provenance for privacy erasure. Never match or replace prose.
-- A snapshot revision older than the purged placeholder is stale; a fresh
-- reference to the placeholder is not. Row markers protect in-flight writes.
PRAGMA defer_foreign_keys=ON;
CREATE TEMP TABLE purge_persona_copy AS SELECT * FROM persona_versions;
DROP TABLE persona_versions;
CREATE TABLE persona_versions (
 id INTEGER PRIMARY KEY, content TEXT NOT NULL, created_at TEXT NOT NULL,
 is_current INTEGER NOT NULL DEFAULT 1,
 status TEXT NOT NULL DEFAULT 'history' CHECK(status IN ('current','pending','rejected','superseded','history')),
 source TEXT NOT NULL DEFAULT 'initial_setting' CHECK(source IN ('initial_setting','periodic','regenerate','admin_edit','rollback','purge')),
 sentences_json TEXT NOT NULL DEFAULT '[]', checks_json TEXT NOT NULL DEFAULT '{}',
 change_degree TEXT NOT NULL DEFAULT 'small' CHECK(change_degree IN ('small','medium','large')),
 base_version_id INTEGER REFERENCES persona_versions(id), rollback_of INTEGER REFERENCES persona_versions(id),
 published_at TEXT, material_json TEXT NOT NULL DEFAULT '{}', self_snapshot_json TEXT,
 author TEXT NOT NULL DEFAULT 'model'
);
INSERT INTO persona_versions SELECT *,CASE WHEN source IN ('admin_edit','rollback','initial_setting') THEN 'admin' ELSE 'model' END FROM purge_persona_copy;
DROP TABLE purge_persona_copy;
CREATE UNIQUE INDEX persona_one_current ON persona_versions(is_current) WHERE is_current=1;
CREATE UNIQUE INDEX persona_one_pending ON persona_versions(status) WHERE status='pending';
CREATE INDEX purge_persona_source ON persona_versions(source,id);
CREATE TEMP TABLE purge_profile_copy AS SELECT * FROM entry_profile_versions;
DROP TABLE entry_profile_versions;
CREATE TABLE entry_profile_versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT NOT NULL REFERENCES entries(id),
 version INTEGER NOT NULL, base_version INTEGER NOT NULL, content TEXT NOT NULL,
 sentences_json TEXT NOT NULL, checks_json TEXT NOT NULL,
 change_degree TEXT NOT NULL CHECK(change_degree IN ('small','medium','large')),
 status TEXT NOT NULL CHECK(status IN ('candidate','published','rejected')),
 generated_at TEXT NOT NULL, published_at TEXT,
 author TEXT NOT NULL CHECK(author IN ('model','admin','purge')),
 source TEXT NOT NULL CHECK(source IN ('periodic','regenerate','edit','rollback','purge')),
 message_through INTEGER NOT NULL, material_json TEXT NOT NULL DEFAULT '{}',
 settings_revision INTEGER NOT NULL, UNIQUE(entry_id,version)
);
INSERT INTO entry_profile_versions SELECT * FROM purge_profile_copy;
DROP TABLE purge_profile_copy;
CREATE INDEX entry_profile_versions_entry ON entry_profile_versions(entry_id,version DESC);
CREATE TABLE purge_redactions (
 table_name TEXT NOT NULL,row_id TEXT NOT NULL,cleared_at TEXT NOT NULL,
 reason TEXT NOT NULL DEFAULT '因彻底清除而清空',metadata_json TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(table_name,row_id)
) WITHOUT ROWID;
CREATE TABLE purge_message_tombstones(message_id INTEGER PRIMARY KEY,cleared_at TEXT NOT NULL);
CREATE TABLE purge_record_refs (
 table_name TEXT NOT NULL,row_id TEXT NOT NULL,ref_kind TEXT NOT NULL,ref_id TEXT NOT NULL,
 revision INTEGER NOT NULL,PRIMARY KEY(table_name,row_id,ref_kind,ref_id,revision)
) WITHOUT ROWID;
CREATE INDEX purge_refs_target ON purge_record_refs(ref_kind,ref_id,table_name,row_id,revision);
CREATE INDEX purge_calls_work ON consolidation_calls(work_id,id);
CREATE INDEX purge_calls_run_purpose ON consolidation_calls(run_id,purpose,id);
CREATE INDEX purge_run_work ON consolidation_run_work(work_id,run_id);
CREATE INDEX purge_annotations_work ON consolidation_annotations(work_id,id);
CREATE INDEX purge_items_work ON maintenance_items(phase,item_key,run_id);
CREATE INDEX purge_profile_attempts_run ON entry_profile_attempts(run_id,id);
CREATE INDEX memories_purged_ids ON memories(id,revision) WHERE purged_at IS NOT NULL;
CREATE VIEW purge_stale_references AS
 SELECT r.table_name,r.row_id FROM purge_record_refs r JOIN memories m ON m.id=CAST(r.ref_id AS INTEGER)
 WHERE r.ref_kind='memory' AND m.purged_at IS NOT NULL AND r.revision<m.revision
 UNION
 SELECT r.table_name,r.row_id FROM purge_record_refs r JOIN purge_message_tombstones m ON m.message_id=CAST(r.ref_id AS INTEGER)
 WHERE r.ref_kind='message' AND r.revision=0;

CREATE TRIGGER purge_batches_insert AFTER INSERT ON batches BEGIN

    DELETE FROM purge_record_refs WHERE table_name='batches' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'batches',CAST(new.id AS TEXT),* FROM (SELECT 'memory',CAST(x.value AS TEXT),-1 FROM json_each(new.result_json) a,json_each(CASE WHEN a.type='array' THEN a.value ELSE '[]' END) x WHERE a.key IN ('created','updated','confirmed') AND x.type='integer');

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'batches',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.result_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='batches' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE batches SET result_json=json_set(json_remove(new.result_json,'$.dropped','$.normalizations'),'$.redacted',1),
      last_error=NULL
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.result_json IS NOT json_set(json_remove(new.result_json,'$.dropped','$.normalizations'),'$.redacted',1) OR new.last_error IS NOT NULL);
END;

CREATE TRIGGER purge_batches_update AFTER UPDATE ON batches BEGIN

    DELETE FROM purge_record_refs WHERE table_name='batches' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'batches',CAST(new.id AS TEXT),* FROM (SELECT 'memory',CAST(x.value AS TEXT),-1 FROM json_each(new.result_json) a,json_each(CASE WHEN a.type='array' THEN a.value ELSE '[]' END) x WHERE a.key IN ('created','updated','confirmed') AND x.type='integer');

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'batches',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.result_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='batches' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE batches SET result_json=json_set(json_remove(new.result_json,'$.dropped','$.normalizations'),'$.redacted',1),
      last_error=NULL
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.result_json IS NOT json_set(json_remove(new.result_json,'$.dropped','$.normalizations'),'$.redacted',1) OR new.last_error IS NOT NULL);
END;

CREATE TRIGGER purge_batches_delete AFTER DELETE ON batches BEGIN
 DELETE FROM purge_record_refs WHERE table_name='batches' AND row_id=CAST(old.id AS TEXT);
 DELETE FROM purge_redactions WHERE table_name='batches' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_batch_attempts_insert AFTER INSERT ON batch_attempts BEGIN

    DELETE FROM purge_record_refs WHERE table_name='batch_attempts' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'batch_attempts',CAST(new.id AS TEXT),* FROM (SELECT 'batches',CAST(new.batch_id AS TEXT),0 WHERE new.batch_id IS NOT NULL AND (1));

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'batch_attempts',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}' WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='batch_attempts' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE batch_attempts SET raw_output=NULL,
      repair_output=NULL,
      error=NULL
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batch_attempts' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.raw_output IS NOT NULL OR new.repair_output IS NOT NULL OR new.error IS NOT NULL);
END;

CREATE TRIGGER purge_batch_attempts_update AFTER UPDATE ON batch_attempts BEGIN

    DELETE FROM purge_record_refs WHERE table_name='batch_attempts' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'batch_attempts',CAST(new.id AS TEXT),* FROM (SELECT 'batches',CAST(new.batch_id AS TEXT),0 WHERE new.batch_id IS NOT NULL AND (1));

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'batch_attempts',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}' WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='batch_attempts' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE batch_attempts SET raw_output=NULL,
      repair_output=NULL,
      error=NULL
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batch_attempts' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.raw_output IS NOT NULL OR new.repair_output IS NOT NULL OR new.error IS NOT NULL);
END;

CREATE TRIGGER purge_batch_attempts_delete AFTER DELETE ON batch_attempts BEGIN
 DELETE FROM purge_record_refs WHERE table_name='batch_attempts' AND row_id=CAST(old.id AS TEXT);
 DELETE FROM purge_redactions WHERE table_name='batch_attempts' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_model_calls_insert AFTER INSERT ON model_calls BEGIN

    DELETE FROM purge_record_refs WHERE table_name='model_calls' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'model_calls',CAST(new.id AS TEXT),* FROM (SELECT 'batches',CAST(new.batch_id AS TEXT),0 WHERE new.batch_id IS NOT NULL AND (1));

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'model_calls',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}' WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='model_calls' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE model_calls SET error_summary=NULL
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='model_calls' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.error_summary IS NOT NULL);
END;

CREATE TRIGGER purge_model_calls_update AFTER UPDATE ON model_calls BEGIN

    DELETE FROM purge_record_refs WHERE table_name='model_calls' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'model_calls',CAST(new.id AS TEXT),* FROM (SELECT 'batches',CAST(new.batch_id AS TEXT),0 WHERE new.batch_id IS NOT NULL AND (1));

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'model_calls',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}' WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='model_calls' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE model_calls SET error_summary=NULL
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='model_calls' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.error_summary IS NOT NULL);
END;

CREATE TRIGGER purge_model_calls_delete AFTER DELETE ON model_calls BEGIN
 DELETE FROM purge_record_refs WHERE table_name='model_calls' AND row_id=CAST(old.id AS TEXT);
 DELETE FROM purge_redactions WHERE table_name='model_calls' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_recalls_insert AFTER INSERT ON recalls BEGIN

    DELETE FROM purge_record_refs WHERE table_name='recalls' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'recalls',CAST(new.id AS TEXT),* FROM (SELECT 'memory',CAST(j.atom AS TEXT),-1 FROM json_tree(new.request_json) j WHERE j.type='integer' AND replace(j.path,'"','') IN ('$.judgment.removed_memory_ids','$.judgment.stale_memory_ids'));

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'recalls',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.request_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='recalls' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE recalls SET request_json=json_set(json_remove(new.request_json,'$.text'),'$.redacted',1)
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='recalls' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.request_json IS NOT json_set(json_remove(new.request_json,'$.text'),'$.redacted',1));
END;

CREATE TRIGGER purge_recalls_update AFTER UPDATE ON recalls BEGIN

    DELETE FROM purge_record_refs WHERE table_name='recalls' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'recalls',CAST(new.id AS TEXT),* FROM (SELECT 'memory',CAST(j.atom AS TEXT),-1 FROM json_tree(new.request_json) j WHERE j.type='integer' AND replace(j.path,'"','') IN ('$.judgment.removed_memory_ids','$.judgment.stale_memory_ids'));

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'recalls',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.request_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='recalls' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE recalls SET request_json=json_set(json_remove(new.request_json,'$.text'),'$.redacted',1)
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='recalls' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.request_json IS NOT json_set(json_remove(new.request_json,'$.text'),'$.redacted',1));
END;

CREATE TRIGGER purge_recalls_delete AFTER DELETE ON recalls BEGIN
 DELETE FROM purge_record_refs WHERE table_name='recalls' AND row_id=CAST(old.id AS TEXT);
 DELETE FROM purge_redactions WHERE table_name='recalls' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_consolidation_work_insert AFTER INSERT ON consolidation_work BEGIN

    DELETE FROM purge_record_refs WHERE table_name='consolidation_work' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'consolidation_work',CAST(new.id AS TEXT),* FROM (SELECT 'memory',CAST(json_extract(m.value,'$.id') AS TEXT),COALESCE(json_extract(m.value,'$.revision'),-1)
 FROM json_each(new.payload_json,'$.memories') m WHERE json_type(m.value,'$.id')='integer'
 UNION ALL SELECT 'message',CAST(json_extract(s.value,'$.id') AS TEXT),0
 FROM json_each(new.payload_json,'$.memories') m,json_each(m.value,'$.sources') s
 WHERE json_type(s.value,'$.id')='integer' AND COALESCE(json_extract(s.value,'$.content'),json_extract(s.value,'$.text'),json_extract(s.value,'$.quote_content'),'')!='');

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'consolidation_work',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.payload_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key field,j.atom value FROM json_tree(new.result_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='consolidation_work' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE consolidation_work SET payload_json='{}',
      result_json=NULL,
      state='obsolete'
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.payload_json IS NOT '{}' OR new.result_json IS NOT NULL OR new.state IS NOT 'obsolete');
END;

CREATE TRIGGER purge_consolidation_work_update AFTER UPDATE ON consolidation_work BEGIN

    DELETE FROM purge_record_refs WHERE table_name='consolidation_work' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'consolidation_work',CAST(new.id AS TEXT),* FROM (SELECT 'memory',CAST(json_extract(m.value,'$.id') AS TEXT),COALESCE(json_extract(m.value,'$.revision'),-1)
 FROM json_each(new.payload_json,'$.memories') m WHERE json_type(m.value,'$.id')='integer'
 UNION ALL SELECT 'message',CAST(json_extract(s.value,'$.id') AS TEXT),0
 FROM json_each(new.payload_json,'$.memories') m,json_each(m.value,'$.sources') s
 WHERE json_type(s.value,'$.id')='integer' AND COALESCE(json_extract(s.value,'$.content'),json_extract(s.value,'$.text'),json_extract(s.value,'$.quote_content'),'')!='');

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'consolidation_work',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.payload_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key field,j.atom value FROM json_tree(new.result_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='consolidation_work' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE consolidation_work SET payload_json='{}',
      result_json=NULL,
      state='obsolete'
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.payload_json IS NOT '{}' OR new.result_json IS NOT NULL OR new.state IS NOT 'obsolete');
END;

CREATE TRIGGER purge_consolidation_work_delete AFTER DELETE ON consolidation_work BEGIN
 DELETE FROM purge_record_refs WHERE table_name='consolidation_work' AND row_id=CAST(old.id AS TEXT);
 DELETE FROM purge_redactions WHERE table_name='consolidation_work' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_consolidation_calls_insert AFTER INSERT ON consolidation_calls BEGIN

    DELETE FROM purge_record_refs WHERE table_name='consolidation_calls' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'consolidation_calls',CAST(new.id AS TEXT),* FROM (SELECT 'consolidation_work',CAST(new.work_id AS TEXT),0 WHERE new.work_id IS NOT NULL AND (1)
 UNION ALL SELECT 'persona_attempts',CAST(mp.attempt_id AS TEXT),0 FROM maintenance_persona mp WHERE mp.run_id=new.run_id AND new.purpose LIKE 'persona_%' AND mp.attempt_id IS NOT NULL
 UNION ALL SELECT 'entry_profile_attempts',CAST(ep.id AS TEXT),0 FROM entry_profile_attempts ep WHERE ep.run_id=new.run_id AND new.purpose LIKE 'consolidation_entry_profile_%');

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'consolidation_calls',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}' WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='consolidation_calls' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE consolidation_calls SET raw_output=NULL,
      error=NULL
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_calls' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.raw_output IS NOT NULL OR new.error IS NOT NULL);
END;

CREATE TRIGGER purge_consolidation_calls_update AFTER UPDATE ON consolidation_calls BEGIN

    DELETE FROM purge_record_refs WHERE table_name='consolidation_calls' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'consolidation_calls',CAST(new.id AS TEXT),* FROM (SELECT 'consolidation_work',CAST(new.work_id AS TEXT),0 WHERE new.work_id IS NOT NULL AND (1)
 UNION ALL SELECT 'persona_attempts',CAST(mp.attempt_id AS TEXT),0 FROM maintenance_persona mp WHERE mp.run_id=new.run_id AND new.purpose LIKE 'persona_%' AND mp.attempt_id IS NOT NULL
 UNION ALL SELECT 'entry_profile_attempts',CAST(ep.id AS TEXT),0 FROM entry_profile_attempts ep WHERE ep.run_id=new.run_id AND new.purpose LIKE 'consolidation_entry_profile_%');

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'consolidation_calls',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}' WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='consolidation_calls' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE consolidation_calls SET raw_output=NULL,
      error=NULL
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_calls' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.raw_output IS NOT NULL OR new.error IS NOT NULL);
END;

CREATE TRIGGER purge_consolidation_calls_delete AFTER DELETE ON consolidation_calls BEGIN
 DELETE FROM purge_record_refs WHERE table_name='consolidation_calls' AND row_id=CAST(old.id AS TEXT);
 DELETE FROM purge_redactions WHERE table_name='consolidation_calls' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_consolidation_run_work_insert AFTER INSERT ON consolidation_run_work BEGIN

    DELETE FROM purge_record_refs WHERE table_name='consolidation_run_work' AND row_id=json_array(new.run_id,new.work_id);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'consolidation_run_work',json_array(new.run_id,new.work_id),* FROM (SELECT 'consolidation_work',CAST(new.work_id AS TEXT),0 WHERE new.work_id IS NOT NULL AND (1)
 UNION ALL SELECT 'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM json_tree(new.details_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM json_tree(new.details_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer' );

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'consolidation_run_work',json_array(new.run_id,new.work_id),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.details_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='consolidation_run_work' AND r.row_id=json_array(new.run_id,new.work_id) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE consolidation_run_work SET details_json='{}',
      reason=NULL
      WHERE run_id=new.run_id AND work_id=new.work_id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_run_work' AND p.row_id=json_array(new.run_id,new.work_id))
      AND (new.details_json IS NOT '{}' OR new.reason IS NOT NULL);
END;

CREATE TRIGGER purge_consolidation_run_work_update AFTER UPDATE ON consolidation_run_work BEGIN

    DELETE FROM purge_record_refs WHERE table_name='consolidation_run_work' AND row_id=json_array(new.run_id,new.work_id);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'consolidation_run_work',json_array(new.run_id,new.work_id),* FROM (SELECT 'consolidation_work',CAST(new.work_id AS TEXT),0 WHERE new.work_id IS NOT NULL AND (1)
 UNION ALL SELECT 'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM json_tree(new.details_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM json_tree(new.details_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer' );

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'consolidation_run_work',json_array(new.run_id,new.work_id),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.details_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='consolidation_run_work' AND r.row_id=json_array(new.run_id,new.work_id) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE consolidation_run_work SET details_json='{}',
      reason=NULL
      WHERE run_id=new.run_id AND work_id=new.work_id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_run_work' AND p.row_id=json_array(new.run_id,new.work_id))
      AND (new.details_json IS NOT '{}' OR new.reason IS NOT NULL);
END;

CREATE TRIGGER purge_consolidation_run_work_delete AFTER DELETE ON consolidation_run_work BEGIN
 DELETE FROM purge_record_refs WHERE table_name='consolidation_run_work' AND row_id=json_array(old.run_id,old.work_id);
 DELETE FROM purge_redactions WHERE table_name='consolidation_run_work' AND row_id=json_array(old.run_id,old.work_id);
END;

CREATE TRIGGER purge_consolidation_annotations_insert AFTER INSERT ON consolidation_annotations BEGIN

    DELETE FROM purge_record_refs WHERE table_name='consolidation_annotations' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'consolidation_annotations',CAST(new.id AS TEXT),* FROM (SELECT 'memory',CAST(new.memory_id AS TEXT),COALESCE(new.memory_revision,COALESCE((SELECT revision FROM memories WHERE id=new.memory_id),0))
 UNION ALL SELECT 'memory',CAST(j.value AS TEXT),COALESCE((SELECT revision FROM memories WHERE id=j.value),0) FROM json_each(new.related_ids_json) j WHERE j.type='integer'
 UNION ALL SELECT 'consolidation_work',CAST(new.work_id AS TEXT),0 WHERE new.work_id IS NOT NULL AND (1));

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'consolidation_annotations',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.evidence_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key field,j.atom value FROM json_tree(new.related_ids_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='consolidation_annotations' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE consolidation_annotations SET text='',
      evidence_json='[]'
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_annotations' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.text IS NOT '' OR new.evidence_json IS NOT '[]');
END;

CREATE TRIGGER purge_consolidation_annotations_update AFTER UPDATE ON consolidation_annotations BEGIN

    DELETE FROM purge_record_refs WHERE table_name='consolidation_annotations' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'consolidation_annotations',CAST(new.id AS TEXT),* FROM (SELECT 'memory',CAST(new.memory_id AS TEXT),COALESCE(new.memory_revision,COALESCE((SELECT revision FROM memories WHERE id=new.memory_id),0))
 UNION ALL SELECT 'memory',CAST(j.value AS TEXT),COALESCE((SELECT revision FROM memories WHERE id=j.value),0) FROM json_each(new.related_ids_json) j WHERE j.type='integer'
 UNION ALL SELECT 'consolidation_work',CAST(new.work_id AS TEXT),0 WHERE new.work_id IS NOT NULL AND (1));

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'consolidation_annotations',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.evidence_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key field,j.atom value FROM json_tree(new.related_ids_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='consolidation_annotations' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE consolidation_annotations SET text='',
      evidence_json='[]'
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_annotations' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.text IS NOT '' OR new.evidence_json IS NOT '[]');
END;

CREATE TRIGGER purge_consolidation_annotations_delete AFTER DELETE ON consolidation_annotations BEGIN
 DELETE FROM purge_record_refs WHERE table_name='consolidation_annotations' AND row_id=CAST(old.id AS TEXT);
 DELETE FROM purge_redactions WHERE table_name='consolidation_annotations' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_persona_versions_insert AFTER INSERT ON persona_versions BEGIN
    UPDATE persona_versions SET author=CASE WHEN new.source IN ('initial_setting','admin_edit','rollback') THEN 'admin' WHEN new.source='purge' THEN 'purge' ELSE 'model' END WHERE id=new.id AND author IS NOT (CASE WHEN new.source IN ('initial_setting','admin_edit','rollback') THEN 'admin' WHEN new.source='purge' THEN 'purge' ELSE 'model' END);
    DELETE FROM purge_record_refs WHERE table_name='persona_versions' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'persona_versions',CAST(new.id AS TEXT),* FROM (SELECT 'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM json_tree(new.sentences_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM json_tree(new.sentences_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer'
 UNION ALL SELECT 'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer'
 UNION ALL SELECT 'persona_versions',CAST(new.base_version_id AS TEXT),0 WHERE new.base_version_id IS NOT NULL AND (new.source NOT IN ('initial_setting','purge')));
    SELECT RAISE(ABORT,'purged evidence changed') WHERE new.is_current=1 AND EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='persona_versions' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'persona_versions',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.sentences_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key field,j.atom value FROM json_tree(new.checks_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key field,j.atom value FROM json_tree(new.material_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='persona_versions' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE persona_versions SET content='',
      sentences_json='[]',
      material_json='{}',
      checks_json='{"passed":false,"redacted":true,"reason":"因彻底清除而清空"}'
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_versions' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.content IS NOT '' OR new.sentences_json IS NOT '[]' OR new.material_json IS NOT '{}' OR new.checks_json IS NOT '{"passed":false,"redacted":true,"reason":"因彻底清除而清空"}');
END;

CREATE TRIGGER purge_persona_versions_update AFTER UPDATE ON persona_versions BEGIN
    UPDATE persona_versions SET author=CASE WHEN new.source IN ('initial_setting','admin_edit','rollback') THEN 'admin' WHEN new.source='purge' THEN 'purge' ELSE 'model' END WHERE id=new.id AND author IS NOT (CASE WHEN new.source IN ('initial_setting','admin_edit','rollback') THEN 'admin' WHEN new.source='purge' THEN 'purge' ELSE 'model' END);
    DELETE FROM purge_record_refs WHERE table_name='persona_versions' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'persona_versions',CAST(new.id AS TEXT),* FROM (SELECT 'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM json_tree(new.sentences_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM json_tree(new.sentences_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer'
 UNION ALL SELECT 'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer'
 UNION ALL SELECT 'persona_versions',CAST(new.base_version_id AS TEXT),0 WHERE new.base_version_id IS NOT NULL AND (new.source NOT IN ('initial_setting','purge')));

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'persona_versions',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.sentences_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key field,j.atom value FROM json_tree(new.checks_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key field,j.atom value FROM json_tree(new.material_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='persona_versions' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE persona_versions SET content='',
      sentences_json='[]',
      material_json='{}',
      checks_json='{"passed":false,"redacted":true,"reason":"因彻底清除而清空"}'
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_versions' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.content IS NOT '' OR new.sentences_json IS NOT '[]' OR new.material_json IS NOT '{}' OR new.checks_json IS NOT '{"passed":false,"redacted":true,"reason":"因彻底清除而清空"}');
END;

CREATE TRIGGER purge_persona_versions_delete AFTER DELETE ON persona_versions BEGIN
 DELETE FROM purge_record_refs WHERE table_name='persona_versions' AND row_id=CAST(old.id AS TEXT);
 DELETE FROM purge_redactions WHERE table_name='persona_versions' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_entry_profile_versions_insert AFTER INSERT ON entry_profile_versions BEGIN

    DELETE FROM purge_record_refs WHERE table_name='entry_profile_versions' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'entry_profile_versions',CAST(new.id AS TEXT),* FROM (SELECT 'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM json_tree(new.sentences_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM json_tree(new.sentences_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer'
 UNION ALL SELECT 'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer'
 UNION ALL SELECT 'profile_base',CAST(json_array(new.entry_id,new.base_version) AS TEXT),0 WHERE json_array(new.entry_id,new.base_version) IS NOT NULL AND (new.source!='purge'));
    SELECT RAISE(ABORT,'purged evidence changed') WHERE new.status='published' AND EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='entry_profile_versions' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'entry_profile_versions',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.sentences_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key field,j.atom value FROM json_tree(new.checks_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key field,j.atom value FROM json_tree(new.material_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='entry_profile_versions' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE entry_profile_versions SET content='',
      sentences_json='[]',
      material_json='{}',
      checks_json='{"passed":false,"redacted":true,"reason":"因彻底清除而清空"}'
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_versions' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.content IS NOT '' OR new.sentences_json IS NOT '[]' OR new.material_json IS NOT '{}' OR new.checks_json IS NOT '{"passed":false,"redacted":true,"reason":"因彻底清除而清空"}');
END;

CREATE TRIGGER purge_entry_profile_versions_update AFTER UPDATE ON entry_profile_versions BEGIN

    DELETE FROM purge_record_refs WHERE table_name='entry_profile_versions' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'entry_profile_versions',CAST(new.id AS TEXT),* FROM (SELECT 'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM json_tree(new.sentences_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM json_tree(new.sentences_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer'
 UNION ALL SELECT 'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer'
 UNION ALL SELECT 'profile_base',CAST(json_array(new.entry_id,new.base_version) AS TEXT),0 WHERE json_array(new.entry_id,new.base_version) IS NOT NULL AND (new.source!='purge'));

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'entry_profile_versions',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.sentences_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key field,j.atom value FROM json_tree(new.checks_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key field,j.atom value FROM json_tree(new.material_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='entry_profile_versions' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE entry_profile_versions SET content='',
      sentences_json='[]',
      material_json='{}',
      checks_json='{"passed":false,"redacted":true,"reason":"因彻底清除而清空"}'
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_versions' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.content IS NOT '' OR new.sentences_json IS NOT '[]' OR new.material_json IS NOT '{}' OR new.checks_json IS NOT '{"passed":false,"redacted":true,"reason":"因彻底清除而清空"}');
END;

CREATE TRIGGER purge_entry_profile_versions_delete AFTER DELETE ON entry_profile_versions BEGIN
 DELETE FROM purge_record_refs WHERE table_name='entry_profile_versions' AND row_id=CAST(old.id AS TEXT);
 DELETE FROM purge_redactions WHERE table_name='entry_profile_versions' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_persona_attempts_insert AFTER INSERT ON persona_attempts BEGIN

    DELETE FROM purge_record_refs WHERE table_name='persona_attempts' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'persona_attempts',CAST(new.id AS TEXT),* FROM (SELECT 'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer'
 UNION ALL SELECT 'persona_versions',CAST(new.base_version_id AS TEXT),0 WHERE new.base_version_id IS NOT NULL AND (1));

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'persona_attempts',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.material_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key field,j.atom value FROM json_tree(new.outputs_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='persona_attempts' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE persona_attempts SET material_json='{}',
      outputs_json='{}',
      reason='因彻底清除而清空'
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.material_json IS NOT '{}' OR new.outputs_json IS NOT '{}' OR new.reason IS NOT '因彻底清除而清空');
END;

CREATE TRIGGER purge_persona_attempts_update AFTER UPDATE ON persona_attempts BEGIN

    DELETE FROM purge_record_refs WHERE table_name='persona_attempts' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'persona_attempts',CAST(new.id AS TEXT),* FROM (SELECT 'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer'
 UNION ALL SELECT 'persona_versions',CAST(new.base_version_id AS TEXT),0 WHERE new.base_version_id IS NOT NULL AND (1));

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'persona_attempts',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.material_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key field,j.atom value FROM json_tree(new.outputs_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='persona_attempts' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE persona_attempts SET material_json='{}',
      outputs_json='{}',
      reason='因彻底清除而清空'
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.material_json IS NOT '{}' OR new.outputs_json IS NOT '{}' OR new.reason IS NOT '因彻底清除而清空');
END;

CREATE TRIGGER purge_persona_attempts_delete AFTER DELETE ON persona_attempts BEGIN
 DELETE FROM purge_record_refs WHERE table_name='persona_attempts' AND row_id=CAST(old.id AS TEXT);
 DELETE FROM purge_redactions WHERE table_name='persona_attempts' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_entry_profile_attempts_insert AFTER INSERT ON entry_profile_attempts BEGIN

    DELETE FROM purge_record_refs WHERE table_name='entry_profile_attempts' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'entry_profile_attempts',CAST(new.id AS TEXT),* FROM (SELECT 'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer'
 UNION ALL SELECT 'profile_base',CAST(json_array(new.entry_id,new.base_version) AS TEXT),0 WHERE json_array(new.entry_id,new.base_version) IS NOT NULL AND (1));

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'entry_profile_attempts',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.material_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key field,j.atom value FROM json_tree(new.outputs_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='entry_profile_attempts' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE entry_profile_attempts SET material_json='{}',
      outputs_json='{}',
      reason='因彻底清除而清空'
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.material_json IS NOT '{}' OR new.outputs_json IS NOT '{}' OR new.reason IS NOT '因彻底清除而清空');
END;

CREATE TRIGGER purge_entry_profile_attempts_update AFTER UPDATE ON entry_profile_attempts BEGIN

    DELETE FROM purge_record_refs WHERE table_name='entry_profile_attempts' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'entry_profile_attempts',CAST(new.id AS TEXT),* FROM (SELECT 'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer'
 UNION ALL SELECT 'profile_base',CAST(json_array(new.entry_id,new.base_version) AS TEXT),0 WHERE json_array(new.entry_id,new.base_version) IS NOT NULL AND (1));

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'entry_profile_attempts',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.material_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key field,j.atom value FROM json_tree(new.outputs_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='entry_profile_attempts' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE entry_profile_attempts SET material_json='{}',
      outputs_json='{}',
      reason='因彻底清除而清空'
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.material_json IS NOT '{}' OR new.outputs_json IS NOT '{}' OR new.reason IS NOT '因彻底清除而清空');
END;

CREATE TRIGGER purge_entry_profile_attempts_delete AFTER DELETE ON entry_profile_attempts BEGIN
 DELETE FROM purge_record_refs WHERE table_name='entry_profile_attempts' AND row_id=CAST(old.id AS TEXT);
 DELETE FROM purge_redactions WHERE table_name='entry_profile_attempts' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_maintenance_items_insert AFTER INSERT ON maintenance_items BEGIN

    DELETE FROM purge_record_refs WHERE table_name='maintenance_items' AND row_id=json_array(new.run_id,new.phase,new.item_key);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'maintenance_items',json_array(new.run_id,new.phase,new.item_key),* FROM (SELECT 'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM json_tree(new.details_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM json_tree(new.details_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer'
 UNION ALL SELECT 'consolidation_work',CAST(new.item_key AS TEXT),0 WHERE new.item_key IS NOT NULL AND (new.phase='consolidation'));

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'maintenance_items',json_array(new.run_id,new.phase,new.item_key),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.details_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='maintenance_items' AND r.row_id=json_array(new.run_id,new.phase,new.item_key) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE maintenance_items SET details_json='{}',
      reason='因彻底清除而清空'
      WHERE run_id=new.run_id AND phase=new.phase AND item_key=new.item_key AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='maintenance_items' AND p.row_id=json_array(new.run_id,new.phase,new.item_key))
      AND (new.details_json IS NOT '{}' OR new.reason IS NOT '因彻底清除而清空');
END;

CREATE TRIGGER purge_maintenance_items_update AFTER UPDATE ON maintenance_items BEGIN

    DELETE FROM purge_record_refs WHERE table_name='maintenance_items' AND row_id=json_array(new.run_id,new.phase,new.item_key);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'maintenance_items',json_array(new.run_id,new.phase,new.item_key),* FROM (SELECT 'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM json_tree(new.details_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM json_tree(new.details_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer'
 UNION ALL SELECT 'consolidation_work',CAST(new.item_key AS TEXT),0 WHERE new.item_key IS NOT NULL AND (new.phase='consolidation'));

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'maintenance_items',json_array(new.run_id,new.phase,new.item_key),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.details_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='maintenance_items' AND r.row_id=json_array(new.run_id,new.phase,new.item_key) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE maintenance_items SET details_json='{}',
      reason='因彻底清除而清空'
      WHERE run_id=new.run_id AND phase=new.phase AND item_key=new.item_key AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='maintenance_items' AND p.row_id=json_array(new.run_id,new.phase,new.item_key))
      AND (new.details_json IS NOT '{}' OR new.reason IS NOT '因彻底清除而清空');
END;

CREATE TRIGGER purge_maintenance_items_delete AFTER DELETE ON maintenance_items BEGIN
 DELETE FROM purge_record_refs WHERE table_name='maintenance_items' AND row_id=json_array(old.run_id,old.phase,old.item_key);
 DELETE FROM purge_redactions WHERE table_name='maintenance_items' AND row_id=json_array(old.run_id,old.phase,old.item_key);
END;

CREATE TRIGGER purge_maintenance_persona_insert AFTER INSERT ON maintenance_persona BEGIN

    DELETE FROM purge_record_refs WHERE table_name='maintenance_persona' AND row_id=CAST(new.run_id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'maintenance_persona',CAST(new.run_id AS TEXT),* FROM (SELECT 'persona_attempts',CAST(new.attempt_id AS TEXT),0 WHERE new.attempt_id IS NOT NULL AND (1));

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'maintenance_persona',CAST(new.run_id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.details_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='maintenance_persona' AND r.row_id=CAST(new.run_id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE maintenance_persona SET details_json='{}'
      WHERE run_id=new.run_id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='maintenance_persona' AND p.row_id=CAST(new.run_id AS TEXT))
      AND (new.details_json IS NOT '{}');
END;

CREATE TRIGGER purge_maintenance_persona_update AFTER UPDATE ON maintenance_persona BEGIN

    DELETE FROM purge_record_refs WHERE table_name='maintenance_persona' AND row_id=CAST(new.run_id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'maintenance_persona',CAST(new.run_id AS TEXT),* FROM (SELECT 'persona_attempts',CAST(new.attempt_id AS TEXT),0 WHERE new.attempt_id IS NOT NULL AND (1));

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'maintenance_persona',CAST(new.run_id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.details_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='maintenance_persona' AND r.row_id=CAST(new.run_id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE maintenance_persona SET details_json='{}'
      WHERE run_id=new.run_id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='maintenance_persona' AND p.row_id=CAST(new.run_id AS TEXT))
      AND (new.details_json IS NOT '{}');
END;

CREATE TRIGGER purge_maintenance_persona_delete AFTER DELETE ON maintenance_persona BEGIN
 DELETE FROM purge_record_refs WHERE table_name='maintenance_persona' AND row_id=CAST(old.run_id AS TEXT);
 DELETE FROM purge_redactions WHERE table_name='maintenance_persona' AND row_id=CAST(old.run_id AS TEXT);
END;

CREATE TRIGGER purge_sources_insert AFTER INSERT ON sources BEGIN

    DELETE FROM purge_record_refs WHERE table_name='sources' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'sources',CAST(new.id AS TEXT),* FROM (SELECT 'memory',CAST(new.source_memory_id AS TEXT),COALESCE((SELECT revision FROM memories WHERE id=new.source_memory_id),0) WHERE new.note IS NOT NULL AND new.source_memory_id IS NOT NULL);

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'sources',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}' WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='sources' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE sources SET note=NULL
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='sources' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.note IS NOT NULL);
END;

CREATE TRIGGER purge_sources_update AFTER UPDATE ON sources BEGIN

    DELETE FROM purge_record_refs WHERE table_name='sources' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'sources',CAST(new.id AS TEXT),* FROM (SELECT 'memory',CAST(new.source_memory_id AS TEXT),COALESCE((SELECT revision FROM memories WHERE id=new.source_memory_id),0) WHERE new.note IS NOT NULL AND new.source_memory_id IS NOT NULL);

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'sources',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}' WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='sources' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE sources SET note=NULL
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='sources' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.note IS NOT NULL);
END;

CREATE TRIGGER purge_sources_delete AFTER DELETE ON sources BEGIN
 DELETE FROM purge_record_refs WHERE table_name='sources' AND row_id=CAST(old.id AS TEXT);
 DELETE FROM purge_redactions WHERE table_name='sources' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_media_objects_insert AFTER INSERT ON media_objects BEGIN

    DELETE FROM purge_record_refs WHERE table_name='media_objects' AND row_id=CAST(new.id AS TEXT);


    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'media_objects',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}' WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='media_objects' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE media_objects SET understanding_text=CASE WHEN new.understanding_source='refused' THEN '敏感信息无法访问' ELSE '' END,
      last_error=NULL
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='media_objects' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.understanding_text IS NOT CASE WHEN new.understanding_source='refused' THEN '敏感信息无法访问' ELSE '' END OR new.last_error IS NOT NULL);
END;

CREATE TRIGGER purge_media_objects_update AFTER UPDATE ON media_objects BEGIN

    DELETE FROM purge_record_refs WHERE table_name='media_objects' AND row_id=CAST(new.id AS TEXT);


    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'media_objects',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}' WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='media_objects' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE media_objects SET understanding_text=CASE WHEN new.understanding_source='refused' THEN '敏感信息无法访问' ELSE '' END,
      last_error=NULL
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='media_objects' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.understanding_text IS NOT CASE WHEN new.understanding_source='refused' THEN '敏感信息无法访问' ELSE '' END OR new.last_error IS NOT NULL);
END;

CREATE TRIGGER purge_media_objects_delete AFTER DELETE ON media_objects BEGIN
 DELETE FROM purge_record_refs WHERE table_name='media_objects' AND row_id=CAST(old.id AS TEXT);
 DELETE FROM purge_redactions WHERE table_name='media_objects' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_admin_operations_insert AFTER INSERT ON admin_operations BEGIN

    DELETE FROM purge_record_refs WHERE table_name='admin_operations' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'admin_operations',CAST(new.id AS TEXT),* FROM (SELECT 'memory',CAST(new.object_id AS TEXT),COALESCE((SELECT revision FROM memories WHERE id=new.object_id),0) WHERE new.object_type='memory' AND EXISTS(SELECT 1 FROM json_tree(new.details_json) j WHERE j.key IN ('content','text','excerpt','raw_output','repair_output','error','report','reason_text','before_memories','after_memories','source_excerpts'))
 UNION ALL SELECT * FROM (SELECT 'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM json_tree(new.details_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM json_tree(new.details_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer' ) WHERE EXISTS(SELECT 1 FROM json_tree(new.details_json) j WHERE j.key IN ('content','text','excerpt','raw_output','repair_output','error','report','reason_text','before_memories','after_memories','source_excerpts')));

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'admin_operations',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.details_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='admin_operations' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE admin_operations SET details_json='{"redacted":true}'
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='admin_operations' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.details_json IS NOT '{"redacted":true}');
END;

CREATE TRIGGER purge_admin_operations_update AFTER UPDATE ON admin_operations BEGIN

    DELETE FROM purge_record_refs WHERE table_name='admin_operations' AND row_id=CAST(new.id AS TEXT);
    INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision) SELECT 'admin_operations',CAST(new.id AS TEXT),* FROM (SELECT 'memory',CAST(new.object_id AS TEXT),COALESCE((SELECT revision FROM memories WHERE id=new.object_id),0) WHERE new.object_type='memory' AND EXISTS(SELECT 1 FROM json_tree(new.details_json) j WHERE j.key IN ('content','text','excerpt','raw_output','repair_output','error','report','reason_text','before_memories','after_memories','source_excerpts'))
 UNION ALL SELECT * FROM (SELECT 'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM json_tree(new.details_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM json_tree(new.details_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer' ) WHERE EXISTS(SELECT 1 FROM json_tree(new.details_json) j WHERE j.key IN ('content','text','excerpt','raw_output','repair_output','error','report','reason_text','before_memories','after_memories','source_excerpts')));

    INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
      SELECT 'admin_operations',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key field,j.atom value FROM json_tree(new.details_json) j WHERE (j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','revision','source_memory_id','source_revision','batch_id','work_id','run_id','version','importance','belief','retention')) OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))) WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='admin_operations' AND r.row_id=CAST(new.id AS TEXT) AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL AND r.revision<m.revision)) OR (r.ref_kind='message' AND r.revision=0 AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER))) OR (r.ref_kind='batches' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=r.ref_id)) OR (r.ref_kind='consolidation_work' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=r.ref_id)) OR (r.ref_kind='persona_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=r.ref_id)) OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=r.ref_id))));
    UPDATE admin_operations SET details_json='{"redacted":true}'
      WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='admin_operations' AND p.row_id=CAST(new.id AS TEXT))
      AND (new.details_json IS NOT '{"redacted":true}');
END;

CREATE TRIGGER purge_admin_operations_delete AFTER DELETE ON admin_operations BEGIN
 DELETE FROM purge_record_refs WHERE table_name='admin_operations' AND row_id=CAST(old.id AS TEXT);
 DELETE FROM purge_redactions WHERE table_name='admin_operations' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_recall_items_insert AFTER INSERT ON recall_items
WHEN EXISTS(SELECT 1 FROM memories WHERE id=new.memory_id AND purged_at IS NOT NULL AND new.revision<revision)
BEGIN
 INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at)
 VALUES('recalls',new.recall_id,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
 UPDATE recalls SET request_json=request_json WHERE id=new.recall_id;
END;

INSERT OR IGNORE INTO purge_record_refs
SELECT 'batches',CAST(new.id AS TEXT),'memory',CAST(x.value AS TEXT),-1 FROM batches new, json_each(new.result_json) a,json_each(CASE WHEN a.type='array' THEN a.value ELSE '[]' END) x WHERE a.key IN ('created','updated','confirmed') AND x.type='integer';

INSERT OR IGNORE INTO purge_record_refs
SELECT 'batch_attempts',CAST(new.id AS TEXT),'batches',CAST(new.batch_id AS TEXT),0 FROM batch_attempts new WHERE new.batch_id IS NOT NULL AND (1);

INSERT OR IGNORE INTO purge_record_refs
SELECT 'model_calls',CAST(new.id AS TEXT),'batches',CAST(new.batch_id AS TEXT),0 FROM model_calls new WHERE new.batch_id IS NOT NULL AND (1);

INSERT OR IGNORE INTO purge_record_refs
SELECT 'recalls',CAST(new.id AS TEXT),'memory',CAST(j.atom AS TEXT),-1 FROM recalls new, json_tree(new.request_json) j WHERE j.type='integer' AND replace(j.path,'"','') IN ('$.judgment.removed_memory_ids','$.judgment.stale_memory_ids');

INSERT OR IGNORE INTO purge_record_refs
SELECT 'consolidation_work',CAST(new.id AS TEXT),'memory',CAST(json_extract(m.value,'$.id') AS TEXT),COALESCE(json_extract(m.value,'$.revision'),-1)
 FROM consolidation_work new, json_each(new.payload_json,'$.memories') m WHERE json_type(m.value,'$.id')='integer'
 UNION ALL SELECT 'consolidation_work',CAST(new.id AS TEXT),'message',CAST(json_extract(s.value,'$.id') AS TEXT),0
 FROM consolidation_work new, json_each(new.payload_json,'$.memories') m,json_each(m.value,'$.sources') s
 WHERE json_type(s.value,'$.id')='integer' AND COALESCE(json_extract(s.value,'$.content'),json_extract(s.value,'$.text'),json_extract(s.value,'$.quote_content'),'')!='';

INSERT OR IGNORE INTO purge_record_refs
SELECT 'consolidation_calls',CAST(new.id AS TEXT),'consolidation_work',CAST(new.work_id AS TEXT),0 FROM consolidation_calls new WHERE new.work_id IS NOT NULL AND (1)
 UNION ALL SELECT 'consolidation_calls',CAST(new.id AS TEXT),'persona_attempts',CAST(mp.attempt_id AS TEXT),0 FROM consolidation_calls new, maintenance_persona mp WHERE mp.run_id=new.run_id AND new.purpose LIKE 'persona_%' AND mp.attempt_id IS NOT NULL
 UNION ALL SELECT 'consolidation_calls',CAST(new.id AS TEXT),'entry_profile_attempts',CAST(ep.id AS TEXT),0 FROM consolidation_calls new, entry_profile_attempts ep WHERE ep.run_id=new.run_id AND new.purpose LIKE 'consolidation_entry_profile_%';

INSERT OR IGNORE INTO purge_record_refs
SELECT 'consolidation_run_work',json_array(new.run_id,new.work_id),'consolidation_work',CAST(new.work_id AS TEXT),0 FROM consolidation_run_work new WHERE new.work_id IS NOT NULL AND (1)
 UNION ALL SELECT 'consolidation_run_work',json_array(new.run_id,new.work_id),'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM consolidation_run_work new, json_tree(new.details_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'consolidation_run_work',json_array(new.run_id,new.work_id),'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM consolidation_run_work new, json_tree(new.details_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer';

INSERT OR IGNORE INTO purge_record_refs
SELECT 'consolidation_annotations',CAST(new.id AS TEXT),'memory',CAST(new.memory_id AS TEXT),COALESCE(new.memory_revision,COALESCE((SELECT revision FROM memories WHERE id=new.memory_id),0)) FROM consolidation_annotations new
 UNION ALL SELECT 'consolidation_annotations',CAST(new.id AS TEXT),'memory',CAST(j.value AS TEXT),COALESCE((SELECT revision FROM memories WHERE id=j.value),0) FROM consolidation_annotations new, json_each(new.related_ids_json) j WHERE j.type='integer'
 UNION ALL SELECT 'consolidation_annotations',CAST(new.id AS TEXT),'consolidation_work',CAST(new.work_id AS TEXT),0 FROM consolidation_annotations new WHERE new.work_id IS NOT NULL AND (1);

INSERT OR IGNORE INTO purge_record_refs
SELECT 'persona_versions',CAST(new.id AS TEXT),'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM persona_versions new, json_tree(new.sentences_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'persona_versions',CAST(new.id AS TEXT),'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM persona_versions new, json_tree(new.sentences_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer'
 UNION ALL SELECT 'persona_versions',CAST(new.id AS TEXT),'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM persona_versions new, json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'persona_versions',CAST(new.id AS TEXT),'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM persona_versions new, json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer'
 UNION ALL SELECT 'persona_versions',CAST(new.id AS TEXT),'persona_versions',CAST(new.base_version_id AS TEXT),0 FROM persona_versions new WHERE new.base_version_id IS NOT NULL AND (new.source NOT IN ('initial_setting','purge'));

INSERT OR IGNORE INTO purge_record_refs
SELECT 'entry_profile_versions',CAST(new.id AS TEXT),'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM entry_profile_versions new, json_tree(new.sentences_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'entry_profile_versions',CAST(new.id AS TEXT),'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM entry_profile_versions new, json_tree(new.sentences_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer'
 UNION ALL SELECT 'entry_profile_versions',CAST(new.id AS TEXT),'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM entry_profile_versions new, json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'entry_profile_versions',CAST(new.id AS TEXT),'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM entry_profile_versions new, json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer'
 UNION ALL SELECT 'entry_profile_versions',CAST(new.id AS TEXT),'profile_base',CAST(json_array(new.entry_id,new.base_version) AS TEXT),0 FROM entry_profile_versions new WHERE json_array(new.entry_id,new.base_version) IS NOT NULL AND (new.source!='purge');

INSERT OR IGNORE INTO purge_record_refs
SELECT 'persona_attempts',CAST(new.id AS TEXT),'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM persona_attempts new, json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'persona_attempts',CAST(new.id AS TEXT),'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM persona_attempts new, json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer'
 UNION ALL SELECT 'persona_attempts',CAST(new.id AS TEXT),'persona_versions',CAST(new.base_version_id AS TEXT),0 FROM persona_attempts new WHERE new.base_version_id IS NOT NULL AND (1);

INSERT OR IGNORE INTO purge_record_refs
SELECT 'entry_profile_attempts',CAST(new.id AS TEXT),'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM entry_profile_attempts new, json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'entry_profile_attempts',CAST(new.id AS TEXT),'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM entry_profile_attempts new, json_tree(new.material_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer'
 UNION ALL SELECT 'entry_profile_attempts',CAST(new.id AS TEXT),'profile_base',CAST(json_array(new.entry_id,new.base_version) AS TEXT),0 FROM entry_profile_attempts new WHERE json_array(new.entry_id,new.base_version) IS NOT NULL AND (1);

INSERT OR IGNORE INTO purge_record_refs
SELECT 'maintenance_items',json_array(new.run_id,new.phase,new.item_key),'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM maintenance_items new, json_tree(new.details_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'maintenance_items',json_array(new.run_id,new.phase,new.item_key),'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM maintenance_items new, json_tree(new.details_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer'
 UNION ALL SELECT 'maintenance_items',json_array(new.run_id,new.phase,new.item_key),'consolidation_work',CAST(new.item_key AS TEXT),0 FROM maintenance_items new WHERE new.item_key IS NOT NULL AND (new.phase='consolidation');

INSERT OR IGNORE INTO purge_record_refs
SELECT 'maintenance_persona',CAST(new.run_id AS TEXT),'persona_attempts',CAST(new.attempt_id AS TEXT),0 FROM maintenance_persona new WHERE new.attempt_id IS NOT NULL AND (1);

INSERT OR IGNORE INTO purge_record_refs
SELECT 'sources',CAST(new.id AS TEXT),'memory',CAST(new.source_memory_id AS TEXT),COALESCE((SELECT revision FROM memories WHERE id=new.source_memory_id),0) FROM sources new WHERE new.note IS NOT NULL AND new.source_memory_id IS NOT NULL;

INSERT OR IGNORE INTO purge_record_refs
SELECT 'admin_operations',CAST(new.id AS TEXT),'memory',CAST(new.object_id AS TEXT),COALESCE((SELECT revision FROM memories WHERE id=new.object_id),0) FROM admin_operations new WHERE new.object_type='memory' AND EXISTS(SELECT 1 FROM json_tree(new.details_json) j WHERE j.key IN ('content','text','excerpt','raw_output','repair_output','error','report','reason_text','before_memories','after_memories','source_excerpts'))
 UNION ALL SELECT 'admin_operations',CAST(new.id AS TEXT),'memory',CAST(json_extract(j.value,'$.memory_id') AS TEXT),
 COALESCE(json_extract(j.value,'$.revision'),CASE WHEN COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),'')!='' THEN -1 ELSE COALESCE((SELECT revision FROM memories WHERE id=json_extract(j.value,'$.memory_id')),0) END)
 FROM admin_operations new,json_tree(new.details_json) j WHERE j.type='object' AND json_type(j.value,'$.memory_id')='integer'
 UNION ALL SELECT 'admin_operations',CAST(new.id AS TEXT),'message',CAST(json_extract(j.value,'$.message_id') AS TEXT),
 CASE WHEN EXISTS(SELECT 1 FROM messages WHERE id=json_extract(j.value,'$.message_id')) OR
 COALESCE(json_extract(j.value,'$.content'),json_extract(j.value,'$.excerpt'),json_extract(j.value,'$.sha256'),'')!='' THEN 0 ELSE 1 END
 FROM admin_operations new,json_tree(new.details_json) j WHERE j.type='object' AND json_type(j.value,'$.message_id')='integer';

CREATE TEMP TABLE purge_upgrade_targets AS
 WITH RECURSIVE affected(table_name,row_id) AS (
 SELECT table_name,row_id FROM purge_stale_references WHERE EXISTS(SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 UNION SELECT r.table_name,r.row_id FROM affected a JOIN purge_record_refs r
 ON r.ref_kind=a.table_name AND r.ref_id=a.row_id
 UNION SELECT r.table_name,r.row_id FROM affected a JOIN entry_profile_versions v
 ON a.table_name='entry_profile_versions' AND v.id=CAST(a.row_id AS INTEGER)
 JOIN purge_record_refs r ON r.ref_kind='profile_base' AND r.ref_id=json_array(v.entry_id,v.version))
 SELECT table_name,row_id FROM affected;
UPDATE runtime_settings SET value_json='""' WHERE key='background' AND EXISTS(
 SELECT 1 FROM purge_record_refs r JOIN memories m ON m.id=CAST(r.ref_id AS INTEGER)
 WHERE r.table_name='persona_versions' AND r.row_id=CAST((SELECT MAX(id) FROM persona_versions WHERE source='initial_setting') AS TEXT)
 AND r.ref_kind='memory' AND m.purged_at IS NOT NULL AND r.revision<m.revision);

CREATE TEMP TABLE purge_upgrade_persona_versions AS
 WITH RECURSIVE affected(id) AS (
 SELECT id FROM memories WHERE purged_at IS NOT NULL
 UNION SELECT s.memory_id FROM sources s JOIN affected a ON s.source_memory_id=a.id WHERE s.kind='memory')
 SELECT v.*,COALESCE((SELECT json_group_array(json(CASE WHEN json_type(s.value,'$.initial_settings') IS NOT NULL THEN json_set(s.value,'$.initial_settings',json('{}')) ELSE s.value END)) FROM json_each(v.sentences_json) s WHERE NOT EXISTS(
 SELECT 1 FROM json_each(s.value,'$.basis') b WHERE json_extract(b.value,'$.memory_id') IN (SELECT id FROM affected))), '[]') AS kept
 FROM persona_versions v WHERE v.is_current=1 AND v.id IN (SELECT row_id FROM purge_upgrade_targets WHERE table_name='persona_versions');

UPDATE persona_versions SET is_current=0,status='history' WHERE id IN (SELECT id FROM purge_upgrade_persona_versions);
INSERT INTO persona_versions(content,created_at,is_current,status,source,sentences_json,checks_json,base_version_id,published_at,author)
 SELECT COALESCE((SELECT group_concat(json_extract(value,'$.text'),'') FROM json_each(kept)),''),strftime('%Y-%m-%dT%H:%M:%fZ','now'),1,'current','purge',kept,
 json_object('passed',json('true'),'purge',json('true'),'needs_regeneration',json(CASE WHEN json_array_length(kept)=0 THEN 'true' ELSE 'false' END)),id,strftime('%Y-%m-%dT%H:%M:%fZ','now'),'purge'
 FROM purge_upgrade_persona_versions;

DROP TABLE purge_upgrade_persona_versions;

CREATE TEMP TABLE purge_upgrade_entry_profile_versions AS
 WITH RECURSIVE affected(id) AS (
 SELECT id FROM memories WHERE purged_at IS NOT NULL
 UNION SELECT s.memory_id FROM sources s JOIN affected a ON s.source_memory_id=a.id WHERE s.kind='memory')
 SELECT v.*,COALESCE((SELECT json_group_array(json(CASE WHEN json_type(s.value,'$.initial_settings') IS NOT NULL THEN json_set(s.value,'$.initial_settings',json('{}')) ELSE s.value END)) FROM json_each(v.sentences_json) s WHERE NOT EXISTS(
 SELECT 1 FROM json_each(s.value,'$.basis') b WHERE json_extract(b.value,'$.memory_id') IN (SELECT id FROM affected))), '[]') AS kept
 FROM entry_profile_versions v WHERE EXISTS(SELECT 1 FROM entry_profile_settings s WHERE s.entry_id=v.entry_id AND s.current_version=v.version) AND v.id IN (SELECT row_id FROM purge_upgrade_targets WHERE table_name='entry_profile_versions');

INSERT INTO entry_profile_versions(entry_id,version,base_version,content,sentences_json,checks_json,change_degree,status,generated_at,published_at,author,source,message_through,settings_revision)
 SELECT v.entry_id,(SELECT MAX(version)+1 FROM entry_profile_versions WHERE entry_id=v.entry_id),v.version,
 COALESCE((SELECT group_concat(json_extract(value,'$.text'),'
') FROM json_each(v.kept)),''),v.kept,
 json_object('format_version',COALESCE(json_extract(v.checks_json,'$.format_version'),1),'passed',json('true'),'purge',json('true'),'needs_regeneration',json(CASE WHEN json_array_length(v.kept)=0 THEN 'true' ELSE 'false' END)),
 'small','published',strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'purge','purge',v.message_through,v.settings_revision
 FROM purge_upgrade_entry_profile_versions v;
UPDATE entry_profile_settings SET current_version=(SELECT MAX(version) FROM entry_profile_versions WHERE entry_id=entry_profile_settings.entry_id)
 WHERE entry_id IN (SELECT entry_id FROM purge_upgrade_entry_profile_versions);

DROP TABLE purge_upgrade_entry_profile_versions;

INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
 SELECT t.table_name,t.row_id,strftime('%Y-%m-%dT%H:%M:%fZ','now'),
 (SELECT json_object('references',json_group_array(json_object('field',r.ref_kind||'_id','value',r.ref_id,'revision',r.revision)))
 FROM purge_record_refs r WHERE r.table_name=t.table_name AND r.row_id=t.row_id)
 FROM purge_upgrade_targets t;

UPDATE batches SET result_json=result_json WHERE EXISTS(SELECT 1 FROM memories WHERE purged_at IS NOT NULL) AND (CAST(id AS TEXT) IN (SELECT row_id FROM purge_upgrade_targets WHERE table_name='batches') OR EXISTS(SELECT 1 FROM purge_record_refs r JOIN purge_redactions p ON p.table_name=r.ref_kind AND p.row_id=r.ref_id WHERE r.table_name='batches' AND r.row_id=CAST(batches.id AS TEXT) AND r.ref_kind IN ('batches','consolidation_work','persona_attempts','entry_profile_attempts')));

UPDATE batch_attempts SET raw_output=raw_output WHERE EXISTS(SELECT 1 FROM memories WHERE purged_at IS NOT NULL) AND (CAST(id AS TEXT) IN (SELECT row_id FROM purge_upgrade_targets WHERE table_name='batch_attempts') OR EXISTS(SELECT 1 FROM purge_record_refs r JOIN purge_redactions p ON p.table_name=r.ref_kind AND p.row_id=r.ref_id WHERE r.table_name='batch_attempts' AND r.row_id=CAST(batch_attempts.id AS TEXT) AND r.ref_kind IN ('batches','consolidation_work','persona_attempts','entry_profile_attempts')));

UPDATE model_calls SET error_summary=error_summary WHERE EXISTS(SELECT 1 FROM memories WHERE purged_at IS NOT NULL) AND (CAST(id AS TEXT) IN (SELECT row_id FROM purge_upgrade_targets WHERE table_name='model_calls') OR EXISTS(SELECT 1 FROM purge_record_refs r JOIN purge_redactions p ON p.table_name=r.ref_kind AND p.row_id=r.ref_id WHERE r.table_name='model_calls' AND r.row_id=CAST(model_calls.id AS TEXT) AND r.ref_kind IN ('batches','consolidation_work','persona_attempts','entry_profile_attempts')));

UPDATE recalls SET request_json=request_json WHERE EXISTS(SELECT 1 FROM memories WHERE purged_at IS NOT NULL) AND (CAST(id AS TEXT) IN (SELECT row_id FROM purge_upgrade_targets WHERE table_name='recalls') OR EXISTS(SELECT 1 FROM purge_record_refs r JOIN purge_redactions p ON p.table_name=r.ref_kind AND p.row_id=r.ref_id WHERE r.table_name='recalls' AND r.row_id=CAST(recalls.id AS TEXT) AND r.ref_kind IN ('batches','consolidation_work','persona_attempts','entry_profile_attempts')));

UPDATE consolidation_work SET payload_json=payload_json WHERE EXISTS(SELECT 1 FROM memories WHERE purged_at IS NOT NULL) AND (CAST(id AS TEXT) IN (SELECT row_id FROM purge_upgrade_targets WHERE table_name='consolidation_work') OR EXISTS(SELECT 1 FROM purge_record_refs r JOIN purge_redactions p ON p.table_name=r.ref_kind AND p.row_id=r.ref_id WHERE r.table_name='consolidation_work' AND r.row_id=CAST(consolidation_work.id AS TEXT) AND r.ref_kind IN ('batches','consolidation_work','persona_attempts','entry_profile_attempts')));

UPDATE consolidation_calls SET raw_output=raw_output WHERE EXISTS(SELECT 1 FROM memories WHERE purged_at IS NOT NULL) AND (CAST(id AS TEXT) IN (SELECT row_id FROM purge_upgrade_targets WHERE table_name='consolidation_calls') OR EXISTS(SELECT 1 FROM purge_record_refs r JOIN purge_redactions p ON p.table_name=r.ref_kind AND p.row_id=r.ref_id WHERE r.table_name='consolidation_calls' AND r.row_id=CAST(consolidation_calls.id AS TEXT) AND r.ref_kind IN ('batches','consolidation_work','persona_attempts','entry_profile_attempts')));

UPDATE consolidation_run_work SET details_json=details_json WHERE EXISTS(SELECT 1 FROM memories WHERE purged_at IS NOT NULL) AND (json_array(run_id,work_id) IN (SELECT row_id FROM purge_upgrade_targets WHERE table_name='consolidation_run_work') OR EXISTS(SELECT 1 FROM purge_record_refs r JOIN purge_redactions p ON p.table_name=r.ref_kind AND p.row_id=r.ref_id WHERE r.table_name='consolidation_run_work' AND r.row_id=json_array(consolidation_run_work.run_id,consolidation_run_work.work_id) AND r.ref_kind IN ('batches','consolidation_work','persona_attempts','entry_profile_attempts')));

UPDATE consolidation_annotations SET text=text WHERE EXISTS(SELECT 1 FROM memories WHERE purged_at IS NOT NULL) AND (CAST(id AS TEXT) IN (SELECT row_id FROM purge_upgrade_targets WHERE table_name='consolidation_annotations') OR EXISTS(SELECT 1 FROM purge_record_refs r JOIN purge_redactions p ON p.table_name=r.ref_kind AND p.row_id=r.ref_id WHERE r.table_name='consolidation_annotations' AND r.row_id=CAST(consolidation_annotations.id AS TEXT) AND r.ref_kind IN ('batches','consolidation_work','persona_attempts','entry_profile_attempts')));

UPDATE persona_versions SET content=content WHERE EXISTS(SELECT 1 FROM memories WHERE purged_at IS NOT NULL) AND (CAST(id AS TEXT) IN (SELECT row_id FROM purge_upgrade_targets WHERE table_name='persona_versions') OR EXISTS(SELECT 1 FROM purge_record_refs r JOIN purge_redactions p ON p.table_name=r.ref_kind AND p.row_id=r.ref_id WHERE r.table_name='persona_versions' AND r.row_id=CAST(persona_versions.id AS TEXT) AND r.ref_kind IN ('batches','consolidation_work','persona_attempts','entry_profile_attempts')));

UPDATE entry_profile_versions SET content=content WHERE EXISTS(SELECT 1 FROM memories WHERE purged_at IS NOT NULL) AND (CAST(id AS TEXT) IN (SELECT row_id FROM purge_upgrade_targets WHERE table_name='entry_profile_versions') OR EXISTS(SELECT 1 FROM purge_record_refs r JOIN purge_redactions p ON p.table_name=r.ref_kind AND p.row_id=r.ref_id WHERE r.table_name='entry_profile_versions' AND r.row_id=CAST(entry_profile_versions.id AS TEXT) AND r.ref_kind IN ('batches','consolidation_work','persona_attempts','entry_profile_attempts')));

UPDATE persona_attempts SET material_json=material_json WHERE EXISTS(SELECT 1 FROM memories WHERE purged_at IS NOT NULL) AND (CAST(id AS TEXT) IN (SELECT row_id FROM purge_upgrade_targets WHERE table_name='persona_attempts') OR EXISTS(SELECT 1 FROM purge_record_refs r JOIN purge_redactions p ON p.table_name=r.ref_kind AND p.row_id=r.ref_id WHERE r.table_name='persona_attempts' AND r.row_id=CAST(persona_attempts.id AS TEXT) AND r.ref_kind IN ('batches','consolidation_work','persona_attempts','entry_profile_attempts')));

UPDATE entry_profile_attempts SET material_json=material_json WHERE EXISTS(SELECT 1 FROM memories WHERE purged_at IS NOT NULL) AND (CAST(id AS TEXT) IN (SELECT row_id FROM purge_upgrade_targets WHERE table_name='entry_profile_attempts') OR EXISTS(SELECT 1 FROM purge_record_refs r JOIN purge_redactions p ON p.table_name=r.ref_kind AND p.row_id=r.ref_id WHERE r.table_name='entry_profile_attempts' AND r.row_id=CAST(entry_profile_attempts.id AS TEXT) AND r.ref_kind IN ('batches','consolidation_work','persona_attempts','entry_profile_attempts')));

UPDATE maintenance_items SET details_json=details_json WHERE EXISTS(SELECT 1 FROM memories WHERE purged_at IS NOT NULL) AND (json_array(run_id,phase,item_key) IN (SELECT row_id FROM purge_upgrade_targets WHERE table_name='maintenance_items') OR EXISTS(SELECT 1 FROM purge_record_refs r JOIN purge_redactions p ON p.table_name=r.ref_kind AND p.row_id=r.ref_id WHERE r.table_name='maintenance_items' AND r.row_id=json_array(maintenance_items.run_id,maintenance_items.phase,maintenance_items.item_key) AND r.ref_kind IN ('batches','consolidation_work','persona_attempts','entry_profile_attempts')));

UPDATE maintenance_persona SET details_json=details_json WHERE EXISTS(SELECT 1 FROM memories WHERE purged_at IS NOT NULL) AND (CAST(run_id AS TEXT) IN (SELECT row_id FROM purge_upgrade_targets WHERE table_name='maintenance_persona') OR EXISTS(SELECT 1 FROM purge_record_refs r JOIN purge_redactions p ON p.table_name=r.ref_kind AND p.row_id=r.ref_id WHERE r.table_name='maintenance_persona' AND r.row_id=CAST(maintenance_persona.run_id AS TEXT) AND r.ref_kind IN ('batches','consolidation_work','persona_attempts','entry_profile_attempts')));

UPDATE sources SET note=note WHERE EXISTS(SELECT 1 FROM memories WHERE purged_at IS NOT NULL) AND (CAST(id AS TEXT) IN (SELECT row_id FROM purge_upgrade_targets WHERE table_name='sources') OR EXISTS(SELECT 1 FROM purge_record_refs r JOIN purge_redactions p ON p.table_name=r.ref_kind AND p.row_id=r.ref_id WHERE r.table_name='sources' AND r.row_id=CAST(sources.id AS TEXT) AND r.ref_kind IN ('batches','consolidation_work','persona_attempts','entry_profile_attempts')));

UPDATE media_objects SET understanding_text=understanding_text WHERE EXISTS(SELECT 1 FROM memories WHERE purged_at IS NOT NULL) AND (CAST(id AS TEXT) IN (SELECT row_id FROM purge_upgrade_targets WHERE table_name='media_objects') OR EXISTS(SELECT 1 FROM purge_record_refs r JOIN purge_redactions p ON p.table_name=r.ref_kind AND p.row_id=r.ref_id WHERE r.table_name='media_objects' AND r.row_id=CAST(media_objects.id AS TEXT) AND r.ref_kind IN ('batches','consolidation_work','persona_attempts','entry_profile_attempts')));

UPDATE admin_operations SET details_json=details_json WHERE EXISTS(SELECT 1 FROM memories WHERE purged_at IS NOT NULL) AND (CAST(id AS TEXT) IN (SELECT row_id FROM purge_upgrade_targets WHERE table_name='admin_operations') OR EXISTS(SELECT 1 FROM purge_record_refs r JOIN purge_redactions p ON p.table_name=r.ref_kind AND p.row_id=r.ref_id WHERE r.table_name='admin_operations' AND r.row_id=CAST(admin_operations.id AS TEXT) AND r.ref_kind IN ('batches','consolidation_work','persona_attempts','entry_profile_attempts')));

DROP TABLE purge_upgrade_targets;
