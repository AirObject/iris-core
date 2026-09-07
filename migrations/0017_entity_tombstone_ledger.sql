-- iris: online_safe=false lock_ms=200 min_app=0.13.0 max_app= recovery=backup bootstrap_safe=true
-- Historical Entity tombstones predate the ForgetRequest ledger integration.
-- Backfill only deletion metadata, preserving the original sequence and time.
-- The ledger's uniqueness index is app-instance-first. Materialize once so
-- lookup by tenant + selector does not rescan a tenant's ledger per tombstone.
WITH existing AS MATERIALIZED (
    SELECT tenant_id, selector_key FROM forget_requests
)
INSERT INTO forget_requests (
    id, tenant_id, selector_key, selector_json, reason_code, requested_by,
    app_instance_id, idempotency_key, erase_content, created_us,
    tombstone_seq_lo, tombstone_seq_hi, target_count, erased_count,
    protected_skipped, held_skipped
)
SELECT
    rt.id, rt.tenant_id, 'entity-tombstone:v1:' || rt.resource_id,
    json_object('kind','resource','resource_type','entity','resource_id',rt.resource_id),
    rt.reason_code, rt.deleted_by, 'migration:entity-tombstone-v1', rt.id, 0,
    rt.created_us, rt.tombstone_seq, rt.tombstone_seq, 1, 1, 0, 0
FROM resource_tombstones AS rt
WHERE rt.resource_type='entity'
  AND NOT EXISTS (
    SELECT 1 FROM existing AS fr
    WHERE fr.tenant_id=rt.tenant_id
      AND fr.selector_key='entity-tombstone:v1:' || rt.resource_id
  );
