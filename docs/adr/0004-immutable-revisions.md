# ADR-0004: Immutable Revisions and Current Pointers

- Status: Accepted
- Date: 2026-08-29
- Owners: Iris Memory Core Team
- Baseline: §4.2, §7, §20.6

## Context

Correction, concurrent editing, historical reads, worker fencing, and rollback require a history that cannot be overwritten in place.

## Decision

Every mutable aggregate is represented by immutable revisions plus a Current Pointer. A modifying request supplies an Idempotency Key and Expected Revision when applicable. The transaction validates the expected base, inserts the new revision, advances the pointer and watermark, writes audit data, and emits Outbox work atomically. Rollback creates a new revision from historical content; it never reactivates an old row.

## Rejected alternatives

- In-place updates with an `updated_at` timestamp.
- Last-write-wins concurrency.
- Workers directly changing Current Pointers.

## Consequences

History and concurrency are explicit, at the cost of storage and retention management. Stable `revision_mismatch` errors are part of the contract.

## Migration impact

Legacy mutable rows become initial revisions. Revision format changes require additive migration and pointer validation before cutover.
