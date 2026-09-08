"""Serve/worker configuration, startup validation and graceful lifecycle."""

from __future__ import annotations

import os
import signal
import threading
import time
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

from iris_memory_core import __version__
from iris_memory_core.api.console.config import ConsoleConfig, parse_bind
from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.notes import NoteService
from iris_memory_core.application.outbox import JobCommit, JobWork, OutboxService
from iris_memory_core.application.recent import RecentContextService
from iris_memory_core.application.retention import RetentionService
from iris_memory_core.application.tasks import TaskService
from iris_memory_core.domain.jobs import OutboxJob
from iris_memory_core.jobs.worker import (
    OutboxWorker,
    phase3_handlers,
    phase4_handlers,
    phase5_handlers,
    phase6_handlers,
    phase7_handlers,
    phase8_handlers,
    phase9_handlers,
    phase10_handlers,
    phase14_handlers,
)
from iris_memory_core.providers.cognitive_deployment import load_cognitive_deployment
from iris_memory_core.recall_runtime import RecallAssemblyConfig, assemble_recall
from iris_memory_core.storage.admin_archives import AdminArchiveService
from iris_memory_core.storage.migrations import MigrationRunner
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    database: Path = Path("./data/canonical.sqlite3")
    host: str = "127.0.0.1"
    port: int = 8765
    grace_seconds: float = 30.0
    poll_seconds: float = 0.1
    migrate: bool = True
    sse_enabled: bool = True
    allow_local_sqlite: bool = False
    backup_root: Path | None = None
    export_root: Path | None = None
    enable_console: bool = False
    console_assets: Path | None = None
    console_bind: str | None = None
    console_origin: str = "https://localhost"
    console_allowed_hosts: tuple[str, ...] = ("localhost",)
    console_trusted_proxy_ips: tuple[str, ...] = ()
    console_dev_http: bool = False
    development_embedding: bool = False
    vector_root: Path | None = None
    vector_required: bool = False
    secret_key_file: Path | None = None
    provider_config_file: Path | None = None
    cognitive_config_file: Path | None = None
    development_cognitive: bool = False
    auto_summary_enabled: bool = False
    summary_min_messages: int = 50
    summary_max_wait_seconds: int = 120
    summary_batch_size: int = 100
    background_retention_days: int = 30

    def recall_config(self) -> RecallAssemblyConfig:
        return RecallAssemblyConfig(
            development_embedding=self.development_embedding,
            vector_root=self.vector_root,
            vector_required=self.vector_required,
            secret_key_file=self.secret_key_file,
            provider_config_file=self.provider_config_file,
        )

    def console_config(self) -> ConsoleConfig:
        host = parse_bind(self.console_bind)[0] if self.console_bind else self.host
        return ConsoleConfig(
            origin=self.console_origin,
            dev_http=self.console_dev_http,
            bind_host=host,
            allowed_hosts=self.console_allowed_hosts,
            trusted_proxy_ips=self.console_trusted_proxy_ips,
            assets=self.console_assets,
        )

    def _context_config(self) -> Any:
        from iris_memory_core.domain.observation_context import ObservationContextConfig

        return ObservationContextConfig(
            auto_summary_enabled=self.auto_summary_enabled,
            summary_min_messages=self.summary_min_messages,
            summary_max_wait_seconds=self.summary_max_wait_seconds,
            summary_batch_size=self.summary_batch_size,
            background_retention_days=self.background_retention_days,
        )

    def validate(self) -> None:
        self._context_config()
        if self.enable_console:
            self.console_config().validate()
            if self.console_bind and parse_bind(self.console_bind) == (self.host, self.port):
                raise ValueError("console bind must differ from the application bind")
            if self.console_assets is not None:
                assets = self.console_assets.resolve()
                roots = (self.database.parent, self.backup_root, self.export_root, self.vector_root)
                if any(
                    root is not None
                    and (
                        root.resolve().is_relative_to(assets)
                        or assets.is_relative_to(root.resolve())
                    )
                    for root in roots
                ):
                    raise ValueError(
                        "console assets must be separate from private data directories"
                    )
        if not self.host or not 1 <= self.port <= 65535:
            raise ValueError("serve host/port is invalid")
        if not 0 < self.grace_seconds <= 300:
            raise ValueError("grace_seconds must be within (0,300]")
        if not 0.01 <= self.poll_seconds <= 60:
            raise ValueError("poll_seconds must be within [0.01,60]")
        database = self.database.expanduser().resolve()
        data_directory = database.parent
        repository_root = Path(__file__).resolve().parents[2]
        unsafe_directories = {Path("/").resolve(), Path.home().resolve(), repository_root}
        if data_directory in unsafe_directories:
            raise ValueError("database must use a dedicated data directory")
        if self.database.exists() and not self.database.is_file():
            raise ValueError("database path is not a regular file")
        data_directory.mkdir(parents=True, exist_ok=True)
        if not os.access(data_directory, os.R_OK | os.W_OK | os.X_OK):
            raise ValueError("database directory is not readable and writable")
        for label, root in (
            ("backup", self.backup_root),
            ("export", self.export_root),
            ("vector", self.vector_root),
        ):
            if root is not None and root.expanduser().resolve() in unsafe_directories:
                raise ValueError(f"{label} root must use a dedicated directory")
        if (
            self.backup_root is not None
            and self.export_root is not None
            and self.backup_root.resolve() == self.export_root.resolve()
        ):
            raise ValueError("backup and export roots must be separate")


def load_config(
    *,
    config_file: Path | None = None,
    cli_values: dict[str, object] | None = None,
    environ: dict[str, str] | None = None,
) -> ServiceConfig:
    """Resolve CLI > environment > TOML > secure defaults."""
    values: dict[str, Any] = {}
    if config_file is not None:
        loaded = tomllib.loads(config_file.read_text(encoding="utf-8"))
        section = loaded.get("service", loaded)
        if not isinstance(section, dict):
            raise ValueError("configuration must contain an object")
        values.update(section)
    env = environ if environ is not None else os.environ
    names = {field.name for field in fields(ServiceConfig)}
    for name in names:
        key = f"IRIS_MEMORY_{name.upper()}"
        if key in env:
            values[name] = env[key]
    values.update({key: value for key, value in (cli_values or {}).items() if value is not None})
    for name in (
        "database",
        "backup_root",
        "export_root",
        "console_assets",
        "vector_root",
        "secret_key_file",
        "provider_config_file",
        "cognitive_config_file",
    ):
        if name in values and values[name] is not None:
            values[name] = Path(str(values[name]))
    for name in (
        "port",
        "summary_min_messages",
        "summary_max_wait_seconds",
        "summary_batch_size",
        "background_retention_days",
    ):
        if name in values:
            values[name] = int(values[name])
    for name in ("grace_seconds", "poll_seconds"):
        if name in values:
            values[name] = float(values[name])
    for name in ("console_allowed_hosts", "console_trusted_proxy_ips"):
        if name in values:
            raw_list = values[name]
            if isinstance(raw_list, str):
                values[name] = tuple(item.strip() for item in raw_list.split(",") if item.strip())
            elif isinstance(raw_list, list) and all(isinstance(item, str) for item in raw_list):
                values[name] = tuple(raw_list)
            elif not isinstance(raw_list, tuple):
                raise ValueError(f"{name} must be a list of strings")
    for name in (
        "auto_summary_enabled",
        "migrate",
        "sse_enabled",
        "allow_local_sqlite",
        "enable_console",
        "console_dev_http",
        "development_embedding",
        "development_cognitive",
        "vector_required",
    ):
        if name in values and isinstance(values[name], str):
            raw = values[name].strip().lower()
            if raw not in {"true", "false", "1", "0", "yes", "no"}:
                raise ValueError(f"{name} must be boolean")
            values[name] = raw in {"true", "1", "yes"}
    unknown = set(values) - names
    if unknown:
        raise ValueError(f"unknown service configuration keys: {sorted(unknown)}")
    config = replace(ServiceConfig(), **values)
    config.validate()
    return config


def open_store(config: ServiceConfig) -> Store:
    config.validate()
    if config.migrate:
        MigrationRunner(config.database).migrate(app_version=__version__)
    allowed = (sqlite_runtime_version(),) if config.allow_local_sqlite else None
    runtime = SQLiteRuntime(config.database, allowed_versions=allowed)
    store = Store(runtime)
    store.statistics_roots["storage.staging_bytes"] = (
        config.export_root or config.database.parent / "exports"
    )
    if config.vector_root is not None:
        store.statistics_roots["storage.vector_bytes"] = config.vector_root
    # Forces runtime allowlist + schema-window validation before Ready.
    with store.read() as tx:
        tx.outbox.status_counts()
        if tx.personas.integrity_problems():
            raise RuntimeError("Persona pointer integrity check failed")
        for projection in (tx.fts, tx.vector, tx.profile, tx.graph):
            projection.projection_state()
    from iris_memory_core.providers.secret_lifecycle import (
        deployment_secrets,
        validate_sealed_startup,
    )

    validate_sealed_startup(store, deployment_secrets(config.database, config.secret_key_file))
    return store


def serve(config: ServiceConfig) -> int:
    import uvicorn

    from iris_memory_core.api import create_app

    store = open_store(config)
    cognitive = load_cognitive_deployment(
        store,
        config.cognitive_config_file,
        development_cognitive=config.development_cognitive,
        master_key_file=config.secret_key_file,
    )
    app = create_app(
        store,
        cognitive_tenants=frozenset(cognitive.providers) if cognitive else frozenset(),
        sse_enabled=config.sse_enabled,
        backup_root=config.backup_root,
        export_root=config.export_root,
        enable_console=config.enable_console and config.console_bind is None,
        console_config=config.console_config() if config.enable_console else None,
        recall_config=config.recall_config(),
        observation_context_config=config._context_config(),
    )
    if config.enable_console and config.console_bind:
        from iris_memory_core.api.console.listening import serve_separate

        return serve_separate(app, config)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.host,
            port=config.port,
            log_level="info",
            timeout_graceful_shutdown=max(1, int(config.grace_seconds)),
            lifespan="on",
            proxy_headers=not config.enable_console,
            access_log=not config.enable_console,
        )
    )
    server.run()
    return 0


def worker(config: ServiceConfig, *, once: bool = False) -> int:
    store = open_store(config)
    cognitive = load_cognitive_deployment(
        store,
        config.cognitive_config_file,
        development_cognitive=config.development_cognitive,
        master_key_file=config.secret_key_file,
    )
    cognitive_handlers: dict[str, JobWork] = {}
    if cognitive is not None:

        def cognitive_work(job: OutboxJob) -> JobCommit:
            assert cognitive is not None
            pipeline = cognitive.pipeline(store, job.tenant_id)
            return phase10_handlers(pipeline=pipeline)[job.job_kind](job)

        cognitive_handlers = {
            kind: cognitive_work
            for kind in (
                "observation.summarize",
                "episode.consolidation",
                "reflection.generate",
                "memory.reconciliation",
                "persona.evaluation",
            )
        }
    service = OutboxService(store, store.clock)
    recent = RecentContextService(store, store.clock)
    focus = FocusService(store, store.clock)
    notes = NoteService(store, store.clock)
    tasks = TaskService(store, store.clock)
    retention = RetentionService(store, store.clock, forget=ForgetService(store, store.clock))
    projections = assemble_recall(store, store.clock, config.recall_config())
    handlers = {
        **phase3_handlers(store, store.clock, recent=recent, focus=focus),
        **phase4_handlers(store.clock, notes=notes, tasks=tasks),
        **phase5_handlers(store.clock, retention=retention),
        **phase6_handlers(store.clock, projection=projections.fts),
        **(phase7_handlers(projection=projections.vector) if projections.vector else {}),
        **phase8_handlers(
            graph=projections.graph,
            profile=projections.profile,
        ),
        **phase9_handlers(store.clock),
        **cognitive_handlers,
        **phase14_handlers(
            store,
            store.clock,
            store.ids,
            embedding_runtime=projections.embedding_runtime,
            provider_generations=projections.provider_generations,
            archives=AdminArchiveService(
                store,
                backup_root=config.backup_root or config.database.parent / "backups",
                export_root=config.export_root or config.database.parent / "exports",
            ),
        ),
    }
    from iris_memory_core.application.observation_context import ObservationContextService
    from iris_memory_core.application.scheduler import SchedulerService

    context = ObservationContextService(store, store.clock, config=config._context_config())
    scheduler = SchedulerService(store, store.clock, enabled_kinds=frozenset(handlers))
    next_maintenance = 0.0
    runtime = OutboxWorker(service, handlers)
    stopping = threading.Event()

    def request_stop(signum: int, frame: object) -> None:
        del signum, frame
        stopping.set()

    previous = {
        signal_name: signal.signal(signal_name, request_stop)
        for signal_name in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        while not stopping.is_set():
            if time.monotonic() >= next_maintenance:
                scheduler.advance()
                context.advance(frozenset(cognitive.providers) if cognitive else frozenset())
                context.expire_background()
                next_maintenance = time.monotonic() + 1.0
            runtime.run_once()
            if once:
                break
            stopping.wait(config.poll_seconds)
    finally:
        deadline = time.monotonic() + config.grace_seconds
        while time.monotonic() < deadline:
            # Serial workers have no in-flight task after run_once returns;
            # this bounded hook documents and enforces the Grace Deadline.
            break
        for signal_name, handler in previous.items():
            signal.signal(signal_name, handler)
    return 0


__all__ = ["ServiceConfig", "load_config", "open_store", "serve", "worker"]
