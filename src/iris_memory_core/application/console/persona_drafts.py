"""Managed draft commands use the shared transaction, actor and replay boundary."""

import json
from dataclasses import dataclass
from typing import Any

from iris_memory_core.application.console.commands import CommandTarget, ConsoleCommandExecutor
from iris_memory_core.application.console.reads import ResourceReader
from iris_memory_core.application.console.resources import ReadRecord, ResourceRef
from iris_memory_core.application.console.security import OperatorSecurity, authorize, denied
from iris_memory_core.application.persona_draft_deletion import draft_is_held
from iris_memory_core.application.persona_drafts import PersonaDraftService
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.domain.console import CommandActor, OperatorPrincipal
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.domain.scope import Scope


@dataclass(frozen=True, slots=True)
class PersonaDraftReceipt:
    resource_id: str
    agent_id: str
    revision: int
    status: str
    published_revision_id: str | None
    resource_type: str = "persona_draft"
    canonical_status: str = "committed"


@dataclass(frozen=True, slots=True)
class PersonaDraftContext:
    agent_id: str
    draft: ReadRecord | None
    current: ReadRecord | None
    expected_revision: int
    base_revision: int | None
    expected_policy_revision: int | None
    available_actions: tuple[str, ...]


class ConsolePersonaDraftCommands:
    def __init__(self, security: OperatorSecurity) -> None:
        self.security = security
        self.executor = ConsoleCommandExecutor(security)
        self.drafts = PersonaDraftService(security.uow, security.clock)

    def context(
        self, principal: OperatorPrincipal, agent_id: str, draft_id: str | None = None
    ) -> PersonaDraftContext:
        with self.security.uow.read() as tx, tx.console_reads.budget():
            now = self.security.clock.now_us()
            fresh = authorize(tx, principal, now, "memory.read")
            if "console.manage" not in fresh.key.grant.data_purposes:
                raise denied("permission_denied")
            reader = ResourceReader(tx, fresh, now)
            agent = reader.get(ResourceRef("agent", agent_id))
            if agent is None:
                raise NotFoundError("Persona draft parent not found")
            record = None
            if draft_id is not None:
                stored = tx.persona_drafts.get(fresh.key.tenant_id, agent_id, draft_id)
                record = reader.get(ResourceRef("persona_draft", stored.id))
                if record is None:
                    raise NotFoundError("Persona draft not found")
            persona = tx.personas.current(agent_id)
            current = reader.get(ResourceRef("persona_revision", persona.id))
            policy = tx.personas.current_policy(agent_id) if current is not None else None
            actions = []
            writable = "persona.publish" in fresh.permissions and reader.authority.mutable(
                tx, agent
            )
            held = record is not None and draft_is_held(
                tx, fresh.key.tenant_id, agent_id, record.id
            )
            if writable and (record is None or record.status == "draft"):
                if current is not None:
                    if not held:
                        actions.append("create" if record is None else "update")
                    if (
                        record is not None
                        and policy is not None
                        and (
                            record.fields["base_revision"] == current.revision
                            and record.fields["policy_revision"] == policy.revision
                        )
                    ):
                        actions.append("publish")
                if record is not None and not held:
                    actions.append("discard")
            return PersonaDraftContext(
                agent_id,
                record,
                current,
                record.revision if record else 0,
                current.revision if current else None,
                policy.revision if policy else None,
                tuple(actions),
            )

    def mutate(
        self,
        principal: OperatorPrincipal,
        agent_id: str,
        *,
        operation: str,
        draft_id: str | None,
        expected_revision: int,
        base_revision: int | None,
        policy_revision: int | None,
        fields: dict[str, Any],
        source_refs: list[dict[str, object]],
        reason: str,
        idempotency_key: str,
    ) -> PersonaDraftReceipt:
        target = CommandTarget(
            "agent", Scope(principal.key.tenant_id, agent_id), resource_id=agent_id
        )

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            return self.drafts.mutate_for_command(
                tx,
                actor,
                agent_id,
                draft_id=draft_id,
                expected_revision=expected_revision,
                base_revision=base_revision,
                policy_revision=policy_revision,
                fields=fields,
                source_refs=source_refs,
                request_key=idempotency_key,
            )

        result = self.executor.run(
            principal,
            operation=operation,
            target=target,
            payload={
                "draft_id": draft_id,
                "expected_revision": expected_revision,
                "base_revision": base_revision,
                "policy_revision": policy_revision,
                "fields": fields,
                "source_refs": source_refs,
            },
            execute=execute,
            reason=reason,
            idempotency_key=idempotency_key,
        )
        value = json.loads(result.body)
        return PersonaDraftReceipt(
            value["id"],
            agent_id,
            value["revision"],
            value["status"],
            value["published_revision_id"],
        )
