"""Typed statistics backfill and existing-Scheduler rollup job entry points."""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

from iris_memory_core.application.console.execution_context import ExecutionContext
from iris_memory_core.application.console.operations import ConsoleOperations
from iris_memory_core.application.console.statistics import authorize_statistics
from iris_memory_core.application.console.statistics_rollup import STAGES, StatisticsRollup
from iris_memory_core.application.outbox import JobCommit
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.domain.console import OperatorPrincipal
from iris_memory_core.domain.console_operations import (
    ConsoleOperation,
    OperationProblem,
    StatisticsBackfillPayload,
)
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    IdempotencyUnavailableError,
    InvalidRequestError,
)
from iris_memory_core.domain.hashing import request_fingerprint
from iris_memory_core.domain.jobs import JOB_PAYLOAD_VERSION, JobLane, NewOutboxJob, OutboxJob
from iris_memory_core.domain.statistics import HOUR_US, bucket_start

KIND = "console.stats.rollup"


class StatisticsOperations:
    def __init__(self, context: ExecutionContext) -> None:
        self.context = context
        self.operations = ConsoleOperations(context)

    @staticmethod
    def _enqueue(
        tx: Transaction,
        tenant: str,
        identifier: str,
        revision: int,
        now: int,
        *,
        scheduled: bool = False,
    ) -> str:
        job, _ = tx.outbox.enqueue(
            NewOutboxJob(
                tenant_id=tenant,
                job_kind=KIND,
                aggregate_type="console_stat_build" if scheduled else "console_operation",
                aggregate_id=identifier,
                source_revision=revision,
                payload={
                    "build_id" if scheduled else "operation_id": identifier,
                    "revision": revision,
                },
                dedupe_key=f"statistics:{identifier}:{revision}",
                priority=6,
                lane=JobLane.NORMAL,
                available_at_us=now,
            )
        )
        return job.id

    def create(
        self,
        principal: OperatorPrincipal,
        *,
        lower: int,
        upper: int,
        reason: str,
        idempotency_key: str,
    ) -> ConsoleOperation:
        StatisticsRollup.validate_range(lower, upper)
        if reason != "operator_request" or not idempotency_key:
            raise InvalidRequestError("invalid statistics backfill")
        runner = self.context.idempotency
        if runner is None:
            raise IdempotencyUnavailableError("statistics idempotency is unavailable")
        with self.context.uow.read() as tx:
            initial = authorize_statistics(
                tx, principal, self.context.clock.now_us(), system=True, recent=True
            )

        def execute(tx: Transaction) -> tuple[str, str, list[str]]:
            fresh = authorize_statistics(
                tx, principal, self.context.clock.now_us(), system=True, recent=True
            )
            if (fresh.key.revision, fresh.key.grant.fingerprint) != (
                initial.key.revision,
                initial.key.grant.fingerprint,
            ):
                raise AccessDeniedError("statistics authority changed")
            now = self.context.clock.now_us()
            identifier = str(self.context.ids.new())
            build_id = str(self.context.ids.new())
            tx.statistics.create_build(
                build_id, fresh.key.tenant_id, lower, upper, now, operation_id=identifier
            )
            job_id = self._enqueue(tx, fresh.key.tenant_id, identifier, 1, now)
            operation = ConsoleOperation(
                id=identifier,
                tenant_id=fresh.key.tenant_id,
                key_id=fresh.key.id,
                key_revision=fresh.key.revision,
                grant_fingerprint=fresh.key.grant.fingerprint,
                session_id=fresh.session.id,
                session_epoch=fresh.session.epoch,
                kind="statistics_backfill",
                reason_code=reason,
                status="queued",
                revision=1,
                processed=0,
                total=len(STAGES),
                current_job_id=job_id,
                blocked_reason=None,
                created_us=now,
                updated_us=now,
                started_us=None,
                finished_us=None,
                statistics=StatisticsBackfillPayload(build_id, lower, upper),
            )
            tx.console_operations.insert(operation)
            tx.audit(
                tenant_id=fresh.key.tenant_id,
                actor="console:" + fresh.key.id,
                action="console.stats.backfill.accepted",
                resource_type="console_operation",
                resource_id=identifier,
                reason_code=reason,
                details={"from_us": lower, "to_us": upper},
            )
            return "accepted", json.dumps({"operation_id": identifier}), []

        result = runner.run(
            tenant_id=initial.key.tenant_id,
            app_instance_id="console:" + initial.key.id,
            operation="console.stats.backfill",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint(
                "console.stats.backfill", {"from_us": lower, "to_us": upper, "reason_code": reason}
            ),
            execute=execute,
        )
        return self.operations.detail(principal, json.loads(result.body)["operation_id"])

    def work(self, job: OutboxJob) -> JobCommit:
        if job.job_kind != KIND or job.payload_version != JOB_PAYLOAD_VERSION:
            raise InvalidRequestError("invalid statistics job")
        if job.aggregate_type == "schedule_tick":
            return self._tick(job)
        field = "build_id" if job.aggregate_type == "console_stat_build" else "operation_id"
        if (
            job.aggregate_type not in {"console_operation", "console_stat_build"}
            or set(job.payload) != {field, "revision"}
            or job.payload[field] != job.aggregate_id
            or type(job.payload["revision"]) is not int
            or job.payload["revision"] != job.source_revision
        ):
            raise InvalidRequestError("invalid statistics job payload")

        def commit(tx: Transaction) -> None:
            now = self.context.clock.now_us()
            operation = (
                tx.console_operations.get(job.tenant_id, job.aggregate_id)
                if field == "operation_id"
                else None
            )
            if field == "operation_id":
                if (
                    operation is None
                    or operation.statistics is None
                    or operation.kind != "statistics_backfill"
                    or operation.status not in {"queued", "running"}
                    or operation.current_job_id != job.id
                    or operation.revision != job.source_revision
                ):
                    return
                try:
                    self.operations._worker_principal(tx, operation)
                except AccessDeniedError:
                    tx.statistics.cancel(job.tenant_id, operation.statistics.build_id)
                    tx.console_operations.advance(
                        replace(
                            operation,
                            revision=operation.revision + 1,
                            status="blocked",
                            blocked_reason="authority_changed",
                            current_job_id=None,
                            updated_us=max(now, operation.updated_us),
                        ),
                        expected_revision=operation.revision,
                    )
                    tx.console_operations.add_problem(
                        OperationProblem(operation.id, -1, "authority_changed", now)
                    )
                    return
                identifier = operation.statistics.build_id
            else:
                identifier = job.aggregate_id
            build = tx.statistics.build(job.tenant_id, identifier)
            if build is None or build["state"] != "building":
                return
            stage, complete = StatisticsRollup().step(tx, job.tenant_id, identifier, now)
            if operation is None:
                if not complete:
                    self._enqueue(
                        tx, job.tenant_id, identifier, job.source_revision + 1, now, scheduled=True
                    )
                return
            next_revision = operation.revision + 1
            next_job = (
                None
                if complete
                else self._enqueue(tx, job.tenant_id, operation.id, next_revision, now)
            )
            tx.console_operations.advance(
                replace(
                    operation,
                    revision=next_revision,
                    status="completed" if complete else "running",
                    processed=stage,
                    current_job_id=next_job,
                    started_us=operation.started_us or now,
                    finished_us=now if complete else None,
                    updated_us=max(now, operation.updated_us),
                ),
                expected_revision=operation.revision,
            )
            if complete:
                tx.audit(
                    tenant_id=job.tenant_id,
                    actor="console:" + operation.key_id,
                    action="console.stats.backfill.completed",
                    resource_type="console_operation",
                    resource_id=operation.id,
                    reason_code=operation.reason_code,
                    details={"stages": stage},
                )

        return commit

    def _tick(self, job: OutboxJob) -> JobCommit:
        def commit(tx: Transaction) -> None:
            tick = tx.schedules.get_tick(job.aggregate_id)
            schedule = tx.schedules.get(tick.schedule_id)
            if (
                tick.outbox_id != job.id
                or schedule.tenant_id != job.tenant_id
                or schedule.job_kind != KIND
                or job.payload.get("tick_id") != tick.id
            ):
                raise InvalidRequestError("statistics schedule identity changed")
            now = self.context.clock.now_us()
            upper = bucket_start(now, "hour")
            identifier = str(uuid5(NAMESPACE_URL, "iris-statistics-tick:" + tick.id))
            if tx.statistics.build(job.tenant_id, identifier) is not None:
                return
            tx.statistics.create_build(
                identifier, job.tenant_id, max(0, upper - 24 * HOUR_US), upper, now
            )
            self._enqueue(tx, job.tenant_id, identifier, 1, now, scheduled=True)

        return commit
