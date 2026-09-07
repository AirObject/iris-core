"""Accepted Provider activation: external COW build and one fenced publication."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace

from iris_memory_core.application.console.execution_context import ExecutionContext
from iris_memory_core.application.console.operations import ConsoleOperations
from iris_memory_core.application.console.provider_activation import (
    ProviderActivationPlan,
    ProviderActivationPlanner,
    gate,
)
from iris_memory_core.application.outbox import JobCommit
from iris_memory_core.application.ports.provider_generations import (
    PreparedProviderGeneration,
    ProviderBuildStopped,
    ProviderGenerations,
)
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
    ProviderConfig,
    ProviderConfigRevision,
    ProviderServing,
)
from iris_memory_core.domain.vector import EmbeddingProviderError

ACTIVATION_JOB_KIND = "console.embedding_activate"


@dataclass(frozen=True, slots=True)
class _ActivationCommit:
    execute: JobCommit
    prepared: PreparedProviderGeneration | None
    published: list[bool]

    def __call__(self, tx: Transaction) -> None:
        self.execute(tx)

    def after_commit(self) -> None:
        if self.prepared is not None and self.published:
            self.prepared.after_commit()


class ProviderActivations:
    def __init__(
        self,
        context: ExecutionContext,
        runtime: ConfiguredEmbeddingRuntime | None,
        generations: ProviderGenerations | None,
    ) -> None:
        self.context, self.runtime, self.generations = context, runtime, generations
        self.operations = ConsoleOperations(context)
        self.planner = (
            ProviderActivationPlanner(runtime, generations.count_resources, generations.can_reuse)
            if runtime and generations
            else None
        )

    def accept(
        self,
        principal: OperatorPrincipal,
        identifier: str,
        *,
        expected_revision: int,
        rebuild_ack: str | None,
        reason: str,
        idempotency_key: str,
    ) -> ConsoleOperation:
        return self._accept(
            principal,
            identifier,
            expected_revision=expected_revision,
            rebuild_ack=rebuild_ack,
            reason=reason,
            idempotency_key=idempotency_key,
            action="activate",
        )

    def rollback(
        self,
        principal: OperatorPrincipal,
        identifier: str,
        *,
        expected_revision: int,
        reason: str,
        idempotency_key: str,
    ) -> ConsoleOperation:
        return self._accept(
            principal,
            identifier,
            expected_revision=expected_revision,
            rebuild_ack=None,
            reason=reason,
            idempotency_key=idempotency_key,
            action="rollback",
        )

    def _accept(
        self,
        principal: OperatorPrincipal,
        identifier: str,
        *,
        expected_revision: int,
        rebuild_ack: str | None,
        reason: str,
        idempotency_key: str,
        action: str,
    ) -> ConsoleOperation:
        if (
            reason != "operator_request"
            or not idempotency_key
            or type(expected_revision) is not int
            or expected_revision < 1
        ):
            raise InvalidRequestError("invalid provider activation request")
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
                raise AccessDeniedError("provider activation authority changed")
            config = tx.providers.get(fresh.key.tenant_id, identifier)
            if config is None:
                raise NotFoundError("provider configuration not found")
            if config.revision != expected_revision or config.status != (
                "retired" if action == "rollback" else "probed"
            ):
                raise gate("provider_configuration_moved")
            if self.planner is None:
                raise gate("provider_unavailable")
            now = max(self.context.clock.now_us(), config.updated_us)
            plan = self.planner.plan(tx, config, now_us=now)
            if action == "activate":
                plan.require_ack(rebuild_ack)
            operation_id = str(self.context.ids.new())
            job, _ = tx.outbox.enqueue(
                NewOutboxJob(
                    tenant_id=config.tenant_id,
                    job_kind=ACTIVATION_JOB_KIND,
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
            operation = ConsoleOperation(
                id=operation_id,
                tenant_id=config.tenant_id,
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
                    config.id,
                    config.content_revision,
                    action,
                    plan.serving_epoch,
                    plan_hash=plan.rebuild_plan_hash,
                    plan_json=json.dumps(asdict(plan), sort_keys=True),
                ),
            )
            tx.console_operations.insert(operation)
            tx.providers.advance(
                replace(
                    config,
                    status="activating",
                    revision=config.revision + 1,
                    current_operation_id=operation_id,
                    updated_us=now,
                ),
                expected_revision=config.revision,
            )
            tx.audit(
                tenant_id=config.tenant_id,
                actor="console:" + fresh.key.id,
                action="console.provider.activation.accepted",
                resource_type="provider_config",
                resource_id=config.id,
                reason_code=reason,
                details={
                    "operation_id": operation_id,
                    "rebuild": plan.rebuild,
                    "estimated_resources": plan.estimated_resources,
                    "plan_hash": plan.rebuild_plan_hash,
                },
            )
            return "accepted", json.dumps({"operation_id": operation_id}), []

        result = runner.run(
            tenant_id=initial.key.tenant_id,
            app_instance_id="console:" + initial.key.id,
            operation="console.provider." + action,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint(
                "console.provider." + action,
                {
                    "id": identifier,
                    "expected_revision": expected_revision,
                    "rebuild_ack": rebuild_ack,
                    "reason_code": reason,
                },
            ),
            execute=execute,
        )
        return self.operations.detail(principal, json.loads(result.body)["operation_id"])

    @staticmethod
    def _state(
        tx: Transaction, job: OutboxJob
    ) -> (
        tuple[ConsoleOperation, ProviderConfig, ProviderConfigRevision, ProviderActivationPlan]
        | None
    ):
        operation = tx.console_operations.get(job.tenant_id, job.aggregate_id)
        if (
            operation is None
            or operation.kind != "embedding_provider"
            or operation.provider is None
            or operation.provider.action not in {"activate", "rollback"}
            or operation.status not in {"queued", "running"}
            or operation.current_job_id != job.id
            or operation.revision != job.source_revision
        ):
            return None
        payload = operation.provider
        config = tx.providers.get(job.tenant_id, payload.config_id)
        if (
            config is None
            or config.current_operation_id != operation.id
            or config.status != "activating"
            or config.content_revision != payload.content_revision
        ):
            return None
        revision = tx.providers.revision(job.tenant_id, config.id, config.content_revision)
        if revision is None or payload.plan_json is None:
            raise gate("provider_configuration_unavailable")
        try:
            plan = ProviderActivationPlan(**json.loads(payload.plan_json))
        except (ValueError, TypeError):
            raise InvalidRequestError("invalid saved activation plan") from None
        if (
            plan.config_id != config.id
            or plan.content_revision != config.content_revision
            or plan.expected_revision + 1 != config.revision
            or plan.serving_epoch != payload.expected_serving_epoch
            or plan.rebuild_plan_hash != payload.plan_hash
            or type(plan.rebuild) is not bool
        ):
            raise InvalidRequestError("saved activation plan does not match intent")
        return operation, config, revision, plan

    def _check(
        self, tx: Transaction, job: OutboxJob
    ) -> tuple[ConsoleOperation, ProviderConfig, ProviderConfigRevision, ProviderActivationPlan]:
        leased = tx.outbox.get(job.id)
        if (
            leased.status != "leased"
            or leased.lease_owner != job.lease_owner
            or leased.lease_generation != job.lease_generation
            or leased.source_revision != job.source_revision
            or (leased.lease_expires_us or 0) <= self.context.clock.now_us()
        ):
            raise LeaseFencedError("provider activation lease expired")
        state = self._state(tx, job)
        if state is None:
            raise ProviderBuildStopped("provider_configuration_moved")
        operation, config, revision, plan = state
        try:
            self.operations._worker_principal(tx, operation)
        except AccessDeniedError:
            raise ProviderBuildStopped("authority_changed") from None
        serving = tx.providers.serving(job.tenant_id)
        if (serving.epoch if serving else 0) != plan.serving_epoch or tx.vector.current_epoch(
            job.tenant_id
        ) != plan.vector_epoch:
            raise ProviderBuildStopped("provider_serving_moved")
        if self.planner is None:
            raise ProviderBuildStopped("provider_unavailable")
        try:
            self.planner.verified_probe(tx, config, revision, now_us=self.context.clock.now_us())
        except (ConflictError, EmbeddingProviderError, InvalidRequestError):
            raise ProviderBuildStopped("provider_probe_invalid") from None
        return state

    def work(self, job: OutboxJob) -> JobCommit:
        if (
            job.job_kind != ACTIVATION_JOB_KIND
            or job.aggregate_type != "console_operation"
            or job.payload_version != JOB_PAYLOAD_VERSION
            or set(job.payload) != {"operation_id", "revision"}
            or job.payload["operation_id"] != job.aggregate_id
            or type(job.payload["revision"]) is not int
            or job.payload["revision"] != job.source_revision
        ):
            raise InvalidRequestError("invalid provider activation job")
        prepared: PreparedProviderGeneration | None = None
        blocked: str | None = None

        def check() -> None:
            with self.context.uow.read() as tx:
                self._check(tx, job)

        try:
            with self.context.uow.read() as tx:
                operation, config, revision, plan = self._check(tx, job)
            if self.generations is None:
                raise ProviderBuildStopped("provider_unavailable")
            assert operation.provider is not None
            if operation.provider.action == "rollback":
                prepared = self.generations.prepare_rollback(
                    revision, config.last_generation_id, check
                )
            elif plan.rebuild:
                prepared = self.generations.prepare(revision, check)
            if prepared is not None and prepared.expected_vector_epoch != plan.vector_epoch:
                raise ProviderBuildStopped("provider_serving_moved")
        except ProviderBuildStopped as error:
            blocked = error.reason
        published: list[bool] = []

        def commit(tx: Transaction) -> None:
            state = self._state(tx, job)
            if state is None:
                return
            operation, config, revision, plan = state
            reason = blocked
            try:
                self._check(tx, job)
            except ProviderBuildStopped as error:
                reason = error.reason
            now = max(self.context.clock.now_us(), config.updated_us, operation.updated_us)
            generation_id = None
            if reason is None:
                assert self.generations is not None and self.planner is not None
                probe = self.planner.verified_probe(tx, config, revision, now_us=now)
                if prepared is not None:
                    if prepared.secret_fingerprint != probe.secret_fingerprint:
                        reason = "provider_probe_invalid"
                    else:
                        generation_id = prepared.publish(tx)
                        if not prepared.reused:
                            tx.providers.bind_generation(
                                job.tenant_id,
                                generation_id,
                                config.id,
                                config.content_revision,
                                now_us=now,
                            )
                else:
                    serving = tx.providers.serving(job.tenant_id)
                    if serving is None:
                        raise gate("provider_serving_moved")
                    self.generations.verify_current(tx, revision, serving.generation_id)
                    generation_id = serving.generation_id
            if reason is None:
                assert generation_id is not None
                previous = tx.providers.serving(job.tenant_id)
                if previous is not None:
                    active = tx.providers.get(job.tenant_id, previous.config_id)
                    if active is None or active.status != "active":
                        raise gate("provider_serving_moved")
                    tx.providers.advance(
                        replace(
                            active,
                            status="retired",
                            revision=active.revision + 1,
                            updated_us=max(now, active.updated_us),
                        ),
                        expected_revision=active.revision,
                    )
                tx.providers.advance(
                    replace(
                        config,
                        status="active",
                        revision=config.revision + 1,
                        current_operation_id=None,
                        last_generation_id=generation_id,
                        updated_us=now,
                    ),
                    expected_revision=config.revision,
                )
                tx.providers.switch_serving(
                    ProviderServing(
                        job.tenant_id,
                        config.id,
                        config.content_revision,
                        generation_id,
                        plan.serving_epoch + 1,
                        now,
                    ),
                    expected_epoch=plan.serving_epoch,
                )
                published.append(True)
            else:
                self.operations.release_provider_intent(tx, operation, now_us=now)
                tx.console_operations.add_problem(OperationProblem(operation.id, -1, reason, now))
            assert operation.provider is not None
            tx.console_operations.advance(
                replace(
                    operation,
                    revision=operation.revision + 1,
                    status="blocked" if reason else "completed",
                    blocked_reason=reason,
                    processed=0 if reason else operation.total,
                    current_job_id=None,
                    started_us=operation.started_us or now,
                    finished_us=None if reason else now,
                    updated_us=now,
                    provider=replace(
                        operation.provider, generation_id=generation_id if reason is None else None
                    ),
                ),
                expected_revision=operation.revision,
            )
            tx.audit(
                tenant_id=job.tenant_id,
                actor="console:" + operation.key_id,
                action="console.provider.activation.finished",
                resource_type="provider_config",
                resource_id=config.id,
                reason_code=operation.reason_code,
                details={
                    "operation_id": operation.id,
                    "reason": reason,
                    "generation_id": generation_id if reason is None else None,
                },
            )

        return _ActivationCommit(commit, prepared, published)
