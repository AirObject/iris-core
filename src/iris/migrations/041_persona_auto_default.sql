-- Preserve the previous effective mode when an administrator saved settings
-- without specifying a publication mode.
INSERT OR IGNORE INTO runtime_settings(key,value_json)
VALUES('persona_publish_mode','"all_manual"');

-- Only the untouched manual default moves to automatic publication.
UPDATE runtime_settings SET value_json='"all_auto"'
WHERE key='persona_publish_mode'
  AND CASE WHEN json_valid(value_json) THEN json_extract(value_json,'$') END='all_manual'
  AND NOT EXISTS (
    SELECT 1 FROM admin_operations
    WHERE actor='admin' AND action='persona_settings_saved'
  );
