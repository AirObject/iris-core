-- iris: online_safe=true lock_ms=200 min_app=0.13.0 max_app= recovery=none
-- Fixed Console deletion previews contain identifiers and state hashes only.
-- Canonical content and credentials never belong in preview payloads.
CREATE TABLE console_command_previews (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    key_id TEXT NOT NULL REFERENCES console_operator_keys(id),
    key_revision INTEGER NOT NULL CHECK(key_revision>=1),
    grant_fingerprint TEXT NOT NULL CHECK(length(grant_fingerprint)=64),
    kind TEXT NOT NULL CHECK(kind='memory_forget'),
    mode TEXT NOT NULL CHECK(mode IN ('soft','erase')),
    reason_code TEXT NOT NULL CHECK(reason_code='operator_request'),
    preview_hash TEXT NOT NULL CHECK(length(preview_hash)=64),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND length(CAST(payload_json AS BLOB))<=262144),
    created_us INTEGER NOT NULL CHECK(created_us>=0),
    expires_us INTEGER NOT NULL CHECK(expires_us=created_us+600000000),
    status TEXT NOT NULL CHECK(status IN ('ready','consumed')),
    consumed_us INTEGER,
    receipt_json TEXT CHECK(receipt_json IS NULL OR (json_valid(receipt_json) AND length(CAST(receipt_json AS BLOB))<=65536)),
    CHECK((status='ready' AND consumed_us IS NULL AND receipt_json IS NULL)
       OR (status='consumed' AND consumed_us IS NOT NULL AND receipt_json IS NOT NULL))
) STRICT;
CREATE INDEX idx_console_previews_owner ON console_command_previews(tenant_id,key_id,created_us DESC,id);
CREATE INDEX idx_console_previews_expiry ON console_command_previews(status,expires_us,id);
