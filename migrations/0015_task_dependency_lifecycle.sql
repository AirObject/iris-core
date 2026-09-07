-- iris: online_safe=false lock_ms=200 min_app=0.13.0 max_app= recovery=backup bootstrap_safe=true
-- Existing installations require stopped API/Worker processes and a verified backup.
-- A genuinely new database can apply this during its initial schema installation.
ALTER TABLE task_dependencies ADD COLUMN status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'removed'));
ALTER TABLE task_dependency_revisions ADD COLUMN status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'removed'));
CREATE INDEX idx_task_dependencies_active ON task_dependencies (task_id, status, created_us, id);
