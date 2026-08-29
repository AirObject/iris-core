# ADR-0005: Tombstone Priority

- Status: Accepted
- Date: 2026-08-29
- Owners: Iris Memory Core Team
- Baseline: §19.3–19.5, §21, §22

## Context

Deletion can race with reads, caches, indexes, background jobs, migration, and backup restoration.

## Decision

A committed Tombstone synchronously excludes matching Canonical reads and advances the Tombstone/Agent Watermark. Projection cleanup is asynchronous, but every cache, route, builder, worker commit, historical/current read, restore, and import compares Tombstone state before exposing or recreating content. Forget and security work retain a priority path under backpressure. Legal Hold and resources excluded from ordinary Forget are explicit policy states.

## Rejected alternatives

- Waiting for physical index deletion before Canonical exclusion.
- Treating cache expiration as deletion.
- Restoring old backups without replaying and validating Tombstones.

## Consequences

Deleted content cannot reappear through stale projections. Physical reclamation is eventually consistent and must be observable.

## Migration impact

Legacy deletion records are loaded as a priority deletion set and materialized against mapped resources before other content becomes visible. Selectors and watermarks are versioned.
