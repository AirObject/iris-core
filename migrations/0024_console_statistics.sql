-- iris: online_safe=false lock_ms=100 min_app=0.15.0 max_app= recovery=backup bootstrap_safe=true
-- W07 independent snapshot; final integration orders this after W08 as 0024.
ALTER TABLE recall_requests ADD COLUMN duration_us INTEGER CHECK(duration_us IS NULL OR duration_us>=0);
ALTER TABLE recall_requests ADD COLUMN statistics_json TEXT CHECK(statistics_json IS NULL OR (json_valid(statistics_json) AND length(CAST(statistics_json AS BLOB))<=262144));
ALTER TABLE outbox_jobs ADD COLUMN last_heartbeat_us INTEGER CHECK(last_heartbeat_us IS NULL OR last_heartbeat_us>=0);
CREATE INDEX idx_recall_statistics_time ON recall_requests(tenant_id,created_us,id);
CREATE TABLE console_stat_coverage (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    coverage_from_us INTEGER NOT NULL CHECK(coverage_from_us>=0)
) STRICT;
INSERT INTO console_stat_coverage VALUES(1,CAST(unixepoch('subsec')*1000000 AS INTEGER));
CREATE TABLE console_stat_builds (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    from_us INTEGER NOT NULL CHECK(from_us>=0),
    to_us INTEGER NOT NULL CHECK(to_us>from_us),
    started_us INTEGER NOT NULL CHECK(started_us>=0),
    completed_us INTEGER,
    stage INTEGER NOT NULL DEFAULT 0 CHECK(stage>=0),
    cursor_created_us INTEGER,
    cursor_id TEXT,
    state TEXT NOT NULL DEFAULT 'building' CHECK(state IN ('building','complete','cancelled')),
    UNIQUE(id,tenant_id),
    CHECK((state='complete' AND completed_us IS NOT NULL) OR (state!='complete' AND completed_us IS NULL)),
    CHECK((cursor_created_us IS NULL)=(cursor_id IS NULL))
) STRICT;
CREATE INDEX idx_console_stat_builds_tenant ON console_stat_builds(tenant_id,state,completed_us DESC,id);
CREATE TABLE console_stat_rollups (
    build_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    bucket_start_us INTEGER NOT NULL CHECK(bucket_start_us>=0),
    granularity TEXT NOT NULL CHECK(granularity IN ('hour','day','week','snapshot')),
    metric TEXT NOT NULL,
    atom_id TEXT NOT NULL,
    agent_id TEXT,
    space_group_id TEXT,
    space_id TEXT,
    session_id TEXT,
    labels_json TEXT NOT NULL CHECK(json_valid(labels_json) AND length(CAST(labels_json AS BLOB))<=262144),
    value_json TEXT NOT NULL CHECK(json_valid(value_json) AND length(CAST(value_json AS BLOB))<=8192),
    PRIMARY KEY(build_id,granularity,metric,bucket_start_us,atom_id),
    FOREIGN KEY(build_id,tenant_id) REFERENCES console_stat_builds(id,tenant_id) ON DELETE CASCADE,
    CHECK(session_id IS NULL OR space_id IS NOT NULL)
) STRICT;
CREATE INDEX idx_console_stat_rollup_scope ON console_stat_rollups(tenant_id,build_id,granularity,metric,bucket_start_us,agent_id,space_id,atom_id);
CREATE TABLE console_stat_leases (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants(id),
    build_id TEXT NOT NULL REFERENCES console_stat_builds(id),
    operation_id TEXT,
    epoch INTEGER NOT NULL CHECK(epoch>=1),
    expires_us INTEGER NOT NULL
) STRICT;

-- Extend the finite typed Operation registry while preserving every prior payload.
PRAGMA defer_foreign_keys=ON;
CREATE TEMP TABLE w07_console_operations AS SELECT * FROM console_operations;
CREATE TEMP TABLE w07_console_operation_forget AS SELECT * FROM console_operation_forget;
CREATE TEMP TABLE w07_console_operation_backups AS SELECT * FROM console_operation_backups;
CREATE TEMP TABLE w07_console_operation_providers AS SELECT * FROM console_operation_providers;
CREATE TEMP TABLE w07_console_operation_problems AS SELECT * FROM console_operation_problems;
DROP TABLE console_operation_problems;
DROP TABLE console_operation_providers;
DROP TABLE console_operation_backups;
DROP TABLE console_operation_forget;
DROP TABLE console_operations;
CREATE TABLE console_operations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    key_id TEXT NOT NULL REFERENCES console_operator_keys(id),
    key_revision INTEGER NOT NULL CHECK(key_revision>=1),
    grant_fingerprint TEXT NOT NULL CHECK(length(grant_fingerprint)=64),
    session_id TEXT NOT NULL,
    session_epoch INTEGER NOT NULL CHECK(session_epoch>=1),
    kind TEXT NOT NULL CHECK(kind IN ('memory_forget','trusted_backup','embedding_provider','statistics_backfill')),
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
    provider_id TEXT REFERENCES console_operation_providers(operation_id) DEFERRABLE INITIALLY DEFERRED,
    statistics_id TEXT REFERENCES console_operation_statistics(operation_id) DEFERRABLE INITIALLY DEFERRED,
    CHECK((kind='memory_forget' AND forget_id IS NOT NULL AND forget_id=id AND backup_id IS NULL AND provider_id IS NULL AND statistics_id IS NULL)
       OR (kind='trusted_backup' AND backup_id IS NOT NULL AND backup_id=id AND forget_id IS NULL AND provider_id IS NULL AND statistics_id IS NULL)
       OR (kind='embedding_provider' AND provider_id IS NOT NULL AND provider_id=id AND forget_id IS NULL AND backup_id IS NULL AND statistics_id IS NULL)
       OR (kind='statistics_backfill' AND statistics_id IS NOT NULL AND statistics_id=id AND forget_id IS NULL AND backup_id IS NULL AND provider_id IS NULL)),
    UNIQUE(id,tenant_id,key_id,kind),
    UNIQUE(id,tenant_id),
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


CREATE TABLE console_operation_providers (
    operation_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'embedding_provider' CHECK(kind='embedding_provider'),
    config_id TEXT NOT NULL,
    content_revision INTEGER NOT NULL CHECK(content_revision>=1),
    action TEXT NOT NULL CHECK(action IN ('probe','activate','rollback')),
    plan_json TEXT CHECK(plan_json IS NULL OR (json_valid(plan_json) AND length(CAST(plan_json AS BLOB))<=4096)),
    expected_serving_epoch INTEGER NOT NULL CHECK(expected_serving_epoch>=0),
    plan_hash TEXT CHECK(plan_hash IS NULL OR length(plan_hash)=64),
    generation_id TEXT,
    FOREIGN KEY(operation_id,tenant_id,key_id,kind) REFERENCES console_operations(id,tenant_id,key_id,kind),
    FOREIGN KEY(tenant_id,config_id,content_revision) REFERENCES provider_config_revisions(tenant_id,config_id,content_revision)
) STRICT;

CREATE TRIGGER console_provider_intent_immutable
BEFORE UPDATE ON console_operation_providers
WHEN NEW.operation_id IS NOT OLD.operation_id OR NEW.tenant_id IS NOT OLD.tenant_id
 OR NEW.key_id IS NOT OLD.key_id OR NEW.kind IS NOT OLD.kind
 OR NEW.config_id IS NOT OLD.config_id OR NEW.content_revision IS NOT OLD.content_revision
 OR NEW.action IS NOT OLD.action OR NEW.expected_serving_epoch IS NOT OLD.expected_serving_epoch
 OR NEW.plan_hash IS NOT OLD.plan_hash OR NEW.plan_json IS NOT OLD.plan_json
BEGIN SELECT RAISE(ABORT, 'provider operation intent is immutable'); END;


CREATE TABLE console_operation_statistics (
    operation_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'statistics_backfill' CHECK(kind='statistics_backfill'),
    build_id TEXT NOT NULL REFERENCES console_stat_builds(id),
    from_us INTEGER NOT NULL CHECK(from_us>=0),
    to_us INTEGER NOT NULL CHECK(to_us>from_us AND to_us-from_us<=2678400000000),
    FOREIGN KEY(operation_id,tenant_id,key_id,kind) REFERENCES console_operations(id,tenant_id,key_id,kind)
) STRICT;
CREATE TRIGGER console_statistics_intent_immutable BEFORE UPDATE ON console_operation_statistics
BEGIN SELECT RAISE(ABORT,'statistics intent is immutable'); END;
CREATE TRIGGER console_statistics_completion BEFORE UPDATE OF status ON console_operations
WHEN NEW.kind='statistics_backfill' AND NEW.status IN ('completed','completed_with_warnings')
AND NOT EXISTS(SELECT 1 FROM console_operation_statistics s JOIN console_stat_builds b
ON b.id=s.build_id AND b.tenant_id=s.tenant_id WHERE s.operation_id=NEW.id AND b.state='complete')
BEGIN SELECT RAISE(ABORT,'statistics completion requires published rollup'); END;

INSERT INTO console_operations(id,tenant_id,key_id,key_revision,grant_fingerprint,session_id,session_epoch,kind,reason_code,status,revision,processed,total,current_job_id,blocked_reason,created_us,updated_us,started_us,finished_us,problems_count,forget_id,backup_id,provider_id) SELECT id,tenant_id,key_id,key_revision,grant_fingerprint,session_id,session_epoch,kind,reason_code,status,revision,processed,total,current_job_id,blocked_reason,created_us,updated_us,started_us,finished_us,problems_count,forget_id,backup_id,provider_id FROM w07_console_operations;
INSERT INTO console_operation_forget SELECT * FROM w07_console_operation_forget;
INSERT INTO console_operation_backups SELECT * FROM w07_console_operation_backups;
INSERT INTO console_operation_providers SELECT * FROM w07_console_operation_providers;
INSERT INTO console_operation_problems SELECT * FROM w07_console_operation_problems;
DROP TABLE w07_console_operations;
DROP TABLE w07_console_operation_forget;
DROP TABLE w07_console_operation_backups;
DROP TABLE w07_console_operation_providers;
DROP TABLE w07_console_operation_problems;
