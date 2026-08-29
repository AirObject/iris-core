# ADR-0008: Persona Bootstrap Seam

- Status: Accepted
- Date: 2026-08-29
- Owners: Iris Memory Core Team
- Baseline: §14, §18, Phase 1 and Phase 9

## Context

Ready and early Recall require a stable Persona revision/hash before the complete policy, proposal, state, and evolution system is delivered in Phase 9.

## Decision

The Phase 1 Agent creation transaction creates one minimal, locked, immutable, Published Persona revision and atomically sets its Current Pointer. The bootstrap Schema contains only stable Core/Trait/Narrative defaults, revision, content hash, status, and source metadata. Application credentials may read but never publish it. Phase 9 expands state, policy, proposal, approval, notification, and rollback around the same revision/pointer contract; it migrates bootstrap rows without changing their IDs, hashes, or meaning.

## Rejected alternatives

- Allowing an Agent to become Ready without Persona Current.
- Returning an implicit host-local default with no revision/hash.
- Building the entire Persona evolution system in Phase 1.

## Consequences

All hosts observe a stable Persona from Agent creation, while advanced evolution remains correctly sequenced. Bootstrap content stays locked until an authorized Phase 9 publication creates a new revision.

## Migration impact

Phase 9 uses additive tables/columns and validates every Agent has exactly one valid Published Current Pointer before cutover. Rollback continues to read the original bootstrap-compatible fields.
