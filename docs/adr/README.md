# Architecture Decision Records

Accepted decisions are immutable historical records; replacements use a new ADR with explicit supersession and migration impact. ADR-0001 to ADR-0008 froze the boundaries Phase 1 needed; each later phase adds the decisions that phase froze.

Every ADR carries context, decision, rejected alternatives, consequences, and migration impact. Within-phase review rounds are appended to the phase's own ADR as dated revision sections rather than filed as new ADRs; a decision that changes another phase's frozen boundary gets a new ADR.

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](./0001-canonical-projection-boundary.md) | Canonical/Projection boundary | Accepted |
| [0002](./0002-scope-null-semantics.md) | Scope Null semantics | Accepted |
| [0003](./0003-identity-and-binding.md) | Identity and Binding | Accepted |
| [0004](./0004-immutable-revisions.md) | Immutable Revision and Current Pointer | Accepted |
| [0005](./0005-tombstone-priority.md) | Tombstone priority | Accepted |
| [0006](./0006-api-version-and-compatibility.md) | API version and compatibility | Accepted |
| [0007](./0007-repository-boundaries.md) | Repository and dependency boundaries | Accepted |
| [0008](./0008-persona-bootstrap-seam.md) | Persona Bootstrap seam | Accepted |
| [0009](./0009-phase2-reliability-spine.md) | Phase 2 reliability spine (observation identity, integer cursors, outbox fencing, bounded catch-up) | Accepted |
| [0010](./0010-active-surface-coordinator.md) | Active Surface Coordinator (lease/epoch/modes) | Accepted |
| [0011](./0011-phase3-recent-state-focus.md) | Phase 3 recent generations, state coalesced stream, focus decay model, structured-recall skeleton | Accepted |
| [0012](./0012-phase4-notes-tasks-events.md) | Phase 4 note lifecycle, task evidence semantics, trigger/occurrence identity, at-least-once host delivery | Accepted |
| [0013](./0013-phase5-long-term-memory.md) | Phase 5 explicit long-term memory, bi-temporal claims, evidence invariants, artifact security, non-resurrecting erasure | Accepted |
| [0014](./0014-phase6-fts-recall.md) | Phase 6 FTS5 generations, full recall protocol, fresh rehydrate boundary, deterministic fusion/budgets, usage four stages | Accepted |
| [0015](./0015-phase7-vector-recall.md) | Phase 7 embedding provider port, FAISS generation lifecycle with fenced COW swap, surrogate ID map, delta ledger freshness, vector route + hybrid ranker v3 | Accepted |
| [0016](./0016-phase8-profile-graph.md) | Phase 8 profile projection (field-level sourced summaries), relation graph allowlist projection with fenced generations, budget-bound graph route + profile route, canonical fallback | Accepted |
| [0017](./0017-contract-surface-alignment.md) | Contract surface alignment: error-code supersession, endpoint naming adjudication, HTTP transport layer assigned to Phase 10 | Accepted |
| [0018](./0018-phase9-persona.md) | Phase 9 complete Persona: additive bootstrap expansion, policy/evidence gates, atomic publication, state TTL and rollback-by-new-revision | Accepted |
| [0019](./0019-phase10-consolidation-transport.md) | Phase 10 fixed-watermark consolidation/reflection, provider governance, credential/SSE transport, replay and process lifecycle | Accepted |

Open conflicts blocking the next phases (Phase 11/12 host adapters): **none**.
