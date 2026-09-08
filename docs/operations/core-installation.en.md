# Core installation and trusted initialization

2026-09-08 新实施项：[Observation 全量上下文与批量总结](../development/observation-context.md)。统一使用 Observation 保存背景/交互原始事件，复用 Episode 保存分组摘要；自动总结显式开启，背景默认保留 30 天可配置。已接通实现，使用方式与本轮验证进度见链接说明；下文历史验收记录仅代表当时版本。

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
uv pip install --python /path/to/client-venv/bin/python /path/to/iris_memory_sdk-0.12.0-py3-none-any.whl
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
Graph recall is assembled by default. Vector support requires an explicit embedding
configuration shared by API and worker; without one, vector jobs remain pending and
Vector capabilities are omitted. The [development setup](../../README.md#run-from-the-checkout)
documents the opt-in deterministic embedding and required-readiness settings.
These development settings do not establish production Provider acceptance.

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

## Upgrade to Core 0.16.0 / Schema 25

The runtime supports Schema 25. Migration 0025 adds Observation context metadata,
processing ledgers and query indexes. Upgrading Schema 24 uses ordinary `migrate`
after stopping the old processes and reports `schema_version=25 applied=1`.
A fresh database applies all 25 migrations.

Schema 23 or earlier must still satisfy the offline and verified-backup requirements
of earlier migrations, including 0015, 0017, 0018, 0021, 0022 and 0024. Stop the API
and Worker, then run:

```sh
iris-memory-core migrate /var/lib/iris-core/data/core.sqlite3 \
  --allow-offline \
  --with-backup /var/lib/iris-core/backups/before-schema-25
```

The CLI verifies the backup before migration. From Schema 14 the expected result is
`schema_version=25 applied=11`; from Schema 23 it is `schema_version=25 applied=2`.
Retain the old installation artifacts and verify schema-version and readiness before
restarting. Automatic model summaries remain disabled by default. Configure
`auto_summary_enabled`, `summary_min_messages` (50), `summary_max_wait_seconds` (120),
`summary_batch_size` (100), and `background_retention_days` (30) under `[service]` or
through matching `IRIS_MEMORY_` environment variables. Background messages are
Observations; only explicitly cited sources outlive ordinary expiry.

Rollback requires stopping the new processes, restoring the pre-upgrade backup
in isolation and using the installation matching that backup's schema (for example,
Core 0.12.0 for Schema 14). There is no in-place downgrade from Schema 25. Preserve
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
