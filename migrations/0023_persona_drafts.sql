-- iris: online_safe=true lock_ms=100 min_app=0.15.0 max_app= recovery=none
-- Unpublished drafts never consume Persona's immutable publication sequence.
CREATE TABLE persona_drafts (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    agent_id TEXT NOT NULL REFERENCES agents(id),
    revision INTEGER NOT NULL CHECK(revision>=1),
    status TEXT NOT NULL CHECK(status IN ('draft','published','discarded')),
    base_revision INTEGER NOT NULL CHECK(base_revision>=1),
    policy_revision INTEGER NOT NULL CHECK(policy_revision>=1),
    fields_json TEXT CHECK(fields_json IS NULL OR
        (json_valid(fields_json) AND json_type(fields_json)='object'
        AND length(CAST(fields_json AS BLOB))<=131072)),
    source_refs_json TEXT CHECK(source_refs_json IS NULL OR
        (json_valid(source_refs_json) AND json_type(source_refs_json)='array'
        AND length(CAST(source_refs_json AS BLOB))<=65536)),
    content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_us INTEGER NOT NULL CHECK(created_us>=0),
    updated_us INTEGER NOT NULL CHECK(updated_us>=created_us),
    published_revision_id TEXT REFERENCES persona_revisions(id),
    CHECK((status='discarded' AND fields_json IS NULL AND source_refs_json IS NULL)
        OR (status!='discarded' AND fields_json IS NOT NULL AND source_refs_json IS NOT NULL)),
    CHECK((status='published' AND published_revision_id IS NOT NULL)
        OR (status!='published' AND published_revision_id IS NULL))
) STRICT;
CREATE INDEX idx_persona_drafts_agent ON persona_drafts
    (tenant_id,agent_id,created_us DESC,id DESC);
CREATE INDEX idx_console_persona_drafts_created ON persona_drafts
    (tenant_id,created_us DESC,id DESC);
CREATE TABLE persona_draft_discards (
    draft_id TEXT PRIMARY KEY REFERENCES persona_drafts(id),
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    agent_id TEXT NOT NULL REFERENCES agents(id),
    revision INTEGER NOT NULL CHECK(revision>=2),
    discarded_us INTEGER NOT NULL CHECK(discarded_us>=0),
    discarded_by TEXT NOT NULL,
    reason_code TEXT NOT NULL CHECK(reason_code='operator_request')
) STRICT;
