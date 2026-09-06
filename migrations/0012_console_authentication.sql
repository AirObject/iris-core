-- iris: online_safe=true lock_ms=200 min_app=0.12.0 max_app= recovery=none
-- Phase 13 slice 2 only: operator credentials, durable authentication and
-- nullable host credential metadata. Canonical business tables are unchanged.

CREATE TABLE console_operator_keys (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    token_sha256 TEXT NOT NULL UNIQUE CHECK(length(token_sha256)=64),
    token_prefix TEXT NOT NULL,
    label TEXT NOT NULL CHECK(length(label) BETWEEN 1 AND 128),
    description TEXT NOT NULL CHECK(length(description)<=1024),
    template TEXT NOT NULL CHECK(template IN ('owner','maintainer','viewer')),
    grants_json TEXT NOT NULL CHECK(json_valid(grants_json)),
    can_delegate INTEGER NOT NULL CHECK(can_delegate IN (0,1)),
    delegable_subjects_json TEXT NOT NULL CHECK(json_valid(delegable_subjects_json)),
    created_us INTEGER NOT NULL CHECK(created_us>=0),
    expires_us INTEGER NOT NULL CHECK(expires_us>created_us),
    status TEXT NOT NULL CHECK(status IN ('active','pending_confirmation','revoked')),
    revision INTEGER NOT NULL CHECK(revision>=1),
    created_by TEXT NOT NULL,
    rotated_from_id TEXT REFERENCES console_operator_keys(id),
    confirmation_expires_us INTEGER,
    revoked_us INTEGER,
    revoke_reason TEXT,
    CHECK(status!='pending_confirmation' OR (rotated_from_id IS NOT NULL AND confirmation_expires_us IS NOT NULL)),
    CHECK(status!='revoked' OR revoked_us IS NOT NULL)
) STRICT;
CREATE INDEX idx_console_keys_tenant ON console_operator_keys(tenant_id,created_us DESC,id DESC);
CREATE UNIQUE INDEX ux_console_pending_successor ON console_operator_keys(rotated_from_id)
    WHERE status='pending_confirmation';

CREATE TABLE console_sessions (
    id TEXT PRIMARY KEY,
    key_id TEXT NOT NULL REFERENCES console_operator_keys(id),
    token_sha256 TEXT NOT NULL UNIQUE CHECK(length(token_sha256)=64),
    epoch INTEGER NOT NULL CHECK(epoch>=1),
    created_us INTEGER NOT NULL CHECK(created_us>=0),
    expires_us INTEGER NOT NULL CHECK(expires_us>created_us),
    idle_expires_us INTEGER NOT NULL CHECK(idle_expires_us<=expires_us),
    last_active_us INTEGER NOT NULL,
    client_digest TEXT NOT NULL CHECK(length(client_digest)=64),
    reauth_until_us INTEGER,
    revoked_us INTEGER,
    previous_digest TEXT UNIQUE,
    refresh_request_hash TEXT,
    refresh_cipher BLOB,
    alias_expires_us INTEGER
) STRICT;
CREATE INDEX idx_console_sessions_key ON console_sessions(key_id,created_us DESC,id DESC);

CREATE TABLE console_auth_attempts (
    bucket_key TEXT PRIMARY KEY,
    window_start_us INTEGER NOT NULL CHECK(window_start_us>=0),
    attempts INTEGER NOT NULL CHECK(attempts>=0),
    blocked_until_us INTEGER NOT NULL CHECK(blocked_until_us>=0),
    last_attempt_us INTEGER NOT NULL CHECK(last_attempt_us>=0)
) STRICT;
CREATE INDEX idx_console_auth_attempts_expiry ON console_auth_attempts(last_attempt_us);

ALTER TABLE service_credentials ADD COLUMN label TEXT;
ALTER TABLE service_credentials ADD COLUMN description TEXT;
ALTER TABLE service_credentials ADD COLUMN token_prefix TEXT;
ALTER TABLE service_credentials ADD COLUMN created_by TEXT;
ALTER TABLE service_credentials ADD COLUMN revoke_reason TEXT;
ALTER TABLE service_credentials ADD COLUMN revoke_after_us INTEGER;
ALTER TABLE service_credentials ADD COLUMN console_revision INTEGER;
