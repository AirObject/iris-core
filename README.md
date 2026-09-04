# Iris Memory Core

Iris Memory Core is a host-independent cognitive memory service. This repository contains the accepted architecture baseline and the completed Phase 0–9 vertical slices: the SQLite canonical store, identity and scope, the observation journal with a transactional outbox and persistent scheduler, recent context / state / focus, notes / tasks / cognitive events, explicit long-term memory, the recall protocol with FTS, vector, graph and profile routes, and the versioned Persona system.

**There is no HTTP transport layer yet.** Phases 0–9 deliver application-layer services plus the generated contract and a mock server; the ASGI application and the `serve` / `worker` process entry points land in Phase 10 (ADR-0017 §3). The published OpenAPI is the frozen contract shape, not a running server.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Node.js 22+ and npm

`faiss-cpu` and `numpy` are runtime dependencies (imported lazily — a runtime without them degrades the Vector capability instead of failing startup). No web framework, external embedding provider, or host adapter is required to run the test suite.

## Bootstrap

```bash
make bootstrap
make ci
```

`make bootstrap` creates the Python environment from `uv.lock` and installs the locked TypeScript toolchain. `make ci` performs format, lint, static type, contract drift/compatibility, Python test/coverage, and TypeScript SDK checks.

Useful focused commands:

```bash
make format
make lint
make typecheck
make contracts
make contracts-check
make test
make sdk-test
```

Run a disposable migration:

```bash
uv run iris-memory-core migrate /tmp/iris-phase0.sqlite3
uv run iris-memory-core schema-version /tmp/iris-phase0.sqlite3
```

Run the contract mock server:

```bash
uv run python -m tools.mock_server --port 8765
```

## Architecture and development

- [Architecture & Implementation Baseline](docs/IRIS_MEMORY_CORE_IMPLEMENTATION_PLAN.md)
- [Documentation index](docs/README.md)
- [Development roadmap](docs/development/README.md) — phase status, dependencies, milestones
- [Latest delivery: Phase 9](docs/development/phase-09-persona.md) · [verification report](docs/reports/phase-09-verification.md)
- [Accepted ADRs](docs/adr/README.md)
- [Contribution guide](CONTRIBUTING.md)

Generated OpenAPI and JSON Schema files are committed artifacts. Change `contracts/source/contracts.json`, run `make contracts`, and commit the source and generated changes together. Never edit an applied migration; add a new migration instead.

## License

Copyright (C) 2026 Iris Memory Core contributors. Licensed under the GNU Affero General Public License v3.0 only; see [LICENSE](LICENSE).
