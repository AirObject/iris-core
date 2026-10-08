-- Lifecycle work is durable, deterministic, and independent of model calls.
INSERT OR IGNORE INTO runtime_settings(key,value_json) VALUES('lifecycle',
'{"forget_threshold":20,"restore_threshold":35,"feedback_increment":8,"confirmation_increment":5,"decay_amount":1,"auto_delete_enabled":true,"auto_delete_days":180,"upcoming_delete_days":14,"message_retention_days":30,"maintenance_time":"03:00","abandoned_retry_enabled":true,"dependency_penalty":10}');
ALTER TABLE memories ADD COLUMN decay_visits INTEGER NOT NULL DEFAULT 0;
-- A purged object keeps an empty tombstone, so every insertion path reserves its ID.
ALTER TABLE memories ADD COLUMN purged_at TEXT;
CREATE INDEX memories_forgotten_since ON memories(forgotten_at,id) WHERE lifecycle='forgotten';
CREATE INDEX messages_received_instant ON messages(julianday(received_at));
CREATE INDEX sources_by_message ON sources(message_id);
CREATE INDEX sources_by_parent ON sources(source_memory_id,memory_id);
CREATE INDEX subject_aliases_by_message ON subject_aliases(source_message_id);
CREATE INDEX subject_links_by_message ON subject_links(source_message_id);
CREATE INDEX goal_sources_by_message ON goal_sources(message_id);

CREATE TABLE maintenance_runs (
    id INTEGER PRIMARY KEY,
    trigger TEXT NOT NULL,
    schedule_key TEXT UNIQUE,
    state TEXT NOT NULL DEFAULT 'running' CHECK(state IN ('running','completed')),
    phase INTEGER NOT NULL DEFAULT 0,
    settings_json TEXT NOT NULL,
    timezone TEXT NOT NULL,
    memory_through INTEGER NOT NULL,
    message_through INTEGER NOT NULL,
    batch_through INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX maintenance_one_open ON maintenance_runs(state) WHERE state='running';
CREATE TABLE maintenance_items (
    run_id INTEGER NOT NULL REFERENCES maintenance_runs(id),
    phase TEXT NOT NULL,
    item_key TEXT NOT NULL,
    memory_id INTEGER,
    object_id INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id,phase,item_key)
);
CREATE TABLE memory_dependency_losses (
    memory_id INTEGER NOT NULL REFERENCES memories(id),
    source_memory_id INTEGER NOT NULL REFERENCES memories(id),
    lost_at TEXT NOT NULL,
    applied_at TEXT,
    amount INTEGER,
    run_id INTEGER REFERENCES maintenance_runs(id),
    PRIMARY KEY(memory_id,source_memory_id)
);
CREATE INDEX dependency_losses_pending ON memory_dependency_losses(memory_id,source_memory_id) WHERE applied_at IS NULL;
INSERT OR IGNORE INTO memory_dependency_losses(memory_id,source_memory_id,lost_at)
SELECT s.memory_id,s.source_memory_id,COALESCE(m.forgotten_at,m.updated_at)
FROM sources s JOIN memories m ON m.id=s.source_memory_id
WHERE s.kind='memory' AND m.lifecycle IN ('forgotten','deleted') AND s.memory_id!=s.source_memory_id;
CREATE TRIGGER memory_lost_support AFTER UPDATE OF lifecycle ON memories
WHEN new.lifecycle IN ('forgotten','deleted') AND old.lifecycle!=new.lifecycle BEGIN
    INSERT OR IGNORE INTO memory_dependency_losses(memory_id,source_memory_id,lost_at)
    SELECT memory_id,new.id,COALESCE(new.forgotten_at,new.updated_at)
    FROM sources WHERE kind='memory' AND source_memory_id=new.id AND memory_id!=new.id;
END;
CREATE TRIGGER memory_source_already_lost AFTER INSERT ON sources
WHEN new.kind='memory' AND new.memory_id!=new.source_memory_id BEGIN
    INSERT OR IGNORE INTO memory_dependency_losses(memory_id,source_memory_id,lost_at)
    SELECT new.memory_id,m.id,COALESCE(m.forgotten_at,m.updated_at)
    FROM memories m WHERE m.id=new.source_memory_id AND m.lifecycle IN ('forgotten','deleted');
END;
CREATE TABLE maintenance_batch_retries (
    batch_id INTEGER PRIMARY KEY REFERENCES batches(id),
    run_id INTEGER NOT NULL REFERENCES maintenance_runs(id),
    created_at TEXT NOT NULL
);

-- Frozen JSON segments are real references too. Index them without changing queue.py.
CREATE TABLE batch_message_refs (
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL,
    segment TEXT NOT NULL,
    PRIMARY KEY(batch_id,message_id,segment)
);
CREATE INDEX batch_message_refs_by_message ON batch_message_refs(message_id);
INSERT OR IGNORE INTO batch_message_refs SELECT b.id,j.value,'history' FROM batches b,json_each(b.history_ids) j;
INSERT OR IGNORE INTO batch_message_refs SELECT b.id,j.value,'target' FROM batches b,json_each(b.target_ids) j;
INSERT OR IGNORE INTO batch_message_refs SELECT b.id,j.value,'future' FROM batches b,json_each(b.future_ids) j;
CREATE TRIGGER batch_refs_insert AFTER INSERT ON batches BEGIN
    INSERT OR IGNORE INTO batch_message_refs SELECT new.id,value,'history' FROM json_each(new.history_ids);
    INSERT OR IGNORE INTO batch_message_refs SELECT new.id,value,'target' FROM json_each(new.target_ids);
    INSERT OR IGNORE INTO batch_message_refs SELECT new.id,value,'future' FROM json_each(new.future_ids);
END;
CREATE TRIGGER batch_refs_update AFTER UPDATE OF history_ids,target_ids,future_ids ON batches BEGIN
    DELETE FROM batch_message_refs WHERE batch_id=new.id;
    INSERT OR IGNORE INTO batch_message_refs SELECT new.id,value,'history' FROM json_each(new.history_ids);
    INSERT OR IGNORE INTO batch_message_refs SELECT new.id,value,'target' FROM json_each(new.target_ids);
    INSERT OR IGNORE INTO batch_message_refs SELECT new.id,value,'future' FROM json_each(new.future_ids);
END;

ALTER TABLE admin_operations ADD COLUMN object_type TEXT;
ALTER TABLE admin_operations ADD COLUMN object_id TEXT;
CREATE INDEX operations_time ON admin_operations(julianday(created_at),id);
CREATE INDEX operations_type ON admin_operations(action,id);
CREATE INDEX operations_object ON admin_operations(object_type,object_id,id);
-- Existing audit callers can continue using their minimal, non-sensitive details.
CREATE TRIGGER operation_object AFTER INSERT ON admin_operations WHEN new.object_type IS NULL BEGIN
    UPDATE admin_operations SET
        object_type=CASE
            WHEN json_extract(new.details_json,'$.memory_id') IS NOT NULL THEN 'memory'
            WHEN json_extract(new.details_json,'$.batch_id') IS NOT NULL THEN 'batch'
            WHEN json_extract(new.details_json,'$.entry_id') IS NOT NULL THEN 'entry'
            WHEN json_extract(new.details_json,'$.purpose') IS NOT NULL THEN 'model'
            ELSE 'settings' END,
        object_id=CAST(COALESCE(json_extract(new.details_json,'$.memory_id'),
            json_extract(new.details_json,'$.batch_id'),json_extract(new.details_json,'$.entry_id'),
            json_extract(new.details_json,'$.purpose'),'instance') AS TEXT)
    WHERE id=new.id;
END;
UPDATE admin_operations SET
    object_type=CASE WHEN json_extract(details_json,'$.batch_id') IS NOT NULL THEN 'batch'
        WHEN json_extract(details_json,'$.purpose') IS NOT NULL THEN 'model' ELSE 'settings' END,
    object_id=CAST(COALESCE(json_extract(details_json,'$.batch_id'),json_extract(details_json,'$.purpose'),'instance') AS TEXT);
CREATE TRIGGER admin_memory_revision_audit AFTER INSERT ON memory_revisions WHEN new.actor='admin' BEGIN
    INSERT INTO admin_operations(actor,action,object_type,object_id,details_json,created_at)
    VALUES('admin','memory_revision','memory',CAST(new.memory_id AS TEXT),
        json_object('memory_id',new.memory_id,'revision_id',new.id,
            'revision_before',new.revision_before,'revision_after',new.revision_after),new.created_at);
END;
INSERT INTO admin_operations(actor,action,object_type,object_id,details_json,created_at)
SELECT 'admin','memory_revision','memory',CAST(memory_id AS TEXT),
    json_object('memory_id',memory_id,'revision_id',id,'revision_before',revision_before,'revision_after',revision_after),created_at
FROM memory_revisions WHERE actor='admin';
-- Captures online, offline, recovery and evaluation results in the result transaction.
CREATE TRIGGER batch_result_audit AFTER UPDATE OF state ON batches
WHEN old.state='running' AND new.state IN ('waiting','succeeded','abandoned','refused') BEGIN
    INSERT INTO admin_operations(actor,action,object_type,object_id,details_json,created_at)
    VALUES('system','batch_result','batch',CAST(new.id AS TEXT),
        json_object('batch_id',new.id,'entry_id',new.entry_id,'state',new.state,
            'attempt_count',new.attempt_count,
            'created_count',CASE WHEN new.state='succeeded' THEN COALESCE(json_array_length(new.result_json,'$.created'),0) ELSE 0 END,
            'updated_count',CASE WHEN new.state='succeeded' THEN COALESCE(json_array_length(new.result_json,'$.updated'),0) ELSE 0 END,
            'confirmed_count',CASE WHEN new.state='succeeded' THEN COALESCE(json_array_length(new.result_json,'$.confirmed'),0) ELSE 0 END),
        COALESCE(new.finished_at,(SELECT MAX(finished_at) FROM batch_attempts WHERE batch_id=new.id),strftime('%Y-%m-%dT%H:%M:%fZ','now')));
END;
