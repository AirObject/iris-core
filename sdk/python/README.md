# Iris Memory Python SDK

Dependency-free async client for Iris Memory Core: capability negotiation,
stable error models, and forward-compatible fixture validation. Client
methods track the release train and currently cover Phase 2–8: observation
batches and cursors, surface leases, admin jobs/schedules, recent context,
state records, focus items, notes, tasks and cognitive events, explicit
memory (remember / correct / forget, episodes, relations, artifacts,
retention, legal holds), recall / search / usage, and the entity profile
read surface. Run `pytest` (root) to validate the shared fixtures.

The service has no HTTP transport layer yet (Phase 10, ADR-0017 §3); the
contract tests run against `tools/mock_server.py`.
