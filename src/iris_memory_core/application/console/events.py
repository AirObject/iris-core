"""Explicit event dismissal without an ACK, external effect or content edit."""

import json
from dataclasses import dataclass

from iris_memory_core.application.console.commands import CommandTarget, ConsoleCommandExecutor
from iris_memory_core.application.console.reads import ResourceReader
from iris_memory_core.application.console.resources import ReadRecord, ResourceRef
from iris_memory_core.application.console.security import OperatorSecurity, authorize
from iris_memory_core.application.events import CognitiveEventService
from iris_memory_core.application.ports import Transaction
from iris_memory_core.domain.console import CommandActor, OperatorPrincipal
from iris_memory_core.domain.errors import NotFoundError


@dataclass(frozen=True, slots=True)
class EventCommandReceipt:
    resource_id: str
    revision: int
    resource_type: str = "cognitive_event"
    canonical_status: str = "committed"


class ConsoleEventCommands:
    def __init__(self, security: OperatorSecurity) -> None:
        self.security = security
        self.executor = ConsoleCommandExecutor(security)
        self.events = CognitiveEventService(security.uow, security.clock)

    def available_actions(
        self, principal: OperatorPrincipal, record: ReadRecord
    ) -> tuple[str, ...]:
        with self.security.uow.read() as tx:
            now = self.security.clock.now_us()
            fresh = authorize(tx, principal, now, "memory.read")
            reader = ResourceReader(tx, fresh, now)
            current = reader.get(ResourceRef("cognitive_event", record.id))
            if (
                current is None
                or current.revision != record.revision
                or current.status not in {"pending", "delivered"}
                or "memory.write" not in fresh.permissions
                or not reader.authority.mutable(tx, current)
            ):
                return ()
            return ("dismiss",)

    def dismiss(
        self,
        principal: OperatorPrincipal,
        identifier: str,
        *,
        expected_revision: int,
        reason: str,
        idempotency_key: str,
    ) -> EventCommandReceipt:
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.write")
            record = ResourceReader(tx, fresh, self.security.clock.now_us()).get(
                ResourceRef("cognitive_event", identifier)
            )
            if record is None:
                raise NotFoundError("event not found")
            target = CommandTarget("cognitive_event", record.scope, resource_id=identifier)

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            return self.events.dismiss_for_command(
                tx, actor, identifier, expected_revision=expected_revision
            )

        result = self.executor.run(
            principal,
            operation="cognitive_event.dismiss",
            target=target,
            payload={"expected_revision": expected_revision},
            idempotency_key=idempotency_key,
            reason=reason,
            execute=execute,
        )
        value = json.loads(result.body)
        return EventCommandReceipt(value["event_id"], value["revision"])
