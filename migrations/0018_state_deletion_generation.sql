-- iris: online_safe=false lock_ms=200 min_app=0.13.0 max_app= recovery=backup bootstrap_safe=true
-- Preserve the original rows and revisions; only live natural keys are unique.
CREATE TABLE state_records_v18 (
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
    deleted_us INTEGER,
    CHECK (namespace != '' AND LENGTH(namespace) <= 128),
    CHECK (key != '' AND LENGTH(key) <= 256),
    CHECK (session_id IS NULL OR space_id IS NOT NULL)
) STRICT;

CREATE TABLE state_record_revisions_v18 (
    id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL REFERENCES state_records_v18 (id),
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

INSERT INTO state_records_v18 (
    id,tenant_id,agent_id,space_group_id,space_id,session_id,scope_key,namespace,key,
    current_revision,current_revision_id,created_us,updated_us,deleted_us
)
SELECT r.id,r.tenant_id,r.agent_id,r.space_group_id,r.space_id,r.session_id,
    r.scope_key,r.namespace,r.key,r.current_revision,r.current_revision_id,r.created_us,r.updated_us,
    (SELECT t.created_us FROM resource_tombstones t WHERE t.tenant_id=r.tenant_id
     AND t.resource_type='state_record' AND t.resource_id=r.id)
FROM state_records r;
INSERT INTO state_record_revisions_v18 SELECT * FROM state_record_revisions;
DROP TABLE state_record_revisions;
DROP TABLE state_records;
ALTER TABLE state_records_v18 RENAME TO state_records;
ALTER TABLE state_record_revisions_v18 RENAME TO state_record_revisions;
CREATE UNIQUE INDEX idx_state_records_live_key ON state_records (scope_key,namespace,key)
    WHERE deleted_us IS NULL;
CREATE INDEX idx_state_records_key_history ON state_records (scope_key,namespace,key,created_us DESC,id DESC);
CREATE INDEX idx_state_records_scope ON state_records (tenant_id,agent_id,namespace);
CREATE INDEX idx_console_state_records_created ON state_records (tenant_id,created_us DESC,id DESC);
CREATE INDEX idx_state_revisions_record ON state_record_revisions (record_id,revision);

-- This derived flag only releases the unique live key; the Tombstone remains
-- the authority for every current/history read and deletion-ledger replay.
CREATE TRIGGER state_tombstone_releases_live_key
AFTER INSERT ON resource_tombstones
WHEN NEW.resource_type='state_record'
BEGIN
    UPDATE state_records SET deleted_us=NEW.created_us
    WHERE tenant_id=NEW.tenant_id AND id=NEW.resource_id;
END;

CREATE TRIGGER state_record_id_not_deleted
BEFORE INSERT ON state_records
WHEN EXISTS (
    SELECT 1 FROM resource_tombstones t WHERE t.tenant_id=NEW.tenant_id
    AND t.resource_type='state_record' AND t.resource_id=NEW.id
)
BEGIN
    SELECT RAISE(ABORT, 'state record id is deleted');
END;
