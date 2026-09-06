-- iris: online_safe=true lock_ms=200 min_app=0.12.0 max_app= recovery=none
-- Tenant-local recall identity; preserve requests and usage under composite FK.
CREATE TABLE recall_requests_v14 (
    id TEXT NOT NULL,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    persona_revision INTEGER NOT NULL CHECK (persona_revision >= 0),
    source_watermark INTEGER NOT NULL CHECK (source_watermark >= 0),
    tombstone_watermark INTEGER NOT NULL CHECK (tombstone_watermark >= 0),
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    ranker_version INTEGER NOT NULL CHECK (ranker_version >= 1),
    token_estimator_version INTEGER NOT NULL CHECK (token_estimator_version >= 1),
    retrieved_count INTEGER NOT NULL CHECK (retrieved_count >= 0),
    returned_candidate_ids TEXT NOT NULL DEFAULT '[]',
    returned_count INTEGER NOT NULL CHECK (returned_count >= 0),
    request_fingerprint TEXT NOT NULL CHECK (request_fingerprint != ''),
    resource_ids_json TEXT NOT NULL DEFAULT '[]',
    response_json TEXT CHECK (response_json IS NULL OR response_json != ''),
    created_us INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, id)
) STRICT;
CREATE TABLE recall_usage_reports_v14 (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    request_id TEXT NOT NULL,
    agent_id TEXT NOT NULL REFERENCES agents (id),
    app_instance_id TEXT NOT NULL,
    host_cycle_id TEXT NOT NULL,
    persona_revision INTEGER NOT NULL CHECK (persona_revision >= 0),
    host_selected_ids TEXT NOT NULL DEFAULT '[]',
    model_visible_ids TEXT NOT NULL DEFAULT '[]',
    reported_at_us INTEGER NOT NULL,
    created_us INTEGER NOT NULL,
    FOREIGN KEY (tenant_id, request_id) REFERENCES recall_requests_v14 (tenant_id, id),
    UNIQUE (tenant_id, request_id, host_cycle_id)
) STRICT;
INSERT INTO recall_requests_v14 SELECT * FROM recall_requests;
INSERT INTO recall_usage_reports_v14 SELECT * FROM recall_usage_reports;
DROP TABLE recall_usage_reports;
DROP TABLE recall_requests;
ALTER TABLE recall_requests_v14 RENAME TO recall_requests;
ALTER TABLE recall_usage_reports_v14 RENAME TO recall_usage_reports;
CREATE INDEX idx_recall_requests_tenant ON recall_requests (tenant_id, created_us);
CREATE INDEX idx_recall_usage_reports_request ON recall_usage_reports (tenant_id, request_id);
