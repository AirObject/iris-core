-- iris: online_safe=true lock_ms=200 min_app=0.3.0 max_app= recovery=none
CREATE TABLE observations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    space_group_id TEXT REFERENCES space_groups (id),
    space_id TEXT REFERENCES spaces (id),
    session_id TEXT REFERENCES sessions (id),
    app_instance_id TEXT NOT NULL,
    source_stream TEXT,
    source_cursor INTEGER,
    source_event_id TEXT,
    occurrence_id TEXT,
    idempotency_key TEXT NOT NULL,
    record_fingerprint TEXT NOT NULL,
    actor_external_identity_id TEXT,
    actor_entity_id_at_ingest TEXT,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool', 'system', 'external')),
    kind TEXT NOT NULL,
    content TEXT,
    structured_payload TEXT,
    artifact_refs TEXT NOT NULL DEFAULT '[]',
    privacy_labels TEXT NOT NULL DEFAULT '[]',
    effect_state TEXT NOT NULL CHECK (effect_state IN ('committed', 'partial')),
    effect_proof TEXT,
    occurred_us INTEGER NOT NULL,
    committed_us INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    revision INTEGER NOT NULL DEFAULT 1,
    created_us INTEGER NOT NULL,
    UNIQUE (tenant_id, agent_id, idempotency_key),
    UNIQUE (tenant_id, agent_id, source_stream, source_cursor),
    UNIQUE (tenant_id, agent_id, occurrence_id),
    UNIQUE (tenant_id, agent_id, source_event_id),
    CHECK ((source_stream IS NULL) = (source_cursor IS NULL)),
    CHECK (occurred_us <= committed_us)
) STRICT;
CREATE INDEX idx_observations_agent_time ON observations (tenant_id, agent_id, created_us);

CREATE TABLE source_cursors (
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    source_stream TEXT NOT NULL,
    cursor_position INTEGER NOT NULL CHECK (cursor_position >= 0),
    gap_policy TEXT NOT NULL DEFAULT 'reject' CHECK (gap_policy IN ('accept', 'reject', 'mark')),
    updated_us INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, agent_id, source_stream),
    FOREIGN KEY (tenant_id) REFERENCES tenants (id),
    FOREIGN KEY (agent_id) REFERENCES agents (id)
) STRICT;

CREATE TABLE outbox_jobs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    agent_id TEXT,
    job_kind TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    payload TEXT NOT NULL,
    payload_version INTEGER NOT NULL DEFAULT 1,
    dedupe_key TEXT NOT NULL,
    coalesce_key TEXT,
    priority INTEGER NOT NULL DEFAULT 5 CHECK (priority >= 0 AND priority <= 9),
    lane TEXT NOT NULL DEFAULT 'normal' CHECK (lane IN ('normal', 'safety')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'leased', 'completed', 'retryable', 'dead')),
    available_at_us INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 8,
    lease_owner TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    lease_expires_us INTEGER,
    last_error_code TEXT,
    replay_of TEXT,
    created_us INTEGER NOT NULL,
    completed_us INTEGER,
    UNIQUE (tenant_id, dedupe_key),
    CHECK (coalesce_key IS NULL OR coalesce_key != ''),
    CHECK (payload_version >= 1)
) STRICT;
CREATE INDEX idx_outbox_claim ON outbox_jobs (status, priority, available_at_us);
CREATE INDEX idx_outbox_coalesce
    ON outbox_jobs (tenant_id, agent_id, job_kind, coalesce_key, status);
CREATE INDEX idx_outbox_tenant_status ON outbox_jobs (tenant_id, status);

CREATE TABLE schedules (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT REFERENCES agents (id),
    job_kind TEXT NOT NULL,
    schedule_spec TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    catch_up_policy TEXT NOT NULL DEFAULT 'latest'
        CHECK (catch_up_policy IN ('all', 'latest', 'coalesce', 'skip')),
    misfire_grace_us INTEGER NOT NULL DEFAULT 60000000,
    max_ticks_per_run INTEGER NOT NULL DEFAULT 100,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    next_tick_at_us INTEGER NOT NULL,
    last_tick_at_us INTEGER,
    policy_version INTEGER NOT NULL DEFAULT 1,
    revision INTEGER NOT NULL DEFAULT 1,
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL
) STRICT;
CREATE INDEX idx_schedules_due ON schedules (enabled, next_tick_at_us);

CREATE TABLE schedule_ticks (
    id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL REFERENCES schedules (id),
    scheduled_at_us INTEGER NOT NULL,
    occurrence_key TEXT NOT NULL,
    observed_wall_us INTEGER,
    observed_monotonic_delta_us INTEGER,
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'enqueued', 'completed', 'skipped', 'failed')),
    outbox_id TEXT,
    started_us INTEGER,
    completed_us INTEGER,
    reason_code TEXT,
    created_us INTEGER NOT NULL,
    UNIQUE (schedule_id, occurrence_key),
    -- A tick whose outbox job dead-letters settles as failed: the ledger
    -- row must record the terminal outcome, never vanish behind the DLQ.
    CHECK (outbox_id IS NULL OR status IN ('enqueued', 'completed', 'failed'))
) STRICT;
CREATE INDEX idx_ticks_schedule ON schedule_ticks (schedule_id, scheduled_at_us);

CREATE TABLE surface_lease_state (
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'off' CHECK (mode IN ('off', 'advisory', 'required')),
    current_epoch INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_us INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, agent_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants (id),
    FOREIGN KEY (agent_id) REFERENCES agents (id)
) STRICT;

CREATE TABLE surface_leases (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    holder_space_id TEXT,
    holder_app_instance_id TEXT NOT NULL,
    lease_epoch INTEGER NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL
        CHECK (status IN ('active', 'draining', 'released', 'expired')),
    acquired_us INTEGER NOT NULL,
    expires_us INTEGER NOT NULL,
    last_heartbeat_us INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL,
    UNIQUE (tenant_id, agent_id, lease_epoch),
    CHECK (expires_us > acquired_us)
) STRICT;
CREATE UNIQUE INDEX ux_surface_leases_active
    ON surface_leases (tenant_id, agent_id) WHERE status = 'active';

CREATE TABLE surface_lease_events (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    lease_epoch INTEGER NOT NULL,
    event TEXT NOT NULL CHECK (
        event IN ('acquired', 'preempted', 'heartbeat', 'released', 'expired', 'fenced')
    ),
    actor TEXT NOT NULL,
    reason_code TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '{}',
    created_us INTEGER NOT NULL
) STRICT;
CREATE INDEX idx_surface_events_lease ON surface_lease_events (tenant_id, agent_id, created_us);
