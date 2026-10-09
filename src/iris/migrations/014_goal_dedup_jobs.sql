-- Purpose-specific durable review state; no duplicate learning task queue.
-- A lease permits recovery after a process exits between model call and commit.
CREATE TABLE goal_dedup_jobs (
    goal_id INTEGER PRIMARY KEY REFERENCES goals(id),
    state TEXT NOT NULL CHECK(state IN ('pending','running','done','cancelled')),
    method TEXT NOT NULL,
    input_revision INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    lease_until TEXT,
    lease_token TEXT,
    candidate_revisions_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX goal_dedup_jobs_due ON goal_dedup_jobs(state,available_at,goal_id);
CREATE INDEX goal_dedup_jobs_recover ON goal_dedup_jobs(state,lease_until)
    WHERE state='running';
-- Historical goals are not silently re-deduplicated by installing this feature.
