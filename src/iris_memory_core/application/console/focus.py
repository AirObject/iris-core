"""Console Focus commands delegate Canonical writes to the existing Focus service."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from iris_memory_core.application.console.commands import CommandTarget, ConsoleCommandExecutor
from iris_memory_core.application.console.reads import ResourceReader
from iris_memory_core.application.console.resources import ReadRecord, ResourceRef
from iris_memory_core.application.console.security import OperatorSecurity, authorize
from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.domain.console import CommandActor, OperatorPrincipal
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.domain.model import IdempotentResult
from iris_memory_core.domain.scope import Scope


class ConsoleFocusCommands:
    def __init__(self, security: OperatorSecurity) -> None:
        self.security = security
        self.executor = ConsoleCommandExecutor(security)
        self.focus = FocusService(
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
            "focus_item",
            scope,
            privacy_labels=tuple(privacy_labels),
            source_refs=tuple(
                ResourceRef(ref["resource_type"], ref["resource_id"], ref.get("revision"))
                for ref in source_refs
            ),
        )

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            code, body, refs = self.focus.create_for_command(
                tx,
                actor,
                fields,
                privacy_labels=privacy_labels,
                source_refs=source_refs,
            )
            outcome = json.loads(body)
            outcome["snapshot_updated_us"] = tx.focus.get(outcome["item_id"]).updated_us
            return code, json.dumps(outcome), refs

        result = self.executor.run(
            principal,
            operation="focus.create",
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
        item_id: str,
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
            current = reader.get(ResourceRef("focus_item", item_id))
            if current is None:
                raise NotFoundError("focus item not found")
            target = CommandTarget(
                "focus_item", current.scope, resource_id=item_id, source_refs=current.source_refs
            )

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            code, _, refs = self.focus.mutate_for_command(
                tx,
                actor,
                item_id,
                expected_revision=expected_revision,
                fields=fields,
            )
            item = tx.focus.get(item_id)
            return (
                code,
                json.dumps(
                    {
                        "item_id": item_id,
                        "revision": item.current_revision,
                        "snapshot_updated_us": item.updated_us,
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

    def available_actions(
        self, principal: OperatorPrincipal, record: ReadRecord
    ) -> tuple[str, ...]:
        """Discover actions from fresh permission and current lifecycle; commands recheck."""
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.read")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            current = reader.get(ResourceRef("focus_item", record.id))
            if (
                current is None
                or current.revision != record.revision
                or "memory.write" not in fresh.permissions
                or not reader.authority.mutable(tx, current)
            ):
                return ()
            actions = ["update"] if current.status in {"active", "dormant"} else []
            if current.status in {"active", "dormant"}:
                actions.extend(("activate", "transition"))
            return tuple(actions)

    def _result(self, principal: OperatorPrincipal, result: IdempotentResult) -> ReadRecord:
        outcome = json.loads(result.body)
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.read")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            # Replay returns the original revision only while both it and the
            # current record remain visible. This is not a general history API.
            record = reader.get(
                ResourceRef("focus_item", outcome["item_id"], int(outcome["revision"]))
            )
            if record is None:
                raise NotFoundError("command focus is no longer visible")
            record = replace(record, updated_us=int(outcome["snapshot_updated_us"]))
            return reader.sanitized(record, summary=False)
