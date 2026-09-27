CREATE TABLE entries (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    kind TEXT NOT NULL,
    pace TEXT NOT NULL DEFAULT 'standard',
    last_message_at TEXT
);
CREATE TABLE subjects (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('self','person')),
    name TEXT NOT NULL,
    parent_id TEXT REFERENCES subjects(id),
    created_at TEXT NOT NULL
);
CREATE TABLE subject_aliases (
    subject_id TEXT NOT NULL REFERENCES subjects(id),
    alias TEXT NOT NULL,
    PRIMARY KEY (subject_id, alias)
);
CREATE TABLE platform_identities (
    subject_id TEXT NOT NULL REFERENCES subjects(id),
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    PRIMARY KEY (platform, account_id)
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES entries(id),
    kind TEXT NOT NULL CHECK(kind IN ('message','self_output','action_result','event')),
    sender_subject_id TEXT NOT NULL REFERENCES subjects(id),
    scene_identity TEXT,
    content TEXT NOT NULL,
    quote_author_subject_id TEXT REFERENCES subjects(id),
    quote_content TEXT,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    learning_state TEXT NOT NULL DEFAULT 'pending',
    batch_id INTEGER,
    UNIQUE(entry_id, dedupe_key)
);
CREATE INDEX messages_queue ON messages(entry_id, learning_state, id);
CREATE TABLE batches (
    id INTEGER PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES entries(id),
    history_ids TEXT NOT NULL,
    target_ids TEXT NOT NULL,
    future_ids TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    prompt_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    result_json TEXT NOT NULL DEFAULT '{}',
    last_error TEXT
);
CREATE INDEX batches_by_entry ON batches(entry_id, id);
CREATE TABLE batch_attempts (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    number INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    raw_output TEXT,
    repair_output TEXT,
    parse_status TEXT NOT NULL,
    error TEXT,
    duration_ms INTEGER NOT NULL,
    UNIQUE(batch_id, number)
);
CREATE TABLE memories (
    id INTEGER PRIMARY KEY,
    content TEXT NOT NULL,
    kind TEXT NOT NULL,
    speaker_subject_id TEXT NOT NULL REFERENCES subjects(id),
    stance TEXT NOT NULL,
    belief INTEGER NOT NULL,
    importance INTEGER NOT NULL,
    retention INTEGER NOT NULL,
    event_time TEXT,
    lifecycle TEXT NOT NULL DEFAULT 'active',
    forgotten_at TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    entry_id TEXT REFERENCES entries(id),
    world TEXT NOT NULL DEFAULT 'real',
    embedding BLOB,
    embedding_model TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    first_confirmed_at TEXT NOT NULL,
    last_confirmed_at TEXT NOT NULL
);
CREATE TABLE memory_subjects (
    memory_id INTEGER NOT NULL REFERENCES memories(id),
    subject_id TEXT NOT NULL REFERENCES subjects(id),
    PRIMARY KEY(memory_id, subject_id)
);
CREATE TABLE memory_tags (
    memory_id INTEGER NOT NULL REFERENCES memories(id),
    tag TEXT NOT NULL,
    PRIMARY KEY(memory_id, tag)
);
CREATE TABLE sources (
    id INTEGER PRIMARY KEY,
    memory_id INTEGER NOT NULL REFERENCES memories(id),
    kind TEXT NOT NULL CHECK(kind IN ('message','memory','initial_setting')),
    message_id INTEGER REFERENCES messages(id),
    source_memory_id INTEGER REFERENCES memories(id),
    source_revision INTEGER,
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(memory_id, kind, message_id, source_memory_id)
);
CREATE TABLE memory_revisions (
    id INTEGER PRIMARY KEY,
    memory_id INTEGER NOT NULL REFERENCES memories(id),
    revision_before INTEGER NOT NULL,
    revision_after INTEGER NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE subject_links (
    id INTEGER PRIMARY KEY,
    subject_a TEXT NOT NULL REFERENCES subjects(id),
    subject_b TEXT NOT NULL REFERENCES subjects(id),
    kind TEXT NOT NULL CHECK(kind IN ('same_as','roleplay')),
    belief INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'possible',
    world TEXT,
    source_message_id INTEGER REFERENCES messages(id),
    created_at TEXT NOT NULL,
    UNIQUE(subject_a, subject_b, kind)
);
CREATE TABLE goals (
    id INTEGER PRIMARY KEY,
    content TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('normal','question')),
    origin TEXT NOT NULL DEFAULT 'internal',
    state TEXT NOT NULL DEFAULT 'open',
    deadline TEXT,
    created_at TEXT NOT NULL,
    merged_into INTEGER REFERENCES goals(id)
);
CREATE TABLE goal_sources (
    goal_id INTEGER NOT NULL REFERENCES goals(id),
    message_id INTEGER NOT NULL REFERENCES messages(id),
    PRIMARY KEY(goal_id, message_id)
);
CREATE TABLE memory_gaps (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL UNIQUE REFERENCES batches(id),
    entry_id TEXT NOT NULL REFERENCES entries(id),
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE model_calls (
    id INTEGER PRIMARY KEY,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    reasoning_tokens INTEGER,
    result_category TEXT NOT NULL,
    error_summary TEXT,
    input_sensitive INTEGER,
    output_sensitive INTEGER,
    status_code INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE runtime_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
CREATE TABLE persona_versions (
    id INTEGER PRIMARY KEY,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 1
);
