# Publishing Python distributions

The [publication workflow](../../.github/workflows/publish.yml) is manual and
builds both Core and the independently versioned Python SDK. Its default mode runs
all development gates, validates the exact upload artifacts, and retains them with
SHA-256 checksums. It does not upload to PyPI by default.

Before enabling uploads, maintainers must finish the
[Phase 14 release gates](../development/phase-14-hardening-release.md), approve an
immutable release candidate and manifest, and review the selected distribution version. Core and Python SDK uploads require
separate workflow runs; there is no combined upload selection.
A green development build with SQLite override does not close those gates.

Select `distribution=core` or `distribution=python-sdk` (default: Core). For an
upload, `expected_version` must equal that distribution's committed pyproject
version. An empty or mismatching approval fails before the build. Both packages
still build and undergo joint installation checks; only the selected package
is eligible for upload.

Configure separate GitHub environments with required reviewers and release-tag
restrictions:

| Distribution | Environment / Trusted Publisher environment | Enable variable |
| --- | --- | --- |
| iris-memory-core | pypi-core | PYPI_CORE_RELEASE_ENABLED=true |
| iris-memory-sdk | pypi-sdk | PYPI_SDK_RELEASE_ENABLED=true |

Register `AirObject/iris_memory_core` and `publish.yml` with the corresponding
environment for each project's PyPI Trusted Publisher. The old shared `pypi`
environment and `PYPI_RELEASE_ENABLED` variable no longer enable either upload.
These external settings have not been configured or verified locally.

Run the workflow on the reviewed release tag and explicitly select `publish`.
The selected publish job requires the build to pass, verifies the downloaded
artifact hashes and uploads only its own distribution using OIDC. Each job has
its own approval boundary and no long-lived PyPI token.
This follows the [uv publication guide](https://docs.astral.sh/uv/guides/package/).

Core and SDK versions need not match. The private Console frontend is delivered
as the independent `console-static` workflow artifact; it is never uploaded to
PyPI. This is a build artifact, not a hosted deployment. After an actual publication,
complete registry installation/read-back and attach the resulting evidence to the
release manifest. Publication and production acceptance have not occurred merely
because this workflow exists.
