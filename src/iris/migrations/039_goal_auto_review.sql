-- G line (full-auto stage): daily maintenance reviews learning-created goals
-- and judges "possible duplicate" pairs. Both switches default off in code;
-- this migration only adds durable judgment records, no settings or state.

-- G1: one row per completed model judgment. The fingerprint is the goal
-- revision plus the newest message ID in the material; an unchanged fingerprint
-- is never sent to the model again. Evidence IDs are JSON (no message FK), so
-- message retention keeps working; a cleaned message shows as unavailable.
-- The model's free-text reason is never stored: it may quote message prose,
-- which privacy purges could not then reach.
CREATE TABLE goal_auto_reviews (
    id INTEGER PRIMARY KEY,
    goal_id INTEGER NOT NULL REFERENCES goals(id),
    goal_revision INTEGER NOT NULL,
    latest_message_id INTEGER NOT NULL,
    run_id INTEGER,
    decision TEXT NOT NULL CHECK(decision IN ('completed','abandoned','open','invalid')),
    outcome TEXT NOT NULL CHECK(outcome IN ('closed','kept_open')),
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(evidence_json)),
    material_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(material_ids_json)),
    prompt_version TEXT NOT NULL,
    revision_after INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(goal_id,goal_revision,latest_message_id)
);
CREATE INDEX goal_auto_reviews_goal ON goal_auto_reviews(goal_id,id);
CREATE INDEX goal_auto_reviews_closed ON goal_auto_reviews(outcome,id) WHERE outcome='closed';

-- An administrator reopen sticks: afterwards only messages newer than this
-- watermark can serve as candidates, so the same evidence cannot re-close it.
ALTER TABLE goals ADD COLUMN auto_review_after_message_id INTEGER;

-- G2: one row per completed pair judgment with a fingerprint of every goal
-- revision and the newest source message in the shared dedup material.
-- Failures and timeouts are not recorded here; the pair is retried later.
CREATE TABLE goal_duplicate_judgments (
    id INTEGER PRIMARY KEY,
    goal_a INTEGER NOT NULL REFERENCES goals(id),
    goal_b INTEGER NOT NULL REFERENCES goals(id),
    fingerprint TEXT NOT NULL,
    run_id INTEGER,
    method TEXT NOT NULL,
    verdict TEXT CHECK(verdict IN ('same','different','uncertain')),
    outcome TEXT NOT NULL CHECK(outcome IN ('merged','separated','kept')),
    reason TEXT NOT NULL,
    target_id INTEGER,
    created_at TEXT NOT NULL,
    CHECK(goal_a<goal_b),
    UNIQUE(goal_a,goal_b,fingerprint)
);
CREATE INDEX goal_duplicate_judgments_pair ON goal_duplicate_judgments(goal_a,goal_b,id);
CREATE INDEX goal_duplicate_judgments_outcome ON goal_duplicate_judgments(outcome,id);
