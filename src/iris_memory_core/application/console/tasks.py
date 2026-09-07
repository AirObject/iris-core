"""Console Task commands delegate Canonical writes to the existing Task service."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from iris_memory_core.application.console.commands import CommandTarget, ConsoleCommandExecutor
from iris_memory_core.application.console.reads import ResourceReader
from iris_memory_core.application.console.resources import ReadRecord, ResourceRef
from iris_memory_core.application.console.security import OperatorSecurity, authorize
from iris_memory_core.application.ports import Transaction
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.application.tasks import TaskService
from iris_memory_core.domain.console import CommandActor, OperatorPrincipal
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.domain.model import IdempotentResult
from iris_memory_core.domain.scope import Scope
from iris_memory_core.domain.task import TASK_STEP_TRANSITIONS, TASK_TRANSITIONS


class ConsoleTaskCommands:
    def __init__(self, security: OperatorSecurity) -> None:
        self.security = security
        self.executor = ConsoleCommandExecutor(security)
        self.tasks = TaskService(
            security.uow,
            security.clock,
            surface=SurfaceCoordinatorService(security.uow, security.clock),
        )

    def create(
        self,
        principal: OperatorPrincipal,
        *,
        scope: Scope,
        fields: dict[str, Any],
        privacy_labels: list[str],
        source_refs: list[dict[str, Any]],
        reason: str,
        idempotency_key: str,
    ) -> ReadRecord:
        target = CommandTarget(
            "task",
            scope,
            privacy_labels=tuple(privacy_labels),
            source_refs=tuple(
                ResourceRef(ref["resource_type"], ref["resource_id"], ref.get("revision"))
                for ref in source_refs
            )
            + (
                (ResourceRef("entity", fields["owner_entity_id"]),)
                if fields.get("owner_entity_id")
                else ()
            ),
        )

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            code, body, refs = self.tasks.create_for_command(
                tx,
                actor,
                fields,
                privacy_labels=privacy_labels,
                source_refs=source_refs,
            )
            outcome = json.loads(body)
            outcome["snapshot_updated_us"] = tx.tasks.get_task(outcome["task_id"]).updated_us
            return code, json.dumps(outcome), refs

        result = self.executor.run(
            principal,
            operation="task.create",
            target=target,
            payload=fields,
            reason=reason,
            idempotency_key=idempotency_key,
            execute=execute,
        )
        return self._result(principal, result)

    def mutate(
        self,
        principal: OperatorPrincipal,
        task_id: str,
        *,
        operation: str,
        expected_revision: int,
        fields: dict[str, Any],
        reason: str,
        idempotency_key: str,
    ) -> ReadRecord:
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.write")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            current = reader.get(ResourceRef("task", task_id))
            if current is None:
                raise NotFoundError("task not found")
            target = CommandTarget(
                "task",
                current.scope,
                resource_id=task_id,
                source_refs=tuple(
                    ResourceRef(ref["resource_type"], ref["resource_id"], ref.get("revision"))
                    for ref in fields.get("completion_evidence_refs", [])
                ),
            )

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            code, _, refs = self.tasks.mutate_for_command(
                tx,
                actor,
                task_id,
                expected_revision=expected_revision,
                fields=fields,
            )
            task = tx.tasks.get_task(task_id)
            return (
                code,
                json.dumps(
                    {
                        "task_id": task_id,
                        "revision": task.current_revision,
                        "snapshot_updated_us": task.updated_us,
                    }
                ),
                refs,
            )

        result = self.executor.run(
            principal,
            operation=operation,
            target=target,
            payload={"expected_revision": expected_revision, "fields": fields},
            reason=reason,
            idempotency_key=idempotency_key,
            execute=execute,
        )
        return self._result(principal, result)

    def trigger(
        self,
        principal: OperatorPrincipal,
        task_id: str,
        *,
        operation: str,
        expected_revision: int,
        fields: dict[str, Any],
        reason: str,
        idempotency_key: str,
        trigger_id: str | None = None,
        child_expected_revision: int | None = None,
    ) -> dict[str, Any]:
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.write")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            parent = reader.get(ResourceRef("task", task_id))
            if parent is None:
                raise NotFoundError("task not found")
            refs: tuple[ResourceRef, ...] = ()
            if fields.get("task_step_id"):
                refs += (ResourceRef("task_step", fields["task_step_id"]),)
            condition = fields.get("condition_spec")
            if isinstance(condition, dict):
                refs += tuple(
                    ResourceRef(kind, condition[name])
                    for name, kind in (
                        ("task_id", "task"),
                        ("task_step_id", "task_step"),
                    )
                    if condition.get(name)
                )
            if trigger_id is not None:
                refs += (ResourceRef("task_trigger", trigger_id),)
            target = CommandTarget("task", parent.scope, resource_id=task_id, source_refs=refs)
        result = self.executor.run(
            principal,
            operation=operation,
            target=target,
            payload={
                "expected_revision": expected_revision,
                "trigger_id": trigger_id,
                "child_expected_revision": child_expected_revision,
                "fields": fields,
            },
            reason=reason,
            idempotency_key=idempotency_key,
            execute=lambda tx, actor: self.tasks.trigger_for_command(
                tx,
                actor,
                task_id,
                expected_revision=expected_revision,
                fields=fields,
                trigger_id=trigger_id,
                child_expected_revision=child_expected_revision,
            ),
        )
        outcome: dict[str, Any] = json.loads(result.body)
        return {**outcome, "canonical_status": "committed"}

    def dependency(
        self,
        principal: OperatorPrincipal,
        task_id: str,
        *,
        operation: str,
        expected_revision: int,
        fields: dict[str, Any],
        reason: str,
        idempotency_key: str,
        dependency_id: str | None = None,
        child_expected_revision: int | None = None,
    ) -> dict[str, Any]:
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.write")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            parent = reader.get(ResourceRef("task", task_id))
            if parent is None:
                raise NotFoundError("task not found")
            refs = tuple(
                ResourceRef("task_step", fields[name])
                for name in ("predecessor_step_id", "successor_step_id")
                if name in fields
            )
            if dependency_id is not None:
                refs += (ResourceRef("task_dependency", dependency_id),)
            target = CommandTarget("task", parent.scope, resource_id=task_id, source_refs=refs)
        result = self.executor.run(
            principal,
            operation=operation,
            target=target,
            payload={
                "expected_revision": expected_revision,
                "dependency_id": dependency_id,
                "child_expected_revision": child_expected_revision,
                "fields": fields,
            },
            reason=reason,
            idempotency_key=idempotency_key,
            execute=lambda tx, actor: self.tasks.dependency_for_command(
                tx,
                actor,
                task_id,
                expected_revision=expected_revision,
                fields=fields,
                dependency_id=dependency_id,
                child_expected_revision=child_expected_revision,
            ),
        )
        outcome: dict[str, Any] = json.loads(result.body)
        return {**outcome, "canonical_status": "committed"}

    def step(
        self,
        principal: OperatorPrincipal,
        task_id: str,
        *,
        operation: str,
        expected_revision: int,
        fields: dict[str, Any],
        reason: str,
        idempotency_key: str,
        step_id: str | None = None,
        child_expected_revision: int | None = None,
    ) -> dict[str, Any]:
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.write")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            parent = reader.get(ResourceRef("task", task_id))
            if parent is None:
                raise NotFoundError("task not found")
            refs = tuple(
                ResourceRef(ref["resource_type"], ref["resource_id"], ref.get("revision"))
                for ref in fields.get("completion_evidence_refs", [])
            )
            if step_id is not None:
                refs += (ResourceRef("task_step", step_id),)
            target = CommandTarget("task", parent.scope, resource_id=task_id, source_refs=refs)
        result = self.executor.run(
            principal,
            operation=operation,
            target=target,
            payload={
                "expected_revision": expected_revision,
                "step_id": step_id,
                "child_expected_revision": child_expected_revision,
                "fields": fields,
            },
            reason=reason,
            idempotency_key=idempotency_key,
            execute=lambda tx, actor: self.tasks.step_for_command(
                tx,
                actor,
                task_id,
                expected_revision=expected_revision,
                fields=fields,
                step_id=step_id,
                child_expected_revision=child_expected_revision,
            ),
        )
        outcome: dict[str, Any] = json.loads(result.body)
        return {**outcome, "canonical_status": "committed"}

    def trigger_listing_actions(
        self,
        principal: OperatorPrincipal,
        task_id: str,
        records: tuple[ReadRecord, ...],
    ) -> tuple[bool, dict[str, tuple[str, ...]]]:
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.read")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            parent = reader.get(ResourceRef("task", task_id))
            writable = bool(
                parent is not None
                and "memory.write" in fresh.permissions
                and parent.status in {"proposed", "active", "waiting", "blocked"}
                and reader.authority.mutable(tx, parent)
            )
            actions: dict[str, tuple[str, ...]] = {}
            for record in records:
                current = reader.get(ResourceRef("task_trigger", record.id))
                allowed = bool(
                    writable
                    and current is not None
                    and current.revision == record.revision
                    and reader.authority.mutable(tx, current)
                )
                actions[record.id] = ("update", "enabled") if allowed else ()
            return writable, actions

    def dependency_listing_actions(
        self,
        principal: OperatorPrincipal,
        task_id: str,
        records: tuple[ReadRecord, ...],
    ) -> tuple[bool, dict[str, tuple[str, ...]]]:
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.read")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            parent = reader.get(ResourceRef("task", task_id))
            writable = bool(
                parent is not None
                and "memory.write" in fresh.permissions
                and parent.status in {"proposed", "active", "waiting", "blocked"}
                and reader.authority.mutable(tx, parent)
            )
            actions: dict[str, tuple[str, ...]] = {}
            for record in records:
                current = reader.get(ResourceRef("task_dependency", record.id))
                allowed = bool(
                    writable
                    and current is not None
                    and current.revision == record.revision
                    and reader.authority.mutable(tx, current)
                    and current.status == "active"
                )
                actions[record.id] = ("remove",) if allowed else ()
            return writable, actions

    def step_listing_actions(
        self,
        principal: OperatorPrincipal,
        task_id: str,
        records: tuple[ReadRecord, ...],
    ) -> tuple[bool, dict[str, tuple[str, ...]]]:
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.read")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            parent = reader.get(ResourceRef("task", task_id))
            writable = bool(
                parent is not None
                and "memory.write" in fresh.permissions
                and parent.status in {"proposed", "active", "waiting", "blocked"}
                and reader.authority.mutable(tx, parent)
            )
            actions: dict[str, tuple[str, ...]] = {}
            for record in records:
                current = reader.get(ResourceRef("task_step", record.id))
                allowed = bool(
                    writable
                    and current is not None
                    and current.revision == record.revision
                    and reader.authority.mutable(tx, current)
                    and TASK_STEP_TRANSITIONS.get(current.status)
                )
                actions[record.id] = ("transition",) if allowed else ()
            return writable, actions

    def available_actions(
        self, principal: OperatorPrincipal, record: ReadRecord
    ) -> tuple[str, ...]:
        """Discover actions from fresh permission and current lifecycle; commands recheck."""
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.read")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            current = reader.get(ResourceRef("task", record.id))
            if (
                current is None
                or current.revision != record.revision
                or "memory.write" not in fresh.permissions
                or not reader.authority.mutable(tx, current)
            ):
                return ()
            actions = (
                ["update"] if current.status in {"proposed", "active", "waiting", "blocked"} else []
            )
            if TASK_TRANSITIONS.get(current.status):
                actions.append("transition")
            return tuple(actions)

    def _result(self, principal: OperatorPrincipal, result: IdempotentResult) -> ReadRecord:
        outcome = json.loads(result.body)
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.read")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            # Replay returns the original revision only while both it and the
            # current record remain visible. This is not a general history API.
            record = reader.get(ResourceRef("task", outcome["task_id"], int(outcome["revision"])))
            if record is None:
                raise NotFoundError("command task is no longer visible")
            record = replace(record, updated_us=int(outcome["snapshot_updated_us"]))
            return reader.sanitized(record, summary=False)
