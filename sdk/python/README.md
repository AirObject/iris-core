# Iris Memory Python SDK

Dependency-free async client for Iris Memory Core: capability negotiation,
stable error models, and forward-compatible fixture validation. Client
methods track the release train and currently cover Phase 2–10: observation
batches and cursors, surface leases, admin jobs/schedules, recent context,
state records, focus items, notes, tasks and cognitive events, explicit
memory (remember / correct / forget, episodes, relations, artifacts,
retention, legal holds), recall / search / usage, the entity profile read
surface, and the Phase 10 entity / identity / binding / space-group and
admin surfaces. Run `pytest` (root) to validate the shared fixtures.

The service now has a real HTTP transport layer (Phase 10, ADR-0019): the
repository's contract tests run against the ASGI application, and
`tools/mock_server.py` is kept only as this SDK's offline test double.
