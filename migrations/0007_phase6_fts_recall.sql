-- iris: online_safe=true lock_ms=200 min_app=0.7.0 max_app= recovery=none
-- Phase 6: FTS5 projection state and recall usage accounting (§18, §22.1).
-- FTS documents bind (resource_type, resource_id, resource_revision) and
-- carry only rebuildable metadata (ADR-0014 §1); the FTS5 virtual table
-- itself is created lazily by the projection service after a runtime
-- capability probe — migrations must not depend on FTS5 being compiled in.
-- Usage rows persist the four recall stages; they never store prompts or
-- candidate bodies (ADR-0014 §7).
CREATE TABLE fts_generations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    builder_version INTEGER NOT NULL CHECK (builder_version >= 1),
    tokenizer_version INTEGER NOT NULL CHECK (tokenizer_version >= 1),
    config_json TEXT NOT NULL CHECK (config_json != ''),
    source_watermark INTEGER NOT NULL CHECK (source_watermark >= 0),
    tombstone_watermark INTEGER NOT NULL CHECK (tombstone_watermark >= 0),
    document_count INTEGER NOT NULL CHECK (document_count >= 0),
    content_checksum TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('verified', 'retired')),
    created_us INTEGER NOT NULL,
    verified_us INTEGER NOT NULL,
    UNIQUE (tenant_id, id)
) STRICT;
CREATE INDEX idx_fts_generations_tenant ON fts_generations (tenant_id, status, created_us);

-- One current-pointer row per tenant that has built a generation. The
-- pointer flip and the retiring of the previous generation happen in ONE
-- write transaction (ADR-0014 §1); a partially visible build state is not
-- representable.
CREATE TABLE fts_current (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants (id),
    generation_id TEXT NOT NULL REFERENCES fts_generations (id),
    builder_version INTEGER NOT NULL CHECK (builder_version >= 1),
    tokenizer_version INTEGER NOT NULL CHECK (tokenizer_version >= 1),
    source_watermark INTEGER NOT NULL CHECK (source_watermark >= 0),
    tombstone_watermark INTEGER NOT NULL CHECK (tombstone_watermark >= 0),
    switched_us INTEGER NOT NULL
) STRICT;

-- FTS documents are projections (ADR-0001): at most one live row per
-- resource per generation; a revision advance replaces the row inside the
-- same transaction that emits the change event, and a tombstone flips
-- doc_status to 'invalid' immediately (physical cleanup is async). The
-- external-content FTS5 virtual table references this table by rowid.
CREATE TABLE fts_documents (
    id INTEGER PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    generation_id TEXT NOT NULL REFERENCES fts_generations (id),
    resource_type TEXT NOT NULL CHECK (resource_type IN ('claim', 'episode', 'note')),
    resource_id TEXT NOT NULL,
    resource_revision INTEGER NOT NULL CHECK (resource_revision >= 1),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    space_group_id TEXT,
    space_id TEXT,
    session_id TEXT,
    scope_key TEXT NOT NULL CHECK (scope_key != ''),
    canonical_status TEXT NOT NULL,
    privacy_labels TEXT NOT NULL DEFAULT '[]',
    subject_entity_id TEXT,
    content_hash TEXT NOT NULL,
    occurred_us INTEGER NOT NULL,
    valid_from_us INTEGER,
    valid_until_us INTEGER,
    index_text TEXT NOT NULL CHECK (index_text != ''),
    doc_status TEXT NOT NULL CHECK (doc_status IN ('active', 'invalid')),
    invalidated_us INTEGER,
    builder_version INTEGER NOT NULL CHECK (builder_version >= 1),
    source_watermark INTEGER NOT NULL CHECK (source_watermark >= 0),
    tombstone_watermark INTEGER NOT NULL CHECK (tombstone_watermark >= 0),
    created_us INTEGER NOT NULL,
    CHECK (doc_status != 'invalid' OR invalidated_us IS NOT NULL),
    CHECK (session_id IS NULL OR space_id IS NOT NULL),
    UNIQUE (generation_id, resource_type, resource_id)
) STRICT;
CREATE INDEX idx_fts_documents_resource
    ON fts_documents (tenant_id, resource_type, resource_id, doc_status);
CREATE INDEX idx_fts_documents_generation
    ON fts_documents (generation_id, doc_status);

-- Global projection lifecycle marker. Restore resets FTS and flips this to
-- 'pending_rebuild': the FTS projection is never restored as a source of
-- truth (ADR-0014 §9).
CREATE TABLE fts_projection_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL CHECK (state IN ('never_built', 'ready', 'pending_rebuild')),
    marked_us INTEGER NOT NULL
) STRICT;

-- Core-side usage stages (retrieved/returned). One row per accepted recall
-- request; the host-side usage report validates against this row. The row
-- also carries the request fingerprint and the SERVED response envelope so
-- a transport retry of the same request id REPLAYS the first response
-- instead of re-executing (ADR-0014 §7): response_json holds candidate
-- bodies and is nulled by the erasure path when any referenced resource is
-- invalidated — a scrubbed replay fails closed instead of resurrecting
-- erased content.
CREATE TABLE recall_requests (
    id TEXT PRIMARY KEY,
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
    UNIQUE (tenant_id, id)
) STRICT;
CREATE INDEX idx_recall_requests_tenant ON recall_requests (tenant_id, created_us);

-- Host-side usage stages (host_selected/model_visible). Idempotent by the
-- full logical identity (tenant, request, host cycle): repeats replay the
-- first report exactly once into any accumulation (ADR-0014 §7).
CREATE TABLE recall_usage_reports (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    request_id TEXT NOT NULL REFERENCES recall_requests (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    app_instance_id TEXT NOT NULL,
    host_cycle_id TEXT NOT NULL,
    persona_revision INTEGER NOT NULL CHECK (persona_revision >= 0),
    host_selected_ids TEXT NOT NULL DEFAULT '[]',
    model_visible_ids TEXT NOT NULL DEFAULT '[]',
    reported_at_us INTEGER NOT NULL,
    created_us INTEGER NOT NULL,
    UNIQUE (tenant_id, request_id, host_cycle_id)
) STRICT;
CREATE INDEX idx_recall_usage_reports_request ON recall_usage_reports (tenant_id, request_id);
