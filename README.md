# Iris Memory Core

Iris Memory Core is a host-independent cognitive memory service built around a SQLite canonical store, versioned memory and Persona, explainable recall, and persistent background work. The HTTP service and worker are implemented. Bellis integration and the optional Web console are in progress; the AstrBot bridge is still planned. See the [roadmap](docs/development/README.md) for verified phase status and the [Phase 14 plan](docs/development/phase-14-hardening-release.md) for the remaining path to a stable release.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/) in the range declared by `pyproject.toml`.
- Node.js 22.12+ and npm for the TypeScript SDK and Console; CI uses Node.js 24.

FAISS and NumPy are runtime dependencies with lazy imports and explicit Vector degradation. The default runtime wires deterministic providers; passing local tests does not establish production retrieval or cognitive quality. Production provider configuration and deployment validation remain release work.

## Development setup

```bash
make bootstrap
make ci
```

`make bootstrap` installs locked Python and TypeScript SDK dependencies. `make ci` checks formatting, lint, imports, documentation, types, contract drift/compatibility, Python tests/coverage, and the TypeScript SDK. Console checks are currently separate:

```bash
npm ci --prefix web/console
npm run check --prefix web/console
```

These are verification commands, not a claim that the current workspace passes every gate. Current Console failures and the distinction between real and simulated browser coverage are recorded in the [Console report](docs/reports/phase-13-verification.md).

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
- [Application integrations](application/README.md) and [Web Console](web/console/README.md).

Edit `contracts/source/contracts.json` for `/v1` or `contracts/source/console.json` for `/console/v1`, run `make contracts`, and include generated artifacts and fixtures in the same change. Never edit an applied migration; add a new one.

## License

Copyright (C) 2026 Iris Memory Core contributors. Licensed under the GNU Affero General Public License v3.0 only; see [LICENSE](LICENSE).
