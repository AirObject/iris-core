# Security policy

Iris Memory Core is a development candidate. No version is currently designated
as a supported stable production release. Security fixes are developed on the
current development branch; older candidates have no promised backport window.

## Reporting a vulnerability

Use the repository's
[private vulnerability reporting page](https://github.com/AirObject/iris_memory_core/security/advisories/new)
when it is enabled. Include the affected commit/version, reproduction steps,
impact and a minimal example with synthetic data. Do not include live credentials
or private memory contents.

If private reporting is unavailable, open an issue asking a maintainer to enable
a private reporting channel. Keep vulnerability details out of that public issue.
There is currently no published security mailbox or response-time commitment.

## Deployment boundary

Clients receive scoped credentials and service endpoints. Core database, index,
backup and key directories belong to a separate service identity. Python module
privacy does not enforce OS isolation. Deterministic providers, development SQLite
overrides and development HTTP options are not production acceptance evidence.
See the [installation guide](docs/operations/core-installation.en.md) and the
[release plan](docs/development/phase-14-hardening-release.md) for current limitations.
