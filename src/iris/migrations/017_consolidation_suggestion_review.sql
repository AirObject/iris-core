-- Suggestions are administrator-only, including after explicit confirmation.
-- Existing suggestions remain pending; clearing never reactivates them.
ALTER TABLE consolidation_annotations ADD COLUMN confirmed_at TEXT;
ALTER TABLE consolidation_annotations ADD COLUMN confirmed_by TEXT;
CREATE INDEX consolidation_annotations_run ON consolidation_annotations(run_id,work_id,id);
