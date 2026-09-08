# Changelog

This project is under development. Version labels below identify repository
candidates; they do not claim publication to PyPI or stable release acceptance.
Detailed historical evidence lives in the [verification reports](docs/reports/README.md).

## Unreleased

### Observation context — Core 0.16.0 / Schema 25 / Contract 1.13.0

- Keep authorized background and interaction messages in Observation, with optional thread and reply metadata; ingestion requires no relevance model call.
- Add paginated raw context and explicit summary admission through HTTP, Embedded, and Python/TypeScript SDK 0.12.0.
- Group interleaved messages into evidence-backed Episode summaries, record ignored inputs, and retain durable processing state across retries and restarts.
- Make automatic summaries opt-in, triggered by message count or waiting time; protect Recent interaction capacity from background floods.
- Expire background content after a configurable 30 days by default, preserving explicitly cited evidence and existing holds. Explicit Forget still invalidates source visibility.
- Preserve old observation identities and retry fingerprints through the additive migration. See the [implementation guide](docs/development/observation-context.md).

### Earlier unreleased work

- Assemble Graph and explicitly configured Vector recall in the HTTP runtime using shared API/worker configuration; require opt-in for deterministic development embeddings.
- Advance business Contract to 1.11.0 with optional required-capability negotiation, and fix Note vector indexing to use its immutable revision.
- Move business HTTP paths and schemas into the declarative contract source.
- Split application ports by context, retaining compatibility imports.
- Check current documentation counts against generated OpenAPI.
- Separate latency measurements from coverage tests and extend the CI time budget.
- Add distribution metadata, security reporting guidance and manual publication tooling.

## Core 0.13.0 / Schema 20 — development candidate

- Add the optional Console plane and incremental resource management operations.
- Enforce online Surface Lease proofs and verify public SDK consumption of installed packages.
- Add trusted CLI initialization and verified offline migration and backup workflows.
- Persist deletion previews, deletion generations and resumable Console operations.
- Keep business Contract 1.10.0 and Console Contract 1.1.0; Python SDK 0.11.1 and
  TypeScript SDK 0.11.2 have independent versions.

The [Console integration matrix](web/console/INTEGRATION_MATRIX.md) identifies
unfinished features. Production providers, isolation, Soak, recovery and final
release acceptance remain open in the [Phase 14 plan](docs/development/phase-14-hardening-release.md).
