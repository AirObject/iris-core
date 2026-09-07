# Core installation and trusted initialization

[中文版](core-installation.md)

This guide covers the current development candidate. Production runtime, isolation,
recovery and release acceptance remain open in [Phase 14](../development/phase-14-hardening-release.md).
Core, Python SDK, TypeScript SDK and Console static assets are separate deliverables.
Bellis and AstrBot adapters are deferred.

## Build and verify the packages

```sh
make package-check
```

This gate rebuilds the Core wheel from its sdist, checks packaged resources,
dependencies, extras and CLI, installs the SDK independently, and runs that SDK
against a real Core process. It explicitly allows the development SQLite build;
production acceptance must run without that override. Bundled SQLite, FAISS and
queue implementations are private components, outside the client API.

Install the verified wheels into separate service and client environments:

```sh
uv pip install --python /path/to/core-venv/bin/python /path/to/iris_memory_core-0.13.0-py3-none-any.whl
uv pip install --python /path/to/client-venv/bin/python /path/to/iris_memory_sdk-0.11.1-py3-none-any.whl
```

These paths are placeholders for locally built artifacts, not claims of PyPI availability.

## Trusted operator initialization

Run Core as a dedicated OS identity. Provision private directories for its data and
credentials before running the following command; keep them outside client and
frontend static directories.

```sh
iris-memory-core init \
  --database /var/lib/iris-core/data/core.sqlite3 \
  --tenant tenant-example \
  --agent-name assistant \
  --app-instance client-example \
  --credential-file /var/lib/iris-core/provisioning/client.json \
  --surface-mode required
```

`init` applies migrations and creates a tenant, an agent with an initial Persona,
a local space and an application credential. That credential grants access to
that agent/space for reply use, without administration rights. The credential
file must not exist: it is created with mode 0600, and existing files or symlinks
are rejected. Console output contains resource IDs only. Expiry defaults to
30 days; `--expires-days` accepts 1–365 days.

Add `--initialize-search` to build and validate the new tenant's FTS generation in
the same transaction. Success reports `search_generation_id`; the worker maintains
it incrementally. Failure rolls back initialization and leaves no usable credential
file. This option neither grants administration rights nor upgrades an existing database.

Recall needs a confirmed speaker identity. At first initialization, an operator
who has verified the identity can add `--actor-provider PROVIDER --actor-subject SUBJECT`
to register an identity, entity and admin-confirmed binding in the same transaction.
Bootstrap cannot rebind an existing identity. Do not infer real user identities in bulk.

Securely deliver only the endpoint and scoped credential file to the client.
Clients must not share the Core service identity or its data, key and backup directories.
See the [Python SDK guide](../../sdk/python/README.md) for supported capabilities,
timeouts and version limits.

## Start the service and acquire a lease

```sh
iris-memory-core serve --database /var/lib/iris-core/data/core.sqlite3 --host 127.0.0.1
iris-memory-core worker --database /var/lib/iris-core/data/core.sqlite3
```

The Console is disabled by default. To enable it, install Core with the `console`
extra, deploy its separately built static assets, and configure HTTPS origins,
hosts and trusted proxies as described in the [Console guide](../../web/console/README.md).
Production must not use `--allow-local-sqlite` or `--console-dev-http`.
The [README](../../README.md#requirements) explains the deterministic provider limitations.

In Required mode, call `acquire_surface_lease` through the SDK to obtain proof for
the client's own app instance. Pass `lease_id` and `lease_epoch` in Recall and Focus
create request dictionaries; `focus_transition` accepts matching keyword arguments.
A business idempotency key can be retried after reacquiring a valid lease. Prior
successes are not replayed without current valid proof.

Core has no public embedded facade. Client business operations must use the public
service/SDK surface. Private Python imports, SQL, FAISS files and queue records
are outside that surface; OS identities, directory permissions and private volume
mounts enforce the actual process boundary.

Trusted `init` credentials include `events.sse.v1` to negotiate public invalidation
notifications within their scope. This neither grants administration rights nor
updates existing credentials. Advance the stream cursor only after the host has
acknowledged its own durable processing.

## Upgrade to Core 0.13.0 / Schema 20

The runtime supports Schema 20. A fresh database receives all migrations through
`init`, `serve` or `worker`. Existing databases cannot automatically apply 0015,
0017 or 0018. Migration 0016 adds durable previews and is online-safe; 0017 backfills
the entity tombstone deletion ledger, and 0018 rebuilds State tables and adds
uniqueness for deletion generations. Both require downtime and verified backups.
Migration 0019 adds Task deletion indexes; 0020 adds durable batch deletion operations.
Schema 18/19 can therefore upgrade online.

For Schema 17 or earlier, stop both API and worker, then run:

```sh
iris-memory-core migrate /var/lib/iris-core/data/core.sqlite3 \
  --allow-offline \
  --with-backup /var/lib/iris-core/backups/before-schema-20
```

The CLI creates and verifies the backup before migrating. Use `--backup-key-file`
for backup authentication when needed. Keep backups separate and retain the old
installation artifacts. From Schema 14 the result is `schema_version=20 applied=6`;
from Schema 15/16/17 the applied counts are 5/4/3. Ordinary migration from Schema
18/19 reports 2/1 applied migrations. Repeating a completed migration applies none.
Check `schema-version` and readiness before returning the service to use.

Rollback requires stopping the new processes, restoring the pre-upgrade backup
in isolation and using the installation matching that backup's schema (for example,
Core 0.12.0 for Schema 14). There is no in-place downgrade from Schema 20. Preserve
and reconcile post-upgrade data separately. An interrupted initial database is
handled as an existing database requiring checks, backup and continued migration.
See [ADR-0026](../adr/0026-task-dependency-lifecycle.md),
[ADR-0037](../adr/0037-console-durable-forget-previews.md),
[ADR-0038](../adr/0038-console-entity-tombstone-ledger.md),
[ADR-0040](../adr/0040-console-state-forget-generations.md),
[ADR-0041](../adr/0041-console-task-forget-cascade.md) and
[ADR-0042](../adr/0042-console-forget-operations.md).

When restoring Schema 20, the latest deletion ledger protects committed deletions.
Unfinished deletion operations are reconciled before directory switching and become
`blocked / restore_requires_review`; they do not resume deletion automatically.
Review remaining targets and create a new preview. Cancellation does not undo
committed deletions. Full credential reset and production recovery drills remain
part of Phase 14.5.
