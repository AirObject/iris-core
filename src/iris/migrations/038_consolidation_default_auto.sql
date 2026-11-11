-- consolidation_hidden_v3 passed: auto_v1 becomes the default resolution.
-- Upgrade only the untouched migration-written report_only_v1. Any administrator
-- save of consolidation settings, whatever it saved (including a save made only
-- to change a budget, a switch or the time), preserves the stored value. Run
-- snapshots stay fixed: an unfinished run keeps the resolution it accepted.
UPDATE runtime_settings
SET value_json=json_set(value_json,'$.resolution','auto_v1')
WHERE key='consolidation'
  AND CASE WHEN json_valid(value_json) THEN json_extract(value_json,'$.resolution') END='report_only_v1'
  AND NOT EXISTS (
    SELECT 1 FROM admin_operations WHERE action='consolidation_settings_saved'
  );
