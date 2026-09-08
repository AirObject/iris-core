"""Public, no-listener embedded business API (ADR-0053).

Import/construction performs no I/O. Private implementation is loaded at start.
JSON requests and responses use the same frozen contract as HTTP.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self, cast

if TYPE_CHECKING:
    from iris_memory_core.application.ports.providers import EmbeddingProvider
    from iris_memory_core.embedded_providers import AsyncCognitiveAdapter, AsyncEmbeddingAdapter

__all__ = ["EmbeddedConfig", "EmbeddedError", "EmbeddedMemory", "LocalBootstrap"]


@dataclass(frozen=True)
class LocalBootstrap:
    """Explicit trusted initialization grant for an exclusive local data directory.

    Application operations remain constrained by these grants. Persona publication
    additionally requires manage_persona; this does not grant other management APIs.
    """

    tenant_id: str = "iris-local"
    app_instance_id: str = "iris-plugin"
    agent_name: str = "Iris"
    capabilities: tuple[str, ...] = (
        "observe.batch.v1",
        "claims.v1",
        "recall.v1",
        "active-surface.v1",
        "search.fts.v1",
        "recall.graph.v1",
        "persona.v1",
        "memory-forget.v1",
        "persona.read.v1",
        "persona.state.v1",
        "observation-context.v1",
        "consolidation.v1",
        "recent-context.v1",
        "source-cursor.v1",
    )
    data_purposes: tuple[str, ...] = ("reply",)
    manage_persona: bool = False
    manage_identities: bool = False
    manage_indexes: bool = False
    manage_agents: bool = False
    surface_mode: str = "off"


@dataclass(frozen=True)
class EmbeddedConfig:
    data_directory: Path
    bootstrap: LocalBootstrap = field(default_factory=LocalBootstrap)
    background: bool = False
    poll_seconds: float = 0.25
    close_timeout_seconds: float = 5.0
    max_pending: int = 32
    allow_local_sqlite: bool = False
    development_embedding: bool = False
    auto_summary_enabled: bool = False
    summary_min_messages: int = 50
    summary_max_wait_seconds: int = 120
    summary_batch_size: int = 100
    background_retention_days: int = 30

    def _context_config(self) -> Any:
        from iris_memory_core.domain.observation_context import ObservationContextConfig

        return ObservationContextConfig(
            auto_summary_enabled=self.auto_summary_enabled,
            summary_min_messages=self.summary_min_messages,
            summary_max_wait_seconds=self.summary_max_wait_seconds,
            summary_batch_size=self.summary_batch_size,
            background_retention_days=self.background_retention_days,
        )

    def __post_init__(self) -> None:
        self._context_config()
        if not 0.01 <= self.poll_seconds <= 60:
            raise ValueError("poll_seconds must be within [0.01,60]")
        if not 0 < self.close_timeout_seconds <= 300:
            raise ValueError("close_timeout_seconds must be within (0,300]")
        if not 1 <= self.max_pending <= 1024:
            raise ValueError("max_pending must be within [1,1024]")
        if self.bootstrap.surface_mode not in {"off", "advisory", "required"}:
            raise ValueError("surface_mode must be off, advisory or required")
        if not self.bootstrap.tenant_id or not self.bootstrap.app_instance_id:
            raise ValueError("bootstrap tenant and application must be explicit")


class EmbeddedError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        request_id: str,
        retryable: bool = False,
        result_unknown: bool = False,
    ) -> None:
        self.code = code
        self.request_id = request_id
        self.retryable = retryable
        self.result_unknown = result_unknown
        super().__init__(message)


class _Query(dict[str, str]):
    def getlist(self, name: str) -> list[str]:
        return [self[name]] if name in self else []


class _Headers(dict[str, str]):
    def get(self, key: str, default: str | None = None) -> str | None:  # type: ignore[override]
        return super().get(key.lower(), default)


class EmbeddedMemory:
    def __init__(
        self,
        config: EmbeddedConfig,
        *,
        embedding: EmbeddingProvider | AsyncEmbeddingAdapter | None = None,
        cognitive: AsyncCognitiveAdapter | None = None,
    ) -> None:
        self._config = config
        self._embedding = embedding
        self._cognitive = cognitive
        self._state = "new"
        self._lifecycle = asyncio.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._pending: set[asyncio.Future[Any]] = set()
        self._request_states: dict[asyncio.Future[Any], dict[str, Any]] = {}
        self._recent_outcomes: deque[dict[str, Any]] = deque(maxlen=64)
        self._background: asyncio.Task[None] | None = None
        self._runtime: Any = None
        self._worker: Any = None
        self._lock_file: Any = None
        self._bridge: Any = None
        self._access: Any = None
        self._scope: dict[str, str] = {}
        self._operations: dict[str, Any] = {}
        self._contract: dict[str, Any] = {}
        self._last_background_error: str | None = None
        self._manifest: dict[str, Any] = {}

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def start(self) -> dict[str, str]:
        async with self._lifecycle:
            if self._state == "running":
                return dict(self._scope)
            if self._state == "closing":
                raise self._error("not_ready", "previous shutdown is still draining")
            self._state = "starting"
            from iris_memory_core.embedded_providers import _HostBridge

            self._bridge = _HostBridge(asyncio.get_running_loop())
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="iris-embedded")
            future = self._submit(self._open)
            try:
                await asyncio.shield(future)
            except BaseException:
                self._state = "closing"
                self._bridge.cancel()
                # A cancelled start may still be opening the store. Retain the
                # lock until that work ends, then clean up on the host loop.
                if future.done():
                    self._finish_close()
                else:
                    future.add_done_callback(lambda _: self._finish_close())
                raise
            self._state = "running"
            if self._config.background:
                self._background = asyncio.create_task(self._maintain(), name="iris-maintenance")
            return dict(self._scope)

    def _submit(
        self, call: Callable[[], Any], request_id: str | None = None
    ) -> asyncio.Future[Any]:
        assert self._executor is not None
        future = asyncio.get_running_loop().run_in_executor(self._executor, call)
        self._pending.add(future)
        if request_id is not None:
            self._request_states[future] = {"request_id": request_id, "state": "running"}
        future.add_done_callback(self._completed)
        return future

    def _completed(self, future: asyncio.Future[Any]) -> None:
        self._pending.discard(future)
        outcome = self._request_states.pop(future, None)
        if outcome is not None:
            error = future.exception() if not future.cancelled() else None
            outcome["state"] = "failed" if error or future.cancelled() else "completed"
            outcome["code"] = getattr(error, "code", "internal_error") if error else None
            self._recent_outcomes.append(outcome)
        if not future.cancelled():
            future.exception()  # consume failures even when the caller cancelled

    def _open(self) -> None:
        import fcntl

        from iris_memory_core import __version__
        from iris_memory_core.application.security import CredentialService
        from iris_memory_core.business import BusinessRuntime, _load_contract
        from iris_memory_core.domain.access import AccessContext
        from iris_memory_core.embedded_providers import AsyncEmbeddingAdapter, _Embedding
        from iris_memory_core.recall_runtime import RecallAssemblyConfig
        from iris_memory_core.storage.migrations import MigrationRunner
        from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
        from iris_memory_core.storage.uow import Store

        directory = self._config.data_directory.expanduser().resolve()
        if directory in {Path("/"), Path.home().resolve()}:
            raise ValueError("a dedicated data directory is required")
        directory.mkdir(parents=True, exist_ok=True)
        self._lock_file = (directory / ".embedded.lock").open("a+b")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise self._error("conflict", "data directory already has an embedded owner") from None
        database = directory / "canonical.sqlite3"
        allowed = (sqlite_runtime_version(),) if self._config.allow_local_sqlite else None
        runtime = SQLiteRuntime(database, allowed_versions=allowed)
        runtime.runtime_report.require_allowed()
        MigrationRunner(database).migrate(app_version=__version__)
        store = Store(runtime)
        embedding = self._embedding
        if isinstance(embedding, AsyncEmbeddingAdapter):
            embedding = _Embedding(embedding, self._bridge)
        self._runtime = BusinessRuntime(
            store,
            CredentialService(store, store.clock),
            sse_enabled=False,
            recall_config=RecallAssemblyConfig(
                embedding=embedding,
                vector_root=directory / "vector",
                development_embedding=self._config.development_embedding,
            ),
            observation_context_config=self._config._context_config(),
            cognitive_tenants=(
                frozenset({self._config.bootstrap.tenant_id}) if self._cognitive else frozenset()
            ),
        )
        bootstrap = self._config.bootstrap
        provisioning = self._runtime.provisioning
        provisioning.create_tenant(bootstrap.tenant_id, idempotency_key="embedded-tenant-v1")
        trusted = AccessContext(
            tenant_id=bootstrap.tenant_id,
            app_instance_id=bootstrap.app_instance_id,
            admin=True,
        )
        manifest_path = directory / "embedded-bootstrap.json"
        identity = {
            "tenant_id": bootstrap.tenant_id,
            "app_instance_id": bootstrap.app_instance_id,
            "agent_name": bootstrap.agent_name,
            "surface_mode": bootstrap.surface_mode,
        }
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            self._manifest = manifest
            if manifest["identity"] != identity:
                raise self._error("conflict", "bootstrap identity differs from the persisted owner")
            with store.read() as tx:
                agent = tx.get_agent(manifest["agent_id"])
                space = tx.get_space(manifest["space_id"])
            if agent.tenant_id != bootstrap.tenant_id or space.agent_id != agent.id:
                raise self._error("conflict", "bootstrap registration is inconsistent")
        else:
            agent = provisioning.create_agent(
                trusted, bootstrap.agent_name, idempotency_key="embedded-agent-v1"
            )
            trusted = replace(trusted, agent_ids=frozenset({agent.id}))
            space = provisioning.create_space(
                trusted, "local", agent_id=agent.id, idempotency_key="embedded-space-v1"
            )
            if bootstrap.surface_mode != "off":
                from iris_memory_core.domain.surface import SurfaceMode

                with store.write() as tx:
                    _, _, revision = tx.surfaces.state(bootstrap.tenant_id, agent.id)
                    tx.surfaces.set_mode(
                        bootstrap.tenant_id,
                        agent.id,
                        SurfaceMode(bootstrap.surface_mode),
                        expected_revision=revision,
                    )
            import os
            import tempfile

            descriptor, temporary = tempfile.mkstemp(prefix=".bootstrap-", dir=directory)
            try:
                with os.fdopen(descriptor, "w") as output:
                    json.dump(
                        {"identity": identity, "agent_id": agent.id, "space_id": space.id}, output
                    )
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, manifest_path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        if not self._manifest:
            self._manifest = {"identity": identity, "agent_id": agent.id, "space_id": space.id}
        registrations = list(self._manifest.get("agents", {}).values())
        with store.read() as tx:
            for registration in registrations:
                registered_agent = tx.get_agent(registration["agent_id"])
                registered_space = tx.get_space(registration["space_id"])
                if (
                    registered_agent.tenant_id != bootstrap.tenant_id
                    or registered_space.agent_id != registered_agent.id
                ):
                    raise self._error("conflict", "registered agent scope is inconsistent")
        trusted = replace(
            trusted, agent_ids=frozenset([agent.id, *(item["agent_id"] for item in registrations)])
        )
        self._access = replace(
            trusted,
            admin=False,
            allowed_space_ids=frozenset([space.id, *(item["space_id"] for item in registrations)]),
            capabilities=(
                frozenset(bootstrap.capabilities)
                - {
                    "persona.manage.v1",
                    "persona.mirror.v1",
                    "persona.state.write.v1",
                    "persona.review.v1",
                    "events.sse.v1",
                    "events.checkpoint.v1",
                    "metrics.v1",
                    "admin.audit.v1",
                    "admin.backup.v1",
                    "admin.export.v1",
                    "admin.index-rebuild.v1",
                    "admin.recent-context-rebuild.v1",
                    "outbox.jobs.v1",
                    "schedules.v1",
                    "space-groups.v1",
                }
            )
            | (
                frozenset({"persona.mirror.v1", "persona.state.write.v1"})
                if bootstrap.manage_persona
                else frozenset()
            ),
            data_purposes=frozenset(bootstrap.data_purposes),
        )
        if bootstrap.manage_indexes:
            self._access = replace(
                self._access, capabilities=self._access.capabilities | {"admin.index-rebuild.v1"}
            )
        self._scope = {"tenant_id": bootstrap.tenant_id, "agent_id": agent.id, "space_id": space.id}
        self._contract = _load_contract()
        self._operations = {
            operation["operationId"]: operation
            for path in self._contract["paths"].values()
            for method, operation in path.items()
            if method in {"get", "post", "put", "patch", "delete"}
        }
        self._build_worker()

    def _build_worker(self) -> None:
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
        )

        runtime = self._runtime
        handlers = {
            **phase3_handlers(
                runtime.uow, runtime.clock, recent=runtime.recent, focus=runtime.focus
            ),
            **phase4_handlers(runtime.clock, notes=runtime.notes, tasks=runtime.tasks),
            **phase5_handlers(runtime.clock, retention=runtime.retention),
            **phase6_handlers(runtime.clock, projection=runtime.fts),
            **phase8_handlers(graph=runtime.projections.graph, profile=runtime.profiles),
            **phase9_handlers(runtime.clock),
        }
        if runtime.projections.vector is not None:
            handlers.update(phase7_handlers(projection=runtime.projections.vector))
        if self._cognitive is not None:
            from iris_memory_core.application.reflection import ReflectionPipeline
            from iris_memory_core.embedded_providers import _Cognitive
            from iris_memory_core.providers.cognitive import (
                DurableProviderState,
                ProviderGovernance,
            )

            provider = _Cognitive(self._cognitive, self._bridge)
            pipeline = ReflectionPipeline(
                runtime.uow,
                runtime.clock,
                governance=ProviderGovernance(
                    {"extraction": self._cognitive.limits, "summarization": self._cognitive.limits},
                    durable_state=DurableProviderState(runtime.uow, runtime.clock),
                ),
                extraction=provider,
                summarization=provider,
            )
            handlers.update(phase10_handlers(pipeline=pipeline))
        from iris_memory_core.application.scheduler import SchedulerService

        runtime.scheduler = SchedulerService(
            runtime.uow, runtime.clock, enabled_kinds=frozenset(handlers)
        )
        self._worker = OutboxWorker(runtime.outbox, handlers, concurrency=1)

    async def _maintain(self) -> None:
        while self._state == "running":
            try:
                await self.run_pending()
                self._last_background_error = None
            except EmbeddedError as error:
                self._last_background_error = error.code
            await asyncio.sleep(self._config.poll_seconds)

    async def run_pending(self) -> dict[str, int]:
        """Process one bounded durable batch; no automatic model work by default."""

        def maintain() -> dict[str, int]:
            self._runtime.scheduler.advance()
            self._runtime.observation_context.advance(
                self._runtime.cognitive_tenants, local_access=self._access
            )
            self._runtime.observation_context.expire_background()
            return cast(dict[str, int], self._worker.run_once())

        return await self._call(maintain)  # type: ignore[no-any-return]

    async def execute(
        self,
        operation_id: str,
        body: dict[str, Any] | None = None,
        *,
        path_parameters: Mapping[str, str] | None = None,
        query_parameters: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Invoke a supported business operation using its frozen operationId and JSON DTOs."""
        # Copy before crossing threads; reject non-JSON values and NaN.
        if body is not None and type(body) is not dict:
            raise self._error("invalid_request", "request body must be a JSON object")
        try:
            copied = json.loads(json.dumps(body if body is not None else {}, allow_nan=False))
        except (TypeError, ValueError):
            raise self._error("invalid_request", "request must contain JSON values") from None
        request = SimpleNamespace(
            headers=_Headers({"idempotency-key": idempotency_key} if idempotency_key else {}),
            path_params=dict(path_parameters or {}),
            query_params=_Query(query_parameters or {}),
            state=SimpleNamespace(request_id=str(uuid.uuid4())),
        )
        return await self._call(
            lambda: self._execute(operation_id, copied, request),
            request_id=request.state.request_id,
        )

    def _execute(self, operation_id: str, body: dict[str, Any], request: Any) -> Any:
        from jsonschema import Draft202012Validator

        from iris_memory_core.business import (
            _dereference_schema,
            _jsonable,
            _narrow,
            _request_schema,
            _require_management,
            _validate_parameters,
        )
        from iris_memory_core.domain.errors import InvalidRequestError, UnsupportedVersionError

        operation = self._operations.get(operation_id)
        if operation is None:
            raise UnsupportedVersionError("unknown business operation")
        access = self._access
        _validate_parameters(request, self._contract, operation)
        schema = _request_schema(operation)
        if schema is not None and list(
            Draft202012Validator(
                cast(dict[str, Any], _dereference_schema(self._contract, schema)),
            ).iter_errors(body)
        ):
            raise InvalidRequestError("request body failed the frozen schema")
        _narrow(access, body, request)
        if self._config.bootstrap.manage_persona and operation_id in {
            "updatePersonaState",
        }:
            access = replace(access, admin=True)
        if self._config.bootstrap.manage_indexes and operation_id == "rebuildIndex":
            access = replace(access, admin=True)
        _require_management(access, operation_id)
        result = self._runtime.dispatch(operation_id, access, request, body)
        if result is None:
            raise UnsupportedVersionError("operation is unavailable in embedded mode")
        return _jsonable(result[1])

    async def _call(self, call: Callable[[], Any], *, request_id: str | None = None) -> Any:
        if self._state != "running":
            raise self._error("not_ready", "embedded instance is not accepting requests")
        if len(self._pending) >= self._config.max_pending:
            raise self._error("database_busy", "embedded request capacity exhausted")
        from iris_memory_core.domain.errors import DomainError

        try:
            future = self._submit(call, request_id)
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                if future in self._request_states:
                    self._request_states[future]["caller_cancelled"] = True
                raise
        except DomainError as error:
            raise EmbeddedError(
                error.code,
                str(error.args[0]) if error.args else "request failed",
                request_id=request_id or str(uuid.uuid4()),
                retryable=error.retryable,
            ) from None

        except EmbeddedError:
            raise
        except Exception:
            raise EmbeddedError(
                "internal_error",
                "internal service error",
                request_id=request_id or str(uuid.uuid4()),
                result_unknown=True,
            ) from None

    async def provision_agent(self, display_name: str, *, key: str) -> dict[str, str]:
        """Persist a plugin persona -> Agent/Space registration under a stable local key."""
        if not self._config.bootstrap.manage_agents:
            raise self._error("access_denied", "local agent management is not granted")
        if not all(
            isinstance(value, str) and 0 < len(value) <= 128 for value in (display_name, key)
        ):
            raise self._error(
                "invalid_request", "agent name and key must contain 1..128 characters"
            )

        def provision() -> dict[str, str]:
            registrations = self._manifest.get("agents", {})
            existing = registrations.get(key)
            if existing is not None:
                if existing["display_name"] != display_name:
                    raise self._error(
                        "idempotency_key_reused", "agent key already has another name"
                    )
                return {name: str(existing[name]) for name in ("agent_id", "space_id")}
            if len(registrations) >= 128:
                raise self._error("invalid_request", "local agent registration limit is 128")
            access = replace(self._access, admin=True)
            service = self._runtime.provisioning
            agent = service.create_agent(access, display_name, idempotency_key="agent:" + key)
            access = replace(access, agent_ids=access.agent_ids | {agent.id})
            space = service.create_space(
                access, "local", agent_id=agent.id, idempotency_key="space:" + key
            )
            from iris_memory_core.domain.surface import SurfaceMode

            with self._runtime.uow.write() as tx:
                _, _, revision = tx.surfaces.state(access.tenant_id, agent.id)
                tx.surfaces.set_mode(
                    access.tenant_id,
                    agent.id,
                    SurfaceMode(self._config.bootstrap.surface_mode),
                    expected_revision=revision,
                )
            import os
            import tempfile

            updated = {
                **self._manifest,
                "agents": {
                    **registrations,
                    key: {
                        "display_name": display_name,
                        "agent_id": agent.id,
                        "space_id": space.id,
                    },
                },
            }
            directory = self._config.data_directory.expanduser().resolve()
            descriptor, temporary = tempfile.mkstemp(prefix=".bootstrap-", dir=directory)
            try:
                with os.fdopen(descriptor, "w") as output:
                    json.dump(updated, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, directory / "embedded-bootstrap.json")
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self._manifest = updated
            self._access = replace(
                self._access,
                agent_ids=access.agent_ids,
                allowed_space_ids=self._access.allowed_space_ids | {space.id},
            )
            return {"agent_id": agent.id, "space_id": space.id}

        return await self._call(provision)  # type: ignore[no-any-return]

    async def register_actor(
        self,
        provider: str,
        external_id: str,
        *,
        display_name: str,
        realm: str = "default",
        idempotency_key: str,
    ) -> dict[str, str]:
        """Trusted local identity provisioning; never rebind an existing identity."""
        if not self._config.bootstrap.manage_identities:
            raise self._error("access_denied", "local identity management is not granted")
        if not all(
            isinstance(value, str) and 0 < len(value) <= 256
            for value in (provider, external_id, display_name, realm, idempotency_key)
        ):
            raise self._error("invalid_request", "actor fields must be nonempty bounded strings")

        def register() -> dict[str, str]:
            from iris_memory_core.domain.identity import EntityKind, ExternalIdentityKey

            access = replace(self._access, admin=True)
            service = self._runtime.identities
            with self._runtime.uow.read() as tx:
                existing = tx.find_external_identity(
                    ExternalIdentityKey(access.tenant_id, provider, realm, external_id)
                )
                if existing:
                    binding = tx.verified_binding_for(existing.id)
                    if binding is None or tx.is_tombstoned(
                        access.tenant_id, "external_identity", existing.id
                    ):
                        raise self._error("conflict", "identity requires explicit binding review")
                    self._access = replace(
                        self._access,
                        consent_subject_entity_ids=(
                            self._access.consent_subject_entity_ids | {binding.entity_id}
                        ),
                    )
                    return {"entity_id": binding.entity_id, "identity_id": existing.id}
            entity = service.create_entity(
                access,
                EntityKind.PERSON,
                display_name=display_name,
                idempotency_key=idempotency_key + ":entity",
            )
            identity = service.register_external_identity(
                access,
                provider,
                realm,
                external_id,
                entity_id=entity.id,
                idempotency_key=idempotency_key + ":identity",
            )
            binding = service.propose_binding(
                access,
                identity.id,
                entity.id,
                proof_digest=idempotency_key,
                reason="trusted_local_registration",
                idempotency_key=idempotency_key + ":binding",
            )
            service.confirm_binding(
                access,
                binding.id,
                expected_revision=binding.revision,
                reason="trusted_local_registration",
                idempotency_key=idempotency_key + ":confirm",
            )
            self._access = replace(
                self._access,
                consent_subject_entity_ids=(self._access.consent_subject_entity_ids | {entity.id}),
            )
            return {"entity_id": entity.id, "identity_id": identity.id}

        return await self._call(register)  # type: ignore[no-any-return]

    async def diagnostics(self) -> dict[str, Any]:
        """Local resource ownership only; never invokes a Provider."""
        import sqlite3

        return {
            "state": self._state,
            "in_flight": len(self._pending),
            "pending_requests": [dict(item) for item in self._request_states.values()],
            "recent_outcomes": [dict(item) for item in self._recent_outcomes],
            "background": self._background is not None and not self._background.done(),
            "provider_in_flight": len(self._bridge.tasks) if self._bridge else 0,
            "last_background_error": self._last_background_error,
            "sqlite_version": sqlite3.sqlite_version,
            "scope": dict(self._scope),
        }

    async def aclose(self) -> None:
        try:
            async with asyncio.timeout(self._config.close_timeout_seconds):
                await self._lifecycle.acquire()
        except TimeoutError:
            raise self._error(
                "deadline_exceeded", "startup or another shutdown is still running"
            ) from None
        try:
            if self._state in {"new", "closed"}:
                self._state = "closed"
                return
            self._state = "closing"
            if self._background is not None:
                self._background.cancel()
                await asyncio.gather(self._background, return_exceptions=True)
                self._background = None
            if self._bridge is not None:
                self._bridge.cancel()
            pending = set(self._pending) | (set(self._bridge.tasks) if self._bridge else set())
            if pending:
                _, unfinished = await asyncio.wait(
                    pending, timeout=self._config.close_timeout_seconds
                )
                if unfinished:
                    raise self._error("deadline_exceeded", "shutdown is still draining")
            self._finish_close()
        finally:
            self._lifecycle.release()

    def _finish_close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        self._worker = None
        self._runtime = None
        self._access = None
        self._operations = {}
        self._contract = {}
        self._manifest = {}
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None
        self._state = "closed"

    @staticmethod
    def _error(code: str, message: str) -> EmbeddedError:
        return EmbeddedError(
            code,
            message,
            request_id=str(uuid.uuid4()),
            retryable=code in {"not_ready", "database_busy", "deadline_exceeded"},
        )

    async def capabilities(self) -> dict[str, Any]:
        return await self.execute("getCapabilities")  # type: ignore[no-any-return]

    async def observation_context(self, request: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], await self.execute("readObservationContext", request))

    async def summarize_observations(
        self, request: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self.execute(
                "summarizeObservationContext", request, idempotency_key=idempotency_key
            ),
        )

    async def observe_batch(
        self,
        records: list[dict[str, Any]],
        *,
        idempotency_key: str | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"records": records}
        if lease_id is not None:
            body.update(lease_id=lease_id, lease_epoch=lease_epoch)
        return await self.execute("observeBatch", body, idempotency_key=idempotency_key)  # type: ignore[no-any-return]

    async def remember_claim(
        self, record: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        return await self.execute("createClaimRemember", record, idempotency_key=idempotency_key)  # type: ignore[no-any-return]

    async def correct_claim(
        self,
        claim_id: str,
        record: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self.execute(  # type: ignore[no-any-return]
            "correctClaim",
            record,
            path_parameters={"claim_id": claim_id},
            idempotency_key=idempotency_key,
        )

    async def forget_memory(
        self, record: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        return await self.execute("forgetMemory", record, idempotency_key=idempotency_key)  # type: ignore[no-any-return]

    async def recall(self, record: dict[str, Any]) -> dict[str, Any]:
        return await self.execute("recall", record)  # type: ignore[no-any-return]
