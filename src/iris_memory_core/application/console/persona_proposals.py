"""Actual management authority for proposal-only creation and human review."""

import json
from dataclasses import dataclass
from typing import Any

from iris_memory_core.application.console.commands import CommandTarget, ConsoleCommandExecutor
from iris_memory_core.application.console.reads import ResourceReader
from iris_memory_core.application.console.resources import ReadRecord, ResourceRef
from iris_memory_core.application.console.security import OperatorSecurity, authorize, denied
from iris_memory_core.application.persona import PersonaService
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.domain.console import CommandActor, OperatorPrincipal
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.domain.persona import PersonaPolicyMode, PersonaProposalStatus
from iris_memory_core.domain.scope import Scope


@dataclass(frozen=True, slots=True)
class PersonaProposalReceipt:
    resource_id: str
    agent_id: str
    status: str
    published_revision_id: str | None
    resource_type: str = "persona_proposal"
    canonical_status: str = "committed"


@dataclass(frozen=True, slots=True)
class PersonaProposalContext:
    agent_id: str
    proposal: ReadRecord | None
    base_revision: int
    current_revision: int | None
    expected_policy_revision: int | None
    policy_mode: str | None
    available_actions: tuple[str, ...]


class ConsolePersonaProposalCommands:
    def __init__(self, security: OperatorSecurity) -> None:
        self.security = security
        self.executor = ConsoleCommandExecutor(security)
        self.personas = PersonaService(security.uow, security.clock)

    def context(
        self, principal: OperatorPrincipal, agent_id: str, proposal_id: str | None = None
    ) -> PersonaProposalContext:
        with self.security.uow.read() as tx, tx.console_reads.budget():
            now = self.security.clock.now_us()
            fresh = authorize(tx, principal, now, "memory.read")
            if "console.manage" not in fresh.key.grant.data_purposes:
                raise denied("permission_denied")
            reader = ResourceReader(tx, fresh, now)
            agent = reader.get(ResourceRef("agent", agent_id))
            if agent is None:
                raise NotFoundError("Persona proposal not found")
            mutable = reader.authority.mutable(tx, agent)
            current = tx.personas.current(agent_id)
            visible_current = reader.get(ResourceRef("persona_revision", current.id))
            policy = tx.personas.current_policy(agent_id) if visible_current else None
            actions: list[str] = []
            record = None
            base_revision = current.revision
            if proposal_id is None:
                if visible_current is None:
                    raise NotFoundError("Persona not found")
                if (
                    mutable
                    and "memory.write" in fresh.permissions
                    and policy is not None
                    and policy.mode is not PersonaPolicyMode.LOCKED
                ):
                    actions.append("create")
            else:
                record = reader.get(ResourceRef("persona_proposal", proposal_id))
                if record is None or record.scope.agent_id != agent_id:
                    raise NotFoundError("Persona proposal not found")
                proposal = tx.personas.proposal(proposal_id)
                base_revision = proposal.base_revision
                if (
                    mutable
                    and "persona.publish" in fresh.permissions
                    and proposal.status is PersonaProposalStatus.PROPOSED
                ):
                    if (
                        policy is not None
                        and policy.mode is not PersonaPolicyMode.LOCKED
                        and proposal.base_revision == current.revision
                        and proposal.expires_us > now
                    ):
                        actions.append("approve")
                    actions.append("reject")
                record = reader.sanitized(record, summary=False)
            return PersonaProposalContext(
                agent_id,
                record,
                base_revision,
                current.revision if visible_current else None,
                policy.revision if policy else None,
                policy.mode.value if policy else None,
                tuple(actions),
            )

    def mutate(
        self,
        principal: OperatorPrincipal,
        agent_id: str,
        *,
        operation: str,
        base_revision: int,
        expected_policy_revision: int | None,
        fields: dict[str, Any],
        evidence_refs: list[dict[str, object]],
        proposal_id: str | None,
        reason: str,
        idempotency_key: str,
    ) -> PersonaProposalReceipt:
        target = CommandTarget(
            "agent", Scope(principal.key.tenant_id, agent_id), resource_id=agent_id
        )

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            return self.personas.mutate_proposal_for_command(
                tx,
                actor,
                agent_id,
                base_revision=base_revision,
                expected_policy_revision=expected_policy_revision,
                fields=fields,
                evidence_refs=evidence_refs,
                proposal_id=proposal_id,
            )

        result = self.executor.run(
            principal,
            operation=operation,
            target=target,
            payload={
                "base_revision": base_revision,
                "expected_policy_revision": expected_policy_revision,
                "fields": fields,
                "evidence_refs": evidence_refs,
                "proposal_id": proposal_id,
            },
            reason=reason,
            idempotency_key=idempotency_key,
            execute=execute,
        )
        value = json.loads(result.body)
        return PersonaProposalReceipt(
            value["id"], agent_id, value["status"], value["published_revision_id"]
        )
