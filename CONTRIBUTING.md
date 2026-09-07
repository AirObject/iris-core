# Contributing

Read the [roadmap](docs/development/README.md), the relevant phase document and its architecture/ADR references before changing code. Confirm prerequisite evidence and add or update a decision record before changing a frozen boundary. API, Schema, error or persistence changes must update source contracts, fixtures, migrations, compatibility expectations, tests and affected documentation together.

## Development workflow

```bash
make bootstrap
make format
make ci
```

For Console changes also run `npm run check --prefix web/console` (including the production build) and the relevant real-backend browser tests described in the [Console README](web/console/README.md). The root CI includes these frontend and installation gates; install Playwright Chromium or set `CONSOLE_BROWSER_EXECUTABLE` before running locally.

The domain package may import only the Python standard library and domain-safe primitives. It must not import API, storage, indexing, provider, coordinator, SDK or host framework packages.

Applied migrations are immutable. Use the next sequential number and test both an empty-database upgrade and an upgrade from the supported predecessor schema. Distinguish online-safe changes from migrations requiring downtime and a verified backup.

Run `make public-api-check` for public interface changes. The [public interface gate](docs/development/public-api.md) checks an explicit SDK operation mapping and reviewed export/signature/DTO/HTTP/CLI snapshot, including installed wheels. Generate a separate candidate for review; do not accept unknown methods or update a snapshot merely to silence CI.

## Documentation maintenance

- The [documentation index](docs/README.md) defines each document's responsibility. Keep phase status in the roadmap and the corresponding phase; link to evidence instead of copying reports into plans.
- Use the [phase template](docs/development/phase-template.md), including all required headings and anchored architecture references. Completed phases retain concise outcomes, gates, known limits and evidence.
- Architecture and ADRs define semantics; contracts, generated schemas, fixtures and version manifests define machine-readable surfaces. Do not maintain a second manual enumeration of generated routes, errors or capabilities.
- Reports state date, candidate version/commit, environment, command, result and unverified scope. Preserve historical evidence; mark limitations resolved by later phases with their replacement reference. Carry unresolved items to a named work package.
- When merging documents, move unique valid content first, update inbound links and heading anchors, then remove redundant files. Accepted decisions retain history and explicit supersession; a documentation cleanup does not accept an unwritten ADR or certify unfinished implementation.
- Run `uv run python -m tools.check_docs`. The scan includes root, docs, SDK, application and Console hand-written Markdown and skips dependency/build caches.

## Change review

Tests should exercise behavior, failure and compatibility. Generated contracts must have no unexplained drift, public changes need forward-compatibility fixtures, and logs/errors must exclude secrets and sensitive body text. Record current verification failures and remaining gates explicitly; historical CI and mock-only tests cannot establish a current release or real host integration.
