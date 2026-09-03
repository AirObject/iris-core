-- iris: online_safe=true lock_ms=200 min_app=0.8.0 max_app= recovery=none
-- Phase 7: vector projection state, surrogate ID map, delta ledger and the
-- FAISS generation pointer (§22.2-22.4, ADR-0015). FAISS index files live on
-- the filesystem next to the database (never inside SQLite, never created by
-- a migration); these tables hold only rebuildable metadata and pointer
-- state. The vector projection is never a source of truth (ADR-0001).
CREATE TABLE vector_projection_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL CHECK (state IN ('never_built', 'ready', 'pending_rebuild')),
    marked_us INTEGER NOT NULL,
    last_surrogate_id INTEGER NOT NULL CHECK (last_surrogate_id >= 0),
    CHECK (last_surrogate_id <= 4611686018427387904)
) STRICT;

-- Immutable vector generations (status ∈ {verified, retired}). The space
-- identity (model/dimension/metric/normalization/template/builder) is frozen
-- on the row: any component change means a different space and a new
-- generation — indexes are never mixed across spaces (ADR-0015 §5).
CREATE TABLE vector_generations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    model TEXT NOT NULL CHECK (model != ''),
    dimension INTEGER NOT NULL CHECK (dimension >= 1),
    metric TEXT NOT NULL CHECK (metric != ''),
    normalization TEXT NOT NULL CHECK (normalization != ''),
    template_version INTEGER NOT NULL CHECK (template_version >= 1),
    builder_version INTEGER NOT NULL CHECK (builder_version >= 1),
    source_watermark INTEGER NOT NULL CHECK (source_watermark >= 0),
    tombstone_watermark INTEGER NOT NULL CHECK (tombstone_watermark >= 0),
    vector_count INTEGER NOT NULL CHECK (vector_count >= 0),
    content_checksum TEXT NOT NULL,
    id_map_checksum TEXT NOT NULL,
    index_checksum TEXT NOT NULL,
    agent_watermarks_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN ('verified', 'retired')),
    created_us INTEGER NOT NULL,
    verified_us INTEGER NOT NULL,
    retired_us INTEGER,
    UNIQUE (tenant_id, id)
) STRICT;
CREATE INDEX idx_vector_generations_tenant
    ON vector_generations (tenant_id, status, created_us);

-- One current-pointer row per tenant that published a generation. The
-- switch_epoch is the fencing counter: every CAS bump increments it, and a
-- stale builder publishing after a newer switch is rejected (ADR-0015 §5).
CREATE TABLE vector_current (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants (id),
    generation_id TEXT NOT NULL REFERENCES vector_generations (id),
    switch_epoch INTEGER NOT NULL CHECK (switch_epoch >= 1),
    model TEXT NOT NULL CHECK (model != ''),
    dimension INTEGER NOT NULL CHECK (dimension >= 1),
    metric TEXT NOT NULL CHECK (metric != ''),
    normalization TEXT NOT NULL CHECK (normalization != ''),
    template_version INTEGER NOT NULL CHECK (template_version >= 1),
    builder_version INTEGER NOT NULL CHECK (builder_version >= 1),
    source_watermark INTEGER NOT NULL CHECK (source_watermark >= 0),
    tombstone_watermark INTEGER NOT NULL CHECK (tombstone_watermark >= 0),
    switched_us INTEGER NOT NULL
) STRICT;

-- Server-assigned surrogate ID map: UUID ↔ signed int64 (ADR-0015 §3).
-- Surrogates are allocated from a per-tenant monotonic counter stored in
-- vector_projection_state — never derived from the UUID — and are unique
-- within the tenant in both directions. Rows are logically invalidated on
-- correct/forget/tombstone/revision change; physical cleanup is async.
CREATE TABLE vector_id_map (
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    resource_type TEXT NOT NULL CHECK (resource_type IN ('claim', 'episode', 'note')),
    resource_id TEXT NOT NULL,
    resource_revision INTEGER NOT NULL CHECK (resource_revision >= 1),
    surrogate_id INTEGER NOT NULL
        CHECK (surrogate_id >= 1 AND surrogate_id <= 4611686018427387904),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    model TEXT NOT NULL CHECK (model != ''),
    dimension INTEGER NOT NULL CHECK (dimension >= 1),
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'invalid')),
    created_us INTEGER NOT NULL,
    invalidated_us INTEGER,
    -- Membership proof (ADR-0015 §4): the generation whose switch
    -- transaction stamped this row at this revision. Written ONLY inside the
    -- pointer-CAS transaction (NULL while unproven); a row's revision counts
    -- as incorporated by the CURRENT generation exactly when this equals the
    -- pointer's generation id. Deliberately not a foreign key — retired
    -- generations may be cleaned up while stamped rows live on; a dangling
    -- stamp simply never matches the pointer and is always safe.
    incorporated_generation TEXT,
    CHECK (status != 'invalid' OR invalidated_us IS NOT NULL),
    PRIMARY KEY (tenant_id, resource_type, resource_id),
    UNIQUE (tenant_id, surrogate_id)
) STRICT;
CREATE INDEX idx_vector_id_map_agent ON vector_id_map (tenant_id, agent_id, status);

-- Delta ledger: incremental changes land here (written by vector.apply in
-- its fenced transaction) or trigger a new generation; the current query
-- handle is never mutated (§22.4). A full rebuild clears the tenant's rows
-- in the same transaction that publishes the generation — the publish
-- snapshot covers every committed change by construction.
CREATE TABLE vector_delta_ledger (
    id INTEGER PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    resource_type TEXT NOT NULL CHECK (resource_type IN ('claim', 'episode', 'note')),
    resource_id TEXT NOT NULL,
    resource_revision INTEGER NOT NULL CHECK (resource_revision >= 0),
    op TEXT NOT NULL CHECK (op IN ('upsert', 'remove')),
    source_watermark INTEGER NOT NULL CHECK (source_watermark >= 0),
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL,
    UNIQUE (tenant_id, resource_type, resource_id)
) STRICT;
CREATE INDEX idx_vector_delta_agent ON vector_delta_ledger (tenant_id, agent_id);
