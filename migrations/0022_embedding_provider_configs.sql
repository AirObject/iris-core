-- iris: online_safe=false lock_ms=100 min_app=0.15.0 max_app= recovery=backup bootstrap_safe=true
-- Extend the typed-operation finite registry; preserve every Schema21 record.
CREATE TEMP TABLE w05_operations AS SELECT * FROM console_operations;
CREATE TEMP TABLE w05_forget AS SELECT * FROM console_operation_forget;
CREATE TEMP TABLE w05_backups AS SELECT * FROM console_operation_backups;
CREATE TEMP TABLE w05_problems AS SELECT * FROM console_operation_problems;
DROP TABLE console_operation_problems;
DROP TABLE console_operation_forget;
DROP TABLE console_operation_backups;
DROP TABLE console_operations;
CREATE TABLE console_operations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    key_id TEXT NOT NULL REFERENCES console_operator_keys(id),
    key_revision INTEGER NOT NULL CHECK(key_revision>=1),
    grant_fingerprint TEXT NOT NULL CHECK(length(grant_fingerprint)=64),
    session_id TEXT NOT NULL,
    session_epoch INTEGER NOT NULL CHECK(session_epoch>=1),
    kind TEXT NOT NULL CHECK(kind IN ('memory_forget','trusted_backup','embedding_provider')),
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
    CHECK((kind='memory_forget' AND forget_id IS NOT NULL AND forget_id=id AND backup_id IS NULL AND provider_id IS NULL)
       OR (kind='trusted_backup' AND backup_id IS NOT NULL AND backup_id=id AND forget_id IS NULL AND provider_id IS NULL)
       OR (kind='embedding_provider' AND provider_id IS NOT NULL AND provider_id=id AND forget_id IS NULL AND backup_id IS NULL)),
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

CREATE TABLE provider_configs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    provider_kind TEXT NOT NULL DEFAULT 'embedding' CHECK(provider_kind='embedding'),
    status TEXT NOT NULL CHECK(status IN ('draft','probing','probed','activating','active','retired','discarded')),
    revision INTEGER NOT NULL CHECK(revision>=1),
    content_revision INTEGER NOT NULL CHECK(content_revision>=1),
    created_us INTEGER NOT NULL CHECK(created_us>=0),
    updated_us INTEGER NOT NULL CHECK(updated_us>=created_us),
    created_by TEXT NOT NULL REFERENCES console_operator_keys(id),
    current_operation_id TEXT REFERENCES console_operations(id),
    latest_probe_id TEXT REFERENCES provider_probes(id) DEFERRABLE INITIALLY DEFERRED,
    last_generation_id TEXT,
    UNIQUE(tenant_id,id),
    FOREIGN KEY(tenant_id,id,content_revision) REFERENCES provider_config_revisions(tenant_id,config_id,content_revision) DEFERRABLE INITIALLY DEFERRED
) STRICT;
CREATE UNIQUE INDEX idx_provider_one_active ON provider_configs(tenant_id,provider_kind) WHERE status='active';
CREATE UNIQUE INDEX idx_provider_one_activating ON provider_configs(tenant_id,provider_kind) WHERE status='activating';
CREATE INDEX idx_provider_configs_page ON provider_configs(tenant_id,created_us DESC,id DESC);

CREATE TABLE provider_config_revisions (
    tenant_id TEXT NOT NULL,
    config_id TEXT NOT NULL,
    content_revision INTEGER NOT NULL CHECK(content_revision>=1),
    definition_json TEXT NOT NULL CHECK(json_valid(definition_json) AND length(CAST(definition_json AS BLOB))<=16384),
    content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
    secret_mode TEXT NOT NULL CHECK(secret_mode IN ('none','secret_ref','sealed')),
    secret_reference TEXT CHECK(secret_reference IS NULL OR length(secret_reference)<=2048),
    secret_digest_prefix TEXT NOT NULL CHECK(length(secret_digest_prefix) IN (0,8)),
    secret_hint TEXT NOT NULL CHECK(length(secret_hint)<=4),
    created_us INTEGER NOT NULL CHECK(created_us>=0),
    created_by TEXT NOT NULL REFERENCES console_operator_keys(id),
    PRIMARY KEY(tenant_id,config_id,content_revision),
    UNIQUE(tenant_id,config_id,content_revision,secret_mode),
    FOREIGN KEY(tenant_id,config_id) REFERENCES provider_configs(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
    CHECK((secret_mode='secret_ref' AND secret_reference IS NOT NULL)
       OR (secret_mode='sealed' AND secret_reference IS NULL AND length(secret_digest_prefix)=8 AND length(secret_hint)=4)
       OR (secret_mode='none' AND secret_reference IS NULL AND secret_digest_prefix='' AND secret_hint=''))
) STRICT;
CREATE TRIGGER provider_revision_immutable BEFORE UPDATE ON provider_config_revisions
BEGIN SELECT RAISE(ABORT,'provider configuration content is immutable'); END;
CREATE TRIGGER provider_revision_preserve BEFORE DELETE ON provider_config_revisions
BEGIN SELECT RAISE(ABORT,'provider configuration history is retained'); END;

-- Only this envelope is mutable during offline master-key rotation; identity and content stay immutable.
CREATE TABLE provider_sealed_secrets (
    tenant_id TEXT NOT NULL,
    config_id TEXT NOT NULL,
    content_revision INTEGER NOT NULL,
    secret_mode TEXT NOT NULL DEFAULT 'sealed' CHECK(secret_mode='sealed'),
    ciphertext TEXT NOT NULL CHECK(length(ciphertext)>=40 AND length(ciphertext)<=6000),
    PRIMARY KEY(tenant_id,config_id,content_revision),
    FOREIGN KEY(tenant_id,config_id,content_revision,secret_mode) REFERENCES provider_config_revisions(tenant_id,config_id,content_revision,secret_mode)
) STRICT;
CREATE TRIGGER provider_sealed_identity_immutable BEFORE UPDATE ON provider_sealed_secrets
WHEN NEW.tenant_id!=OLD.tenant_id OR NEW.config_id!=OLD.config_id OR NEW.content_revision!=OLD.content_revision OR NEW.secret_mode!=OLD.secret_mode
BEGIN SELECT RAISE(ABORT,'provider envelope identity is immutable'); END;

CREATE TABLE provider_probes (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    config_id TEXT NOT NULL,
    content_revision INTEGER NOT NULL,
    operation_id TEXT NOT NULL UNIQUE,
    ok INTEGER NOT NULL CHECK(ok IN (0,1)),
    dimension_observed INTEGER CHECK(dimension_observed IS NULL OR dimension_observed BETWEEN 0 AND 33554432),
    normalized INTEGER CHECK(normalized IS NULL OR normalized IN (0,1)),
    latency_ms REAL NOT NULL CHECK(latency_ms>=0 AND latency_ms<=120000),
    outcome TEXT NOT NULL CHECK(outcome IN ('ok','timeout','rate_limited','circuit_open','invalid_output','transport_error')),
    secret_fingerprint TEXT NOT NULL CHECK(length(secret_fingerprint) IN (0,64)),
    created_us INTEGER NOT NULL CHECK(created_us>=0),
    FOREIGN KEY(tenant_id,config_id,content_revision) REFERENCES provider_config_revisions(tenant_id,config_id,content_revision),
    FOREIGN KEY(operation_id,tenant_id) REFERENCES console_operations(id,tenant_id),
    CHECK((ok=1 AND outcome='ok' AND dimension_observed IS NOT NULL AND normalized=1) OR (ok=0 AND outcome!='ok'))
) STRICT;
CREATE INDEX idx_provider_probes_history ON provider_probes(tenant_id,config_id,created_us DESC,id DESC);
CREATE TRIGGER provider_probe_immutable BEFORE UPDATE ON provider_probes
BEGIN SELECT RAISE(ABORT,'provider probe result is immutable'); END;

CREATE TABLE provider_serving (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants(id),
    config_id TEXT NOT NULL,
    content_revision INTEGER NOT NULL,
    generation_id TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK(epoch>=1),
    updated_us INTEGER NOT NULL CHECK(updated_us>=0),
    FOREIGN KEY(tenant_id,config_id,content_revision) REFERENCES provider_config_revisions(tenant_id,config_id,content_revision),
    FOREIGN KEY(tenant_id,generation_id) REFERENCES vector_generations(tenant_id,id)
) STRICT;
CREATE TABLE provider_generation_bindings (
    tenant_id TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    config_id TEXT NOT NULL,
    content_revision INTEGER NOT NULL,
    created_us INTEGER NOT NULL CHECK(created_us>=0),
    PRIMARY KEY(tenant_id,generation_id),
    FOREIGN KEY(tenant_id,config_id,content_revision) REFERENCES provider_config_revisions(tenant_id,config_id,content_revision),
    FOREIGN KEY(tenant_id,generation_id) REFERENCES vector_generations(tenant_id,id) ON DELETE CASCADE
) STRICT;
CREATE TRIGGER provider_generation_binding_immutable BEFORE UPDATE ON provider_generation_bindings
BEGIN SELECT RAISE(ABORT,'generation configuration binding is immutable'); END;

CREATE TABLE provider_probe_budgets (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants(id),
    window_start_us INTEGER NOT NULL CHECK(window_start_us>=0),
    attempts INTEGER NOT NULL CHECK(attempts>=0),
    input_chars INTEGER NOT NULL CHECK(input_chars>=0)
) STRICT;
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

INSERT INTO console_operations (id,tenant_id,key_id,key_revision,grant_fingerprint,session_id,session_epoch,kind,reason_code,status,revision,processed,total,current_job_id,blocked_reason,created_us,updated_us,started_us,finished_us,problems_count,forget_id,backup_id) SELECT id,tenant_id,key_id,key_revision,grant_fingerprint,session_id,session_epoch,kind,reason_code,status,revision,processed,total,current_job_id,blocked_reason,created_us,updated_us,started_us,finished_us,problems_count,forget_id,backup_id FROM w05_operations;
INSERT INTO console_operation_forget SELECT * FROM w05_forget;
INSERT INTO console_operation_backups SELECT * FROM w05_backups;
INSERT INTO console_operation_problems SELECT * FROM w05_problems;
DROP TABLE w05_operations;
DROP TABLE w05_forget;
DROP TABLE w05_backups;
DROP TABLE w05_problems;
