-- iris: online_safe=true lock_ms=100 min_app=0.13.0 max_app= recovery=none
-- Operations own fixed metadata; their work stays in the existing Outbox.
CREATE TABLE console_operations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    key_id TEXT NOT NULL REFERENCES console_operator_keys(id),
    key_revision INTEGER NOT NULL CHECK(key_revision>=1),
    grant_fingerprint TEXT NOT NULL CHECK(length(grant_fingerprint)=64),
    session_id TEXT NOT NULL,
    session_epoch INTEGER NOT NULL CHECK(session_epoch>=1),
    preview_id TEXT NOT NULL,
    preview_hash TEXT NOT NULL CHECK(length(preview_hash)=64),
    kind TEXT NOT NULL CHECK(kind='memory_forget'),
    mode TEXT NOT NULL CHECK(mode IN ('soft','erase')),
    reason_code TEXT NOT NULL CHECK(reason_code='operator_request'),
    status TEXT NOT NULL CHECK(status IN (
        'queued','running','paused','blocked','completed','completed_with_warnings',
        'failed','cancelled','cancelled_partial'
    )),
    revision INTEGER NOT NULL CHECK(revision>=1),
    processed INTEGER NOT NULL CHECK(processed>=0),
    total INTEGER NOT NULL CHECK(total>=1 AND total<=500 AND processed<=total),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND length(CAST(payload_json AS BLOB))<=262144),
    expected_deletion_seq INTEGER NOT NULL CHECK(expected_deletion_seq>=0),
    holds_version TEXT NOT NULL CHECK(length(holds_version)=64),
    current_job_id TEXT,
    blocked_reason TEXT CHECK(blocked_reason IS NULL OR length(blocked_reason)<=64),
    created_us INTEGER NOT NULL CHECK(created_us>=0),
    updated_us INTEGER NOT NULL CHECK(updated_us>=created_us),
    started_us INTEGER,
    finished_us INTEGER,
    problems_count INTEGER NOT NULL DEFAULT 0 CHECK(problems_count>=0 AND problems_count<=500),
    UNIQUE(tenant_id,key_id,preview_id),
    CHECK(status!='blocked' OR blocked_reason IS NOT NULL),
    CHECK(status!='completed' OR processed=total),
    CHECK(status!='cancelled' OR processed=0),
    CHECK(status!='cancelled_partial' OR (processed>0 AND processed<total))
) STRICT;
CREATE INDEX idx_console_operations_owner ON console_operations
    (tenant_id,key_id,grant_fingerprint,key_revision,created_us DESC,id DESC);
CREATE INDEX idx_console_operations_status ON console_operations
    (tenant_id,key_id,grant_fingerprint,key_revision,status,created_us DESC,id DESC);

CREATE TABLE console_operation_problems (
    operation_id TEXT NOT NULL REFERENCES console_operations(id),
    input_index INTEGER NOT NULL CHECK(input_index>=-1 AND input_index<500),
    code TEXT NOT NULL CHECK(length(code)>0 AND length(code)<=64),
    created_us INTEGER NOT NULL CHECK(created_us>=0),
    PRIMARY KEY(operation_id,input_index)
) STRICT;
