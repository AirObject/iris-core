-- goal_review_hidden_v1 and goal_dedup_hidden_v1 passed (2026-11-12): G1
-- automatic review and G2 duplicate judgment become default on.
-- Goal settings saves (PATCH /admin/api/settings/goals and question expiry)
-- always record an admin_operations row with action 'settings_goals', whatever
-- they saved, including an empty change. Migration 012 wrote the 'goals' row.
-- Never saved: both switches become true.
UPDATE runtime_settings
SET value_json=json_set(value_json,'$.goal_auto_review_enabled',json('true'),
                        '$.goal_duplicate_auto_judge_enabled',json('true'))
WHERE key='goals'
  AND json_valid(value_json)
  AND NOT EXISTS (SELECT 1 FROM admin_operations WHERE action='settings_goals');
-- Saved at least once: keep the effective value. A save stored a switch only
-- when it differed from the old default (off), so a missing field meant off;
-- pin it before the code fallback turns on. Stored values are not touched.
UPDATE runtime_settings
SET value_json=json_insert(value_json,'$.goal_auto_review_enabled',json('false'),
                           '$.goal_duplicate_auto_judge_enabled',json('false'))
WHERE key='goals'
  AND json_valid(value_json)
  AND EXISTS (SELECT 1 FROM admin_operations WHERE action='settings_goals');
