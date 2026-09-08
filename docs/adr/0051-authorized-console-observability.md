# ADR-0051: Authorized Console statistics and durable low-sensitivity observations

Status: Accepted for W07 implementation; integration and release gates remain open.

Console statistics use the current operator Grant, including Purpose, privacy,
subject consent, selector intersection, current resource state and source closure.
Low-sensitivity projection atoms retain their authorization envelope and resource
references; an aggregate never becomes a substitute for authorization. A bounded
query that cannot verify all atoms reports a partial result and an explicit warning.
No invisible remainder or `other` group is returned.

Recall records receive nullable duration and low-sensitivity observation columns.
The duration is captured from the exact same monotonic interval as RecallTrace,
including requests whose public response omits the trace. Legacy values remain
NULL. Coverage starts at the migration activation time; response_json is never
used to reconstruct historical telemetry. Counts, fixed logarithmic latency
histograms and route outcomes are rebuildable from retained Canonical observations.

Hourly projection writes replace deterministic keys and day/week projections
aggregate those hour atoms. A finite `console.stats.rollup` job uses existing
Outbox, Scheduler and lease fencing. Explicit range backfill is a typed Console
Operation with reauthentication, a bounded cursor, per-batch transactional progress,
authority revalidation and resumable idempotent replacement, not additive retries.

Every immediate read is query_only, statement-budgeted at 250ms, and row-bounded.
Timeouts preserve completed submetrics. Instance counters and database file sizes
remain explicitly instance-local; they are never described as scoped tenant bytes.
Unavailable external provider telemetry stays unavailable.

The independent W07 development snapshot uses temporary migration 0023; the
integration owner will order this after W08 as 0024 before publishing. Existing
0001–0022 migrations remain immutable. Phase 11/12 remain Deferred.

The local completion slice also persists nullable Outbox heartbeat observations at
actual claim/heartbeat boundaries. Login lockout rejections retain low-sensitivity
key-attributable audit events; unknown credentials never become tenant telemetry.
Managed file-size reads use configured roots, a 250ms/4000-entry budget and no
symlink traversal; their values are explicitly instance-local. Existing typed
Operation rows and Provider references remain intact through the migration.
