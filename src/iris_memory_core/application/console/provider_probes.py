"""Real bounded Provider probes committed through existing Outbox lease fencing."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

from iris_memory_core.application.console.execution_context import ExecutionContext
from iris_memory_core.application.console.operations import ConsoleOperations
from iris_memory_core.application.outbox import JobCommit
from iris_memory_core.application.ports.provider_secrets import ConfiguredEmbeddingRuntime
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.domain.console import OperatorPrincipal
from iris_memory_core.domain.console_operations import (
    ConsoleOperation,
    OperationProblem,
    ProviderOperationPayload,
)
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    ConflictError,
    IdempotencyUnavailableError,
    InvalidRequestError,
    LeaseFencedError,
    NotFoundError,
)
from iris_memory_core.domain.hashing import request_fingerprint
from iris_memory_core.domain.jobs import JOB_PAYLOAD_VERSION, JobLane, NewOutboxJob, OutboxJob
from iris_memory_core.domain.provider_configs import (
    PROBE_INPUT_CHARS,
    ProviderConfigRevision,
    ProviderProbe,
    ProviderProbeObservation,
)
from iris_memory_core.domain.vector import EmbeddingProviderError

PROBE_JOB_KIND = "console.embedding_probe"
# Mirrors the fixed non-business adapter probe. This is a conservative character
# charge even when a breaker, configuration failure or timeout prevents sending.
PROBE_CHAR_CHARGE = PROBE_INPUT_CHARS


@dataclass(frozen=True, slots=True)
class ProviderProbeBudget:
    attempts: int = 12
    input_chars: int = 372
    window_us: int = 60_000_000

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 1
            for value in (self.attempts, self.input_chars, self.window_us)
        ):
            raise InvalidRequestError("invalid provider probe budget")


class ProviderProbes:
    def __init__(
        self,
        context: ExecutionContext,
        runtime: ConfiguredEmbeddingRuntime | None,
        *,
        budget: ProviderProbeBudget | None = None,
    ) -> None:
        self.context, self.runtime = context, runtime
        self.operations = ConsoleOperations(context)
        self.budget = budget or ProviderProbeBudget()

    def accept(
        self,
        principal: OperatorPrincipal,
        identifier: str,
        *,
        expected_revision: int,
        reason: str,
        idempotency_key: str,
    ) -> ConsoleOperation:
        if (
            reason != "operator_request"
            or not idempotency_key
            or type(expected_revision) is not int
            or expected_revision < 1
        ):
            raise InvalidRequestError("invalid provider probe request")
        runner = self.context.idempotency
        if runner is None:
            raise IdempotencyUnavailableError("management idempotency is unavailable")
        with self.context.uow.read() as tx:
            initial = self.operations.authorize_type(
                tx, principal, "embedding_provider", recent=True
            )

        def execute(tx: Transaction) -> tuple[str, str, list[str]]:
            fresh = self.operations.authorize_type(tx, principal, "embedding_provider", recent=True)
            if (fresh.key.revision, fresh.key.grant.fingerprint) != (
                initial.key.revision,
                initial.key.grant.fingerprint,
            ):
                raise AccessDeniedError("provider probe authority changed")
            config = tx.providers.get(fresh.key.tenant_id, identifier)
            if config is None:
                raise NotFoundError("provider configuration not found")
            if config.revision != expected_revision or config.status not in {
                "draft",
                "probed",
                "retired",
            }:
                raise ConflictError("provider draft revision moved")
            now = max(self.context.clock.now_us(), config.updated_us)
            operation_id = str(self.context.ids.new())
            job, _ = tx.outbox.enqueue(
                NewOutboxJob(
                    tenant_id=config.tenant_id,
                    job_kind=PROBE_JOB_KIND,
                    aggregate_type="console_operation",
                    aggregate_id=operation_id,
                    source_revision=1,
                    payload={"operation_id": operation_id, "revision": 1},
                    dedupe_key=f"console-operation:{operation_id}:1",
                    priority=5,
                    lane=JobLane.NORMAL,
                    available_at_us=now,
                )
            )
            serving = tx.providers.serving(config.tenant_id)
            operation = ConsoleOperation(
                id=operation_id,
                tenant_id=fresh.key.tenant_id,
                key_id=fresh.key.id,
                key_revision=fresh.key.revision,
                grant_fingerprint=fresh.key.grant.fingerprint,
                session_id=fresh.session.id,
                session_epoch=fresh.session.epoch,
                kind="embedding_provider",
                reason_code=reason,
                status="queued",
                revision=1,
                processed=0,
                total=2,
                current_job_id=job.id,
                blocked_reason=None,
                created_us=now,
                updated_us=now,
                started_us=None,
                finished_us=None,
                provider=ProviderOperationPayload(
                    identifier,
                    config.content_revision,
                    "probe",
                    serving.epoch if serving else 0,
                    plan_json=json.dumps({"return_status": "retired"})
                    if config.status == "retired"
                    else None,
                ),
            )
            tx.console_operations.insert(operation)
            tx.providers.advance(
                replace(
                    config,
                    status="probing",
                    revision=config.revision + 1,
                    current_operation_id=operation_id,
                    latest_probe_id=None,
                    updated_us=now,
                ),
                expected_revision=config.revision,
            )
            tx.audit(
                tenant_id=config.tenant_id,
                actor="console:" + fresh.key.id,
                action="console.provider.probe.accepted",
                resource_type="provider_config",
                resource_id=config.id,
                reason_code=reason,
                details={"operation_id": operation_id, "content_revision": config.content_revision},
            )
            return "accepted", json.dumps({"operation_id": operation_id}), []

        result = runner.run(
            tenant_id=initial.key.tenant_id,
            app_instance_id="console:" + initial.key.id,
            operation="console.provider.probe",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint(
                "console.provider.probe",
                {"id": identifier, "expected_revision": expected_revision, "reason_code": reason},
            ),
            execute=execute,
        )
        return self.operations.detail(principal, json.loads(result.body)["operation_id"])

    @staticmethod
    def _current(
        tx: Transaction, job: OutboxJob
    ) -> tuple[ConsoleOperation, ProviderConfigRevision] | None:
        operation = tx.console_operations.get(job.tenant_id, job.aggregate_id)
        if (
            operation is None
            or operation.kind != "embedding_provider"
            or operation.provider is None
            or operation.provider.action != "probe"
            or operation.status not in {"queued", "running"}
            or operation.current_job_id != job.id
            or operation.revision != job.source_revision
        ):
            return None
        config = tx.providers.get(job.tenant_id, operation.provider.config_id)
        if (
            config is None
            or config.status != "probing"
            or config.current_operation_id != operation.id
            or config.content_revision != operation.provider.content_revision
        ):
            return None
        revision = tx.providers.revision(config.tenant_id, config.id, config.content_revision)
        if revision is None:
            raise ConflictError("provider configuration revision unavailable")
        return operation, revision

    def _lease(self, tx: Transaction, job: OutboxJob) -> None:
        current = tx.outbox.get(job.id)
        if (
            current.status != "leased"
            or current.lease_owner != job.lease_owner
            or current.lease_generation != job.lease_generation
            or current.source_revision != job.source_revision
            or (current.lease_expires_us or 0) <= self.context.clock.now_us()
        ):
            raise LeaseFencedError("provider probe lease expired")

    def work(self, job: OutboxJob) -> JobCommit:
        if (
            job.job_kind != PROBE_JOB_KIND
            or job.aggregate_type != "console_operation"
            or job.payload_version != JOB_PAYLOAD_VERSION
            or set(job.payload) != {"operation_id", "revision"}
            or job.payload["operation_id"] != job.aggregate_id
            or type(job.payload["revision"]) is not int
            or job.payload["revision"] != job.source_revision
        ):
            raise InvalidRequestError("invalid provider probe job")
        blocked: str | None = None
        observation: ProviderProbeObservation | None = None
        fingerprint = ""
        observed_us = self.context.clock.now_us()
        binding_prepared = False
        # Durable admission before any outbound attempt, including repeated delivery
        # after an expired lease. The caller's real fence is checked before charging.
        with self.context.uow.write() as tx:
            self._lease(tx, job)
            current = self._current(tx, job)
            if current is None:
                return lambda tx: None
            operation, revision = current
            try:
                self.operations._worker_principal(tx, operation)
            except AccessDeniedError:
                blocked = "authority_changed"
            if blocked is None:
                try:
                    tx.providers.reserve_probe_budget(
                        job.tenant_id,
                        now_us=self.context.clock.now_us(),
                        input_chars=PROBE_CHAR_CHARGE,
                        max_attempts=self.budget.attempts,
                        max_input_chars=self.budget.input_chars,
                        window_us=self.budget.window_us,
                    )
                except ConflictError:
                    blocked = "provider_budget_exhausted"
        if blocked is None:
            if self.runtime is None:
                blocked = "provider_unavailable"
            else:
                try:
                    binding = self.runtime.resolve(revision)
                    binding_prepared = True
                    fingerprint = binding.secret_fingerprint
                    observation = binding.probe_fixed()
                    observed_us = self.context.clock.now_us()
                    if observation.ok and (
                        observation.dimension_observed != revision.definition.space.dimension
                        or observation.normalized is not True
                    ):
                        observation = replace(observation, ok=False, outcome="invalid_output")
                except ConflictError:
                    blocked = "secret_unavailable"
                except (EmbeddingProviderError, InvalidRequestError):
                    observation = ProviderProbeObservation(
                        False, None, None, 0.0, "transport_error"
                    )

        def commit(tx: Transaction) -> None:
            current = self._current(tx, job)
            if current is None:
                return
            operation, revision = current
            reason = blocked
            try:
                self.operations._worker_principal(tx, operation)
            except AccessDeniedError:
                reason = "authority_changed"
            if reason is None and binding_prepared and self.runtime is not None:
                try:
                    if self.runtime.resolve(revision).secret_fingerprint != fingerprint:
                        reason = "secret_changed"
                except (ConflictError, InvalidRequestError, EmbeddingProviderError):
                    reason = "secret_unavailable"
            now = max(self.context.clock.now_us(), operation.updated_us)
            probe_id: str | None = None
            ok = False
            if reason is None and observation is not None:
                probe_id = str(self.context.ids.new())
                ok = observation.ok
                tx.providers.insert_probe(
                    ProviderProbe(
                        probe_id,
                        job.tenant_id,
                        revision.config_id,
                        revision.content_revision,
                        operation.id,
                        observation.ok,
                        observation.dimension_observed,
                        observation.normalized,
                        observation.latency_ms,
                        observation.outcome,
                        fingerprint,
                        observed_us,
                    )
                )
            config = tx.providers.get(job.tenant_id, revision.config_id)
            assert config is not None
            tx.providers.advance(
                replace(
                    config,
                    status="retired"
                    if self.operations.provider_return_status(operation) == "retired"
                    else "probed"
                    if ok
                    else "draft",
                    revision=config.revision + 1,
                    latest_probe_id=probe_id,
                    current_operation_id=None,
                    updated_us=now,
                ),
                expected_revision=config.revision,
            )
            tx.console_operations.advance(
                replace(
                    operation,
                    revision=operation.revision + 1,
                    status="blocked" if reason else "completed" if ok else "failed",
                    blocked_reason=reason,
                    processed=0 if reason else operation.total,
                    current_job_id=None,
                    started_us=operation.started_us or now,
                    finished_us=None if reason else now,
                    updated_us=now,
                ),
                expected_revision=operation.revision,
            )
            if reason or not ok:
                tx.console_operations.add_problem(
                    OperationProblem(operation.id, -1, reason or "provider_probe_failed", now)
                )
            tx.audit(
                tenant_id=job.tenant_id,
                actor="console:" + operation.key_id,
                action="console.provider.probe.finished",
                resource_type="provider_config",
                resource_id=revision.config_id,
                reason_code=operation.reason_code,
                details={"operation_id": operation.id, "ok": ok, "reason": reason},
            )

        return commit
