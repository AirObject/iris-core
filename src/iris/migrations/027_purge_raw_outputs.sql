-- Linked prose is scrubbed transactionally; this ledger also guards late writes.
-- No original text is retained here. Snapshot identifiers/numbers survive as metadata.
CREATE TABLE purge_redactions (
 table_name TEXT NOT NULL,
 row_id TEXT NOT NULL,
 cleared_at TEXT NOT NULL,
 reason TEXT NOT NULL DEFAULT '因彻底清除而清空',
 metadata_json TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(table_name,row_id)
);
CREATE TABLE purge_message_tombstones (
 message_id INTEGER PRIMARY KEY,
 cleared_at TEXT NOT NULL
);

CREATE TRIGGER purge_batches_insert AFTER INSERT ON batches
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM json_each(new.result_json) a, json_each(CASE WHEN a.type='array' THEN a.value ELSE '[]' END) x WHERE a.key IN ('created','updated','confirmed') AND x.value IN (SELECT id FROM memories WHERE purged_at IS NOT NULL)))) AND ((new.result_json IS NOT json_set(json_remove(new.result_json,'$.dropped','$.normalizations'),'$.redacted',1) OR new.last_error IS NOT NULL OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('batches',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.result_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE batches SET result_json=json_set(json_remove(new.result_json,'$.dropped','$.normalizations'),'$.redacted',1),
        last_error=NULL WHERE batches.id=new.id;
END;

CREATE TRIGGER purge_batches_update AFTER UPDATE ON batches
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM json_each(new.result_json) a, json_each(CASE WHEN a.type='array' THEN a.value ELSE '[]' END) x WHERE a.key IN ('created','updated','confirmed') AND x.value IN (SELECT id FROM memories WHERE purged_at IS NOT NULL)))) AND ((new.result_json IS NOT json_set(json_remove(new.result_json,'$.dropped','$.normalizations'),'$.redacted',1) OR new.last_error IS NOT NULL OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('batches',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.result_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE batches SET result_json=json_set(json_remove(new.result_json,'$.dropped','$.normalizations'),'$.redacted',1),
        last_error=NULL WHERE batches.id=new.id;
END;

CREATE TRIGGER purge_batches_delete AFTER DELETE ON batches BEGIN
    DELETE FROM purge_redactions WHERE table_name='batches' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_batch_attempts_insert AFTER INSERT ON batch_attempts
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='batch_attempts' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=CAST(new.batch_id AS TEXT)))) AND ((new.raw_output IS NOT NULL OR new.repair_output IS NOT NULL OR new.error IS NOT NULL OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='batch_attempts' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('batch_attempts',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}')
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE batch_attempts SET raw_output=NULL,
        repair_output=NULL,
        error=NULL WHERE batch_attempts.id=new.id;
END;

CREATE TRIGGER purge_batch_attempts_update AFTER UPDATE ON batch_attempts
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='batch_attempts' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=CAST(new.batch_id AS TEXT)))) AND ((new.raw_output IS NOT NULL OR new.repair_output IS NOT NULL OR new.error IS NOT NULL OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='batch_attempts' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('batch_attempts',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}')
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE batch_attempts SET raw_output=NULL,
        repair_output=NULL,
        error=NULL WHERE batch_attempts.id=new.id;
END;

CREATE TRIGGER purge_batch_attempts_delete AFTER DELETE ON batch_attempts BEGIN
    DELETE FROM purge_redactions WHERE table_name='batch_attempts' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_model_calls_insert AFTER INSERT ON model_calls
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='model_calls' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=CAST(new.batch_id AS TEXT)))) AND ((new.error_summary IS NOT NULL OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='model_calls' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('model_calls',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}')
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE model_calls SET error_summary=NULL WHERE model_calls.id=new.id;
END;

CREATE TRIGGER purge_model_calls_update AFTER UPDATE ON model_calls
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='model_calls' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=CAST(new.batch_id AS TEXT)))) AND ((new.error_summary IS NOT NULL OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='model_calls' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('model_calls',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}')
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE model_calls SET error_summary=NULL WHERE model_calls.id=new.id;
END;

CREATE TRIGGER purge_model_calls_delete AFTER DELETE ON model_calls BEGIN
    DELETE FROM purge_redactions WHERE table_name='model_calls' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_recalls_insert AFTER INSERT ON recalls
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='recalls' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM recall_items i JOIN memories m ON m.id=i.memory_id WHERE i.recall_id=new.id AND m.purged_at IS NOT NULL) OR EXISTS (SELECT 1 FROM json_tree(new.request_json) j WHERE j.type='integer' AND (replace(j.path,'"','') IN ('$.known_memory_ids','$.judgment.removed_memory_ids','$.judgment.stale_memory_ids')) AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL)))) AND ((new.request_json IS NOT json_set(json_remove(new.request_json,'$.text'),'$.redacted',1) OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='recalls' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('recalls',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.request_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE recalls SET request_json=json_set(json_remove(new.request_json,'$.text'),'$.redacted',1) WHERE recalls.id=new.id;
END;

CREATE TRIGGER purge_recalls_update AFTER UPDATE ON recalls
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='recalls' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM recall_items i JOIN memories m ON m.id=i.memory_id WHERE i.recall_id=new.id AND m.purged_at IS NOT NULL) OR EXISTS (SELECT 1 FROM json_tree(new.request_json) j WHERE j.type='integer' AND (replace(j.path,'"','') IN ('$.known_memory_ids','$.judgment.removed_memory_ids','$.judgment.stale_memory_ids')) AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL)))) AND ((new.request_json IS NOT json_set(json_remove(new.request_json,'$.text'),'$.redacted',1) OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='recalls' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('recalls',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.request_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE recalls SET request_json=json_set(json_remove(new.request_json,'$.text'),'$.redacted',1) WHERE recalls.id=new.id;
END;

CREATE TRIGGER purge_recalls_delete AFTER DELETE ON recalls BEGIN
    DELETE FROM purge_redactions WHERE table_name='recalls' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_consolidation_work_insert AFTER INSERT ON consolidation_work
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(new.payload_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        OR (j.key='id' AND j.path IN (SELECT fullkey FROM json_tree(COALESCE(new.payload_json,'{}')) WHERE path='$.memories' AND type='object') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))) OR EXISTS (SELECT 1 FROM json_each(new.payload_json,'$.memories') m, json_each(m.value,'$.sources') x WHERE json_extract(x.value,'$.id') IN (SELECT message_id FROM purge_message_tombstones)))) AND ((new.payload_json IS NOT '{}' OR new.result_json IS NOT NULL OR new.state IS NOT 'obsolete' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('consolidation_work',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.payload_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.result_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE consolidation_work SET payload_json='{}',
        result_json=NULL,
        state='obsolete' WHERE consolidation_work.id=new.id;
END;

CREATE TRIGGER purge_consolidation_work_update AFTER UPDATE ON consolidation_work
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(new.payload_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        OR (j.key='id' AND j.path IN (SELECT fullkey FROM json_tree(COALESCE(new.payload_json,'{}')) WHERE path='$.memories' AND type='object') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))) OR EXISTS (SELECT 1 FROM json_each(new.payload_json,'$.memories') m, json_each(m.value,'$.sources') x WHERE json_extract(x.value,'$.id') IN (SELECT message_id FROM purge_message_tombstones)))) AND ((new.payload_json IS NOT '{}' OR new.result_json IS NOT NULL OR new.state IS NOT 'obsolete' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('consolidation_work',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.payload_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.result_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE consolidation_work SET payload_json='{}',
        result_json=NULL,
        state='obsolete' WHERE consolidation_work.id=new.id;
END;

CREATE TRIGGER purge_consolidation_work_delete AFTER DELETE ON consolidation_work BEGIN
    DELETE FROM purge_redactions WHERE table_name='consolidation_work' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_consolidation_calls_insert AFTER INSERT ON consolidation_calls
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_calls' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=CAST(new.work_id AS TEXT)) OR EXISTS (SELECT 1 FROM maintenance_persona mp WHERE mp.run_id=new.run_id AND EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=CAST(mp.attempt_id AS TEXT))) OR (new.purpose LIKE 'consolidation_entry_profile_%' AND EXISTS (SELECT 1 FROM entry_profile_attempts ep WHERE ep.run_id=new.run_id AND EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=CAST(ep.id AS TEXT)))))) AND ((new.raw_output IS NOT NULL OR new.error IS NOT NULL OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_calls' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('consolidation_calls',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}')
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE consolidation_calls SET raw_output=NULL,
        error=NULL WHERE consolidation_calls.id=new.id;
END;

CREATE TRIGGER purge_consolidation_calls_update AFTER UPDATE ON consolidation_calls
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_calls' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=CAST(new.work_id AS TEXT)) OR EXISTS (SELECT 1 FROM maintenance_persona mp WHERE mp.run_id=new.run_id AND EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=CAST(mp.attempt_id AS TEXT))) OR (new.purpose LIKE 'consolidation_entry_profile_%' AND EXISTS (SELECT 1 FROM entry_profile_attempts ep WHERE ep.run_id=new.run_id AND EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=CAST(ep.id AS TEXT)))))) AND ((new.raw_output IS NOT NULL OR new.error IS NOT NULL OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_calls' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('consolidation_calls',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}')
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE consolidation_calls SET raw_output=NULL,
        error=NULL WHERE consolidation_calls.id=new.id;
END;

CREATE TRIGGER purge_consolidation_calls_delete AFTER DELETE ON consolidation_calls BEGIN
    DELETE FROM purge_redactions WHERE table_name='consolidation_calls' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_consolidation_run_work_insert AFTER INSERT ON consolidation_run_work
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_run_work' AND p.row_id=CAST(json_array(new.run_id,new.work_id) AS TEXT)) OR EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=CAST(new.work_id AS TEXT)))) AND ((new.details_json IS NOT '{}' OR new.reason IS NOT NULL OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_run_work' AND p.row_id=CAST(json_array(new.run_id,new.work_id) AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('consolidation_run_work',CAST(json_array(new.run_id,new.work_id) AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.details_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE consolidation_run_work SET details_json='{}',
        reason=NULL WHERE json_array(consolidation_run_work.run_id,consolidation_run_work.work_id)=json_array(new.run_id,new.work_id);
END;

CREATE TRIGGER purge_consolidation_run_work_update AFTER UPDATE ON consolidation_run_work
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_run_work' AND p.row_id=CAST(json_array(new.run_id,new.work_id) AS TEXT)) OR EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=CAST(new.work_id AS TEXT)))) AND ((new.details_json IS NOT '{}' OR new.reason IS NOT NULL OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_run_work' AND p.row_id=CAST(json_array(new.run_id,new.work_id) AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('consolidation_run_work',CAST(json_array(new.run_id,new.work_id) AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.details_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE consolidation_run_work SET details_json='{}',
        reason=NULL WHERE json_array(consolidation_run_work.run_id,consolidation_run_work.work_id)=json_array(new.run_id,new.work_id);
END;

CREATE TRIGGER purge_consolidation_run_work_delete AFTER DELETE ON consolidation_run_work BEGIN
    DELETE FROM purge_redactions WHERE table_name='consolidation_run_work' AND row_id=CAST(json_array(old.run_id,old.work_id) AS TEXT);
END;

CREATE TRIGGER purge_consolidation_annotations_insert AFTER INSERT ON consolidation_annotations
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_annotations' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=CAST(new.work_id AS TEXT)) OR new.memory_id IN (SELECT id FROM memories WHERE purged_at IS NOT NULL) OR EXISTS (SELECT 1 FROM json_each(new.related_ids_json) j JOIN memories m ON m.id=j.value WHERE m.purged_at IS NOT NULL))) AND ((new.text IS NOT '' OR new.evidence_json IS NOT '[]' OR new.cleared_at IS NOT COALESCE(new.cleared_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')) OR new.cleared_by IS NOT 'purge' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_annotations' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('consolidation_annotations',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.evidence_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE consolidation_annotations SET text='',
        evidence_json='[]',
        cleared_at=COALESCE(new.cleared_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        cleared_by='purge' WHERE consolidation_annotations.id=new.id;
END;

CREATE TRIGGER purge_consolidation_annotations_update AFTER UPDATE ON consolidation_annotations
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_annotations' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=CAST(new.work_id AS TEXT)) OR new.memory_id IN (SELECT id FROM memories WHERE purged_at IS NOT NULL) OR EXISTS (SELECT 1 FROM json_each(new.related_ids_json) j JOIN memories m ON m.id=j.value WHERE m.purged_at IS NOT NULL))) AND ((new.text IS NOT '' OR new.evidence_json IS NOT '[]' OR new.cleared_at IS NOT COALESCE(new.cleared_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')) OR new.cleared_by IS NOT 'purge' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_annotations' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('consolidation_annotations',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.evidence_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE consolidation_annotations SET text='',
        evidence_json='[]',
        cleared_at=COALESCE(new.cleared_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        cleared_by='purge' WHERE consolidation_annotations.id=new.id;
END;

CREATE TRIGGER purge_consolidation_annotations_delete AFTER DELETE ON consolidation_annotations BEGIN
    DELETE FROM purge_redactions WHERE table_name='consolidation_annotations' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_persona_versions_insert AFTER INSERT ON persona_versions
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_versions' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(new.material_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        ) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(new.sentences_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        ))) AND ((new.content IS NOT '' OR new.sentences_json IS NOT '[]' OR new.checks_json IS NOT '{"passed":false,"redacted":true,"reason":"因彻底清除而清空"}' OR new.material_json IS NOT '{}' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_versions' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('persona_versions',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.sentences_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.checks_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.material_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE persona_versions SET content='',
        sentences_json='[]',
        checks_json='{"passed":false,"redacted":true,"reason":"因彻底清除而清空"}',
        material_json='{}' WHERE persona_versions.id=new.id;
END;

CREATE TRIGGER purge_persona_versions_update AFTER UPDATE ON persona_versions
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_versions' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(new.material_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        ) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(new.sentences_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        ))) AND ((new.content IS NOT '' OR new.sentences_json IS NOT '[]' OR new.checks_json IS NOT '{"passed":false,"redacted":true,"reason":"因彻底清除而清空"}' OR new.material_json IS NOT '{}' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_versions' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('persona_versions',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.sentences_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.checks_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.material_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE persona_versions SET content='',
        sentences_json='[]',
        checks_json='{"passed":false,"redacted":true,"reason":"因彻底清除而清空"}',
        material_json='{}' WHERE persona_versions.id=new.id;
END;

CREATE TRIGGER purge_persona_versions_delete AFTER DELETE ON persona_versions BEGIN
    DELETE FROM purge_redactions WHERE table_name='persona_versions' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_entry_profile_versions_insert AFTER INSERT ON entry_profile_versions
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_versions' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(new.material_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        ) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(new.sentences_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        ))) AND ((new.content IS NOT '' OR new.sentences_json IS NOT '[]' OR new.checks_json IS NOT '{"passed":false,"redacted":true,"reason":"因彻底清除而清空"}' OR new.material_json IS NOT '{}' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_versions' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('entry_profile_versions',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.sentences_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.checks_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.material_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE entry_profile_versions SET content='',
        sentences_json='[]',
        checks_json='{"passed":false,"redacted":true,"reason":"因彻底清除而清空"}',
        material_json='{}' WHERE entry_profile_versions.id=new.id;
END;

CREATE TRIGGER purge_entry_profile_versions_update AFTER UPDATE ON entry_profile_versions
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_versions' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(new.material_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        ) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(new.sentences_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        ))) AND ((new.content IS NOT '' OR new.sentences_json IS NOT '[]' OR new.checks_json IS NOT '{"passed":false,"redacted":true,"reason":"因彻底清除而清空"}' OR new.material_json IS NOT '{}' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_versions' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('entry_profile_versions',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.sentences_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.checks_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))) UNION ALL SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.material_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE entry_profile_versions SET content='',
        sentences_json='[]',
        checks_json='{"passed":false,"redacted":true,"reason":"因彻底清除而清空"}',
        material_json='{}' WHERE entry_profile_versions.id=new.id;
END;

CREATE TRIGGER purge_entry_profile_versions_delete AFTER DELETE ON entry_profile_versions BEGIN
    DELETE FROM purge_redactions WHERE table_name='entry_profile_versions' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_persona_attempts_insert AFTER INSERT ON persona_attempts
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(new.material_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        ))) AND ((new.material_json IS NOT '{}' OR new.outputs_json IS NOT '{}' OR new.reason IS NOT '因彻底清除而清空' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('persona_attempts',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.material_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE persona_attempts SET material_json='{}',
        outputs_json='{}',
        reason='因彻底清除而清空' WHERE persona_attempts.id=new.id;
END;

CREATE TRIGGER purge_persona_attempts_update AFTER UPDATE ON persona_attempts
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(new.material_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        ))) AND ((new.material_json IS NOT '{}' OR new.outputs_json IS NOT '{}' OR new.reason IS NOT '因彻底清除而清空' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('persona_attempts',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.material_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE persona_attempts SET material_json='{}',
        outputs_json='{}',
        reason='因彻底清除而清空' WHERE persona_attempts.id=new.id;
END;

CREATE TRIGGER purge_persona_attempts_delete AFTER DELETE ON persona_attempts BEGIN
    DELETE FROM purge_redactions WHERE table_name='persona_attempts' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_entry_profile_attempts_insert AFTER INSERT ON entry_profile_attempts
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(new.material_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        ))) AND ((new.material_json IS NOT '{}' OR new.outputs_json IS NOT '{}' OR new.reason IS NOT '因彻底清除而清空' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('entry_profile_attempts',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.material_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE entry_profile_attempts SET material_json='{}',
        outputs_json='{}',
        reason='因彻底清除而清空' WHERE entry_profile_attempts.id=new.id;
END;

CREATE TRIGGER purge_entry_profile_attempts_update AFTER UPDATE ON entry_profile_attempts
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(new.material_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        ))) AND ((new.material_json IS NOT '{}' OR new.outputs_json IS NOT '{}' OR new.reason IS NOT '因彻底清除而清空' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('entry_profile_attempts',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.material_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE entry_profile_attempts SET material_json='{}',
        outputs_json='{}',
        reason='因彻底清除而清空' WHERE entry_profile_attempts.id=new.id;
END;

CREATE TRIGGER purge_entry_profile_attempts_delete AFTER DELETE ON entry_profile_attempts BEGIN
    DELETE FROM purge_redactions WHERE table_name='entry_profile_attempts' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_maintenance_items_insert AFTER INSERT ON maintenance_items
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='maintenance_items' AND p.row_id=CAST(json_array(new.run_id,new.phase,new.item_key) AS TEXT)) OR (new.phase='consolidation' AND EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=CAST(new.item_key AS TEXT))) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(new.details_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        ) OR new.memory_id IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))) AND ((new.details_json IS NOT '{}' OR new.reason IS NOT '因彻底清除而清空' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='maintenance_items' AND p.row_id=CAST(json_array(new.run_id,new.phase,new.item_key) AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('maintenance_items',CAST(json_array(new.run_id,new.phase,new.item_key) AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.details_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE maintenance_items SET details_json='{}',
        reason='因彻底清除而清空' WHERE json_array(maintenance_items.run_id,maintenance_items.phase,maintenance_items.item_key)=json_array(new.run_id,new.phase,new.item_key);
END;

CREATE TRIGGER purge_maintenance_items_update AFTER UPDATE ON maintenance_items
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='maintenance_items' AND p.row_id=CAST(json_array(new.run_id,new.phase,new.item_key) AS TEXT)) OR (new.phase='consolidation' AND EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=CAST(new.item_key AS TEXT))) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(new.details_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        ) OR new.memory_id IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))) AND ((new.details_json IS NOT '{}' OR new.reason IS NOT '因彻底清除而清空' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='maintenance_items' AND p.row_id=CAST(json_array(new.run_id,new.phase,new.item_key) AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('maintenance_items',CAST(json_array(new.run_id,new.phase,new.item_key) AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.details_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE maintenance_items SET details_json='{}',
        reason='因彻底清除而清空' WHERE json_array(maintenance_items.run_id,maintenance_items.phase,maintenance_items.item_key)=json_array(new.run_id,new.phase,new.item_key);
END;

CREATE TRIGGER purge_maintenance_items_delete AFTER DELETE ON maintenance_items BEGIN
    DELETE FROM purge_redactions WHERE table_name='maintenance_items' AND row_id=CAST(json_array(old.run_id,old.phase,old.item_key) AS TEXT);
END;

CREATE TRIGGER purge_maintenance_persona_insert AFTER INSERT ON maintenance_persona
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='maintenance_persona' AND p.row_id=CAST(new.run_id AS TEXT)) OR EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=CAST(new.attempt_id AS TEXT)))) AND ((new.details_json IS NOT '{}' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='maintenance_persona' AND p.row_id=CAST(new.run_id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('maintenance_persona',CAST(new.run_id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.details_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE maintenance_persona SET details_json='{}' WHERE maintenance_persona.run_id=new.run_id;
END;

CREATE TRIGGER purge_maintenance_persona_update AFTER UPDATE ON maintenance_persona
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='maintenance_persona' AND p.row_id=CAST(new.run_id AS TEXT)) OR EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=CAST(new.attempt_id AS TEXT)))) AND ((new.details_json IS NOT '{}' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='maintenance_persona' AND p.row_id=CAST(new.run_id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('maintenance_persona',CAST(new.run_id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.details_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE maintenance_persona SET details_json='{}' WHERE maintenance_persona.run_id=new.run_id;
END;

CREATE TRIGGER purge_maintenance_persona_delete AFTER DELETE ON maintenance_persona BEGIN
    DELETE FROM purge_redactions WHERE table_name='maintenance_persona' AND row_id=CAST(old.run_id AS TEXT);
END;

CREATE TRIGGER purge_sources_insert AFTER INSERT ON sources
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='sources' AND p.row_id=CAST(new.id AS TEXT)) OR new.source_memory_id IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))) AND ((new.note IS NOT NULL OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='sources' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('sources',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}')
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE sources SET note=NULL WHERE sources.id=new.id;
END;

CREATE TRIGGER purge_sources_update AFTER UPDATE ON sources
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='sources' AND p.row_id=CAST(new.id AS TEXT)) OR new.source_memory_id IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))) AND ((new.note IS NOT NULL OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='sources' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('sources',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}')
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE sources SET note=NULL WHERE sources.id=new.id;
END;

CREATE TRIGGER purge_sources_delete AFTER DELETE ON sources BEGIN
    DELETE FROM purge_redactions WHERE table_name='sources' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_media_objects_insert AFTER INSERT ON media_objects
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='media_objects' AND p.row_id=CAST(new.id AS TEXT)) OR 0)) AND ((new.understanding_text IS NOT CASE WHEN new.understanding_source='refused' THEN '敏感信息无法访问' ELSE '' END OR new.last_error IS NOT NULL OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='media_objects' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('media_objects',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}')
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE media_objects SET understanding_text=CASE WHEN new.understanding_source='refused' THEN '敏感信息无法访问' ELSE '' END,
        last_error=NULL WHERE media_objects.id=new.id;
END;

CREATE TRIGGER purge_media_objects_update AFTER UPDATE ON media_objects
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='media_objects' AND p.row_id=CAST(new.id AS TEXT)) OR 0)) AND ((new.understanding_text IS NOT CASE WHEN new.understanding_source='refused' THEN '敏感信息无法访问' ELSE '' END OR new.last_error IS NOT NULL OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='media_objects' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('media_objects',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}')
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE media_objects SET understanding_text=CASE WHEN new.understanding_source='refused' THEN '敏感信息无法访问' ELSE '' END,
        last_error=NULL WHERE media_objects.id=new.id;
END;

CREATE TRIGGER purge_media_objects_delete AFTER DELETE ON media_objects BEGIN
    DELETE FROM purge_redactions WHERE table_name='media_objects' AND row_id=CAST(old.id AS TEXT);
END;

CREATE TRIGGER purge_admin_operations_insert AFTER INSERT ON admin_operations
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND (((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='admin_operations' AND p.row_id=CAST(new.id AS TEXT)) OR (new.object_type='memory' AND CAST(new.object_id AS INTEGER) IN (SELECT id FROM memories WHERE purged_at IS NOT NULL)) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(new.details_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        ) OR EXISTS (SELECT 1 FROM json_each(new.details_json,'$.memory_ids') j JOIN memories m ON m.id=j.value WHERE m.purged_at IS NOT NULL))) AND new.action NOT IN ('memory_purge','memory_bulk_purge') AND (EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='admin_operations' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM json_tree(new.details_json) j WHERE j.key IN ('content','text','excerpt','report','raw_output','repair_output','background','template_text','sentence','annotation')))) AND ((new.details_json IS NOT json_object('redacted',1) OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='admin_operations' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('admin_operations',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.details_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE admin_operations SET details_json=json_object('redacted',1) WHERE admin_operations.id=new.id;
END;

CREATE TRIGGER purge_admin_operations_update AFTER UPDATE ON admin_operations
WHEN EXISTS (SELECT 1 FROM memories WHERE purged_at IS NOT NULL)
 AND (((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='admin_operations' AND p.row_id=CAST(new.id AS TEXT)) OR (new.object_type='memory' AND CAST(new.object_id AS INTEGER) IN (SELECT id FROM memories WHERE purged_at IS NOT NULL)) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(new.details_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        ) OR EXISTS (SELECT 1 FROM json_each(new.details_json,'$.memory_ids') j JOIN memories m ON m.id=j.value WHERE m.purged_at IS NOT NULL))) AND new.action NOT IN ('memory_purge','memory_bulk_purge') AND (EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='admin_operations' AND p.row_id=CAST(new.id AS TEXT)) OR EXISTS (SELECT 1 FROM json_tree(new.details_json) j WHERE j.key IN ('content','text','excerpt','report','raw_output','repair_output','background','template_text','sentence','annotation')))) AND ((new.details_json IS NOT json_object('redacted',1) OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='admin_operations' AND p.row_id=CAST(new.id AS TEXT)))) BEGIN
    INSERT INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
        VALUES('admin_operations',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),(SELECT json_object('references',json_group_array(json_object('field',field,'value',value))) FROM (SELECT j.key AS field,j.atom AS value FROM json_tree(COALESCE(new.details_json,'{}')) j WHERE j.type IN ('integer','real','true','false') AND j.key IN ('id','memory_id','message_id','source_memory_id','revision','revision_before','revision_after','batch_id','work_id','run_id','version','importance','belief','retention') OR (j.type='integer' AND (replace(j.path,'"','') LIKE '%memory_ids' OR replace(j.path,'"','') LIKE '%message_ids' OR j.path IN ('$.pair_ids','$.evidence','$.created','$.updated','$.confirmed'))))))
        ON CONFLICT(table_name,row_id) DO UPDATE SET metadata_json=excluded.metadata_json
        WHERE purge_redactions.metadata_json='{}';
    UPDATE admin_operations SET details_json=json_object('redacted',1) WHERE admin_operations.id=new.id;
END;

CREATE TRIGGER purge_admin_operations_delete AFTER DELETE ON admin_operations BEGIN
    DELETE FROM purge_redactions WHERE table_name='admin_operations' AND row_id=CAST(old.id AS TEXT);
END;

-- The purge entry point drains this fixed point before committing.
CREATE VIEW purge_redaction_candidates AS
SELECT 'batches' AS table_name,CAST(r.id AS TEXT) AS row_id FROM batches r WHERE (EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=CAST(r.id AS TEXT)) OR EXISTS (SELECT 1 FROM json_each(r.result_json) a, json_each(CASE WHEN a.type='array' THEN a.value ELSE '[]' END) x WHERE a.key IN ('created','updated','confirmed') AND x.value IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))) AND (r.result_json IS NOT json_set(json_remove(r.result_json,'$.dropped','$.normalizations'),'$.redacted',1) OR r.last_error IS NOT NULL OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=CAST(r.id AS TEXT)))
UNION ALL
SELECT 'batch_attempts' AS table_name,CAST(r.id AS TEXT) AS row_id FROM batch_attempts r WHERE (EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='batch_attempts' AND p.row_id=CAST(r.id AS TEXT)) OR EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=CAST(r.batch_id AS TEXT))) AND (r.raw_output IS NOT NULL OR r.repair_output IS NOT NULL OR r.error IS NOT NULL OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='batch_attempts' AND p.row_id=CAST(r.id AS TEXT)))
UNION ALL
SELECT 'model_calls' AS table_name,CAST(r.id AS TEXT) AS row_id FROM model_calls r WHERE (EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='model_calls' AND p.row_id=CAST(r.id AS TEXT)) OR EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='batches' AND p.row_id=CAST(r.batch_id AS TEXT))) AND (r.error_summary IS NOT NULL OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='model_calls' AND p.row_id=CAST(r.id AS TEXT)))
UNION ALL
SELECT 'recalls' AS table_name,CAST(r.id AS TEXT) AS row_id FROM recalls r WHERE (EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='recalls' AND p.row_id=CAST(r.id AS TEXT)) OR EXISTS (SELECT 1 FROM recall_items i JOIN memories m ON m.id=i.memory_id WHERE i.recall_id=r.id AND m.purged_at IS NOT NULL) OR EXISTS (SELECT 1 FROM json_tree(r.request_json) j WHERE j.type='integer' AND (replace(j.path,'"','') IN ('$.known_memory_ids','$.judgment.removed_memory_ids','$.judgment.stale_memory_ids')) AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))) AND (r.request_json IS NOT json_set(json_remove(r.request_json,'$.text'),'$.redacted',1) OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='recalls' AND p.row_id=CAST(r.id AS TEXT)))
UNION ALL
SELECT 'consolidation_work' AS table_name,CAST(r.id AS TEXT) AS row_id FROM consolidation_work r WHERE (EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=CAST(r.id AS TEXT)) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(r.payload_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        OR (j.key='id' AND j.path IN (SELECT fullkey FROM json_tree(COALESCE(r.payload_json,'{}')) WHERE path='$.memories' AND type='object') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))) OR EXISTS (SELECT 1 FROM json_each(r.payload_json,'$.memories') m, json_each(m.value,'$.sources') x WHERE json_extract(x.value,'$.id') IN (SELECT message_id FROM purge_message_tombstones))) AND (r.payload_json IS NOT '{}' OR r.result_json IS NOT NULL OR r.state IS NOT 'obsolete' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=CAST(r.id AS TEXT)))
UNION ALL
SELECT 'consolidation_calls' AS table_name,CAST(r.id AS TEXT) AS row_id FROM consolidation_calls r WHERE (EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_calls' AND p.row_id=CAST(r.id AS TEXT)) OR EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=CAST(r.work_id AS TEXT)) OR EXISTS (SELECT 1 FROM maintenance_persona mp WHERE mp.run_id=r.run_id AND EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=CAST(mp.attempt_id AS TEXT))) OR (r.purpose LIKE 'consolidation_entry_profile_%' AND EXISTS (SELECT 1 FROM entry_profile_attempts ep WHERE ep.run_id=r.run_id AND EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=CAST(ep.id AS TEXT))))) AND (r.raw_output IS NOT NULL OR r.error IS NOT NULL OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_calls' AND p.row_id=CAST(r.id AS TEXT)))
UNION ALL
SELECT 'consolidation_run_work' AS table_name,CAST(json_array(r.run_id,r.work_id) AS TEXT) AS row_id FROM consolidation_run_work r WHERE (EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_run_work' AND p.row_id=CAST(json_array(r.run_id,r.work_id) AS TEXT)) OR EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=CAST(r.work_id AS TEXT))) AND (r.details_json IS NOT '{}' OR r.reason IS NOT NULL OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_run_work' AND p.row_id=CAST(json_array(r.run_id,r.work_id) AS TEXT)))
UNION ALL
SELECT 'consolidation_annotations' AS table_name,CAST(r.id AS TEXT) AS row_id FROM consolidation_annotations r WHERE (EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_annotations' AND p.row_id=CAST(r.id AS TEXT)) OR EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=CAST(r.work_id AS TEXT)) OR r.memory_id IN (SELECT id FROM memories WHERE purged_at IS NOT NULL) OR EXISTS (SELECT 1 FROM json_each(r.related_ids_json) j JOIN memories m ON m.id=j.value WHERE m.purged_at IS NOT NULL)) AND (r.text IS NOT '' OR r.evidence_json IS NOT '[]' OR r.cleared_at IS NOT COALESCE(r.cleared_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')) OR r.cleared_by IS NOT 'purge' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_annotations' AND p.row_id=CAST(r.id AS TEXT)))
UNION ALL
SELECT 'persona_versions' AS table_name,CAST(r.id AS TEXT) AS row_id FROM persona_versions r WHERE (EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_versions' AND p.row_id=CAST(r.id AS TEXT)) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(r.material_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        ) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(r.sentences_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        )) AND (r.content IS NOT '' OR r.sentences_json IS NOT '[]' OR r.checks_json IS NOT '{"passed":false,"redacted":true,"reason":"因彻底清除而清空"}' OR r.material_json IS NOT '{}' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_versions' AND p.row_id=CAST(r.id AS TEXT)))
UNION ALL
SELECT 'entry_profile_versions' AS table_name,CAST(r.id AS TEXT) AS row_id FROM entry_profile_versions r WHERE (EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_versions' AND p.row_id=CAST(r.id AS TEXT)) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(r.material_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        ) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(r.sentences_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        )) AND (r.content IS NOT '' OR r.sentences_json IS NOT '[]' OR r.checks_json IS NOT '{"passed":false,"redacted":true,"reason":"因彻底清除而清空"}' OR r.material_json IS NOT '{}' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_versions' AND p.row_id=CAST(r.id AS TEXT)))
UNION ALL
SELECT 'persona_attempts' AS table_name,CAST(r.id AS TEXT) AS row_id FROM persona_attempts r WHERE (EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=CAST(r.id AS TEXT)) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(r.material_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        )) AND (r.material_json IS NOT '{}' OR r.outputs_json IS NOT '{}' OR r.reason IS NOT '因彻底清除而清空' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=CAST(r.id AS TEXT)))
UNION ALL
SELECT 'entry_profile_attempts' AS table_name,CAST(r.id AS TEXT) AS row_id FROM entry_profile_attempts r WHERE (EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=CAST(r.id AS TEXT)) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(r.material_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        )) AND (r.material_json IS NOT '{}' OR r.outputs_json IS NOT '{}' OR r.reason IS NOT '因彻底清除而清空' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_attempts' AND p.row_id=CAST(r.id AS TEXT)))
UNION ALL
SELECT 'maintenance_items' AS table_name,CAST(json_array(r.run_id,r.phase,r.item_key) AS TEXT) AS row_id FROM maintenance_items r WHERE (EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='maintenance_items' AND p.row_id=CAST(json_array(r.run_id,r.phase,r.item_key) AS TEXT)) OR (r.phase='consolidation' AND EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='consolidation_work' AND p.row_id=CAST(r.item_key AS TEXT))) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(r.details_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        ) OR r.memory_id IN (SELECT id FROM memories WHERE purged_at IS NOT NULL)) AND (r.details_json IS NOT '{}' OR r.reason IS NOT '因彻底清除而清空' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='maintenance_items' AND p.row_id=CAST(json_array(r.run_id,r.phase,r.item_key) AS TEXT)))
UNION ALL
SELECT 'maintenance_persona' AS table_name,CAST(r.run_id AS TEXT) AS row_id FROM maintenance_persona r WHERE (EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='maintenance_persona' AND p.row_id=CAST(r.run_id AS TEXT)) OR EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='persona_attempts' AND p.row_id=CAST(r.attempt_id AS TEXT))) AND (r.details_json IS NOT '{}' OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='maintenance_persona' AND p.row_id=CAST(r.run_id AS TEXT)))
UNION ALL
SELECT 'sources' AS table_name,CAST(r.id AS TEXT) AS row_id FROM sources r WHERE (EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='sources' AND p.row_id=CAST(r.id AS TEXT)) OR r.source_memory_id IN (SELECT id FROM memories WHERE purged_at IS NOT NULL)) AND (r.note IS NOT NULL OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='sources' AND p.row_id=CAST(r.id AS TEXT)))
UNION ALL
SELECT 'media_objects' AS table_name,CAST(r.id AS TEXT) AS row_id FROM media_objects r WHERE (EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='media_objects' AND p.row_id=CAST(r.id AS TEXT)) OR 0) AND (r.understanding_text IS NOT CASE WHEN r.understanding_source='refused' THEN '敏感信息无法访问' ELSE '' END OR r.last_error IS NOT NULL OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='media_objects' AND p.row_id=CAST(r.id AS TEXT)))
UNION ALL
SELECT 'admin_operations' AS table_name,CAST(r.id AS TEXT) AS row_id FROM admin_operations r WHERE ((EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='admin_operations' AND p.row_id=CAST(r.id AS TEXT)) OR (r.object_type='memory' AND CAST(r.object_id AS INTEGER) IN (SELECT id FROM memories WHERE purged_at IS NOT NULL)) OR EXISTS (SELECT 1 FROM json_tree(COALESCE(r.details_json,'{}')) j
        WHERE (j.key IN ('memory_id','source_memory_id') AND j.atom IN (SELECT id FROM memories WHERE purged_at IS NOT NULL))
        OR (j.key='message_id' AND j.atom IN (SELECT message_id FROM purge_message_tombstones))
        ) OR EXISTS (SELECT 1 FROM json_each(r.details_json,'$.memory_ids') j JOIN memories m ON m.id=j.value WHERE m.purged_at IS NOT NULL))) AND r.action NOT IN ('memory_purge','memory_bulk_purge') AND (EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='admin_operations' AND p.row_id=CAST(r.id AS TEXT)) OR EXISTS (SELECT 1 FROM json_tree(r.details_json) j WHERE j.key IN ('content','text','excerpt','report','raw_output','repair_output','background','template_text','sentence','annotation'))) AND (r.details_json IS NOT json_object('redacted',1) OR NOT EXISTS (SELECT 1 FROM purge_redactions p WHERE p.table_name='admin_operations' AND p.row_id=CAST(r.id AS TEXT)));

-- Recall items are inserted after their parent request. Re-evaluate after linking.
CREATE TRIGGER purge_recall_item AFTER INSERT ON recall_items
WHEN EXISTS (SELECT 1 FROM memories WHERE id=new.memory_id AND purged_at IS NOT NULL) BEGIN
    UPDATE recalls SET request_json=request_json WHERE id=new.recall_id;
END;
CREATE INDEX memories_purged_ids ON memories(id) WHERE purged_at IS NOT NULL;

-- Scrub identifiable copies of objects purged before this upgrade.
UPDATE batches SET result_json=result_json WHERE batches.id IN (SELECT row_id FROM purge_redaction_candidates WHERE table_name='batches');
UPDATE batch_attempts SET raw_output=raw_output WHERE batch_attempts.id IN (SELECT row_id FROM purge_redaction_candidates WHERE table_name='batch_attempts');
UPDATE model_calls SET error_summary=error_summary WHERE model_calls.id IN (SELECT row_id FROM purge_redaction_candidates WHERE table_name='model_calls');
UPDATE recalls SET request_json=request_json WHERE recalls.id IN (SELECT row_id FROM purge_redaction_candidates WHERE table_name='recalls');
UPDATE consolidation_work SET payload_json=payload_json WHERE consolidation_work.id IN (SELECT row_id FROM purge_redaction_candidates WHERE table_name='consolidation_work');
UPDATE consolidation_calls SET raw_output=raw_output WHERE consolidation_calls.id IN (SELECT row_id FROM purge_redaction_candidates WHERE table_name='consolidation_calls');
UPDATE consolidation_run_work SET details_json=details_json WHERE json_array(consolidation_run_work.run_id,consolidation_run_work.work_id) IN (SELECT row_id FROM purge_redaction_candidates WHERE table_name='consolidation_run_work');
UPDATE consolidation_annotations SET text=text WHERE consolidation_annotations.id IN (SELECT row_id FROM purge_redaction_candidates WHERE table_name='consolidation_annotations');
UPDATE persona_versions SET content=content WHERE persona_versions.id IN (SELECT row_id FROM purge_redaction_candidates WHERE table_name='persona_versions');
UPDATE entry_profile_versions SET content=content WHERE entry_profile_versions.id IN (SELECT row_id FROM purge_redaction_candidates WHERE table_name='entry_profile_versions');
UPDATE persona_attempts SET material_json=material_json WHERE persona_attempts.id IN (SELECT row_id FROM purge_redaction_candidates WHERE table_name='persona_attempts');
UPDATE entry_profile_attempts SET material_json=material_json WHERE entry_profile_attempts.id IN (SELECT row_id FROM purge_redaction_candidates WHERE table_name='entry_profile_attempts');
UPDATE maintenance_items SET details_json=details_json WHERE json_array(maintenance_items.run_id,maintenance_items.phase,maintenance_items.item_key) IN (SELECT row_id FROM purge_redaction_candidates WHERE table_name='maintenance_items');
UPDATE maintenance_persona SET details_json=details_json WHERE maintenance_persona.run_id IN (SELECT row_id FROM purge_redaction_candidates WHERE table_name='maintenance_persona');
UPDATE sources SET note=note WHERE sources.id IN (SELECT row_id FROM purge_redaction_candidates WHERE table_name='sources');
UPDATE media_objects SET understanding_text=understanding_text WHERE media_objects.id IN (SELECT row_id FROM purge_redaction_candidates WHERE table_name='media_objects');
UPDATE admin_operations SET details_json=details_json WHERE admin_operations.id IN (SELECT row_id FROM purge_redaction_candidates WHERE table_name='admin_operations');
