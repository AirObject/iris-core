"""Managed Persona commands anchored to a stable Agent identity."""

import json
from dataclasses import dataclass, replace
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
class PersonaCommandReceipt:
    resource_id: str
    agent_id: str
    revision: int
    content_hash: str
    resource_type: str = "persona_revision"
    canonical_status: str = "committed"


class ConsolePersonaCommands:
    def __init__(self, security: OperatorSecurity) -> None:
        self.security = security
        self.executor = ConsoleCommandExecutor(security)
        self.personas = PersonaService(security.uow, security.clock)

    def current(
        self, principal: OperatorPrincipal, agent_id: str
    ) -> tuple[ReadRecord, tuple[str, ...]]:
        with self.security.uow.read() as tx, tx.console_reads.budget():
            now = self.security.clock.now_us()
            fresh = authorize(tx, principal, now, "memory.read")
            if "console.manage" not in fresh.key.grant.data_purposes:
                raise denied("permission_denied")
            reader = ResourceReader(tx, fresh, now)
            agent = reader.get(ResourceRef("agent", agent_id))
            if agent is None:
                raise NotFoundError("Persona not found")
            current = tx.personas.current(agent_id)
            record = reader.get(ResourceRef("persona_revision", current.id))
            if record is None:
                raise NotFoundError("Persona not found")
            policy = tx.personas.current_policy(agent_id)
            record = replace(
                reader.sanitized(record, summary=False),
                fields={
                    **record.fields,
                    "content_hash": current.content_hash,
                    "policy_revision": policy.revision,
                    "policy_mode": policy.mode.value,
                },
            )
            actions = (
                ("publish", "rollback")
                if ("persona.publish" in fresh.permissions and reader.authority.mutable(tx, agent))
                else ()
            )
            return record, actions

    def mutate(
        self,
        principal: OperatorPrincipal,
        agent_id: str,
        *,
        operation: str,
        expected_revision: int,
        expected_policy_revision: int,
        fields: dict[str, Any],
        source_refs: list[dict[str, object]],
        target_revision: int | None = None,
        reason: str,
        idempotency_key: str,
    ) -> PersonaCommandReceipt:
        target = CommandTarget(
            "agent", Scope(principal.key.tenant_id, agent_id), resource_id=agent_id
        )

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            return self.personas.mutate_for_command(
                tx,
                actor,
                agent_id,
                expected_revision=expected_revision,
                expected_policy_revision=expected_policy_revision,
                fields=fields,
                source_refs=source_refs,
                target_revision=target_revision,
            )

        result = self.executor.run(
            principal,
            operation=operation,
            target=target,
            payload={
                "expected_revision": expected_revision,
                "expected_policy_revision": expected_policy_revision,
                "fields": fields,
                "source_refs": source_refs,
                "target_revision": target_revision,
            },
            idempotency_key=idempotency_key,
            reason=reason,
            execute=execute,
        )
        value = json.loads(result.body)
        return PersonaCommandReceipt(
            value["id"], agent_id, value["revision"], value["content_hash"]
        )
