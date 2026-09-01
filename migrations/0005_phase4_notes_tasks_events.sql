-- iris: online_safe=true lock_ms=200 min_app=0.5.0 max_app= recovery=none
CREATE TABLE notes (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    space_group_id TEXT,
    space_id TEXT REFERENCES spaces (id),
    session_id TEXT REFERENCES sessions (id),
    scope_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (
        kind IN ('important', 'idea', 'follow_up', 'promise', 'question', 'observation')
    ),
    title TEXT NOT NULL CHECK (title != '' AND LENGTH(title) <= 500),
    status TEXT NOT NULL CHECK (
        status IN ('inbox', 'pinned', 'snoozed', 'archived', 'promoted', 'tombstoned')
    ),
    current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
    current_revision_id TEXT NOT NULL DEFAULT '',
    importance REAL NOT NULL CHECK (importance >= 0 AND importance <= 1),
    review_after_us INTEGER,
    snooze_until_us INTEGER,
    due_at_us INTEGER,
    content_hash TEXT NOT NULL,
    archived_us INTEGER,
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL,
    CHECK (scope_key != ''),
    CHECK (session_id IS NULL OR space_id IS NOT NULL),
    CHECK (archived_us IS NULL OR status IN ('archived', 'tombstoned'))
) STRICT;
CREATE INDEX idx_notes_scope ON notes (tenant_id, agent_id, status, kind);
CREATE INDEX idx_notes_review ON notes (tenant_id, agent_id, status, review_after_us);
CREATE INDEX idx_notes_content_hash ON notes (tenant_id, content_hash);

CREATE TABLE note_revisions (
    id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL REFERENCES notes (id),
    tenant_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    kind TEXT NOT NULL CHECK (
        kind IN ('important', 'idea', 'follow_up', 'promise', 'question', 'observation')
    ),
    title TEXT NOT NULL CHECK (title != '' AND LENGTH(title) <= 500),
    body TEXT NOT NULL DEFAULT '' CHECK (LENGTH(body) <= 20000),
    privacy_labels TEXT NOT NULL DEFAULT '[]',
    source_refs TEXT NOT NULL DEFAULT '[]',
    importance REAL NOT NULL CHECK (importance >= 0 AND importance <= 1),
    status TEXT NOT NULL CHECK (
        status IN ('inbox', 'pinned', 'snoozed', 'archived', 'promoted', 'tombstoned')
    ),
    review_after_us INTEGER,
    snooze_until_us INTEGER,
    due_at_us INTEGER,
    promotion_target_type TEXT CHECK (promotion_target_type IN ('task', 'claim', 'episode')),
    promotion_target_id TEXT,
    archived_us INTEGER,
    content_hash TEXT NOT NULL,
    created_us INTEGER NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    UNIQUE (note_id, revision)
) STRICT;
CREATE INDEX idx_note_revisions_note ON note_revisions (note_id, revision);

CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    space_group_id TEXT,
    space_id TEXT REFERENCES spaces (id),
    session_id TEXT REFERENCES sessions (id),
    scope_key TEXT NOT NULL,
    parent_task_id TEXT REFERENCES tasks (id),
    title TEXT NOT NULL CHECK (title != '' AND LENGTH(title) <= 500),
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('agent', 'joint', 'entity', 'space_group')),
    owner_entity_id TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('proposed', 'active', 'waiting', 'blocked', 'completed', 'cancelled', 'archived')
    ),
    priority INTEGER NOT NULL CHECK (priority >= 0 AND priority <= 9),
    next_action TEXT,
    due_at_us INTEGER,
    completed_us INTEGER,
    current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
    current_revision_id TEXT NOT NULL DEFAULT '',
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL,
    CHECK (scope_key != ''),
    CHECK (session_id IS NULL OR space_id IS NOT NULL),
    CHECK (owner_kind != 'entity' OR owner_entity_id IS NOT NULL),
    CHECK (completed_us IS NULL OR status IN ('completed', 'archived'))
) STRICT;
CREATE INDEX idx_tasks_scope ON tasks (tenant_id, agent_id, status);
CREATE INDEX idx_tasks_due ON tasks (tenant_id, agent_id, status, due_at_us);

CREATE TABLE task_revisions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks (id),
    tenant_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    title TEXT NOT NULL CHECK (title != '' AND LENGTH(title) <= 500),
    goal TEXT NOT NULL DEFAULT '' CHECK (LENGTH(goal) <= 8000),
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('agent', 'joint', 'entity', 'space_group')),
    owner_entity_id TEXT,
    privacy_labels TEXT NOT NULL DEFAULT '[]',
    source_refs TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (
        status IN ('proposed', 'active', 'waiting', 'blocked', 'completed', 'cancelled', 'archived')
    ),
    priority INTEGER NOT NULL CHECK (priority >= 0 AND priority <= 9),
    next_action TEXT,
    progress_note TEXT,
    due_at_us INTEGER,
    completed_us INTEGER,
    created_us INTEGER NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    UNIQUE (task_id, revision)
) STRICT;
CREATE INDEX idx_task_revisions_task ON task_revisions (task_id, revision);

CREATE TABLE task_steps (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks (id),
    tenant_id TEXT NOT NULL,
    stable_key TEXT NOT NULL CHECK (stable_key != '' AND LENGTH(stable_key) <= 256),
    title TEXT NOT NULL CHECK (title != '' AND LENGTH(title) <= 500),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    status TEXT NOT NULL CHECK (
        status IN (
            'pending', 'ready', 'in_progress', 'waiting', 'blocked', 'completed', 'skipped',
            'cancelled'
        )
    ),
    current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
    current_revision_id TEXT NOT NULL DEFAULT '',
    started_us INTEGER,
    completed_us INTEGER,
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL,
    UNIQUE (task_id, stable_key),
    CHECK (completed_us IS NULL OR status IN ('completed', 'skipped', 'cancelled'))
) STRICT;
CREATE INDEX idx_task_steps_task ON task_steps (task_id, ordinal);

CREATE TABLE task_step_revisions (
    id TEXT PRIMARY KEY,
    step_id TEXT NOT NULL REFERENCES task_steps (id),
    task_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    stable_key TEXT NOT NULL CHECK (stable_key != '' AND LENGTH(stable_key) <= 256),
    title TEXT NOT NULL CHECK (title != '' AND LENGTH(title) <= 500),
    description TEXT,
    privacy_labels TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (
        status IN (
            'pending', 'ready', 'in_progress', 'waiting', 'blocked', 'completed', 'skipped',
            'cancelled'
        )
    ),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    expected_effect TEXT,
    completion_evidence_refs TEXT NOT NULL DEFAULT '[]',
    started_us INTEGER,
    completed_us INTEGER,
    created_us INTEGER NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    UNIQUE (step_id, revision)
) STRICT;
CREATE INDEX idx_task_step_revisions_step ON task_step_revisions (step_id, revision);

CREATE TABLE task_dependencies (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks (id),
    tenant_id TEXT NOT NULL,
    predecessor_step_id TEXT NOT NULL REFERENCES task_steps (id),
    successor_step_id TEXT NOT NULL REFERENCES task_steps (id),
    condition TEXT NOT NULL CHECK (condition IN ('completed', 'completed_or_skipped')),
    current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
    current_revision_id TEXT NOT NULL DEFAULT '',
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL,
    UNIQUE (task_id, predecessor_step_id, successor_step_id),
    CHECK (predecessor_step_id != successor_step_id)
) STRICT;
CREATE INDEX idx_task_dependencies_successor ON task_dependencies (successor_step_id);

CREATE TABLE task_dependency_revisions (
    id TEXT PRIMARY KEY,
    dependency_id TEXT NOT NULL REFERENCES task_dependencies (id),
    task_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    predecessor_step_id TEXT NOT NULL,
    successor_step_id TEXT NOT NULL,
    condition TEXT NOT NULL CHECK (condition IN ('completed', 'completed_or_skipped')),
    created_us INTEGER NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    UNIQUE (dependency_id, revision)
) STRICT;

CREATE TABLE task_triggers (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks (id),
    task_step_id TEXT REFERENCES task_steps (id),
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (
        kind IN ('at_time', 'recurrence', 'observation_kind', 'state_condition', 'task_transition')
    ),
    timezone TEXT NOT NULL,
    catch_up_policy TEXT NOT NULL CHECK (catch_up_policy IN ('all', 'latest', 'coalesce', 'skip')),
    misfire_grace_us INTEGER NOT NULL CHECK (misfire_grace_us >= 0),
    max_occurrences_per_run INTEGER NOT NULL CHECK (max_occurrences_per_run >= 1),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    next_fire_at_us INTEGER,
    last_scan_us INTEGER,
    current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
    current_revision_id TEXT NOT NULL DEFAULT '',
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL,
    CHECK (kind != 'recurrence' OR timezone != ''),
    CHECK (task_step_id IS NULL OR task_step_id != '')
) STRICT;
CREATE INDEX idx_task_triggers_scan ON task_triggers (tenant_id, agent_id, enabled, next_fire_at_us);

CREATE TABLE task_trigger_revisions (
    id TEXT PRIMARY KEY,
    trigger_id TEXT NOT NULL REFERENCES task_triggers (id),
    task_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    kind TEXT NOT NULL CHECK (
        kind IN ('at_time', 'recurrence', 'observation_kind', 'state_condition', 'task_transition')
    ),
    task_step_id TEXT,
    schedule_spec TEXT,
    condition_spec TEXT,
    timezone TEXT NOT NULL,
    catch_up_policy TEXT NOT NULL CHECK (catch_up_policy IN ('all', 'latest', 'coalesce', 'skip')),
    misfire_grace_us INTEGER NOT NULL CHECK (misfire_grace_us >= 0),
    max_occurrences_per_run INTEGER NOT NULL CHECK (max_occurrences_per_run >= 1),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    created_us INTEGER NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    UNIQUE (trigger_id, revision)
) STRICT;

CREATE TABLE task_trigger_occurrences (
    id TEXT PRIMARY KEY,
    trigger_id TEXT NOT NULL REFERENCES task_triggers (id),
    trigger_revision INTEGER NOT NULL CHECK (trigger_revision >= 1),
    tenant_id TEXT NOT NULL,
    scheduled_at_us INTEGER NOT NULL,
    occurrence_key TEXT NOT NULL CHECK (occurrence_key != ''),
    status TEXT NOT NULL CHECK (status IN ('enqueued', 'completed', 'skipped')),
    reason_code TEXT,
    cognitive_event_id TEXT,
    created_us INTEGER NOT NULL,
    UNIQUE (trigger_id, trigger_revision, scheduled_at_us, occurrence_key)
) STRICT;
CREATE INDEX idx_task_trigger_occurrences_trigger
    ON task_trigger_occurrences (trigger_id, created_us);

CREATE TABLE cognitive_events (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants (id),
    agent_id TEXT NOT NULL REFERENCES agents (id),
    space_group_id TEXT,
    space_id TEXT REFERENCES spaces (id),
    session_id TEXT REFERENCES sessions (id),
    scope_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind != '' AND LENGTH(kind) <= 128),
    object_type TEXT NOT NULL CHECK (object_type != ''),
    object_id TEXT NOT NULL CHECK (object_id != ''),
    occurrence_id TEXT,
    scheduled_at_us INTEGER NOT NULL,
    deliver_after_us INTEGER NOT NULL,
    expires_us INTEGER,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'delivered', 'acknowledged', 'expired', 'cancelled')
    ),
    delivery_target TEXT,
    delivery_attempts INTEGER NOT NULL CHECK (delivery_attempts >= 0),
    last_delivery_us INTEGER,
    delivered_lease_id TEXT,
    delivered_lease_epoch INTEGER,
    ack_id TEXT,
    acknowledged_us INTEGER,
    summary_of_count INTEGER NOT NULL DEFAULT 0 CHECK (summary_of_count >= 0),
    current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
    current_revision_id TEXT NOT NULL DEFAULT '',
    created_us INTEGER NOT NULL,
    updated_us INTEGER NOT NULL,
    CHECK (scope_key != ''),
    CHECK (session_id IS NULL OR space_id IS NOT NULL),
    CHECK (acknowledged_us IS NULL OR status IN ('acknowledged', 'expired', 'cancelled')),
    CHECK (status != 'acknowledged' OR ack_id IS NOT NULL),
    CHECK (status IN ('pending', 'delivered') OR delivered_lease_id IS NULL)
) STRICT;
CREATE INDEX idx_cognitive_events_pull
    ON cognitive_events (tenant_id, agent_id, status, deliver_after_us);
CREATE INDEX idx_cognitive_events_expiry ON cognitive_events (tenant_id, agent_id, status, expires_us);

CREATE TABLE cognitive_event_revisions (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES cognitive_events (id),
    tenant_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'delivered', 'acknowledged', 'expired', 'cancelled')
    ),
    delivery_attempts INTEGER NOT NULL CHECK (delivery_attempts >= 0),
    last_delivery_us INTEGER,
    delivered_lease_id TEXT,
    delivered_lease_epoch INTEGER,
    ack_id TEXT,
    acknowledged_us INTEGER,
    reason_code TEXT,
    created_us INTEGER NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    UNIQUE (event_id, revision)
) STRICT;
CREATE INDEX idx_cognitive_event_revisions_event ON cognitive_event_revisions (event_id, revision);
