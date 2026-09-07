-- iris: online_safe=false lock_ms=100 min_app=0.14.0 max_app= recovery=backup bootstrap_safe=true
-- Preserve all Schema 20 rows; only split the typed payload from common metadata.
CREATE TEMP TABLE w04_operations AS SELECT * FROM console_operations;
CREATE TEMP TABLE w04_problems AS SELECT * FROM console_operation_problems;
DROP TABLE console_operation_problems;
DROP TABLE console_operations;
CREATE TABLE console_operations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    key_id TEXT NOT NULL REFERENCES console_operator_keys(id),
    key_revision INTEGER NOT NULL CHECK(key_revision>=1),
    grant_fingerprint TEXT NOT NULL CHECK(length(grant_fingerprint)=64),
    session_id TEXT NOT NULL,
    session_epoch INTEGER NOT NULL CHECK(session_epoch>=1),
    kind TEXT NOT NULL CHECK(kind IN ('memory_forget','trusted_backup')),
    reason_code TEXT NOT NULL CHECK(reason_code='operator_request'),
    status TEXT NOT NULL CHECK(status IN (
        'queued','running','paused','blocked','completed','completed_with_warnings',
        'failed','cancelled','cancelled_partial'
    )),
    revision INTEGER NOT NULL CHECK(revision>=1),
    processed INTEGER NOT NULL CHECK(processed>=0),
    total INTEGER NOT NULL CHECK(total>=1 AND processed<=total AND (kind!='memory_forget' OR total<=500)),
    current_job_id TEXT,
    blocked_reason TEXT CHECK(blocked_reason IS NULL OR length(blocked_reason)<=64),
    created_us INTEGER NOT NULL CHECK(created_us>=0),
    updated_us INTEGER NOT NULL CHECK(updated_us>=created_us),
    started_us INTEGER,
    finished_us INTEGER,
    problems_count INTEGER NOT NULL DEFAULT 0 CHECK(problems_count>=0 AND (kind!='memory_forget' OR problems_count<=500)),
    forget_id TEXT REFERENCES console_operation_forget(operation_id) DEFERRABLE INITIALLY DEFERRED,
    backup_id TEXT REFERENCES console_operation_backups(operation_id) DEFERRABLE INITIALLY DEFERRED,
    CHECK((kind='memory_forget' AND forget_id IS NOT NULL AND forget_id=id AND backup_id IS NULL)
       OR (kind='trusted_backup' AND backup_id IS NOT NULL AND backup_id=id AND forget_id IS NULL)),
    UNIQUE(id,tenant_id,key_id,kind),
    CHECK(status!='blocked' OR blocked_reason IS NOT NULL),
    CHECK(status!='completed' OR processed=total),
    CHECK(status!='cancelled' OR processed=0),
    CHECK(status!='cancelled_partial' OR (processed>0 AND processed<total))
) STRICT;
CREATE INDEX idx_console_operations_owner ON console_operations
    (tenant_id,key_id,grant_fingerprint,key_revision,created_us DESC,id DESC);
CREATE INDEX idx_console_operations_status ON console_operations
    (tenant_id,key_id,grant_fingerprint,key_revision,status,created_us DESC,id DESC);


CREATE TABLE console_operation_forget (
    operation_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'memory_forget' CHECK(kind='memory_forget'),
    preview_id TEXT NOT NULL,
    preview_hash TEXT NOT NULL CHECK(length(preview_hash)=64),
    mode TEXT NOT NULL CHECK(mode IN ('soft','erase')),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND length(CAST(payload_json AS BLOB))<=262144),
    expected_deletion_seq INTEGER NOT NULL CHECK(expected_deletion_seq>=0),
    holds_version TEXT NOT NULL CHECK(length(holds_version)=64),
    UNIQUE(tenant_id,key_id,preview_id),
    FOREIGN KEY(operation_id,tenant_id,key_id,kind) REFERENCES console_operations(id,tenant_id,key_id,kind)
) STRICT;
CREATE TABLE console_operation_backups (
    operation_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'trusted_backup' CHECK(kind='trusted_backup'),
    result_ref TEXT UNIQUE CHECK(result_ref IS NULL OR length(result_ref)=36),
    manifest_hash TEXT CHECK(manifest_hash IS NULL OR length(manifest_hash)=64),
    verified_us INTEGER CHECK(verified_us IS NULL OR verified_us>=0),
    CHECK((result_ref IS NULL AND manifest_hash IS NULL AND verified_us IS NULL)
       OR (result_ref IS NOT NULL AND manifest_hash IS NOT NULL AND verified_us IS NOT NULL)),
    FOREIGN KEY(operation_id,tenant_id,key_id,kind) REFERENCES console_operations(id,tenant_id,key_id,kind)
) STRICT;
CREATE TABLE console_operation_problems (
    operation_id TEXT NOT NULL REFERENCES console_operations(id),
    input_index INTEGER NOT NULL CHECK(input_index>=-1),
    code TEXT NOT NULL CHECK(length(code)>0 AND length(code)<=64),
    created_us INTEGER NOT NULL CHECK(created_us>=0),
    PRIMARY KEY(operation_id,input_index)
) STRICT;

CREATE TRIGGER console_forget_problem_bound BEFORE INSERT ON console_operation_problems
WHEN NEW.input_index>=500 AND EXISTS (
    SELECT 1 FROM console_operations WHERE id=NEW.operation_id AND kind='memory_forget'
) BEGIN SELECT RAISE(ABORT,'Forget problem index exceeds bound'); END;
CREATE TRIGGER console_forget_problem_update_bound BEFORE UPDATE ON console_operation_problems
WHEN NEW.input_index>=500 AND EXISTS (
    SELECT 1 FROM console_operations WHERE id=NEW.operation_id AND kind='memory_forget'
) BEGIN SELECT RAISE(ABORT,'Forget problem index exceeds bound'); END;
CREATE TRIGGER console_backup_completion BEFORE UPDATE OF status ON console_operations
WHEN NEW.kind='trusted_backup' AND NEW.status IN ('completed','completed_with_warnings')
AND NOT EXISTS (SELECT 1 FROM console_operation_backups WHERE operation_id=NEW.id
    AND result_ref IS NOT NULL AND manifest_hash IS NOT NULL AND verified_us IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'Backup completion requires verified result'); END;
INSERT INTO console_operations (id,tenant_id,key_id,key_revision,grant_fingerprint,session_id,session_epoch,kind,reason_code,status,revision,processed,total,current_job_id,blocked_reason,created_us,updated_us,started_us,finished_us,problems_count,forget_id) SELECT id,tenant_id,key_id,key_revision,grant_fingerprint,session_id,session_epoch,kind,reason_code,status,revision,processed,total,current_job_id,blocked_reason,created_us,updated_us,started_us,finished_us,problems_count,id FROM w04_operations;
INSERT INTO console_operation_forget
    (operation_id,tenant_id,key_id,preview_id,preview_hash,mode,payload_json,expected_deletion_seq,holds_version)
    SELECT id,tenant_id,key_id,preview_id,preview_hash,mode,payload_json,expected_deletion_seq,holds_version FROM w04_operations;
INSERT INTO console_operation_problems SELECT * FROM w04_problems;
DROP TABLE w04_operations;
DROP TABLE w04_problems;
