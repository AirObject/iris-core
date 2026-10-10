-- The 2026-10-10 rollout requires administrator confirmation by default.
-- Materialize the old implicit default first so that an administrator who edited
-- only goal/rules also keeps the old effective mode when the setting is absent.
INSERT OR IGNORE INTO runtime_settings(key,value_json)
VALUES('persona_publish_mode','"small_medium_auto"');

-- Any administrative persona-settings save counts as a deliberate choice,
-- including goal/rules-only saves or explicitly reselecting the old default.
-- Persona text edits and automated operations are not settings changes.
UPDATE runtime_settings SET value_json='"all_manual"'
WHERE key='persona_publish_mode'
  AND json_extract(value_json,'$')='small_medium_auto'
  AND NOT EXISTS (
    SELECT 1 FROM admin_operations
    WHERE actor='admin' AND action='persona_settings_saved'
  );
-- Existing versions, pending candidates and accepted attempts keep their snapshots.
