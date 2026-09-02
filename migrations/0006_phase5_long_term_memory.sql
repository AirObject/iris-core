-- iris: online_safe=true lock_ms=200 min_app=0.6.0 max_app= recovery=none
-- Phase 5: explicit long-term memory (§13, §19). Current tables + immutable
-- revision tables; bi-temporal columns live on claim revisions
-- (recorded_at_us / superseded_at_us write-once). The active-claim evidence
-- invariant and the live-claim dedup identity are expressed as DB constraints
-- (denormalized evidence_count + partial unique index); state-machine legality
-- beyond those stays in the domain layer.
CREATE TABLE episodes (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    space_group_id TEXT,
    space_id TEXT REFERENCES spaces (id),
    session_id TEXT REFERENCES sessions (id),
    scope_key TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '' CHECK (LENGTH(title) <= 500),
    status TEXT NOT NULL CHECK (status IN ('open', 'sealed', 'superseded', 'archived', 'tombstoned')),
    importance REAL NOT NULL CHECK (importance >= 0 AND importance <= 1),
    started_at_us INTEGER,
    ended_at_us INTEGER,
    extractor_version TEXT,
    current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
    current_revision_id TEXT NOT NULL DEFAULT '',
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL,
    CHECK (scope_key != ''),
    CHECK (session_id IS NULL OR space_id IS NOT NULL),
    CHECK (ended_at_us IS NULL OR started_at_us IS NULL OR ended_at_us >= started_at_us)
) STRICT;
CREATE INDEX idx_episodes_scope ON episodes (tenant_id, agent_id, status);
CREATE INDEX idx_episodes_session ON episodes (tenant_id, session_id);

CREATE TABLE episode_revisions (
    id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES episodes (id),
    tenant_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    title TEXT NOT NULL DEFAULT '' CHECK (LENGTH(title) <= 500),
    summary TEXT NOT NULL DEFAULT '' CHECK (LENGTH(summary) <= 20000),
    participant_entity_ids TEXT NOT NULL DEFAULT '[]',
    observation_refs TEXT NOT NULL DEFAULT '[]',
    privacy_labels TEXT NOT NULL DEFAULT '[]',
    source_refs TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('open', 'sealed', 'superseded', 'archived', 'tombstoned')),
    importance REAL NOT NULL CHECK (importance >= 0 AND importance <= 1),
    valence REAL CHECK (valence IS NULL OR (valence >= -1 AND valence <= 1)),
    arousal REAL CHECK (arousal IS NULL OR (arousal >= 0 AND arousal <= 1)),
    started_at_us INTEGER,
    ended_at_us INTEGER,
    extractor_version TEXT,
    content_hash TEXT NOT NULL,
    created_us INTEGER NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    UNIQUE (episode_id, revision)
) STRICT;
CREATE INDEX idx_episode_revisions_episode ON episode_revisions (episode_id, revision);

CREATE TABLE claims (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    space_group_id TEXT,
    space_id TEXT REFERENCES spaces (id),
    session_id TEXT REFERENCES sessions (id),
    scope_key TEXT NOT NULL,
    subject_entity_id TEXT NOT NULL REFERENCES entities (id),
    predicate TEXT NOT NULL CHECK (predicate != '' AND LENGTH(predicate) <= 200),
    category TEXT NOT NULL CHECK (
        category IN (
            'identity', 'preference', 'relationship', 'fact', 'community', 'procedure',
            'self_narrative'
        )
    ),
    status TEXT NOT NULL CHECK (
        status IN ('active', 'disputed', 'superseded', 'retracted', 'expired', 'archived',
                   'tombstoned')
    ),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    importance REAL NOT NULL CHECK (importance >= 0 AND importance <= 1),
    accessibility REAL NOT NULL CHECK (accessibility >= 0 AND accessibility <= 1),
    source_authority TEXT NOT NULL CHECK (
        source_authority IN (
            'agent_inference', 'extracted', 'user_statement', 'platform_verified',
            'admin_confirmed', 'explicit_correction'
        )
    ),
    valid_from_us INTEGER,
    valid_until_us INTEGER,
    evidence_count INTEGER NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
    dedup_key TEXT NOT NULL,
    history_available_from_us INTEGER NOT NULL DEFAULT 0,
    recorded_at_us INTEGER NOT NULL,
    superseded_at_us INTEGER,
    extractor_version TEXT,
    current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
    current_revision_id TEXT NOT NULL DEFAULT '',
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL,
    CHECK (scope_key != ''),
    CHECK (session_id IS NULL OR space_id IS NOT NULL),
    CHECK (status != 'active' OR evidence_count >= 1),
    CHECK (valid_until_us IS NULL OR valid_from_us IS NULL OR valid_until_us > valid_from_us),
    CHECK (superseded_at_us IS NULL OR superseded_at_us > recorded_at_us)
) STRICT;
-- Exact dedup identity among LIVE claims only (§13.2): a tombstoned claim
-- never blocks a later, authorized re-remember of the same fact as a NEW row.
CREATE UNIQUE INDEX idx_claims_live_dedup
    ON claims (tenant_id, dedup_key) WHERE status != 'tombstoned';
CREATE INDEX idx_claims_scope
    ON claims (tenant_id, agent_id, status, subject_entity_id, predicate);
CREATE INDEX idx_claims_scope_updated ON claims (tenant_id, agent_id, updated_us);
CREATE INDEX idx_claims_valid_time ON claims (tenant_id, agent_id, valid_until_us);

CREATE TABLE claim_revisions (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims (id),
    tenant_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    subject_entity_id TEXT NOT NULL,
    predicate TEXT NOT NULL,
    value_json TEXT NOT NULL DEFAULT 'null' CHECK (LENGTH(value_json) <= 65536),
    canonical_text TEXT NOT NULL CHECK (canonical_text != '' AND LENGTH(canonical_text) <= 20000),
    category TEXT NOT NULL CHECK (
        category IN (
            'identity', 'preference', 'relationship', 'fact', 'community', 'procedure',
            'self_narrative'
        )
    ),
    privacy_labels TEXT NOT NULL DEFAULT '[]',
    source_refs TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (
        status IN ('active', 'disputed', 'superseded', 'retracted', 'expired', 'archived',
                   'tombstoned')
    ),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    importance REAL NOT NULL CHECK (importance >= 0 AND importance <= 1),
    accessibility REAL NOT NULL CHECK (accessibility >= 0 AND accessibility <= 1),
    source_authority TEXT NOT NULL,
    valid_from_us INTEGER,
    valid_until_us INTEGER,
    recorded_at_us INTEGER NOT NULL,
    superseded_at_us INTEGER,
    extractor_version TEXT,
    content_hash TEXT NOT NULL,
    created_us INTEGER NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    UNIQUE (claim_id, revision)
) STRICT;
CREATE INDEX idx_claim_revisions_claim ON claim_revisions (claim_id, revision);
CREATE INDEX idx_claim_revisions_system_time
    ON claim_revisions (claim_id, recorded_at_us, superseded_at_us);

CREATE TABLE claim_evidence (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims (id),
    tenant_id TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('observation', 'artifact', 'episode', 'claim', 'note')),
    source_id TEXT NOT NULL,
    source_revision INTEGER,
    relation TEXT NOT NULL CHECK (relation IN ('supports', 'contradicts', 'corrects')),
    source_authority TEXT NOT NULL CHECK (
        source_authority IN (
            'agent_inference', 'extracted', 'user_statement', 'platform_verified',
            'admin_confirmed', 'explicit_correction'
        )
    ),
    evidence_span TEXT CHECK (evidence_span IS NULL OR LENGTH(evidence_span) <= 4000),
    recorded_at_us INTEGER NOT NULL,
    invalidated_us INTEGER,
    created_by TEXT NOT NULL DEFAULT ''
) STRICT;
CREATE UNIQUE INDEX idx_claim_evidence_identity
    ON claim_evidence (claim_id, source_type, source_id, relation, ifnull(source_revision, 0));
CREATE INDEX idx_claim_evidence_source ON claim_evidence (tenant_id, source_type, source_id);

CREATE TABLE relations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    space_group_id TEXT,
    space_id TEXT REFERENCES spaces (id),
    session_id TEXT REFERENCES sessions (id),
    scope_key TEXT NOT NULL,
    source_entity_id TEXT NOT NULL REFERENCES entities (id),
    relation_type TEXT NOT NULL CHECK (relation_type != '' AND LENGTH(relation_type) <= 200),
    target_entity_id TEXT NOT NULL REFERENCES entities (id),
    status TEXT NOT NULL CHECK (
        status IN ('active', 'disputed', 'superseded', 'retracted', 'archived', 'tombstoned')
    ),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    importance REAL NOT NULL CHECK (importance >= 0 AND importance <= 1),
    accessibility REAL NOT NULL CHECK (accessibility >= 0 AND accessibility <= 1),
    valid_from_us INTEGER,
    valid_until_us INTEGER,
    evidence_count INTEGER NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
    current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
    current_revision_id TEXT NOT NULL DEFAULT '',
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL,
    CHECK (scope_key != ''),
    CHECK (session_id IS NULL OR space_id IS NOT NULL),
    CHECK (source_entity_id != target_entity_id),
    CHECK (status != 'active' OR evidence_count >= 1)
) STRICT;
CREATE INDEX idx_relations_scope
    ON relations (tenant_id, agent_id, status, source_entity_id);
CREATE INDEX idx_relations_target ON relations (tenant_id, target_entity_id);

CREATE TABLE relation_evidence (
    id TEXT PRIMARY KEY,
    relation_id TEXT NOT NULL REFERENCES relations (id),
    tenant_id TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('observation', 'artifact', 'episode', 'claim', 'note')),
    source_id TEXT NOT NULL,
    source_revision INTEGER,
    relation TEXT NOT NULL CHECK (relation IN ('supports', 'contradicts', 'corrects')),
    source_authority TEXT NOT NULL CHECK (
        source_authority IN (
            'agent_inference', 'extracted', 'user_statement', 'platform_verified',
            'admin_confirmed', 'explicit_correction'
        )
    ),
    evidence_span TEXT CHECK (evidence_span IS NULL OR LENGTH(evidence_span) <= 4000),
    recorded_at_us INTEGER NOT NULL,
    invalidated_us INTEGER,
    created_by TEXT NOT NULL DEFAULT ''
) STRICT;
CREATE UNIQUE INDEX idx_relation_evidence_identity
    ON relation_evidence (relation_id, source_type, source_id, relation, ifnull(source_revision, 0));
CREATE INDEX idx_relation_evidence_source ON relation_evidence (tenant_id, source_type, source_id);

CREATE TABLE relation_revisions (
    id TEXT PRIMARY KEY,
    relation_id TEXT NOT NULL REFERENCES relations (id),
    tenant_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    source_entity_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    target_entity_id TEXT NOT NULL,
    privacy_labels TEXT NOT NULL DEFAULT '[]',
    evidence_refs TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (
        status IN ('active', 'disputed', 'superseded', 'retracted', 'archived', 'tombstoned')
    ),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    importance REAL NOT NULL CHECK (importance >= 0 AND importance <= 1),
    accessibility REAL NOT NULL CHECK (accessibility >= 0 AND accessibility <= 1),
    valid_from_us INTEGER,
    valid_until_us INTEGER,
    superseded_at_us INTEGER,
    content_hash TEXT NOT NULL,
    created_us INTEGER NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    UNIQUE (relation_id, revision)
) STRICT;
CREATE INDEX idx_relation_revisions_relation ON relation_revisions (relation_id, revision);

CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    space_group_id TEXT,
    space_id TEXT REFERENCES spaces (id),
    session_id TEXT REFERENCES sessions (id),
    scope_key TEXT NOT NULL,
    media_type TEXT NOT NULL CHECK (media_type != '' AND LENGTH(media_type) <= 255),
    storage_kind TEXT NOT NULL CHECK (storage_kind IN ('inline', 'local_blob', 'external_ref')),
    locator TEXT NOT NULL,
    content BLOB,
    content_hash TEXT NOT NULL CHECK (LENGTH(content_hash) = 64),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    privacy_labels TEXT NOT NULL DEFAULT '[]',
    source_ref TEXT NOT NULL DEFAULT 'null',
    status TEXT NOT NULL CHECK (status IN ('active', 'archived', 'tombstoned')),
    refcount INTEGER NOT NULL DEFAULT 0 CHECK (refcount >= 0),
    privacy_key TEXT NOT NULL DEFAULT '',
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL,
    CHECK (scope_key != ''),
    CHECK (session_id IS NULL OR space_id IS NOT NULL),
    CHECK (storage_kind != 'inline' OR status = 'tombstoned' OR content IS NOT NULL),
    CHECK (storage_kind != 'inline' OR size_bytes <= 262144),
    CHECK (storage_kind != 'local_blob' OR content IS NULL),
    CHECK (storage_kind != 'external_ref' OR content IS NULL)
) STRICT;
CREATE INDEX idx_artifacts_scope ON artifacts (tenant_id, agent_id, status);
-- Content dedup identity is the full scope envelope (scope_key), never the
-- tenant: identical bytes in two spaces are two artifacts (§13.6, ADR-0013 §5).
-- privacy_key (the repo-canonical JSON encoding of the privacy label SET) is
-- part of the identity: identical bytes under different privacy labels are two
-- artifacts — a restricted ingest must never alias a public row (ADR-0013 §5).
-- JSON encoding, not a separator join: a label may itself contain the
-- separator, and ["a\x1fb"] must never collide with ["a", "b"].
CREATE UNIQUE INDEX idx_artifacts_live_content
    ON artifacts (tenant_id, scope_key, content_hash, storage_kind, privacy_key)
    WHERE status = 'active';

CREATE TABLE retention_policies (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    resource_type TEXT NOT NULL CHECK (
        resource_type IN ('claim', 'note', 'episode', 'relation', 'observation')
    ),
    action TEXT NOT NULL CHECK (action IN ('decay', 'archive', 'delete')),
    privacy_label TEXT,
    threshold_days INTEGER NOT NULL CHECK (threshold_days >= 1),
    policy_version INTEGER NOT NULL CHECK (policy_version >= 1),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    CHECK (action != 'delete' OR privacy_label IS NOT NULL)
) STRICT;
CREATE UNIQUE INDEX idx_retention_policies_identity
    ON retention_policies (tenant_id, resource_type, action, ifnull(privacy_label, ''));
CREATE INDEX idx_retention_policies_tenant ON retention_policies (tenant_id, enabled);
CREATE TABLE legal_holds (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    space_id TEXT,
    session_id TEXT,
    subject_entity_id TEXT,
    agent_id TEXT,
    reason_code TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_us INTEGER NOT NULL,
    released_us INTEGER,
    CHECK (
        space_id IS NOT NULL OR session_id IS NOT NULL OR subject_entity_id IS NOT NULL
    ),
    CHECK (session_id IS NULL OR space_id IS NOT NULL)
) STRICT;
CREATE INDEX idx_legal_holds_tenant ON legal_holds (tenant_id, released_us);

CREATE TABLE forget_requests (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    selector_key TEXT NOT NULL,
    selector_json TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    app_instance_id TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL DEFAULT '',
    erase_content INTEGER NOT NULL DEFAULT 0 CHECK (erase_content IN (0, 1)),
    created_us INTEGER NOT NULL,
    tombstone_seq_lo INTEGER NOT NULL CHECK (tombstone_seq_lo >= 0),
    tombstone_seq_hi INTEGER NOT NULL CHECK (tombstone_seq_hi >= tombstone_seq_lo),
    target_count INTEGER NOT NULL CHECK (target_count >= 0),
    erased_count INTEGER NOT NULL CHECK (erased_count >= 0),
    protected_skipped INTEGER NOT NULL CHECK (protected_skipped >= 0),
    held_skipped INTEGER NOT NULL CHECK (held_skipped >= 0),
    -- Request identity is the full logical tuple, not (selector, instant):
    -- two forgets from different app instances, or with different idempotency
    -- keys, reasons or erasure modes, landing in the same microsecond are TWO
    -- requests (ADR-0013 §7). app_instance_id mirrors the idempotency
    -- namespace (tenant, app, operation, key): the ledger must distinguish
    -- exactly what the cache distinguishes.
    UNIQUE (
        tenant_id, app_instance_id, selector_key, created_us,
        idempotency_key, reason_code, erase_content
    )
) STRICT;
CREATE INDEX idx_forget_requests_tenant ON forget_requests (tenant_id, created_us);
