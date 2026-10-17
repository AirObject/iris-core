-- Upgrade only the untouched historical default. Any administrator save,
-- including a switch-only save, preserves the recorded choice. Run snapshots
-- and all other settings remain unchanged; new step limits use Python defaults.
UPDATE runtime_settings
SET value_json=json_set(value_json,'$.max_calls',300)
WHERE key='consolidation'
  AND json_type(value_json,'$.max_calls')='integer'
  AND json_extract(value_json,'$.max_calls')=50
  AND NOT EXISTS (
    SELECT 1 FROM admin_operations WHERE action='consolidation_settings_saved'
  );
