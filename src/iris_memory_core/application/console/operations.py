"""Current-owner operation metadata and cancellation of uncommitted work."""

import json
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any

from iris_memory_core.application.console.execution_context import ExecutionContext
from iris_memory_core.application.console.forget import ConsoleForgetCommands
from iris_memory_core.application.console.security import authorize, denied
from iris_memory_core.application.forget import ForgetResult
from iris_memory_core.application.outbox import JobCommit
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.domain.console import CommandPreview, OperatorPrincipal
from iris_memory_core.domain.console_operations import (
    ConsoleOperation,
    ForgetOperationPayload,
    OperationProblem,
    OperationSummary,
)
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    ConflictError,
    DomainError,
    IdempotencyUnavailableError,
    InvalidRequestError,
    NotFoundError,
    NotReadyError,
)
from iris_memory_core.domain.hashing import request_fingerprint
from iris_memory_core.domain.jobs import JOB_PAYLOAD_VERSION, JobLane, NewOutboxJob, OutboxJob

TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "completed_with_warnings",
        "failed",
        "cancelled",
        "cancelled_partial",
    }
)
OPERATION_STATUSES = TERMINAL_STATUSES | {"queued", "running", "paused", "blocked"}
OPERATION_KINDS = frozenset({"memory_forget", "trusted_backup", "embedding_provider"})


@dataclass(frozen=True)
class _BatchCommit:
    execute: JobCommit
    after_commit: Callable[[], None]

    def __call__(self, tx: Transaction) -> None:
        self.execute(tx)


class ConsoleOperations:
    def __init__(self, context: ExecutionContext) -> None:
        self.context = context
        self.forget = ConsoleForgetCommands(context)

    def authorize_type(
        self, tx: Transaction, principal: OperatorPrincipal, kind: str, *, recent: bool = False
    ) -> OperatorPrincipal:
        if kind == "memory_forget":
            return self.forget._principal(tx, principal, recent=recent)
        if kind not in {"trusted_backup", "embedding_provider"}:
            raise InvalidRequestError("unknown operation kind")
        fresh = authorize(
            tx,
            principal,
            self.context.clock.now_us(),
            "providers.manage" if kind == "embedding_provider" else "backups.write",
            recent=recent,
        )
        if "console.manage" not in fresh.key.grant.data_purposes:
            raise denied("permission_denied")
        return fresh

    @staticmethod
    def _enqueue(
        tx: Transaction, *, tenant_id: str, identifier: str, revision: int, now_us: int
    ) -> str:
        job, _ = tx.outbox.enqueue(
            NewOutboxJob(
                tenant_id=tenant_id,
                job_kind="console.memory_forget",
                aggregate_type="console_operation",
                aggregate_id=identifier,
                source_revision=revision,
                payload={"operation_id": identifier, "revision": revision},
                dedupe_key=f"console-operation:{identifier}:{revision}",
                priority=1,
                lane=JobLane.SAFETY,
                available_at_us=now_us,
            )
        )
        return job.id

    def accept_verified_preview(
        self,
        tx: Transaction,
        principal: OperatorPrincipal,
        preview: CommandPreview,
        payload: dict[str, Any],
    ) -> tuple[str, str, list[str]]:
        """Called only after the same transaction verified the immutable preview."""
        fresh = self.forget._principal(tx, principal, recent=preview.mode == "erase")
        states = self.forget._deletion_order(tx, fresh, payload["states"])
        if not 51 <= len(states) <= 500:
            raise InvalidRequestError("asynchronous deletion requires 51 to 500 fixed targets")
        saved_payload = json.dumps({"states": states})
        if len(saved_payload.encode()) > 262144:
            raise NotReadyError("operation target snapshot exceeds its byte budget")
        now = self.context.clock.now_us()
        identifier = str(self.context.ids.new())
        job_id = self._enqueue(
            tx, tenant_id=fresh.key.tenant_id, identifier=identifier, revision=1, now_us=now
        )
        operation = ConsoleOperation(
            id=identifier,
            tenant_id=fresh.key.tenant_id,
            key_id=fresh.key.id,
            key_revision=fresh.key.revision,
            grant_fingerprint=fresh.key.grant.fingerprint,
            session_id=fresh.session.id,
            session_epoch=fresh.session.epoch,
            kind="memory_forget",
            reason_code=preview.reason_code,
            status="queued",
            revision=1,
            processed=0,
            total=len(states),
            forget=ForgetOperationPayload(
                preview.id,
                preview.preview_hash,
                preview.mode,
                saved_payload,
                payload["deletion_watermark"],
                payload["holds_version"],
            ),
            current_job_id=job_id,
            blocked_reason=None,
            created_us=now,
            updated_us=now,
            started_us=None,
            finished_us=None,
        )
        tx.console_operations.insert(operation)
        receipt = json.dumps({"operation_id": identifier})
        tx.console.consume_command_preview(
            fresh.key.tenant_id,
            fresh.key.id,
            preview.id,
            preview.preview_hash,
            now_us=now,
            receipt_json=receipt,
        )
        tx.audit(
            tenant_id=fresh.key.tenant_id,
            actor="console:" + fresh.key.id,
            action="console.operation.accepted",
            resource_type="console_operation",
            resource_id=identifier,
            reason_code=preview.reason_code,
            details={"kind": "memory_forget", "total": len(states), "mode": preview.mode},
        )
        return "accepted", receipt, []

    def _worker_principal(self, tx: Transaction, operation: ConsoleOperation) -> OperatorPrincipal:
        key = tx.console.key(operation.key_id)
        session = tx.console.session(operation.session_id)
        if (
            key is None
            or session is None
            or (
                key.tenant_id,
                key.revision,
                key.grant.fingerprint,
                session.epoch,
            )
            != (
                operation.tenant_id,
                operation.key_revision,
                operation.grant_fingerprint,
                operation.session_epoch,
            )
        ):
            raise AccessDeniedError("operation authority changed")
        return self.authorize_type(
            tx,
            OperatorPrincipal(key, session),
            operation.kind,
            recent=operation.kind in {"trusted_backup", "embedding_provider"}
            or operation.mode == "erase",
        )

    def batch_work(self, job: OutboxJob) -> JobCommit:
        if (
            job.job_kind != "console.memory_forget"
            or job.aggregate_type != "console_operation"
            or job.payload_version != JOB_PAYLOAD_VERSION
            or set(job.payload) != {"operation_id", "revision"}
            or job.payload["operation_id"] != job.aggregate_id
            or type(job.payload["revision"]) is not int
            or job.payload["revision"] != job.source_revision
        ):
            raise InvalidRequestError("invalid management operation job")
        cleanup: list[ForgetResult] = []
        erase_content = False

        def commit(tx: Transaction) -> None:
            nonlocal erase_content
            operation = tx.console_operations.get(job.tenant_id, job.aggregate_id)
            if operation is None:
                raise InvalidRequestError("operation job has no durable intent")
            if operation.status not in {"queued", "running"}:
                return
            if operation.current_job_id != job.id or operation.revision != job.source_revision:
                return
            payload = json.loads(operation.payload_json)
            states = payload.get("states")
            if not isinstance(states, list) or len(states) != operation.total:
                raise InvalidRequestError("operation target snapshot is invalid")
            batch = states[operation.processed : operation.processed + 50]
            if not batch:
                raise InvalidRequestError("operation has no next batch")
            now = self.context.clock.now_us()
            try:
                principal = self._worker_principal(tx, operation)
                if self.forget._watermarks(tx, operation.tenant_id) != {
                    "deletion_watermark": operation.expected_deletion_seq,
                    "holds_version": operation.holds_version,
                }:
                    raise ConflictError("deletion context changed")
                for state in batch:
                    actual, _ = self.forget._inspect(
                        tx,
                        principal,
                        [
                            {
                                name: state[name]
                                for name in ("resource_type", "id", "expected_revision")
                            }
                        ],
                    )
                    if (
                        state.get("status") != "allowed"
                        or {**actual[0], "input_index": state["input_index"]} != state
                    ):
                        raise ConflictError("deletion target changed")
            except (AccessDeniedError, NotFoundError, ConflictError, NotReadyError) as error:
                # All work above this boundary is read-only. Never catch an
                # exception from the mutation phase below and commit a partial batch.
                reason = (
                    "authority_changed"
                    if isinstance(error, AccessDeniedError)
                    else "preview_changed"
                )
                if isinstance(error, NotReadyError):
                    reason = "query_budget"
                updated = replace(
                    operation,
                    revision=operation.revision + 1,
                    status="blocked",
                    blocked_reason=reason,
                    updated_us=max(now, operation.updated_us),
                )
                tx.console_operations.advance(updated, expected_revision=operation.revision)
                tx.console_operations.add_problem(
                    OperationProblem(operation.id, batch[0]["input_index"], reason, now)
                )
                tx.audit(
                    tenant_id=operation.tenant_id,
                    actor="console:" + operation.key_id,
                    action="console.operation.blocked",
                    resource_type="console_operation",
                    resource_id=operation.id,
                    reason_code=operation.reason_code,
                    details={"reason": reason, "processed": operation.processed},
                )
                return
            erase_content = operation.mode == "erase"
            cleanup.extend(
                self.forget.apply_verified_batch(
                    tx,
                    principal,
                    preview_id=operation.preview_id,
                    mode=operation.mode,
                    reason=operation.reason_code,
                    states=batch,
                )
            )
            processed = operation.processed + len(batch)
            completed = processed == operation.total
            next_revision = operation.revision + 1
            next_job = (
                None
                if completed
                else self._enqueue(
                    tx,
                    tenant_id=operation.tenant_id,
                    identifier=operation.id,
                    revision=next_revision,
                    now_us=now,
                )
            )
            updated = replace(
                operation,
                revision=next_revision,
                processed=processed,
                status="completed" if completed else "running",
                current_job_id=next_job,
                forget=replace(
                    operation.forget_payload,
                    payload_json="{}" if completed else operation.payload_json,
                    expected_deletion_seq=tx.tombstone_watermark(),
                ),
                started_us=operation.started_us or now,
                finished_us=now if completed else None,
                updated_us=max(now, operation.updated_us),
            )
            tx.console_operations.advance(updated, expected_revision=operation.revision)
            tx.audit(
                tenant_id=operation.tenant_id,
                actor="console:" + operation.key_id,
                action="console.operation.batch_committed",
                resource_type="console_operation",
                resource_id=operation.id,
                reason_code=operation.reason_code,
                details={"processed": processed, "total": operation.total},
            )

        def after_commit() -> None:
            for outcome in cleanup:
                with suppress(OSError, DomainError):
                    self.forget.domain.cleanup_for_command(
                        job.tenant_id, outcome, erase_content=erase_content
                    )

        return _BatchCommit(commit, after_commit)

    @staticmethod
    def provider_return_status(operation: ConsoleOperation) -> str:
        payload = operation.provider
        if payload is None:
            return "draft"
        if payload.action == "rollback":
            return "retired"
        if payload.action == "probe" and payload.plan_json is not None:
            try:
                if json.loads(payload.plan_json) == {"return_status": "retired"}:
                    return "retired"
            except (ValueError, TypeError):
                pass
        return "draft"

    @staticmethod
    def release_provider_intent(
        tx: Transaction, operation: ConsoleOperation, *, now_us: int
    ) -> None:
        if operation.provider is None:
            return
        config = tx.providers.get(operation.tenant_id, operation.provider.config_id)
        if (
            config is not None
            and config.current_operation_id == operation.id
            and config.content_revision == operation.provider.content_revision
            and config.status in {"probing", "activating"}
        ):
            tx.providers.advance(
                replace(
                    config,
                    status=ConsoleOperations.provider_return_status(operation),
                    revision=config.revision + 1,
                    current_operation_id=None,
                    latest_probe_id=None,
                    updated_us=max(now_us, config.updated_us),
                ),
                expected_revision=config.revision,
            )

    @staticmethod
    def record_dead_job(tx: Transaction, job: OutboxJob, *, now_us: int) -> None:
        """Called in the same transaction as a successful fenced dead-job CAS."""
        operation = tx.console_operations.get(job.tenant_id, job.aggregate_id)
        if operation is None or operation.status not in {"queued", "running"}:
            return
        if operation.current_job_id != job.id or operation.revision != job.source_revision:
            return
        tx.console_operations.advance(
            replace(
                operation,
                revision=operation.revision + 1,
                status="failed",
                forget=replace(operation.forget_payload, payload_json="{}")
                if operation.forget
                else None,
                finished_us=now_us,
                updated_us=max(now_us, operation.updated_us),
            ),
            expected_revision=operation.revision,
        )
        ConsoleOperations.release_provider_intent(tx, operation, now_us=now_us)
        tx.console_operations.add_problem(
            OperationProblem(operation.id, -1, "execution_failed", now_us)
        )
        tx.audit(
            tenant_id=operation.tenant_id,
            actor="console:" + operation.key_id,
            action="console.operation.failed",
            resource_type="console_operation",
            resource_id=operation.id,
            reason_code=operation.reason_code,
            details={"processed": operation.processed, "reason": "execution_failed"},
        )

    def _owned(
        self,
        tx: Transaction,
        principal: OperatorPrincipal,
        identifier: str,
    ) -> tuple[OperatorPrincipal, ConsoleOperation]:
        fresh = authorize(tx, principal, self.context.clock.now_us())
        operation = tx.console_operations.get(fresh.key.tenant_id, identifier)
        if operation is None or (
            operation.key_id,
            operation.key_revision,
            operation.grant_fingerprint,
        ) != (fresh.key.id, fresh.key.revision, fresh.key.grant.fingerprint):
            raise NotFoundError("operation not found")
        return self.authorize_type(tx, fresh, operation.kind), operation

    def detail(self, principal: OperatorPrincipal, identifier: str) -> ConsoleOperation:
        with self.context.uow.read() as tx:
            return self._owned(tx, principal, identifier)[1]

    def list_owned(
        self,
        principal: OperatorPrincipal,
        *,
        kind: str | None = None,
        status: str | None = None,
        created_from: int | None = None,
        created_before: int | None = None,
        after: tuple[int, str] | None = None,
        limit: int = 51,
    ) -> tuple[OperationSummary, ...]:
        if status is not None and status not in OPERATION_STATUSES:
            raise InvalidRequestError("unknown operation status")
        if kind is not None and kind not in OPERATION_KINDS:
            raise InvalidRequestError("unknown operation kind")
        with self.context.uow.read() as tx:
            fresh = authorize(tx, principal, self.context.clock.now_us())
            if kind is not None:
                fresh = self.authorize_type(tx, fresh, kind)
            elif "console.manage" not in fresh.key.grant.data_purposes:
                raise denied("permission_denied")
            return tx.console_operations.list_owned(
                fresh.key.tenant_id,
                fresh.key.id,
                fresh.key.grant.fingerprint,
                key_revision=fresh.key.revision,
                kind=kind,
                status=status,
                created_from=created_from,
                created_before=created_before,
                after=after,
                limit=limit,
            )

    def problems(
        self, principal: OperatorPrincipal, identifier: str, *, after: int = -2, limit: int = 51
    ) -> tuple[OperationProblem, ...]:
        with self.context.uow.read() as tx:
            self._owned(tx, principal, identifier)
            return tx.console_operations.problems(identifier, after=after, limit=limit)

    def cancel(
        self,
        principal: OperatorPrincipal,
        identifier: str,
        *,
        reason: str,
        idempotency_key: str,
    ) -> ConsoleOperation:
        if reason != "operator_request" or not idempotency_key:
            raise InvalidRequestError("invalid operation cancellation")
        runner = self.context.idempotency
        if runner is None:
            raise IdempotencyUnavailableError("management idempotency is unavailable")
        with self.context.uow.read() as tx:
            initial, _ = self._owned(tx, principal, identifier)

        def execute(tx: Transaction) -> tuple[str, str, list[str]]:
            fresh, operation = self._owned(tx, principal, identifier)
            if operation.status not in TERMINAL_STATUSES:
                now = self.context.clock.now_us()
                status = "cancelled_partial" if operation.processed else "cancelled"
                if operation.processed == operation.total:
                    status = "completed"
                updated = replace(
                    operation,
                    revision=operation.revision + 1,
                    status=status,
                    forget=replace(operation.forget_payload, payload_json="{}")
                    if operation.forget
                    else None,
                    blocked_reason=None,
                    updated_us=max(now, operation.updated_us),
                    finished_us=now,
                )
                tx.console_operations.advance(updated, expected_revision=operation.revision)
                self.release_provider_intent(tx, operation, now_us=now)
                tx.audit(
                    tenant_id=fresh.key.tenant_id,
                    actor="console:" + fresh.key.id,
                    action="console.operation.cancelled",
                    resource_type="console_operation",
                    resource_id=identifier,
                    reason_code=reason,
                    details={"processed": operation.processed, "total": operation.total},
                )
            return "ok", json.dumps({"operation_id": identifier}), []

        runner.run(
            tenant_id=initial.key.tenant_id,
            app_instance_id="console:" + initial.key.id,
            operation="console.operation.cancel",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint(
                "console.operation.cancel",
                {"operation_id": identifier, "reason": reason},
            ),
            execute=execute,
        )
        return self.detail(principal, identifier)

    @staticmethod
    def metadata(operation: ConsoleOperation | OperationSummary, *, key_id: str) -> dict[str, Any]:
        """Explicit response fields; private snapshots and queue IDs never enter views."""
        cancellable = operation.status not in TERMINAL_STATUSES
        return {
            "id": operation.id,
            "kind": operation.kind,
            "status": operation.status,
            "phase": {
                "memory_forget": "canonical_forget",
                "trusted_backup": "backup_verify",
                "embedding_provider": "provider_configuration",
            }[operation.kind],
            "progress": {
                "processed": str(operation.processed),
                "total": str(operation.total),
                "unit": "records" if operation.kind == "memory_forget" else "steps",
            },
            "blocked_reason": operation.blocked_reason,
            "created_by": key_id,
            "created_us": operation.created_us,
            "started_us": operation.started_us,
            "finished_us": operation.finished_us,
            "cancellable": cancellable,
            "result_ref": None,
            "problems_count": operation.problems_count,
            "available_actions": ["cancel"] if cancellable else [],
            "reason_codes": ["operator_request"],
        }
