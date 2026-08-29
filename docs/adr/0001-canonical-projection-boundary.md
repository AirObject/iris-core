# ADR-0001: Canonical and Projection Boundary

- Status: Accepted
- Date: 2026-08-29
- Owners: Iris Memory Core Team
- Baseline: §2, §7, §20, §22

## Context

Memory content must survive index loss, model replacement, rebuild, and crash recovery without treating search artifacts as facts.

## Decision

Normalized SQLite records and immutable revisions are the only Canonical truth. FTS, FAISS, Recent Context, Profile, Graph, caches, summaries, and generated manifests are projections. Every projection stores its builder version, source revision/watermark, and Canonical resource reference. A returned projection candidate is rehydrated and authorized from Canonical data before use.

## Rejected alternatives

- Treating vector or FTS entries as durable facts.
- Allowing workers to update Current Pointers directly.
- Recovering Canonical records from projections after data loss.

## Consequences

Projection loss is recoverable and stale candidates cannot bypass authorization or deletion. Rebuild cost and Canonical rehydrate latency must be budgeted.

## Migration impact

Future projections may be replaced by versioned shadow builds. Changing the Canonical/Projection classification requires a new ADR, data/protocol version, and explicit migration.
