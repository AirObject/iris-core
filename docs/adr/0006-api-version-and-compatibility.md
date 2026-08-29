# ADR-0006: API Version and Compatibility

- Status: Accepted
- Date: 2026-08-29
- Owners: Iris Memory Core Team
- Baseline: §23, §28, §39

## Context

Core, two SDK languages, and independent adapters must evolve without silently disagreeing about success, errors, optional fields, or degraded behavior.

## Decision

Business APIs use `/v1`; health and metrics are unversioned. OpenAPI 3.1, JSON Schema 2020-12, fixtures, stable errors, capability negotiation, SDKs, and the version manifest form one release surface. Additive optional fields and unknown capability strings are forward-compatible. Clients preserve or ignore unknown optional values; they do not infer new privileges. Removing an endpoint, method, Schema, or required-field contract requires a new API/data version and migration plan.

## Rejected alternatives

- Unversioned business endpoints.
- Generating SDKs without shared fixtures.
- Closed enums that make every additive capability a breaking release.

## Consequences

Compatibility is machine-checked and adapters can negotiate versions. Generated artifacts must be committed without drift.

## Migration impact

Breaking changes run in parallel version windows. Compatibility snapshots update only as part of an explicitly approved version transition.
