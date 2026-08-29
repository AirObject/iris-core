# Iris Memory Core

Iris Memory Core is a host-independent cognitive memory service. This repository currently contains the accepted architecture baseline and the completed Phase 0 engineering scaffold: module boundaries, generated contracts, SDK skeletons, migration safety, and reproducible quality gates.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Node.js 22+ and npm

No database, web framework, vector provider, or host adapter is required for Phase 0.

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
- [Development roadmap](docs/development/README.md)
- [Phase 0 delivery](docs/development/phase-00-architecture-scaffold.md)
- [Accepted ADRs](docs/adr/README.md)
- [Contribution guide](CONTRIBUTING.md)

Generated OpenAPI and JSON Schema files are committed artifacts. Change `contracts/source/contracts.json`, run `make contracts`, and commit the source and generated changes together. Never edit an applied migration; add a new migration instead.

## License

Copyright (C) 2026 Iris Memory Core contributors. Licensed under the GNU Affero General Public License v3.0 only; see [LICENSE](LICENSE).
