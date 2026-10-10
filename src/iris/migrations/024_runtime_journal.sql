-- Observational metadata only. No request, model output, error text or credentials.
CREATE TABLE runtime_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT,
    occurred_at TEXT NOT NULL,
    event TEXT NOT NULL CHECK(event IN ('process_started','process_stopped','heartbeat','model_state',
        'maintenance_scheduled','maintenance_started','maintenance_completed','maintenance_skipped',
        'maintenance_failed','reminder_resolved','prepare')),
    entry_id TEXT,
    model_kind TEXT,
    configured INTEGER CHECK(configured IN (0,1)),
    previous_state TEXT,
    state TEXT,
    reason TEXT,
    run_id INTEGER,
    phase TEXT,
    scheduled_at TEXT,
    duration_ms REAL CHECK(duration_ms >= 0),
    result TEXT,
    plan_id INTEGER,
    notification_id INTEGER
);
CREATE INDEX runtime_events_time ON runtime_events(occurred_at,id);
CREATE INDEX runtime_events_kind ON runtime_events(event,occurred_at,id);
CREATE TABLE runtime_backlog (
    heartbeat_id INTEGER NOT NULL REFERENCES runtime_events(id) ON DELETE CASCADE,
    entry_id TEXT NOT NULL,
    pending_messages INTEGER NOT NULL,
    waiting_batches INTEGER NOT NULL,
    running_batches INTEGER NOT NULL,
    oldest_wait_seconds REAL,
    memory_gaps INTEGER NOT NULL,
    PRIMARY KEY(heartbeat_id,entry_id)
);
-- Bound heartbeat reads to outstanding work, independent of retained prose volume.
CREATE INDEX runtime_pending_messages ON messages(entry_id,received_at)
    WHERE learning_state IN ('pending','batched');
CREATE INDEX runtime_pending_batches ON batches(entry_id,state) WHERE state IN ('waiting','running');
CREATE INDEX runtime_gaps_entry ON memory_gaps(entry_id);
