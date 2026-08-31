-- iris: online_safe=true lock_ms=200 min_app=0.4.0 max_app= recovery=none
CREATE TABLE recent_context_generations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    space_group_id TEXT,
    space_id TEXT NOT NULL REFERENCES spaces (id),
    session_id TEXT REFERENCES sessions (id),
    target_key TEXT NOT NULL,
    builder_version INTEGER NOT NULL CHECK (builder_version >= 1),
    source_watermark INTEGER NOT NULL CHECK (source_watermark >= 0),
    head_observation_id TEXT,
    tail_observation_id TEXT,
    hot_observation_refs TEXT NOT NULL,
    summary_segments TEXT NOT NULL DEFAULT '[]',
    token_estimate INTEGER NOT NULL CHECK (token_estimate >= 0),
    result_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('verified', 'retired')),
    created_us INTEGER NOT NULL,
    expires_us INTEGER,
    CHECK (LENGTH(result_hash) = 64)
) STRICT;
CREATE INDEX idx_recent_generations_target
    ON recent_context_generations (target_key, status, created_us);

CREATE TABLE recent_context_current (
    target_key TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    space_group_id TEXT,
    space_id TEXT NOT NULL REFERENCES spaces (id),
    session_id TEXT REFERENCES sessions (id),
    current_generation_id TEXT NOT NULL,
    updated_us INTEGER NOT NULL,
    CHECK (tenant_id != '' AND agent_id != '' AND space_id != '')
) STRICT;

CREATE TABLE state_namespace_policies (
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    namespace TEXT NOT NULL,
    default_ttl_us INTEGER NOT NULL CHECK (default_ttl_us >= 0),
    max_ttl_us INTEGER NOT NULL CHECK (max_ttl_us >= 0),
    retain_history INTEGER NOT NULL CHECK (retain_history IN (0, 1)),
    max_history_revisions INTEGER NOT NULL CHECK (max_history_revisions >= 0),
    max_value_bytes INTEGER NOT NULL
        CHECK (max_value_bytes > 0 AND max_value_bytes <= 262144),
    allowed_source_authorities TEXT NOT NULL,
    required_scope TEXT NOT NULL CHECK (required_scope IN ('any', 'agent', 'space', 'session')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, namespace),
    CHECK (namespace != '' AND LENGTH(namespace) <= 128),
    CHECK (max_ttl_us = 0 OR default_ttl_us <= max_ttl_us)
) STRICT;

CREATE TABLE state_records (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT REFERENCES agents (id),
    space_group_id TEXT,
    space_id TEXT REFERENCES spaces (id),
    session_id TEXT REFERENCES sessions (id),
    scope_key TEXT NOT NULL,
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
    current_revision_id TEXT NOT NULL,
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL,
    UNIQUE (scope_key, namespace, key),
    CHECK (namespace != '' AND LENGTH(namespace) <= 128),
    CHECK (key != '' AND LENGTH(key) <= 256),
    CHECK (session_id IS NULL OR space_id IS NOT NULL)
) STRICT;
CREATE INDEX idx_state_records_scope ON state_records (tenant_id, agent_id, namespace);

CREATE TABLE state_record_revisions (
    id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL REFERENCES state_records (id),
    tenant_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    value_json TEXT NOT NULL CHECK (LENGTH(value_json) <= 262144),
    source_ref TEXT,
    source_authority TEXT NOT NULL CHECK (
        source_authority IN ('host', 'platform', 'adapter', 'system', 'user', 'model')
    ),
    observed_us INTEGER NOT NULL,
    expires_us INTEGER,
    coalesce_key TEXT,
    created_us INTEGER NOT NULL,
    UNIQUE (record_id, revision),
    CHECK (coalesce_key IS NULL OR coalesce_key != '')
) STRICT;
CREATE INDEX idx_state_revisions_record ON state_record_revisions (record_id, revision);

CREATE TABLE focus_items (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    space_group_id TEXT,
    space_id TEXT REFERENCES spaces (id),
    session_id TEXT REFERENCES sessions (id),
    scope_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (
        kind IN ('goal', 'question', 'entity', 'clue', 'concern', 'affect', 'pending_input')
    ),
    summary TEXT NOT NULL CHECK (summary != '' AND LENGTH(summary) <= 2000),
    status TEXT NOT NULL CHECK (status IN ('active', 'dormant', 'promoted', 'dismissed', 'expired')),
    current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
    current_revision_id TEXT NOT NULL DEFAULT '',
    activation REAL NOT NULL CHECK (activation >= 0 AND activation <= 1),
    activation_base REAL NOT NULL CHECK (activation_base >= 0 AND activation_base <= 1),
    last_activated_us INTEGER NOT NULL,
    expires_us INTEGER,
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL,
    CHECK (session_id IS NULL OR space_id IS NOT NULL)
) STRICT;
CREATE INDEX idx_focus_items_agent ON focus_items (tenant_id, agent_id, status, kind);

CREATE TABLE focus_item_revisions (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES focus_items (id),
    tenant_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    kind TEXT NOT NULL CHECK (
        kind IN ('goal', 'question', 'entity', 'clue', 'concern', 'affect', 'pending_input')
    ),
    summary TEXT NOT NULL CHECK (summary != '' AND LENGTH(summary) <= 2000),
    structured_payload TEXT,
    privacy_labels TEXT NOT NULL DEFAULT '[]',
    source_refs TEXT NOT NULL DEFAULT '[]',
    salience REAL NOT NULL CHECK (salience >= 0 AND salience <= 1),
    activation REAL NOT NULL CHECK (activation >= 0 AND activation <= 1),
    activation_base REAL NOT NULL CHECK (activation_base >= 0 AND activation_base <= 1),
    importance REAL NOT NULL CHECK (importance >= 0 AND importance <= 1),
    status TEXT NOT NULL CHECK (status IN ('active', 'dormant', 'promoted', 'dismissed', 'expired')),
    promotion_policy TEXT NOT NULL DEFAULT '',
    promotion_target_type TEXT CHECK (promotion_target_type IN ('note', 'task', 'episode', 'claim')),
    promotion_target_id TEXT,
    last_activated_us INTEGER NOT NULL,
    expires_us INTEGER,
    created_us INTEGER NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    UNIQUE (item_id, revision)
) STRICT;
CREATE INDEX idx_focus_revisions_item ON focus_item_revisions (item_id, revision);
