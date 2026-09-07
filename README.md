# Iris Memory Core

Iris Memory Core is a host-independent cognitive memory service built around a SQLite canonical store, versioned memory and Persona, explainable recall, and persistent background work. The HTTP service and worker are implemented; the optional Web console is in progress. Bellis and AstrBot adapters are deferred and excluded from the current Core release. The planned pip distribution contains Core functionality and exposes only the declared public methods and service contracts; direct access to its storage, indexes, queues or private components is outside the public interface. See the [roadmap](docs/development/README.md) for verified phase status and the [Phase 14 plan](docs/development/phase-14-hardening-release.md) for packaging scope, access boundaries and the remaining release work.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/) in the range declared by `pyproject.toml`.
- Node.js 22.12+ and npm for the TypeScript SDK and Console; CI uses Node.js 24.

FAISS and NumPy are runtime dependencies with lazy imports and explicit Vector degradation. The default runtime wires deterministic providers; passing local tests does not establish production retrieval or cognitive quality. Production provider configuration and deployment validation remain release work.

## Development setup

```bash
make bootstrap
make ci
```

`make bootstrap` installs locked Python, TypeScript SDK and Console dependencies. `make ci` checks formatting, lint, imports, documentation, types, contract drift/compatibility, public interface snapshots, Python tests/coverage, SDK/Console tests, production frontend builds, real browser tests and isolated package installation. Install a browser runtime before the first local run:

```bash
cd web/console
npx playwright install chromium
```

These are verification commands, not a claim that the current workspace passes every gate. Current results and remaining release gates are recorded in the [Phase 14 report](docs/reports/phase-14-verification.md). The [installation guide](docs/operations/core-installation.md) describes the trusted CLI bootstrap and public SDK boundary.

`make test` measures functional coverage; `make test-performance` runs latency budgets without coverage instrumentation. `make ci` requires both. See [test navigation](tests/README.md) for focused paths.

Useful focused commands: `make format`, `make lint`, `make typecheck`, `make contracts`, `make contracts-check`, `make test`, and `make sdk-test`.

## Run from the checkout

```bash
uv run iris-memory-core migrate /tmp/iris-dev/core.sqlite3
uv run iris-memory-core schema-version /tmp/iris-dev/core.sqlite3
uv run iris-memory-core serve --database /tmp/iris-dev/core.sqlite3 --allow-local-sqlite
```

In another terminal:

```bash
uv run iris-memory-core worker --database /tmp/iris-dev/core.sqlite3 --allow-local-sqlite
```

`--allow-local-sqlite` accepts the local SQLite build for development. Production must satisfy the runtime allowlist. API access requires provisioned credentials; startup alone does not create a tenant or grant access. The Console is disabled by default; follow its [setup and credential instructions](web/console/README.md) to enable it. An installed package using the Console requires the `console` extra.

The SDK offline test double is available with `uv run python -m tools.mock_server --port 8765`; it does not provide real persistence or replace integration tests.

## Documentation and contracts

- [Documentation index](docs/README.md): architecture, decisions, phase plans, evidence and integration guides.
- [Contribution guide](CONTRIBUTING.md): change workflow and documentation rules.
- [Python SDK](sdk/python/README.md) and [TypeScript SDK](sdk/typescript/README.md).
- [Public method mapping and change gate](docs/development/public-api.md).
- [Host integrations](hosts/README.md) and [Web Console](web/console/README.md).

See the [contract authoring guide](contracts/README.md). Edit `contracts/source/contracts.json` for `/v1` or `contracts/source/console.json` for `/console/v1`, run `make contracts`, and include generated artifacts and fixtures in the same change. Never edit an applied migration; add a new one.

## License

Copyright (C) 2026 Iris Memory Core contributors. Licensed under the GNU Affero General Public License v3.0 only; see [LICENSE](LICENSE).
