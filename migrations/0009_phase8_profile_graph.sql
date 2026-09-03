-- iris: online_safe=true lock_ms=200 min_app=0.9.0 max_app= recovery=none
-- Phase 8: profile and graph projections (§13.3, §13.5, §22.5, ADR-0016).
-- Both projections are rebuildable summaries of canonical claims/relations/
-- bindings — never a source of truth (ADR-0001). Generations are immutable
-- once verified; the per-tenant pointer flips with a fencing epoch CAS in the
-- same transaction that retires the previous generation. Incremental applies
-- maintain the CURRENT generation's rows in place; freshness is expressed by
-- the unsettled graph.apply/profile.apply backlog, not a delta ledger.
CREATE TABLE profile_projection_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL CHECK (state IN ('never_built', 'ready', 'pending_rebuild')),
    marked_us INTEGER NOT NULL
) STRICT;

-- Immutable profile generations (status ∈ {verified, retired}). The row IS
-- the authoritative manifest: field/subject counts and the content checksum
-- (recomputed from the persisted profile_subjects rows at build/verify time)
-- cross-bind the manifest fields to the authoritative content rows.
CREATE TABLE profile_generations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    builder_version INTEGER NOT NULL CHECK (builder_version >= 1),
    source_watermark INTEGER NOT NULL CHECK (source_watermark >= 0),
    tombstone_watermark INTEGER NOT NULL CHECK (tombstone_watermark >= 0),
    subject_count INTEGER NOT NULL CHECK (subject_count >= 0),
    field_count INTEGER NOT NULL CHECK (field_count >= 0),
    content_checksum TEXT NOT NULL,
    agent_watermarks_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN ('verified', 'retired')),
    created_us INTEGER NOT NULL,
    verified_us INTEGER NOT NULL,
    retired_us INTEGER,
    UNIQUE (tenant_id, id)
) STRICT;
CREATE INDEX idx_profile_generations_tenant
    ON profile_generations (tenant_id, status, created_us);

-- One current-pointer row per tenant that published a profile generation.
-- switch_epoch is the fencing counter: every CAS bump increments it and a
-- stale builder publishing after a newer switch is rejected (ADR-0016 §2).
CREATE TABLE profile_current (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants (id),
    generation_id TEXT NOT NULL REFERENCES profile_generations (id),
    switch_epoch INTEGER NOT NULL CHECK (switch_epoch >= 1),
    builder_version INTEGER NOT NULL CHECK (builder_version >= 1),
    source_watermark INTEGER NOT NULL CHECK (source_watermark >= 0),
    tombstone_watermark INTEGER NOT NULL CHECK (tombstone_watermark >= 0),
    switched_us INTEGER NOT NULL
) STRICT;

-- Per-subject summary inside one generation: field count and the subject
-- checksum the generation's content_checksum digests over (sorted subject
-- key + subject checksum). Incremental applies rewrite only the affected
-- subject's rows and re-derive both digests deterministically.
CREATE TABLE profile_subjects (
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    generation_id TEXT NOT NULL REFERENCES profile_generations (id),
    subject_kind TEXT NOT NULL CHECK (subject_kind IN ('entity', 'relationship', 'space_group')),
    subject_id TEXT NOT NULL CHECK (subject_id != ''),
    field_count INTEGER NOT NULL CHECK (field_count >= 0),
    subject_checksum TEXT NOT NULL,
    updated_us INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, generation_id, subject_kind, subject_id)
) STRICT;

-- Field-level profile rows. A field NEVER merges claims across scopes or
-- privacy label sets: the grouping key includes scope_key and the privacy
-- labels, so cross-scope stitching is structurally inexpressible
-- (ADR-0016 §2). Sources are canonical claim ids + revisions; conflict_state
-- explicitly retains conflicts (value_json lists every conflicting value).
CREATE TABLE profile_fields (
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    generation_id TEXT NOT NULL REFERENCES profile_generations (id),
    subject_kind TEXT NOT NULL CHECK (subject_kind IN ('entity', 'relationship', 'space_group')),
    subject_id TEXT NOT NULL CHECK (subject_id != ''),
    section TEXT NOT NULL CHECK (
        section IN (
            'identity', 'preference', 'relationship', 'experience', 'goal',
            'recent_change', 'interaction', 'community', 'fact'
        )
    ),
    field TEXT NOT NULL CHECK (field != ''),
    group_key TEXT NOT NULL CHECK (group_key != ''),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    space_group_id TEXT,
    space_id TEXT,
    session_id TEXT,
    scope_key TEXT NOT NULL CHECK (scope_key != ''),
    privacy_labels_json TEXT NOT NULL CHECK (privacy_labels_json != ''),
    value_json TEXT NOT NULL CHECK (value_json != ''),
    summary_text TEXT NOT NULL,
    source_refs_json TEXT NOT NULL CHECK (source_refs_json != ''),
    conflict_state TEXT NOT NULL CHECK (conflict_state IN ('single', 'conflict', 'disputed')),
    freshness_us INTEGER NOT NULL CHECK (freshness_us >= 0),
    valid_from_us INTEGER,
    valid_until_us INTEGER,
    created_us INTEGER NOT NULL,
    CHECK (session_id IS NULL OR space_id IS NOT NULL),
    PRIMARY KEY (tenant_id, generation_id, subject_kind, subject_id, section, field, group_key)
) STRICT;
CREATE INDEX idx_profile_fields_subject
    ON profile_fields (tenant_id, generation_id, subject_kind, subject_id);

CREATE TABLE graph_projection_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL CHECK (state IN ('never_built', 'ready', 'pending_rebuild')),
    marked_us INTEGER NOT NULL
) STRICT;

-- Immutable graph generations. Edge allowlist (ADR-0016 §3): relation,
-- verified binding, and structurally-targeted relationship claims only —
-- nicknames, co-occurrence and similarity never create edges.
CREATE TABLE graph_generations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    builder_version INTEGER NOT NULL CHECK (builder_version >= 1),
    source_watermark INTEGER NOT NULL CHECK (source_watermark >= 0),
    tombstone_watermark INTEGER NOT NULL CHECK (tombstone_watermark >= 0),
    node_count INTEGER NOT NULL CHECK (node_count >= 0),
    edge_count INTEGER NOT NULL CHECK (edge_count >= 0),
    content_checksum TEXT NOT NULL,
    agent_watermarks_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN ('verified', 'retired')),
    created_us INTEGER NOT NULL,
    verified_us INTEGER NOT NULL,
    retired_us INTEGER,
    UNIQUE (tenant_id, id)
) STRICT;
CREATE INDEX idx_graph_generations_tenant
    ON graph_generations (tenant_id, status, created_us);

CREATE TABLE graph_current (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants (id),
    generation_id TEXT NOT NULL REFERENCES graph_generations (id),
    switch_epoch INTEGER NOT NULL CHECK (switch_epoch >= 1),
    builder_version INTEGER NOT NULL CHECK (builder_version >= 1),
    source_watermark INTEGER NOT NULL CHECK (source_watermark >= 0),
    tombstone_watermark INTEGER NOT NULL CHECK (tombstone_watermark >= 0),
    switched_us INTEGER NOT NULL
) STRICT;

-- Graph nodes: only entities/external identities referenced by at least one
-- edge of this generation. node_id is the entity/external_identity id; kind
-- disambiguates the two id spaces.
CREATE TABLE graph_nodes (
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    generation_id TEXT NOT NULL REFERENCES graph_generations (id),
    node_id TEXT NOT NULL CHECK (node_id != ''),
    node_kind TEXT NOT NULL CHECK (node_kind IN ('entity', 'external_identity')),
    node_status TEXT NOT NULL CHECK (node_status != ''),
    PRIMARY KEY (tenant_id, generation_id, node_id)
) STRICT;

-- Graph edges. Every edge binds its canonical (resource_type, resource_id,
-- resource_revision) and carries scope, privacy, status, valid time and the
-- builder/watermark metadata needed for per-edge authorization during
-- traversal (ADR-0016 §4).
CREATE TABLE graph_edges (
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    generation_id TEXT NOT NULL REFERENCES graph_generations (id),
    edge_id TEXT NOT NULL CHECK (edge_id != ''),
    edge_kind TEXT NOT NULL CHECK (edge_kind IN ('relation', 'binding', 'claim')),
    edge_type TEXT NOT NULL CHECK (edge_type != ''),
    source_node_id TEXT NOT NULL CHECK (source_node_id != ''),
    source_node_kind TEXT NOT NULL CHECK (source_node_kind IN ('entity', 'external_identity')),
    target_node_id TEXT NOT NULL CHECK (target_node_id != ''),
    target_node_kind TEXT NOT NULL CHECK (target_node_kind IN ('entity', 'external_identity')),
    resource_type TEXT NOT NULL CHECK (resource_type IN ('relation', 'binding', 'claim')),
    resource_id TEXT NOT NULL CHECK (resource_id != ''),
    resource_revision INTEGER NOT NULL CHECK (resource_revision >= 1),
    agent_id TEXT REFERENCES agents (id),
    space_group_id TEXT,
    space_id TEXT,
    session_id TEXT,
    privacy_labels_json TEXT NOT NULL CHECK (privacy_labels_json != ''),
    status TEXT NOT NULL CHECK (status IN ('active', 'disputed')),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    importance REAL NOT NULL CHECK (importance >= 0.0 AND importance <= 1.0),
    valid_from_us INTEGER,
    valid_until_us INTEGER,
    content_hash TEXT NOT NULL,
    created_us INTEGER NOT NULL,
    CHECK (session_id IS NULL OR space_id IS NOT NULL),
    -- Binding edges are tenant-level canonical data with no owning agent;
    -- relation/claim edges always carry their agent.
    CHECK (edge_kind = 'binding' OR agent_id IS NOT NULL),
    PRIMARY KEY (tenant_id, generation_id, edge_id),
    UNIQUE (tenant_id, generation_id, source_node_id, target_node_id, edge_kind,
            edge_type, resource_type, resource_id)
) STRICT;
CREATE INDEX idx_graph_edges_source
    ON graph_edges (tenant_id, generation_id, source_node_id);
CREATE INDEX idx_graph_edges_resource
    ON graph_edges (tenant_id, generation_id, resource_type, resource_id);
