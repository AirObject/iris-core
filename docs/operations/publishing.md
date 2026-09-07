# Publishing Python distributions

The [publication workflow](../../.github/workflows/publish.yml) is manual and
builds both Core and the independently versioned Python SDK. Its default mode runs
all development gates, validates the exact upload artifacts, and retains them with
SHA-256 checksums. It does not upload to PyPI by default.

Before enabling uploads, maintainers must finish the
[Phase 14 release gates](../development/phase-14-hardening-release.md), approve an
immutable release candidate and manifest, and review both distribution versions.
A green development build with SQLite override does not close those gates.

Configure the GitHub `pypi` environment with required reviewers and release-tag
restrictions. Register `AirObject/iris_memory_core`, `publish.yml`, and environment
`pypi` as the Trusted Publisher for **both** `iris-memory-core` and `iris-memory-sdk`
on PyPI. Then set the repository variable `PYPI_RELEASE_ENABLED=true`.
These external settings have not been configured or verified by the local audit.

Run the workflow on the reviewed release tag and explicitly select `publish`.
The publish job requires the build to pass, checks the downloaded artifact hashes,
and uploads the same artifacts using OIDC; it has no long-lived PyPI token.
This follows the [uv publication guide](https://docs.astral.sh/uv/guides/package/).

Core and SDK versions need not match. The private Console frontend is delivered
separately and is not published by this workflow. After an actual publication,
complete registry installation/read-back and attach the resulting evidence to the
release manifest. Publication and production acceptance have not occurred merely
because this workflow exists.
