"""Sensitive Policy replacement under the actual parent Agent authority."""

import json
from dataclasses import dataclass

from iris_memory_core.application.console.commands import CommandTarget, ConsoleCommandExecutor
from iris_memory_core.application.console.reads import ResourceReader
from iris_memory_core.application.console.resources import ResourceRef
from iris_memory_core.application.console.security import OperatorSecurity, authorize, denied
from iris_memory_core.application.persona import PersonaService
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.domain.console import CommandActor, OperatorPrincipal
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.domain.persona import PersonaPolicy
from iris_memory_core.domain.scope import Scope


@dataclass(frozen=True, slots=True)
class PersonaPolicyReceipt:
    resource_id: str
    agent_id: str
    revision: int
    content_hash: str
    resource_type: str = "persona_policy"
    canonical_status: str = "committed"


class ConsolePersonaPolicyCommands:
    def __init__(self, security: OperatorSecurity) -> None:
        self.security = security
        self.executor = ConsoleCommandExecutor(security)
        self.personas = PersonaService(security.uow, security.clock)

    def current(self, principal: OperatorPrincipal, agent_id: str) -> tuple[PersonaPolicy, bool]:
        with self.security.uow.read() as tx, tx.console_reads.budget():
            now = self.security.clock.now_us()
            fresh = authorize(tx, principal, now, "memory.read")
            if "console.manage" not in fresh.key.grant.data_purposes:
                raise denied("permission_denied")
            reader = ResourceReader(tx, fresh, now)
            agent = reader.get(ResourceRef("agent", agent_id))
            if agent is None:
                raise NotFoundError("Persona Policy not found")
            policy = tx.personas.current_policy(agent_id)
            return policy, "persona.publish" in fresh.permissions and reader.authority.mutable(
                tx, agent
            )

    def replace(
        self,
        principal: OperatorPrincipal,
        agent_id: str,
        *,
        expected_revision: int,
        config: dict[str, object],
        reason: str,
        idempotency_key: str,
    ) -> PersonaPolicyReceipt:
        target = CommandTarget(
            "agent", Scope(principal.key.tenant_id, agent_id), resource_id=agent_id
        )

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            return self.personas.replace_policy_for_command(
                tx, actor, agent_id, expected_revision=expected_revision, config=config
            )

        result = self.executor.run(
            principal,
            operation="persona.policy.replace",
            target=target,
            payload={"expected_revision": expected_revision, "config": config},
            reason=reason,
            idempotency_key=idempotency_key,
            execute=execute,
        )
        value = json.loads(result.body)
        return PersonaPolicyReceipt(value["id"], agent_id, value["revision"], value["content_hash"])
