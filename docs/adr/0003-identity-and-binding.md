# ADR-0003: Identity and Binding

- Status: Accepted
- Date: 2026-08-29
- Owners: Iris Memory Core Team
- Baseline: §6

## Context

One person may appear through multiple platforms, while nicknames and mutable profile fields are neither stable nor unique.

## Decision

`Entity` is the durable subject. An external account is uniquely identified by `(tenant_id, provider, realm, external_id)`. `Binding` is versioned, auditable, and verified by administrator confirmation or a future challenge flow. Nickname, avatar, text similarity, and model inference may create candidates but never verified bindings. Historical observations retain occurrence-time identity references; reads may separately resolve the current identity view. Redirect chains are bounded and cycle-free.

## Rejected alternatives

- Nickname-based or embedding-based automatic merge.
- Rewriting historical observations after a binding change.
- Using a host session identifier as a long-term person key.

## Consequences

Identity conflicts can coexist without silent data corruption. Consumers must choose `at_ingest` or `current` when that distinction matters.

## Migration impact

Legacy identities require explicit realm/provider mapping. Unproven merges are quarantined; redirect and binding changes never rewrite historical facts.
