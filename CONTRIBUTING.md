# Contributing

Read the [roadmap](docs/development/README.md), the relevant phase document and its architecture/ADR references before changing code. Confirm prerequisite evidence and add or update a decision record before changing a frozen boundary. API, Schema, error or persistence changes must update source contracts, fixtures, migrations, compatibility expectations, tests and affected documentation together.

## Development workflow

```bash
make bootstrap
make format
make test-affected TESTS="tests/integration/console/test_console_persona_commands.py"
# Once at the end of the work package:
make ci
```

Work on one explicitly bounded work package at a time. During implementation, run the affected test subset and relevant static, contract, browser or migration checks. `test-affected` requires explicit test paths and disables repository-wide coverage collection for that local subset; full CI retains its existing coverage and performance thresholds, with latency tests in a separate uninstrumented stage. See [test navigation](tests/README.md) for subject directories and historical path mappings. Run full `make ci` once when the work package is ready for acceptance, not after every feature or edit. Investigate a failed gate and rerun affected checks; repeat full CI only when changes or an unresolved integration concern justify it. Preserve failures in the report; do not automatically repeat unchanged full runs. Long Soak and recovery campaigns are scheduled separately from interactive goals. See the [work-package queue](docs/development/work-packages.md).

For affected Console changes also run `npm run check --prefix web/console` (including the production build) and the relevant real-backend browser tests described in the [Console README](web/console/README.md). The root CI includes these frontend and installation gates; install Playwright Chromium or set `CONSOLE_BROWSER_EXECUTABLE` before running locally.

The domain package may import only the Python standard library and domain-safe primitives. It must not import API, storage, indexing, provider, coordinator, SDK or host framework packages.

Applied migrations are immutable. Use the next sequential number and test both an empty-database upgrade and an upgrade from the supported predecessor schema. Distinguish online-safe changes from migrations requiring downtime and a verified backup.

Run `make public-api-check` for public interface changes. The [public interface gate](docs/development/public-api.md) checks an explicit SDK operation mapping and reviewed export/signature/DTO/HTTP/CLI snapshot, including installed wheels. Generate a separate candidate for review; do not accept unknown methods or update a snapshot merely to silence CI.

## Commits

Keep each commit within one work package from the [work-package queue](docs/development/work-packages.md) or the relevant phase's requirement tracking and work-package sections. Split a larger work package into smaller, independently reviewable and verifiable changes; do not accumulate a whole Phase into one commit. Keep unrelated fixes and cleanup separate. Each implementation commit must leave its affected checks runnable so bisect can isolate a regression; full `make ci` remains a work-package acceptance gate.

Commit a migration, source contract changes, regenerated schemas/fixtures/types and version manifests together with the code that requires them, their tests and affected documentation. Split by behavior, not by directory or layer: separating these dependent changes leaves intermediate commits with incompatible code or contract drift. Run `make contracts` and `make contracts-check` before committing contract changes; generated files remain tracked even when their diffs are collapsed. A smaller commit must preserve this consistency. Reverting code does not undo an applied migration; retain the phase's explicit data and rollback strategy.

Use Conventional Commits: `type(scope): summary`, with an optional scope, following existing types such as `feat`, `fix`, `docs` and `test`. Write summaries and bodies in English, consistent with code comments and the majority of existing commit messages; architecture/ADR and development documents retain their Chinese prose. Use an imperative summary naming the behavior changed, such as `feat(console): expose statistics freshness`, rather than a Phase completion label. Reference the existing work-package number and requirement ID in the body, and record verification or link its report, including unresolved gates. A checkpoint commit does not establish work-package acceptance.

## Documentation maintenance

- The [documentation index](docs/README.md) defines each document's responsibility. Keep phase status in the roadmap and the corresponding phase; link to evidence instead of copying reports into plans.
- Use the [phase template](docs/development/phase-template.md), including all required headings and anchored architecture references. Completed phases retain concise outcomes, gates, known limits and evidence.
- Architecture and ADRs define semantics; contracts, generated schemas, fixtures and version manifests define machine-readable surfaces. Do not maintain a second manual enumeration of generated routes, errors or capabilities.
- Reports state date, candidate version/commit, environment, command, result and unverified scope. Preserve historical evidence; mark limitations resolved by later phases with their replacement reference. Carry unresolved items to a named work package.
- When merging documents, move unique valid content first, update inbound links and heading anchors, then remove redundant files. Accepted decisions retain history and explicit supersession; a documentation cleanup does not accept an unwritten ADR or certify unfinished implementation.
- Run `uv run python -m tools.check_docs`. The scan includes root, docs, SDK, hosts and Console hand-written Markdown and skips dependency/build caches.

## Change review

Tests should exercise behavior, failure and compatibility. Generated contracts must have no unexplained drift, public changes need forward-compatibility fixtures, and logs/errors must exclude secrets and sensitive body text. Record current verification failures and remaining gates explicitly; historical CI and mock-only tests cannot establish a current release or real host integration.
