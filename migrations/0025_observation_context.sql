-- iris: online_safe=true lock_ms=200 min_app=0.16.0 max_app= recovery=none
ALTER TABLE observations ADD COLUMN context_kind TEXT NOT NULL DEFAULT 'interaction'
    CHECK(context_kind IN ('interaction','background'));
ALTER TABLE observations ADD COLUMN source_thread_id TEXT;
ALTER TABLE observations ADD COLUMN reply_to_source_event_id TEXT;
CREATE INDEX idx_observations_context_time ON observations
    (tenant_id,agent_id,space_id,session_id,occurred_us,committed_us,id);
CREATE INDEX idx_observations_background_retention ON observations
    (context_kind,created_us,id);
CREATE TABLE observation_context_batches (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    agent_id TEXT NOT NULL REFERENCES agents(id),
    space_group_id TEXT,
    space_id TEXT NOT NULL REFERENCES spaces(id),
    session_id TEXT,
    job_id TEXT NOT NULL REFERENCES outbox_jobs(id),
    source_watermark INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','completed')),
    episode_ids TEXT NOT NULL DEFAULT '[]',
    created_us INTEGER NOT NULL,
    completed_us INTEGER
) STRICT;
CREATE INDEX idx_observation_context_batches_scope ON observation_context_batches
    (tenant_id,agent_id,space_id,session_id,created_us,id);
CREATE TABLE observation_context_members (
    batch_id TEXT NOT NULL REFERENCES observation_context_batches(id),
    observation_id TEXT NOT NULL REFERENCES observations(id),
    observation_revision INTEGER NOT NULL,
    record_fingerprint TEXT NOT NULL,
    PRIMARY KEY(batch_id,observation_id)
) STRICT;
CREATE INDEX idx_observation_context_members_source ON observation_context_members(observation_id,batch_id);
CREATE TABLE observation_context_cursors (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    app_instance_id TEXT NOT NULL,
    query_hash TEXT NOT NULL,
    source_watermark INTEGER NOT NULL,
    position_json TEXT NOT NULL,
    expires_us INTEGER NOT NULL
) STRICT;
CREATE INDEX idx_observation_context_cursor_expiry ON observation_context_cursors(expires_us);
CREATE TABLE observation_context_scans (
    scope_key TEXT PRIMARY KEY,
    scanned_us INTEGER NOT NULL
) STRICT;
CREATE VIEW observation_context_dependencies AS
SELECT tenant_id,source_id AS observation_id,target_type,target_id FROM resource_links WHERE source_type='observation'
UNION ALL
SELECT r.tenant_id,json_extract(j.value,'$.resource_id'),'episode',r.episode_id
    FROM episode_revisions r,json_each(r.observation_refs) j
    WHERE json_extract(j.value,'$.resource_type')='observation'
UNION ALL
SELECT r.tenant_id,json_extract(j.value,'$.resource_id'),'episode',r.episode_id
    FROM episode_revisions r,json_each(r.source_refs) j
    WHERE json_extract(j.value,'$.resource_type')='observation'
UNION ALL
SELECT r.tenant_id,json_extract(j.value,'$.resource_id'),'claim',r.claim_id
    FROM claim_revisions r,json_each(r.source_refs) j
    WHERE json_extract(j.value,'$.resource_type')='observation'
UNION ALL
SELECT r.tenant_id,json_extract(j.value,'$.resource_id'),'note',r.note_id
    FROM note_revisions r,json_each(r.source_refs) j
    WHERE json_extract(j.value,'$.resource_type')='observation'
UNION ALL
SELECT r.tenant_id,json_extract(j.value,'$.resource_id'),'task',r.task_id
    FROM task_revisions r,json_each(r.source_refs) j
    WHERE json_extract(j.value,'$.resource_type')='observation'
UNION ALL
SELECT r.tenant_id,json_extract(j.value,'$.resource_id'),'focus_item',r.item_id
    FROM focus_item_revisions r,json_each(r.source_refs) j
    WHERE json_extract(j.value,'$.resource_type')='observation'
UNION ALL
SELECT tenant_id,source_id,'claim',claim_id FROM claim_evidence
    WHERE source_type='observation' AND invalidated_us IS NULL
UNION ALL
SELECT tenant_id,source_id,'relation',relation_id FROM relation_evidence
    WHERE source_type='observation' AND invalidated_us IS NULL
UNION ALL
SELECT r.tenant_id,json_extract(j.value,'$.resource_id'),'persona_revision',r.revision_id
    FROM persona_revision_metadata r,json_each(r.source_refs_json) j
    WHERE json_extract(j.value,'$.resource_type')='observation' AND r.lifecycle_status!='revoked'
UNION ALL
SELECT r.tenant_id,json_extract(j.value,'$.resource_id'),'persona_proposal',r.id
    FROM persona_proposals r,json_each(r.evidence_refs_json) j
    WHERE json_extract(j.value,'$.resource_type')='observation' AND r.status IN ('proposed','approved','published')
UNION ALL
SELECT r.tenant_id,json_extract(j.value,'$.resource_id'),'persona_draft',r.id
    FROM persona_drafts r,json_each(r.source_refs_json) j
    WHERE json_extract(j.value,'$.resource_type')='observation' AND r.status!='discarded'
UNION ALL
SELECT r.tenant_id,json_extract(j.value,'$.resource_id'),'task_step',r.step_id
    FROM task_step_revisions r,json_each(r.completion_evidence_refs) j
    WHERE json_extract(j.value,'$.resource_type')='observation' AND 1=1;
CREATE TABLE observation_context_scan_positions (
    admission_key TEXT PRIMARY KEY,
    position_json TEXT NOT NULL
) STRICT;
