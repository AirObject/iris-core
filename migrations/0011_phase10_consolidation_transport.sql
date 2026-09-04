-- iris: online_safe=true lock_ms=200 min_app=0.11.0 max_app= recovery=none
-- Phase 10: fixed-watermark consolidation/reflection, provider governance,
-- hashed credentials, durable SSE cursors and usage activation ledger.

CREATE TABLE consolidation_windows (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    space_group_id TEXT,
    space_id TEXT REFERENCES spaces (id),
    session_id TEXT REFERENCES sessions (id),
    topic_key TEXT NOT NULL CHECK (topic_key != '' AND length(topic_key) <= 256),
    window_start_us INTEGER NOT NULL CHECK (window_start_us >= 0),
    window_end_us INTEGER NOT NULL CHECK (window_end_us > window_start_us),
    source_watermark INTEGER NOT NULL CHECK (source_watermark >= 0),
    observation_refs_json TEXT NOT NULL CHECK (json_valid(observation_refs_json)),
    source_fingerprint TEXT NOT NULL CHECK (length(source_fingerprint) = 64),
    builder_version TEXT NOT NULL CHECK (builder_version != ''),
    status TEXT NOT NULL CHECK (status IN ('selected', 'sealed', 'stale', 'failed')),
    episode_id TEXT REFERENCES episodes (id),
    created_us INTEGER NOT NULL CHECK (created_us >= 0),
    sealed_us INTEGER,
    CHECK (session_id IS NULL OR space_id IS NOT NULL),
    CHECK (sealed_us IS NULL OR sealed_us >= created_us)
) STRICT;
CREATE UNIQUE INDEX ux_consolidation_window_identity ON consolidation_windows (
    tenant_id, agent_id, ifnull(space_group_id, ''), ifnull(space_id, ''),
    ifnull(session_id, ''), topic_key, window_start_us, window_end_us,
    source_watermark, builder_version
);
CREATE INDEX idx_consolidation_windows_agent
    ON consolidation_windows (tenant_id, agent_id, source_watermark, status);

CREATE TABLE reflection_records (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    window_id TEXT NOT NULL REFERENCES consolidation_windows (id),
    run_fingerprint TEXT NOT NULL CHECK (length(run_fingerprint) = 64),
    source_watermark INTEGER NOT NULL CHECK (source_watermark >= 0),
    prompt_version TEXT NOT NULL CHECK (prompt_version != ''),
    provider_schema_version TEXT NOT NULL CHECK (provider_schema_version != ''),
    builder_version TEXT NOT NULL CHECK (builder_version != ''),
    policy_version TEXT NOT NULL CHECK (policy_version != ''),
    reconciliation_version TEXT NOT NULL CHECK (reconciliation_version != ''),
    model_id TEXT NOT NULL CHECK (model_id != '' AND length(model_id) <= 256),
    commit_mode TEXT NOT NULL CHECK (commit_mode IN ('dry_run', 'commit')),
    status TEXT NOT NULL CHECK (status IN ('running', 'committed', 'rejected', 'stale', 'failed')),
    candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
    rejected_count INTEGER NOT NULL DEFAULT 0 CHECK (rejected_count >= 0),
    provider_outcome_id TEXT,
    replay_of TEXT REFERENCES reflection_records (id),
    diff_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(diff_json)),
    created_us INTEGER NOT NULL CHECK (created_us >= 0),
    completed_us INTEGER,
    CHECK (completed_us IS NULL OR completed_us >= created_us),
    UNIQUE (tenant_id, run_fingerprint)
) STRICT;
CREATE INDEX idx_reflection_records_window
    ON reflection_records (window_id, created_us, id);

CREATE TABLE reflection_evidence (
    reflection_id TEXT NOT NULL REFERENCES reflection_records (id),
    observation_id TEXT NOT NULL REFERENCES observations (id),
    observation_revision INTEGER NOT NULL CHECK (observation_revision >= 1),
    span_start INTEGER NOT NULL CHECK (span_start >= 0),
    span_end INTEGER NOT NULL CHECK (span_end > span_start),
    source_fingerprint TEXT NOT NULL CHECK (length(source_fingerprint) = 64),
    PRIMARY KEY (reflection_id, observation_id, span_start, span_end)
) STRICT;
CREATE INDEX idx_reflection_evidence_source
    ON reflection_evidence (observation_id, observation_revision);

CREATE TABLE cognitive_candidates (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    reflection_id TEXT NOT NULL REFERENCES reflection_records (id),
    candidate_type TEXT NOT NULL CHECK (
        candidate_type IN ('claim', 'relation', 'note', 'task', 'persona_proposal')
    ),
    fingerprint TEXT NOT NULL CHECK (length(fingerprint) = 64),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND length(payload_json) <= 131072),
    evidence_refs_json TEXT NOT NULL CHECK (json_valid(evidence_refs_json)),
    scope_json TEXT NOT NULL CHECK (json_valid(scope_json)),
    privacy_labels_json TEXT NOT NULL CHECK (json_valid(privacy_labels_json)),
    decision TEXT NOT NULL CHECK (decision IN ('pending', 'accepted', 'rejected', 'duplicate')),
    reject_reason TEXT,
    canonical_resource_type TEXT,
    canonical_resource_id TEXT,
    created_us INTEGER NOT NULL CHECK (created_us >= 0),
    decided_us INTEGER,
    CHECK ((decision = 'rejected') = (reject_reason IS NOT NULL)),
    CHECK (decided_us IS NULL OR decided_us >= created_us),
    UNIQUE (tenant_id, fingerprint)
) STRICT;
CREATE INDEX idx_cognitive_candidates_reflection
    ON cognitive_candidates (reflection_id, decision, candidate_type);

CREATE TABLE provider_outcomes (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT REFERENCES agents (id),
    job_kind TEXT NOT NULL CHECK (job_kind != ''),
    provider_kind TEXT NOT NULL CHECK (
        provider_kind IN ('extraction', 'summarization', 'reconciliation', 'persona_evolution')
    ),
    model_id TEXT NOT NULL CHECK (model_id != '' AND length(model_id) <= 256),
    prompt_version TEXT NOT NULL CHECK (prompt_version != ''),
    provider_schema_version TEXT NOT NULL CHECK (provider_schema_version != ''),
    outcome TEXT NOT NULL CHECK (
        outcome IN ('success', 'timeout', 'rate_limited', 'server_error', 'invalid_output',
                    'circuit_open', 'budget_exhausted', 'cancelled')
    ),
    retryable INTEGER NOT NULL CHECK (retryable IN (0, 1)),
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    response_hash TEXT CHECK (response_hash IS NULL OR length(response_hash) = 64),
    input_units INTEGER NOT NULL DEFAULT 0 CHECK (input_units >= 0),
    output_units INTEGER NOT NULL DEFAULT 0 CHECK (output_units >= 0),
    cost_microunits INTEGER NOT NULL DEFAULT 0 CHECK (cost_microunits >= 0),
    duration_us INTEGER NOT NULL DEFAULT 0 CHECK (duration_us >= 0),
    diagnostic_code TEXT,
    created_us INTEGER NOT NULL CHECK (created_us >= 0)
) STRICT;
CREATE INDEX idx_provider_outcomes_budget
    ON provider_outcomes (tenant_id, provider_kind, created_us, cost_microunits);

CREATE TABLE provider_circuit_states (
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    provider_kind TEXT NOT NULL CHECK (
        provider_kind IN ('extraction', 'summarization', 'reconciliation', 'persona_evolution')
    ),
    state TEXT NOT NULL CHECK (state IN ('closed', 'open', 'half_open')),
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    opened_until_us INTEGER,
    probe_in_flight INTEGER NOT NULL DEFAULT 0 CHECK (probe_in_flight IN (0, 1)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    updated_us INTEGER NOT NULL CHECK (updated_us >= 0),
    PRIMARY KEY (tenant_id, provider_kind)
) STRICT;

CREATE TABLE provider_budget_states (
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    provider_kind TEXT NOT NULL CHECK (
        provider_kind IN ('extraction', 'summarization', 'reconciliation', 'persona_evolution')
    ),
    budget_day INTEGER NOT NULL CHECK (budget_day >= 0),
    charged_microunits INTEGER NOT NULL DEFAULT 0 CHECK (charged_microunits >= 0),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    updated_us INTEGER NOT NULL CHECK (updated_us >= 0),
    PRIMARY KEY (tenant_id, provider_kind, budget_day)
) STRICT;

CREATE TABLE service_credentials (
    id TEXT PRIMARY KEY,
    token_sha256 TEXT NOT NULL UNIQUE CHECK (length(token_sha256) = 64),
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    app_instance_id TEXT NOT NULL CHECK (app_instance_id != ''),
    plane TEXT NOT NULL CHECK (plane IN ('application', 'management')),
    agent_ids_json TEXT NOT NULL CHECK (json_valid(agent_ids_json)),
    space_group_ids_json TEXT NOT NULL CHECK (json_valid(space_group_ids_json)),
    space_ids_json TEXT NOT NULL CHECK (json_valid(space_ids_json)),
    entity_ids_json TEXT NOT NULL CHECK (json_valid(entity_ids_json)),
    capabilities_json TEXT NOT NULL CHECK (json_valid(capabilities_json)),
    data_purposes_json TEXT NOT NULL CHECK (json_valid(data_purposes_json)),
    created_us INTEGER NOT NULL CHECK (created_us >= 0),
    expires_us INTEGER NOT NULL CHECK (expires_us > created_us),
    revoked_us INTEGER,
    rotated_from_id TEXT REFERENCES service_credentials (id),
    last_used_us INTEGER,
    CHECK (revoked_us IS NULL OR revoked_us >= created_us)
) STRICT;
CREATE INDEX idx_service_credentials_tenant
    ON service_credentials (tenant_id, plane, expires_us);

CREATE TABLE service_events (
    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT REFERENCES agents (id),
    space_group_id TEXT,
    space_id TEXT,
    event_type TEXT NOT NULL CHECK (
        event_type IN ('persona.revised.v1', 'surface.lease_revoked.v1',
                       'revision.invalidated.v1', 'cognitive_event.ready.v1')
    ),
    resource_refs_json TEXT NOT NULL CHECK (json_valid(resource_refs_json)),
    source_watermark INTEGER NOT NULL DEFAULT 0 CHECK (source_watermark >= 0),
    occurred_us INTEGER NOT NULL CHECK (occurred_us >= 0),
    created_us INTEGER NOT NULL CHECK (created_us >= 0)
) STRICT;
CREATE INDEX idx_service_events_scope
    ON service_events (tenant_id, agent_id, cursor);

CREATE TABLE recall_usage_activations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    request_id TEXT NOT NULL,
    host_cycle_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('host_selected', 'model_visible')),
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    resource_revision INTEGER NOT NULL CHECK (resource_revision >= 1),
    activation_delta REAL NOT NULL CHECK (activation_delta > 0 AND activation_delta <= 0.25),
    applied INTEGER NOT NULL CHECK (applied IN (0, 1)),
    reject_reason TEXT,
    created_us INTEGER NOT NULL CHECK (created_us >= 0),
    UNIQUE (tenant_id, request_id, host_cycle_id, candidate_id, stage)
) STRICT;

-- provider_outcomes is declared first to avoid a circular insertion order;
-- Reflection references are application-validated and indexed rather than
-- FK constrained so a failed Provider attempt may be recorded before a run.
CREATE INDEX idx_reflection_provider_outcome ON reflection_records (provider_outcome_id);
