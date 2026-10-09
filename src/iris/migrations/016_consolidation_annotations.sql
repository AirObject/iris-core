-- Upgrade old probe databases safely. No pending v1/v2 decision can write bodies.
ALTER TABLE consolidation_annotations ADD COLUMN kind TEXT NOT NULL DEFAULT 'disputed'
 CHECK(kind IN ('disputed','insufficient_support','superseded'));
ALTER TABLE consolidation_annotations ADD COLUMN superseded_by INTEGER REFERENCES memories(id);
ALTER TABLE consolidation_annotations ADD COLUMN memory_revision INTEGER;
ALTER TABLE consolidation_annotations ADD COLUMN related_ids_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE consolidation_annotations ADD COLUMN run_id INTEGER REFERENCES maintenance_runs(id);
ALTER TABLE consolidation_annotations ADD COLUMN dedupe_key TEXT;
ALTER TABLE consolidation_annotations ADD COLUMN cleared_at TEXT;
ALTER TABLE consolidation_annotations ADD COLUMN cleared_by TEXT;
UPDATE consolidation_annotations SET memory_revision=(SELECT revision FROM memories WHERE id=memory_id),
 run_id=(SELECT MIN(run_id) FROM consolidation_run_work WHERE work_id=consolidation_annotations.work_id),
 dedupe_key='legacy:'||work_id;
CREATE UNIQUE INDEX consolidation_annotation_conclusion ON consolidation_annotations(memory_id,dedupe_key);
CREATE INDEX consolidation_annotations_active ON consolidation_annotations(memory_id,id) WHERE cleared_at IS NULL;
UPDATE runtime_settings SET value_json=json_set(value_json,'$.method','broad','$.resolution','report_only_v1')
 WHERE key='consolidation';
UPDATE consolidation_work SET state='obsolete' WHERE state IN ('pending','decided','classified');
DELETE FROM consolidation_run_work WHERE run_id IN (SELECT run_id FROM consolidation_runs WHERE finished=0);
UPDATE consolidation_runs SET planned=0,settings_json=json_set(settings_json,'$.method','broad','$.resolution','report_only_v1')
 WHERE finished=0;
DELETE FROM consolidation_scanned;
