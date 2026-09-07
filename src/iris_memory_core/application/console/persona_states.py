"""Managed PersonaState commands with independent State revision fencing."""

import json
from dataclasses import dataclass
from typing import Any

from iris_memory_core.application.console.commands import CommandTarget, ConsoleCommandExecutor
from iris_memory_core.application.console.reads import ResourceReader
from iris_memory_core.application.console.resources import ReadRecord, ResourceRef
from iris_memory_core.application.console.security import OperatorSecurity, authorize, denied
from iris_memory_core.application.persona import PersonaService
from iris_memory_core.application.ports import Transaction
from iris_memory_core.domain.console import CommandActor, OperatorPrincipal
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.domain.scope import Scope


@dataclass(frozen=True, slots=True)
class PersonaStateReceipt:
    resource_id: str
    agent_id: str
    revision: int
    resource_type: str = "persona_state"
    canonical_status: str = "committed"


@dataclass(frozen=True, slots=True)
class PersonaStateRead:
    agent_id: str
    record: ReadRecord | None
    expected_revision: int
    available_actions: tuple[str, ...]


class ConsolePersonaStateCommands:
    def __init__(self, security: OperatorSecurity) -> None:
        self.security = security
        self.executor = ConsoleCommandExecutor(security)
        self.personas = PersonaService(security.uow, security.clock)

    def current(self, principal: OperatorPrincipal, agent_id: str) -> PersonaStateRead:
        with self.security.uow.read() as tx, tx.console_reads.budget():
            now = self.security.clock.now_us()
            fresh = authorize(tx, principal, now, "memory.read")
            if "console.manage" not in fresh.key.grant.data_purposes:
                raise denied("permission_denied")
            reader = ResourceReader(tx, fresh, now)
            agent = reader.get(ResourceRef("agent", agent_id))
            if agent is None:
                raise NotFoundError("Persona State not found")
            current = tx.personas.current_state(agent_id)
            record = reader.get(ResourceRef("persona_state", current.id)) if current else None
            if current is not None and record is None:
                raise NotFoundError("Persona State not found")
            actions = []
            if "memory.write" in fresh.permissions and reader.authority.mutable(tx, agent):
                actions.append("update")
                if current is not None and current.expires_us <= now:
                    actions.append("clear")
            return PersonaStateRead(
                agent_id,
                reader.sanitized(record, summary=False) if record else None,
                current.revision if current else 0,
                tuple(actions),
            )

    def mutate(
        self,
        principal: OperatorPrincipal,
        agent_id: str,
        *,
        operation: str,
        expected_revision: int,
        fields: dict[str, Any],
        source_refs: list[dict[str, object]],
        reason: str,
        idempotency_key: str,
    ) -> PersonaStateReceipt:
        target = CommandTarget(
            "agent", Scope(principal.key.tenant_id, agent_id), resource_id=agent_id
        )

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            return self.personas.mutate_state_for_command(
                tx,
                actor,
                agent_id,
                expected_revision=expected_revision,
                fields=fields,
                source_refs=source_refs,
            )

        result = self.executor.run(
            principal,
            operation=operation,
            target=target,
            payload={
                "expected_revision": expected_revision,
                "fields": fields,
                "source_refs": source_refs,
            },
            reason=reason,
            idempotency_key=idempotency_key,
            execute=execute,
        )
        value = json.loads(result.body)
        return PersonaStateReceipt(value["id"], agent_id, value["revision"])
