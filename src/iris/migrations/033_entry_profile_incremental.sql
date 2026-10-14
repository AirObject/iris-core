-- Candidate memories share the ordinary lifecycle. Admission is independent of active.
ALTER TABLE entry_profile_memory_state ADD COLUMN scan_cursor INTEGER NOT NULL DEFAULT 0;
ALTER TABLE entry_profile_memory_state ADD COLUMN boundary_json TEXT NOT NULL DEFAULT '{}';
UPDATE entry_profile_memory_state SET scan_cursor=cursor;
CREATE TABLE entry_profile_admissions (
 memory_id INTEGER PRIMARY KEY REFERENCES entry_profile_memories(memory_id) ON DELETE CASCADE,
 body_revision INTEGER NOT NULL,
 evidence_revision INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'candidate' CHECK(state IN ('candidate','pending','accepted','rejected')),
 checked_sources_json TEXT NOT NULL DEFAULT '[]',
 signature TEXT,
 reason TEXT,
 independent_days INTEGER NOT NULL DEFAULT 0,
 independent_uses INTEGER NOT NULL DEFAULT 0,
 definition_refs_json TEXT NOT NULL DEFAULT '[]',
 checked_at TEXT
);
INSERT INTO entry_profile_admissions(memory_id,body_revision)
 SELECT p.memory_id,m.revision FROM entry_profile_memories p JOIN memories m ON m.id=p.memory_id;
CREATE TRIGGER ep_incremental_candidate AFTER INSERT ON entry_profile_memories BEGIN
 INSERT INTO entry_profile_admissions(memory_id,body_revision)
 SELECT new.memory_id,revision FROM memories WHERE id=new.memory_id;
END;
CREATE TABLE entry_profile_confirmation_groups (
 memory_id INTEGER NOT NULL REFERENCES entry_profile_memories(memory_id) ON DELETE CASCADE,
 identity TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(memory_id,identity)
);
-- Previous receipts were already effective. Never turn migration into new confirmation.
INSERT INTO entry_profile_confirmation_groups
 SELECT r.memory_id,r.identity,r.created_at FROM entry_profile_evidence_receipts r;
CREATE TABLE entry_profile_source_groups (
 source_id INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
 memory_id INTEGER NOT NULL REFERENCES entry_profile_memories(memory_id) ON DELETE CASCADE,
 burst_id TEXT NOT NULL
);
CREATE TABLE entry_profile_incremental_work (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 entry_id TEXT NOT NULL REFERENCES entries(id),
 work_key TEXT NOT NULL UNIQUE,
 kind TEXT NOT NULL CHECK(kind IN ('observe','admit')),
 state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','applied','failed','rejected','cancelled','deferred')),
 cursor_before INTEGER NOT NULL DEFAULT 0,
 cursor_after INTEGER NOT NULL DEFAULT 0,
 local_day TEXT,
 fragment_group TEXT,
 memory_id INTEGER REFERENCES memories(id),
 memory_revision INTEGER,
 payload_json TEXT NOT NULL DEFAULT '{}',
 outputs_json TEXT NOT NULL DEFAULT '{}',
 calls_json TEXT NOT NULL DEFAULT '[]',
 result_json TEXT NOT NULL DEFAULT '{}',
 parsed_json TEXT,
 attempt_id INTEGER REFERENCES entry_profile_attempts(id),
 attempts INTEGER NOT NULL DEFAULT 0,
 reason TEXT,
 retry_at TEXT,
 created_at TEXT NOT NULL,
 finished_at TEXT
);
CREATE INDEX ep_incremental_queue ON entry_profile_incremental_work(entry_id,kind,state,id);
CREATE INDEX ep_incremental_fragments ON entry_profile_incremental_work(fragment_group,state);
CREATE INDEX ep_incremental_target ON entry_profile_incremental_work(memory_id,id);
CREATE TRIGGER ep_admission_dirty_insert AFTER INSERT ON entry_profile_admissions BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty SELECT entry_id FROM entry_profile_memories WHERE memory_id=new.memory_id;
END;
CREATE TRIGGER ep_admission_dirty_update AFTER UPDATE ON entry_profile_admissions BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty SELECT entry_id FROM entry_profile_memories WHERE memory_id=new.memory_id;
END;
CREATE TRIGGER ep_admission_dirty_delete AFTER DELETE ON entry_profile_admissions BEGIN
 INSERT OR IGNORE INTO entry_profile_projection_dirty SELECT entry_id FROM entry_profile_memories WHERE memory_id=old.memory_id;
END;
-- Sources and work are indexed for erasure before any provider response can arrive.
CREATE TRIGGER ep_incremental_work_insert AFTER INSERT ON entry_profile_incremental_work BEGIN
 INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision)
 SELECT 'entry_profile_incremental_work',CAST(new.id AS TEXT),
 CASE j.key WHEN 'message_id' THEN 'message' ELSE 'memory' END,CAST(j.atom AS TEXT),0
 FROM json_tree(new.payload_json) j WHERE j.key IN ('message_id','memory_id') AND j.type='integer';
 INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision)
 SELECT 'entry_profile_incremental_work',CAST(new.id AS TEXT),'memory',CAST(new.memory_id AS TEXT),COALESCE(new.memory_revision,0)
 WHERE new.memory_id IS NOT NULL;
 INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision)
 SELECT 'entry_profile_incremental_work',CAST(new.id AS TEXT),'entry_profile_attempts',CAST(new.attempt_id AS TEXT),0
 WHERE new.attempt_id IS NOT NULL;
END;
CREATE TRIGGER ep_incremental_work_update AFTER UPDATE ON entry_profile_incremental_work BEGIN
 INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision)
 SELECT 'entry_profile_incremental_work',CAST(new.id AS TEXT),
 CASE j.key WHEN 'message_id' THEN 'message' ELSE 'memory' END,CAST(j.atom AS TEXT),0
 FROM json_tree(new.payload_json) j WHERE j.key IN ('message_id','memory_id') AND j.type='integer';
 INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision)
 SELECT 'entry_profile_incremental_work',CAST(new.id AS TEXT),'memory',CAST(j.atom AS TEXT),0
 FROM json_tree(new.result_json) j WHERE j.key='memory_id' AND j.type='integer';
 INSERT OR IGNORE INTO purge_record_refs(table_name,row_id,ref_kind,ref_id,revision)
 SELECT 'entry_profile_incremental_work',CAST(new.id AS TEXT),'entry_profile_attempts',CAST(new.attempt_id AS TEXT),0
 WHERE new.attempt_id IS NOT NULL;
 INSERT OR IGNORE INTO purge_redactions(table_name,row_id,cleared_at,metadata_json)
 SELECT 'entry_profile_incremental_work',CAST(new.id AS TEXT),strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}'
 WHERE EXISTS(SELECT 1 FROM purge_record_refs r WHERE r.table_name='entry_profile_incremental_work' AND r.row_id=CAST(new.id AS TEXT)
 AND ((r.ref_kind='memory' AND EXISTS(SELECT 1 FROM memories m WHERE m.id=CAST(r.ref_id AS INTEGER) AND m.purged_at IS NOT NULL))
 OR (r.ref_kind='message' AND EXISTS(SELECT 1 FROM purge_message_tombstones t WHERE t.message_id=CAST(r.ref_id AS INTEGER)))
 OR (r.ref_kind='entry_profile_attempts' AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name=r.ref_kind AND p.row_id=r.ref_id))));
 UPDATE entry_profile_incremental_work SET state='cancelled',reason='purged',payload_json='{}',outputs_json='{}',calls_json='[]',result_json='{}',parsed_json=NULL
 WHERE id=new.id AND EXISTS(SELECT 1 FROM purge_redactions p WHERE p.table_name='entry_profile_incremental_work' AND p.row_id=CAST(new.id AS TEXT))
 AND (new.state!='cancelled' OR new.payload_json!='{}' OR new.outputs_json!='{}' OR new.calls_json!='[]' OR new.result_json!='{}' OR new.parsed_json IS NOT NULL);
END;
CREATE TRIGGER ep_incremental_work_delete AFTER DELETE ON entry_profile_incremental_work BEGIN
 DELETE FROM purge_record_refs WHERE table_name='entry_profile_incremental_work' AND row_id=CAST(old.id AS TEXT);
 DELETE FROM purge_redactions WHERE table_name='entry_profile_incremental_work' AND row_id=CAST(old.id AS TEXT);
END;
INSERT OR IGNORE INTO entry_profile_projection_dirty SELECT entry_id FROM entry_profile_memories;

-- Category isolation is already enforced by 031; inherit identities without adding retention.
CREATE TRIGGER ep_incremental_merge AFTER UPDATE OF merged_into ON memories
WHEN old.kind='入口画像' AND new.merged_into IS NOT NULL AND new.merged_into IS NOT old.merged_into BEGIN
 INSERT OR IGNORE INTO entry_profile_confirmation_groups
 SELECT new.merged_into,identity,created_at FROM entry_profile_confirmation_groups WHERE memory_id=new.id;
 INSERT OR IGNORE INTO entry_profile_source_groups
 SELECT n.id,new.merged_into,g.burst_id FROM entry_profile_source_groups g
 JOIN sources o ON o.id=g.source_id JOIN sources n ON n.memory_id=new.merged_into
 AND n.message_id IS o.message_id AND n.note IS o.note WHERE g.memory_id=new.id;
 UPDATE entry_profile_admissions SET state='candidate',reason='merged_basis',evidence_revision=evidence_revision+1
 WHERE memory_id=new.merged_into;
END;
