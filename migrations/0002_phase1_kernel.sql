-- iris: online_safe=true lock_ms=200 min_app=0.2.0 max_app= recovery=none
CREATE TABLE tenants (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('active', 'suspended', 'disabled')),
    created_us INTEGER NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE agents (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    display_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'suspended', 'disabled')),
    persona_current_revision_id TEXT REFERENCES persona_revisions (id),
    created_us INTEGER NOT NULL,
    created_at TEXT NOT NULL
) STRICT;
CREATE INDEX idx_agents_tenant ON agents (tenant_id);

CREATE TABLE persona_revisions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL REFERENCES agents (id),
    revision INTEGER NOT NULL,
    core TEXT NOT NULL,
    traits TEXT NOT NULL,
    narrative TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('published', 'draft', 'retired')),
    source TEXT NOT NULL,
    created_us INTEGER NOT NULL,
    UNIQUE (agent_id, revision)
) STRICT;

CREATE TABLE space_groups (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    primary_space_id TEXT REFERENCES spaces (id),
    status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL
) STRICT;

CREATE TABLE space_group_revisions (
    id TEXT PRIMARY KEY,
    space_group_id TEXT NOT NULL REFERENCES space_groups (id),
    revision INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_us INTEGER NOT NULL,
    UNIQUE (space_group_id, revision)
) STRICT;

CREATE TABLE spaces (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT REFERENCES agents (id),
    space_group_id TEXT REFERENCES space_groups (id),
    kind TEXT NOT NULL
        CHECK (kind IN ('chat_group', 'live_channel', 'direct', 'local', 'other')),
    status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL
) STRICT;
CREATE INDEX idx_spaces_tenant ON spaces (tenant_id);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    space_id TEXT NOT NULL REFERENCES spaces (id),
    status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    started_us INTEGER NOT NULL,
    ended_us INTEGER
) STRICT;

CREATE TABLE space_group_bindings (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    space_id TEXT NOT NULL REFERENCES spaces (id),
    space_group_id TEXT NOT NULL REFERENCES space_groups (id),
    bound_us INTEGER NOT NULL,
    unbound_us INTEGER,
    bound_by TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    unbound_by TEXT,
    unbound_reason_code TEXT
) STRICT;
CREATE UNIQUE INDEX ux_space_group_bindings_active
    ON space_group_bindings (space_id) WHERE unbound_us IS NULL;
CREATE INDEX idx_space_group_bindings_group ON space_group_bindings (space_group_id);

CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    kind TEXT NOT NULL CHECK (
        kind IN ('person', 'agent', 'organization', 'community', 'place', 'topic', 'object', 'system')
    ),
    state TEXT NOT NULL
        CHECK (state IN ('provisional', 'canonical', 'redirected', 'restricted', 'tombstoned')),
    display_name TEXT NOT NULL DEFAULT '',
    privacy_labels TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL DEFAULT 1,
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL
) STRICT;
CREATE INDEX idx_entities_tenant ON entities (tenant_id);

CREATE TABLE entity_revisions (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities (id),
    revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    display_name TEXT NOT NULL,
    privacy_labels TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_us INTEGER NOT NULL,
    UNIQUE (entity_id, revision)
) STRICT;

CREATE TABLE external_identities (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    provider TEXT NOT NULL,
    realm TEXT NOT NULL,
    external_id TEXT NOT NULL,
    entity_id TEXT REFERENCES entities (id),
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL,
    UNIQUE (tenant_id, provider, realm, external_id)
) STRICT;
CREATE INDEX idx_external_identities_entity ON external_identities (entity_id);

CREATE TABLE identity_attributes (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    entity_id TEXT NOT NULL REFERENCES entities (id),
    field TEXT NOT NULL,
    value TEXT NOT NULL,
    authority TEXT NOT NULL CHECK (
        authority IN ('inferred', 'claim_supported', 'platform_verified', 'admin_confirmed',
                      'explicit_correction')
    ),
    source_ref TEXT NOT NULL,
    effective_us INTEGER NOT NULL,
    recorded_us INTEGER NOT NULL,
    superseded_us INTEGER,
    status TEXT NOT NULL CHECK (status IN ('current', 'superseded', 'conflict'))
) STRICT;
CREATE UNIQUE INDEX ux_identity_attributes_current
    ON identity_attributes (entity_id, field) WHERE status = 'current';

CREATE TABLE bindings (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    external_identity_id TEXT NOT NULL REFERENCES external_identities (id),
    entity_id TEXT NOT NULL REFERENCES entities (id),
    state TEXT NOT NULL CHECK (state IN ('proposed', 'verified', 'revoked', 'conflicted')),
    method TEXT NOT NULL CHECK (method IN ('admin_confirmation', 'challenge_code')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    proof_digest TEXT NOT NULL,
    valid_from_us INTEGER NOT NULL,
    valid_until_us INTEGER,
    revision INTEGER NOT NULL DEFAULT 1,
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    confirmed_by TEXT,
    confirmed_us INTEGER,
    revoked_by TEXT,
    revoked_us INTEGER
) STRICT;
CREATE UNIQUE INDEX ux_bindings_one_verified
    ON bindings (external_identity_id) WHERE state = 'verified';

CREATE TABLE binding_revisions (
    id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL REFERENCES bindings (id),
    revision INTEGER NOT NULL,
    state TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_us INTEGER NOT NULL,
    UNIQUE (binding_id, revision)
) STRICT;

CREATE TABLE entity_redirects (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    from_entity_id TEXT NOT NULL REFERENCES entities (id),
    to_entity_id TEXT NOT NULL REFERENCES entities (id),
    reason_code TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_us INTEGER NOT NULL,
    UNIQUE (from_entity_id),
    CHECK (from_entity_id <> to_entity_id)
) STRICT;

CREATE TABLE resource_tombstones (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    deleted_by TEXT NOT NULL,
    created_us INTEGER NOT NULL,
    tombstone_seq INTEGER NOT NULL UNIQUE,
    UNIQUE (tenant_id, resource_type, resource_id)
) STRICT;

CREATE TABLE resource_links (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    created_us INTEGER NOT NULL,
    UNIQUE (tenant_id, source_type, source_id, target_type, target_id, relation)
) STRICT;

CREATE TABLE idempotency_records (
    tenant_id TEXT NOT NULL,
    app_instance_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('in_progress', 'completed', 'failed_replayable')),
    response_code TEXT,
    response_body TEXT,
    resource_refs TEXT NOT NULL DEFAULT '[]',
    transaction_ref TEXT,
    owner_token TEXT,
    created_us INTEGER NOT NULL,
    expires_us INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, app_instance_id, operation, idempotency_key)
) STRICT;

CREATE TABLE idempotency_outcomes (
    transaction_ref TEXT PRIMARY KEY,
    result_code TEXT NOT NULL,
    result_body TEXT,
    resource_refs TEXT NOT NULL DEFAULT '[]',
    completed_us INTEGER NOT NULL
) STRICT;

CREATE TABLE audit_events (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    revision INTEGER,
    reason_code TEXT NOT NULL,
    details TEXT NOT NULL,
    created_us INTEGER NOT NULL
) STRICT;
CREATE INDEX idx_audit_events_resource
    ON audit_events (tenant_id, resource_type, resource_id);

CREATE TABLE agent_watermarks (
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    current_seq INTEGER NOT NULL DEFAULT 0,
    updated_us INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, agent_id)
) STRICT;

CREATE TABLE agent_watermark_entries (
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_revision INTEGER NOT NULL,
    recorded_us INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, agent_id, seq, aggregate_type, aggregate_id),
    FOREIGN KEY (tenant_id, agent_id)
        REFERENCES agent_watermarks (tenant_id, agent_id)
) STRICT;

CREATE TABLE backup_catalog (
    id TEXT PRIMARY KEY,
    manifest_path TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('valid', 'restored', 'corrupt')),
    notes TEXT NOT NULL DEFAULT '',
    created_us INTEGER NOT NULL
) STRICT;
