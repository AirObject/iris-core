# ADR-0002: Scope Null Semantics

- Status: Accepted
- Date: 2026-08-29
- Owners: Iris Memory Core Team
- Baseline: §5.2–5.4

## Context

Shared memory requires a stable distinction between an omitted dimension, downward visibility, and a request-side wildcard.

## Decision

Within a stored Scope, `null` means the resource is not restricted at that dimension and may be visible downward subject to every other Scope and Privacy check. In a client request, `null`, omission, or a broad value never grants wildcard access. The server derives an `AccessContext`; request bodies may only narrow it. Scope and Privacy filtering run before relevance scoring.

## Rejected alternatives

- Interpreting `null` as “all tenants/agents/spaces”.
- Trusting a body-supplied access set.
- Performing privacy filtering after ranking or retrieval.

## Consequences

Authorization remains server-controlled and property-testable. Every new scope dimension must define stored-null and request-omission semantics.

## Migration impact

Legacy records with ambiguous Scope enter quarantine or require explicit mapping. Any semantic change requires a protocol/data version and authorization migration.
