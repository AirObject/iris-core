"""State management returns receipts so retention cannot leak pruned values."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from iris_memory_core.application.console.commands import CommandTarget, ConsoleCommandExecutor
from iris_memory_core.application.console.reads import ResourceReader
from iris_memory_core.application.console.resources import ReadRecord, ResourceRef
from iris_memory_core.application.console.security import OperatorSecurity, authorize
from iris_memory_core.application.ports import Transaction
from iris_memory_core.application.state import StateService
from iris_memory_core.domain.console import CommandActor, OperatorPrincipal
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.domain.scope import Scope
from iris_memory_core.domain.state import resolve_namespace_policy


@dataclass(frozen=True, slots=True)
class StateCommandReceipt:
    resource_id: str
    revision: int
    resource_type: str = "state_record"
    canonical_status: str = "committed"


class ConsoleStateCommands:
    def __init__(self, security: OperatorSecurity) -> None:
        self.security = security
        self.executor = ConsoleCommandExecutor(security)
        self.states = StateService(security.uow, security.clock)

    def create(
        self,
        principal: OperatorPrincipal,
        *,
        scope: Scope,
        fields: dict[str, Any],
        expected_revision: int,
        reason: str,
        idempotency_key: str,
    ) -> StateCommandReceipt:
        return self._run(
            principal,
            operation="state.create",
            target=CommandTarget("state_record", scope, namespace=fields.get("namespace")),
            fields=fields,
            expected_revision=expected_revision,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def mutate(
        self,
        principal: OperatorPrincipal,
        record_id: str,
        *,
        operation: str,
        fields: dict[str, Any],
        expected_revision: int,
        reason: str,
        idempotency_key: str,
    ) -> StateCommandReceipt:
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.write")
            record = ResourceReader(tx, fresh, self.security.clock.now_us()).get(
                ResourceRef("state_record", record_id)
            )
            if record is None:
                raise NotFoundError("state not found")
            target = CommandTarget(
                "state_record",
                record.scope,
                resource_id=record_id,
                namespace=record.fields["namespace"],
            )
        return self._run(
            principal,
            operation=operation,
            target=target,
            fields=fields,
            expected_revision=expected_revision,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def _run(
        self,
        principal: OperatorPrincipal,
        *,
        operation: str,
        target: CommandTarget,
        fields: dict[str, Any],
        expected_revision: int,
        reason: str,
        idempotency_key: str,
    ) -> StateCommandReceipt:
        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            return self.states.put_for_command(
                tx,
                actor,
                fields=fields,
                expected_revision=expected_revision,
                record_id=target.resource_id,
            )

        result = self.executor.run(
            principal,
            operation=operation,
            target=target,
            payload={"fields": fields, "expected_revision": expected_revision},
            reason=reason,
            idempotency_key=idempotency_key,
            execute=execute,
        )
        outcome = json.loads(result.body)
        return StateCommandReceipt(outcome["record_id"], int(outcome["revision"]))

    def available_actions(
        self, principal: OperatorPrincipal, record: ReadRecord
    ) -> tuple[str, ...]:
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.read")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            current = reader.get(ResourceRef("state_record", record.id))
            if (
                current is None
                or current.revision != record.revision
                or "memory.write" not in fresh.permissions
                or not reader.authority.mutable(tx, current)
            ):
                return ()
            policy = resolve_namespace_policy(
                current.fields["namespace"],
                tx.states.policy(fresh.key.tenant_id, current.fields["namespace"]),
            )
            if "user" not in policy.allowed_source_authorities:
                return ()
            return ("update", "expire") if current.status != "expired" else ("update",)
