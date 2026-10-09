-- Host reports are isolated from memories, learning, persona and maintenance.
CREATE TABLE current_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    activity TEXT NOT NULL CHECK (length(activity) BETWEEN 1 AND 200),
    activity_updated_at TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json)),
    mood TEXT,
    mood_updated_at TEXT,
    started_at TEXT NOT NULL,
    start_time_basis TEXT NOT NULL CHECK (start_time_basis IN ('host', 'first_report')),
    updated_at TEXT NOT NULL,
    host TEXT,
    entry_id TEXT
);

-- Source labels are retained even if the host has not created an entry yet.
CREATE TABLE state_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT,
    entry_id TEXT,
    reported_at TEXT NOT NULL,
    method TEXT NOT NULL CHECK (method IN ('PUT', 'PATCH', 'DELETE')),
    action TEXT NOT NULL CHECK (action IN ('start', 'replace', 'update', 'heartbeat', 'end')),
    reported_json TEXT NOT NULL CHECK (json_valid(reported_json)),
    changes_json TEXT NOT NULL CHECK (json_valid(changes_json))
);

INSERT OR IGNORE INTO runtime_settings(key,value_json)
VALUES('state','{"stale_after_minutes":30}');
