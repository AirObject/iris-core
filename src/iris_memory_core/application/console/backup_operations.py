"""Trusted backup intent: external preparation followed by the existing fenced commit."""

import json
from dataclasses import replace
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from iris_memory_core.application.console.execution_context import ExecutionContext
from iris_memory_core.application.console.operations import ConsoleOperations
from iris_memory_core.application.outbox import JobCommit
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.domain.console import CommandActor, OperatorPrincipal
from iris_memory_core.domain.console_operations import (
    ConsoleOperation,
    OperationProblem,
    TrustedBackupPayload,
)
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    IdempotencyUnavailableError,
    InvalidRequestError,
)
from iris_memory_core.domain.hashing import request_fingerprint
from iris_memory_core.domain.jobs import JOB_PAYLOAD_VERSION, JobLane, NewOutboxJob, OutboxJob
from iris_memory_core.domain.scope import Scope


class TrustedBackupArchive(Protocol):
    def create_verified(self, reference: str) -> TrustedBackupPayload: ...
    def verify_result(self, result: TrustedBackupPayload) -> bool: ...


class BackupOperations:
    def __init__(self, context: ExecutionContext, archives: TrustedBackupArchive | None) -> None:
        self.context = context
        self.operations = ConsoleOperations(context)
        self.archives = archives

    def create(
        self, principal: OperatorPrincipal, *, reason: str, idempotency_key: str
    ) -> ConsoleOperation:
        if reason != "operator_request" or not idempotency_key:
            raise InvalidRequestError("invalid backup request")
        runner = self.context.idempotency
        if runner is None:
            raise IdempotencyUnavailableError("management idempotency is unavailable")
        with self.context.uow.read() as tx:
            initial = self.operations.authorize_type(tx, principal, "trusted_backup", recent=True)

        def execute(tx: Transaction) -> tuple[str, str, list[str]]:
            fresh = self.operations.authorize_type(tx, principal, "trusted_backup", recent=True)
            actor = CommandActor(
                tenant_id=fresh.key.tenant_id,
                key_id=fresh.key.id,
                key_revision=fresh.key.revision,
                grant_fingerprint=fresh.key.grant.fingerprint,
                session_id=fresh.session.id,
                session_epoch=fresh.session.epoch,
                scope=Scope(fresh.key.tenant_id),
                operation="backup.create",
                reason_code=reason,
            )
            now, identifier = self.context.clock.now_us(), str(self.context.ids.new())
            job, _ = tx.outbox.enqueue(
                NewOutboxJob(
                    tenant_id=actor.tenant_id,
                    job_kind="console.trusted_backup",
                    aggregate_type="console_operation",
                    aggregate_id=identifier,
                    source_revision=1,
                    payload={"operation_id": identifier, "revision": 1},
                    dedupe_key=f"console-operation:{identifier}:1",
                    priority=5,
                    lane=JobLane.NORMAL,
                    available_at_us=now,
                )
            )
            operation = ConsoleOperation(
                id=identifier,
                tenant_id=actor.tenant_id,
                key_id=actor.key_id,
                key_revision=actor.key_revision,
                grant_fingerprint=actor.grant_fingerprint,
                session_id=actor.session_id,
                session_epoch=actor.session_epoch,
                kind="trusted_backup",
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
                backup=TrustedBackupPayload(),
            )
            tx.console_operations.insert(operation)
            tx.audit(
                tenant_id=actor.tenant_id,
                actor=actor.audit_actor,
                action="console.operation.accepted",
                resource_type="console_operation",
                resource_id=identifier,
                reason_code=reason,
                details={"kind": "trusted_backup"},
            )
            return "accepted", json.dumps({"operation_id": identifier}), []

        outcome = runner.run(
            tenant_id=initial.key.tenant_id,
            app_instance_id="console:" + initial.key.id,
            operation="console.backup.create",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint(
                "console.backup.create", {"reason_code": reason}
            ),
            execute=execute,
        )
        return self.operations.detail(principal, json.loads(outcome.body)["operation_id"])

    @staticmethod
    def _current(operation: ConsoleOperation | None, job: OutboxJob) -> bool:
        return (
            operation is not None
            and operation.kind == "trusted_backup"
            and operation.status in {"queued", "running"}
            and operation.current_job_id == job.id
            and operation.revision == job.source_revision
        )

    def work(self, job: OutboxJob) -> JobCommit:
        if (
            job.job_kind != "console.trusted_backup"
            or job.aggregate_type != "console_operation"
            or job.payload_version != JOB_PAYLOAD_VERSION
            or set(job.payload) != {"operation_id", "revision"}
            or job.payload["operation_id"] != job.aggregate_id
            or type(job.payload["revision"]) is not int
            or job.payload["revision"] != job.source_revision
        ):
            raise InvalidRequestError("invalid backup job")
        blocked: str | None = None
        result: TrustedBackupPayload | None = None
        with self.context.uow.read() as tx:
            operation = tx.console_operations.get(job.tenant_id, job.aggregate_id)
            if not self._current(operation, job):
                return lambda tx: None
            assert operation is not None
            try:
                self.operations._worker_principal(tx, operation)
            except AccessDeniedError:
                blocked = "authority_changed"
        if blocked is None:
            if self.archives is None:
                blocked = "backup_unavailable"
            else:
                result = self.archives.create_verified(
                    str(
                        uuid5(NAMESPACE_URL, f"iris-trusted-backup:{job.id}:{job.lease_generation}")
                    )
                )
                if (
                    result.result_ref is None
                    or result.manifest_hash is None
                    or result.verified_us is None
                    or not self.archives.verify_result(result)
                ):
                    raise InvalidRequestError("backup verification failed")

        def commit(tx: Transaction) -> None:
            current = tx.console_operations.get(job.tenant_id, job.aggregate_id)
            if not self._current(current, job):
                return
            assert current is not None
            reason = blocked
            try:
                self.operations._worker_principal(tx, current)
            except AccessDeniedError:
                reason = "authority_changed"
            now = max(self.context.clock.now_us(), current.updated_us)
            tx.console_operations.advance(
                replace(
                    current,
                    revision=current.revision + 1,
                    status="blocked" if reason else "completed",
                    blocked_reason=reason,
                    processed=0 if reason else current.total,
                    current_job_id=None,
                    started_us=current.started_us or now,
                    finished_us=None if reason else now,
                    updated_us=now,
                    backup=TrustedBackupPayload() if reason else result,
                ),
                expected_revision=current.revision,
            )
            if reason:
                tx.console_operations.add_problem(OperationProblem(current.id, -1, reason, now))
            tx.audit(
                tenant_id=current.tenant_id,
                actor="console:" + current.key_id,
                action="console.operation.blocked" if reason else "console.backup.verified",
                resource_type="console_operation",
                resource_id=current.id,
                reason_code=current.reason_code,
                details={"reason": reason},
            )

        return commit

    def prerequisite(
        self, tenant_id: str, operation_id: str
    ) -> tuple[TrustedBackupPayload | None, str | None]:
        """Internal gate for future imports; callers still authorize their actual command."""
        if self.archives is None:
            return None, "backup_unavailable"
        with self.context.uow.read() as tx:
            operation = tx.console_operations.get(tenant_id, operation_id)
        if (
            operation is None
            or operation.kind != "trusted_backup"
            or operation.status != "completed"
            or operation.backup is None
            or operation.backup.result_ref is None
        ):
            return None, "backup_not_verified"
        if not self.archives.verify_result(operation.backup):
            return None, "backup_verification_failed"
        return operation.backup, None
