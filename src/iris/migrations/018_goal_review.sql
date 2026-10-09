-- Do not invent snapshots for revisions predating this migration.
ALTER TABLE goals ADD COLUMN history_missing_through INTEGER;
UPDATE goals SET history_missing_through=revision;
CREATE INDEX goals_merged_into ON goals(merged_into) WHERE merged_into IS NOT NULL;
CREATE TABLE goal_revisions (
    id INTEGER PRIMARY KEY,
    goal_id INTEGER NOT NULL REFERENCES goals(id),
    revision_before INTEGER,
    revision_after INTEGER NOT NULL,
    action TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX goal_revisions_page ON goal_revisions(goal_id,id DESC);
CREATE TABLE goal_basis_annotations (
    id INTEGER PRIMARY KEY,
    goal_id INTEGER NOT NULL REFERENCES goals(id),
    memory_id INTEGER NOT NULL,
    basis_revision INTEGER NOT NULL,
    event_key TEXT NOT NULL,
    observed_revision INTEGER,
    observed_lifecycle TEXT,
    observed_purged INTEGER NOT NULL DEFAULT 0,
    observed_merged_into INTEGER,
    changes_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','cleared','resolved','superseded')),
    created_at TEXT NOT NULL,
    ended_at TEXT,
    ended_by TEXT,
    end_reason TEXT,
    UNIQUE(goal_id,memory_id,basis_revision,event_key)
);
CREATE INDEX goal_basis_active ON goal_basis_annotations(goal_id,status,id);
