-- Keep goal IDs and evidence intact. Unknown/failed legacy states remain open:
-- expiry/failure must never implicitly abandon a goal.
UPDATE goals SET state=CASE
    WHEN state IN ('completed','done') THEN 'completed'
    WHEN state IN ('abandoned','cancelled','canceled') THEN 'abandoned'
    ELSE 'open' END;
UPDATE goals SET origin='internal' WHERE origin NOT IN ('internal','host','admin');
UPDATE goals SET deadline=NULL WHERE kind='question';
ALTER TABLE goals ADD COLUMN reminder_minutes INTEGER CHECK(reminder_minutes BETWEEN 0 AND 525600);
ALTER TABLE goals ADD COLUMN host_key TEXT;
ALTER TABLE goals ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE goals ADD COLUMN updated_at TEXT;
ALTER TABLE goals ADD COLUMN closed_at TEXT;
ALTER TABLE goals ADD COLUMN closed_by TEXT;
ALTER TABLE goals ADD COLUMN deadline_at TEXT;
ALTER TABLE goals ADD COLUMN schedule_initialized INTEGER NOT NULL DEFAULT 0;
ALTER TABLE goals ADD COLUMN last_overdue_date TEXT;
ALTER TABLE goals ADD COLUMN receipt_json TEXT;
UPDATE goals SET updated_at=created_at;
CREATE UNIQUE INDEX goals_host_key ON goals(host_key) WHERE host_key IS NOT NULL;
CREATE INDEX goals_shared_open ON goals(state,merged_into,kind,deadline_at);
CREATE TRIGGER goals_valid_insert BEFORE INSERT ON goals
WHEN NEW.state NOT IN ('open','completed','abandoned') OR NEW.origin NOT IN ('internal','host','admin')
BEGIN SELECT RAISE(ABORT,'invalid goal state or origin'); END;
CREATE TRIGGER goals_valid_update BEFORE UPDATE OF state,origin ON goals
WHEN NEW.state NOT IN ('open','completed','abandoned') OR NEW.origin NOT IN ('internal','host','admin')
BEGIN SELECT RAISE(ABORT,'invalid goal state or origin'); END;
CREATE TABLE goal_people (
    goal_id INTEGER NOT NULL REFERENCES goals(id),
    subject_id TEXT NOT NULL REFERENCES subjects(id),
    PRIMARY KEY(goal_id,subject_id)
);
INSERT OR IGNORE INTO goal_people(goal_id,subject_id)
SELECT gs.goal_id,m.sender_subject_id FROM goal_sources gs JOIN messages m ON m.id=gs.message_id
WHERE m.sender_subject_id NOT IN ('self','scene');
CREATE INDEX goal_people_subject ON goal_people(subject_id,goal_id);
CREATE TABLE goal_memories (
    goal_id INTEGER NOT NULL REFERENCES goals(id),
    memory_id INTEGER NOT NULL REFERENCES memories(id),
    memory_revision INTEGER NOT NULL,
    PRIMARY KEY(goal_id,memory_id)
);
CREATE TABLE goal_duplicates (
    goal_a INTEGER NOT NULL REFERENCES goals(id),
    goal_b INTEGER NOT NULL REFERENCES goals(id),
    status TEXT NOT NULL CHECK(status IN ('possible','dismissed','merged')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    PRIMARY KEY(goal_a,goal_b),
    CHECK(goal_a<goal_b)
);
-- Plans have no delivery cursor. A cursor is allocated only when published.
CREATE TABLE goal_reminder_plans (
    id INTEGER PRIMARY KEY,
    goal_id INTEGER NOT NULL REFERENCES goals(id),
    reminder_kind TEXT NOT NULL CHECK(reminder_kind IN ('soon','due','overdue')),
    scheduled_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('scheduled','published','cancelled','skipped')),
    created_at TEXT NOT NULL
);
CREATE INDEX goal_plans_due ON goal_reminder_plans(status,scheduled_at,goal_id);
CREATE TABLE notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    goal_id INTEGER REFERENCES goals(id),
    reminder_kind TEXT,
    deadline_at TEXT,
    scheduled_at TEXT NOT NULL,
    published_at TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','taken','cancelled')),
    taken_at TEXT,
    cancelled_at TEXT
);
CREATE INDEX notifications_goal ON notifications(goal_id,id);
CREATE INDEX notifications_status ON notifications(status,id);
INSERT OR IGNORE INTO runtime_settings(key,value_json)
VALUES('goals','{"default_reminder_minutes":60,"overdue_reminders":true}');
