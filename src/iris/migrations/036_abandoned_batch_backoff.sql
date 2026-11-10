-- Design 17.7: an abandoned batch retries on days 1, 3 and 7 after it was first abandoned.
-- A failed retry rewrites batches.finished_at, so the first abandonment is pinned here.
CREATE TABLE batch_retry_schedules (
    batch_id INTEGER PRIMARY KEY REFERENCES batches(id),
    abandoned_at TEXT NOT NULL,
    covered_day INTEGER NOT NULL DEFAULT 0 CHECK(covered_day>=0),
    closed_reason TEXT
);
-- The previous rule retried once, on the day after abandonment.
INSERT INTO batch_retry_schedules(batch_id,abandoned_at,covered_day)
SELECT batch_id,strftime('%Y-%m-%dT%H:%M:%SZ',created_at,'-1 day'),1 FROM maintenance_batch_retries;

-- One row per automatic retry; the number never resets, manual relearning adds none.
CREATE TABLE maintenance_batch_retries_v2 (
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    number INTEGER NOT NULL CHECK(number>=1),
    run_id INTEGER NOT NULL REFERENCES maintenance_runs(id),
    local_day TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(batch_id,number),
    UNIQUE(batch_id,local_day)
);
INSERT INTO maintenance_batch_retries_v2(batch_id,number,run_id,local_day,created_at)
SELECT batch_id,1,run_id,substr(created_at,1,10),created_at FROM maintenance_batch_retries;
DROP TABLE maintenance_batch_retries;
ALTER TABLE maintenance_batch_retries_v2 RENAME TO maintenance_batch_retries;

-- Covers automatic and manual resets alike: the first one keeps the original date.
CREATE TRIGGER batch_retry_anchor AFTER UPDATE OF state ON batches
WHEN old.state='abandoned' AND new.state!='abandoned' AND old.finished_at IS NOT NULL BEGIN
    INSERT OR IGNORE INTO batch_retry_schedules(batch_id,abandoned_at) VALUES(new.id,old.finished_at);
END;
