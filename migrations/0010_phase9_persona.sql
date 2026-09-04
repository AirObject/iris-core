-- iris: online_safe=true lock_ms=200 min_app=0.10.0 max_app= recovery=none
-- Phase 9: complete Persona metadata, policy, state and proposal system (§14).
-- The Phase 1 persona_revisions rows and agents.persona_current_revision_id
-- remain untouched: bootstrap ids, content and hashes are part of the public
-- compatibility contract (ADR-0008).  New tables expand around that seam.

CREATE TABLE persona_policies (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    mode TEXT NOT NULL CHECK (mode IN ('locked', 'manual', 'bounded_auto')),
    allowed_fields_json TEXT NOT NULL CHECK (json_valid(allowed_fields_json)),
    max_single_delta REAL NOT NULL CHECK (max_single_delta >= 0.0 AND max_single_delta <= 1.0),
    max_cumulative_delta REAL NOT NULL
        CHECK (max_cumulative_delta >= 0.0 AND max_cumulative_delta <= 1.0),
    cumulative_window_us INTEGER NOT NULL CHECK (cumulative_window_us >= 0),
    min_evidence INTEGER NOT NULL CHECK (min_evidence >= 0),
    min_distinct_sources INTEGER NOT NULL CHECK (min_distinct_sources >= 0),
    min_evidence_span_us INTEGER NOT NULL CHECK (min_evidence_span_us >= 0),
    min_confidence REAL NOT NULL CHECK (min_confidence >= 0.0 AND min_confidence <= 1.0),
    cooldown_us INTEGER NOT NULL CHECK (cooldown_us >= 0),
    observation_us INTEGER NOT NULL CHECK (observation_us >= 0),
    sensitive_fields_json TEXT NOT NULL CHECK (json_valid(sensitive_fields_json)),
    rollback_threshold REAL NOT NULL CHECK (rollback_threshold >= 0.0),
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    status TEXT NOT NULL CHECK (status IN ('current', 'superseded')),
    created_by TEXT NOT NULL CHECK (created_by != ''),
    reason_code TEXT NOT NULL CHECK (reason_code != ''),
    created_us INTEGER NOT NULL CHECK (created_us >= 0),
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    UNIQUE (agent_id, revision)
) STRICT;
CREATE UNIQUE INDEX ux_persona_policy_current
    ON persona_policies (agent_id) WHERE status = 'current';

-- Install the deterministic locked policy around every Phase 1 bootstrap.
-- Its hash is content_hash(default_locked_policy_content()) using canonical
-- JSON v1; it is intentionally the same for every Agent.
INSERT INTO persona_policies (
    id, tenant_id, agent_id, revision, mode, allowed_fields_json,
    max_single_delta, max_cumulative_delta, cumulative_window_us,
    min_evidence, min_distinct_sources, min_evidence_span_us,
    min_confidence, cooldown_us, observation_us, sensitive_fields_json,
    rollback_threshold, content_hash, status, created_by, reason_code,
    created_us, schema_version
)
SELECT
    'persona-policy-bootstrap:' || id, tenant_id, id, 1, 'locked', '[]',
    0.0, 0.0, 0, 1, 1, 0, 1.0, 0, 0, '[]', 0.0,
    '6dfe909652dfc7a2dcb3174d2d6f092f7b74ab664159a1e790176c54632c6f98',
    'current', 'migration', 'bootstrap_expand', created_us, 1
FROM agents;

CREATE TABLE persona_revision_metadata (
    revision_id TEXT PRIMARY KEY REFERENCES persona_revisions (id),
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    policy_id TEXT NOT NULL REFERENCES persona_policies (id),
    previous_revision_id TEXT REFERENCES persona_revisions (id),
    change_reason TEXT NOT NULL CHECK (change_reason != ''),
    source_refs_json TEXT NOT NULL CHECK (json_valid(source_refs_json)),
    effective_from_us INTEGER NOT NULL CHECK (effective_from_us >= 0),
    effective_until_us INTEGER,
    created_by TEXT NOT NULL CHECK (created_by != ''),
    lifecycle_status TEXT NOT NULL
        CHECK (lifecycle_status IN ('published', 'superseded', 'revoked')),
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    CHECK (effective_until_us IS NULL OR effective_until_us >= effective_from_us),
    UNIQUE (agent_id, revision_id)
) STRICT;
CREATE INDEX idx_persona_revision_history
    ON persona_revision_metadata (tenant_id, agent_id, effective_from_us DESC, revision_id);

INSERT INTO persona_revision_metadata (
    revision_id, tenant_id, agent_id, policy_id, previous_revision_id,
    change_reason, source_refs_json, effective_from_us, effective_until_us,
    created_by, lifecycle_status, schema_version
)
SELECT
    p.id, p.tenant_id, p.agent_id, 'persona-policy-bootstrap:' || p.agent_id,
    NULL, 'bootstrap', '[]', p.created_us, NULL, 'provisioning', 'published', 1
FROM persona_revisions AS p;

CREATE TABLE persona_states (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    state_json TEXT NOT NULL CHECK (json_valid(state_json)),
    baseline_json TEXT NOT NULL CHECK (json_valid(baseline_json)),
    source_refs_json TEXT NOT NULL CHECK (json_valid(source_refs_json)),
    started_us INTEGER NOT NULL CHECK (started_us >= 0),
    expires_us INTEGER NOT NULL CHECK (expires_us > started_us),
    decay_policy TEXT NOT NULL CHECK (decay_policy IN ('expire_to_baseline')),
    created_by TEXT NOT NULL CHECK (created_by != ''),
    created_us INTEGER NOT NULL CHECK (created_us >= 0),
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    UNIQUE (agent_id, revision)
) STRICT;
CREATE INDEX idx_persona_states_expiry ON persona_states (expires_us, tenant_id, agent_id);

CREATE TABLE persona_state_current (
    agent_id TEXT PRIMARY KEY REFERENCES agents (id),
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    state_id TEXT NOT NULL REFERENCES persona_states (id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    updated_us INTEGER NOT NULL CHECK (updated_us >= 0)
) STRICT;

CREATE TABLE persona_proposals (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    base_revision INTEGER NOT NULL CHECK (base_revision >= 1),
    target_fields_json TEXT NOT NULL CHECK (json_valid(target_fields_json)),
    patch_json TEXT NOT NULL CHECK (json_valid(patch_json)),
    field_deltas_json TEXT NOT NULL CHECK (json_valid(field_deltas_json)),
    evidence_refs_json TEXT NOT NULL CHECK (json_valid(evidence_refs_json)),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    generator TEXT NOT NULL CHECK (generator != ''),
    generator_version TEXT NOT NULL CHECK (generator_version != ''),
    policy_evaluation_json TEXT NOT NULL CHECK (json_valid(policy_evaluation_json)),
    status TEXT NOT NULL
        CHECK (status IN ('proposed', 'approved', 'rejected', 'published', 'expired')),
    reviewed_by TEXT,
    review_reason TEXT,
    created_us INTEGER NOT NULL CHECK (created_us >= 0),
    expires_us INTEGER NOT NULL CHECK (expires_us > created_us),
    published_revision_id TEXT REFERENCES persona_revisions (id),
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version = 1)
) STRICT;
CREATE INDEX idx_persona_proposals_agent_status
    ON persona_proposals (tenant_id, agent_id, status, created_us DESC);

-- Append-only state-transition history keeps proposal review auditable even
-- though the proposal's current status is updated for efficient reads.
CREATE TABLE persona_proposal_events (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES persona_proposals (id),
    from_status TEXT,
    to_status TEXT NOT NULL
        CHECK (to_status IN ('proposed', 'approved', 'rejected', 'published', 'expired')),
    actor TEXT NOT NULL CHECK (actor != ''),
    reason_code TEXT NOT NULL CHECK (reason_code != ''),
    occurred_us INTEGER NOT NULL CHECK (occurred_us >= 0)
) STRICT;
CREATE INDEX idx_persona_proposal_events
    ON persona_proposal_events (proposal_id, occurred_us, id);

-- Structured adoption feedback is an alert/rollback input, never a direct
-- content mutation.  Phase 10 may add model-generated evaluation candidates.
CREATE TABLE persona_adoption_feedback (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    persona_revision INTEGER NOT NULL CHECK (persona_revision >= 1),
    host_id TEXT NOT NULL CHECK (host_id != ''),
    outcome TEXT NOT NULL CHECK (outcome IN ('adopted', 'rejected', 'error')),
    reason_code TEXT NOT NULL CHECK (reason_code != ''),
    occurred_us INTEGER NOT NULL CHECK (occurred_us >= 0),
    UNIQUE (agent_id, persona_revision, host_id, outcome, reason_code)
) STRICT;
