"""Explicit draft editing and publication under the actual Console command actor."""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from iris_memory_core.application.console.commands import CommandTarget, command_access
from iris_memory_core.application.console.resources import ResourceRef
from iris_memory_core.application.persona import PersonaService
from iris_memory_core.application.persona_draft_deletion import discard_draft_in_tx, draft_is_held
from iris_memory_core.application.ports.clock import Clock
from iris_memory_core.application.ports.transaction import Transaction, UnitOfWork
from iris_memory_core.domain.console import CommandActor
from iris_memory_core.domain.errors import (
    InvalidRequestError,
    InvalidTransitionError,
    RevisionMismatchError,
)
from iris_memory_core.domain.retention import LegalHoldActiveError
from iris_memory_core.domain.scope import Scope


class PersonaDraftService:
    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self.clock = clock
        self.personas = PersonaService(uow, clock)

    def mutate_for_command(
        self,
        tx: Transaction,
        actor: CommandActor,
        agent_id: str,
        *,
        draft_id: str | None,
        expected_revision: int,
        base_revision: int | None,
        policy_revision: int | None,
        fields: Mapping[str, Any],
        source_refs: Sequence[Mapping[str, object]],
        request_key: str,
    ) -> tuple[str, str, list[str]]:
        if actor.operation not in {
            "persona.draft.create",
            "persona.draft.update",
            "persona.draft.publish",
            "persona.draft.discard",
        }:
            raise InvalidRequestError("invalid Persona draft operation")
        scope = Scope(actor.tenant_id, agent_id)
        access = command_access(
            tx,
            actor,
            CommandTarget("agent", scope, resource_id=agent_id),
            now_us=self.clock.now_us(),
        )
        current = None
        if actor.operation == "persona.draft.create":
            if draft_id is not None or type(expected_revision) is not int or expected_revision != 0:
                raise InvalidRequestError("invalid new Persona draft revision")
        else:
            if not draft_id:
                raise InvalidRequestError("Persona draft id required")
            current = tx.persona_drafts.get(actor.tenant_id, agent_id, draft_id)
            command_access(
                tx,
                actor,
                CommandTarget(
                    "agent",
                    scope,
                    resource_id=agent_id,
                    source_refs=(ResourceRef("persona_draft", draft_id),),
                ),
                now_us=self.clock.now_us(),
            )
            if type(expected_revision) is not int or expected_revision < 1:
                raise InvalidRequestError("invalid Persona draft revision")
            if expected_revision != current.revision:
                raise RevisionMismatchError(
                    "persona_draft", draft_id, expected_revision, current.revision
                )
            if current.status != "draft":
                raise InvalidTransitionError("Persona draft is already closed")
        if (
            actor.operation == "persona.draft.update"
            and current is not None
            and draft_is_held(tx, actor.tenant_id, agent_id, current.id)
        ):
            raise LegalHoldActiveError("a legal hold protects the retained Persona draft")
        refs: list[str]
        if actor.operation in {"persona.draft.create", "persona.draft.update"}:
            if type(base_revision) is not int or type(policy_revision) is not int:
                raise InvalidRequestError("reviewed Persona and Policy revisions required")
            persona = tx.personas.current(agent_id)
            command_access(
                tx,
                actor,
                CommandTarget(
                    "agent",
                    scope,
                    resource_id=agent_id,
                    source_refs=(ResourceRef("persona_revision", persona.id),),
                ),
                now_us=self.clock.now_us(),
            )
            evidence = self.personas._validate_evidence(
                tx, access, agent_id, source_refs, required=False, command_actor=actor
            )
            if current is None:
                draft = tx.persona_drafts.create(
                    tenant_id=actor.tenant_id,
                    agent_id=agent_id,
                    base_revision=base_revision,
                    policy_revision=policy_revision,
                    fields=fields,
                    source_refs=[item.ref for item in evidence],
                    actor=actor.audit_actor,
                )
            else:
                draft = tx.persona_drafts.update(
                    tenant_id=actor.tenant_id,
                    agent_id=agent_id,
                    base_revision=base_revision,
                    policy_revision=policy_revision,
                    fields=fields,
                    source_refs=[item.ref for item in evidence],
                    actor=actor.audit_actor,
                    draft_id=current.id,
                    expected_revision=expected_revision,
                )
            refs = [f"persona_draft:{draft.id}"]
        elif actor.operation == "persona.draft.publish":
            assert current is not None
            if (
                fields
                or source_refs
                or base_revision != current.base_revision
                or policy_revision != current.policy_revision
            ):
                raise InvalidRequestError("publication must use the reviewed stored draft")
            _, result, refs = self.personas.mutate_for_command(
                tx,
                actor,
                agent_id,
                expected_revision=current.base_revision,
                expected_policy_revision=current.policy_revision,
                fields=json.loads(current.fields_json or "{}"),
                source_refs=json.loads(current.source_refs_json or "[]"),
            )
            published = json.loads(result)
            draft = tx.persona_drafts.mark_published(
                tenant_id=actor.tenant_id,
                agent_id=agent_id,
                draft_id=current.id,
                expected_revision=expected_revision,
                published_revision_id=published["id"],
                actor=actor.audit_actor,
            )
            refs.append(f"persona_draft:{draft.id}")
        else:
            assert current is not None
            if fields or source_refs or base_revision is not None or policy_revision is not None:
                raise InvalidRequestError("discard requires only the actual draft revision")
            discard_draft_in_tx(
                tx,
                tenant_id=actor.tenant_id,
                agent_id=agent_id,
                draft_id=current.id,
                expected_revision=expected_revision,
                actor=actor.audit_actor,
                request_key=request_key,
            )
            draft = tx.persona_drafts.get(actor.tenant_id, agent_id, current.id)
            refs = [f"agent:{agent_id}"]
        if actor.operation != "persona.draft.discard":
            tx.audit(
                tenant_id=actor.tenant_id,
                actor=actor.audit_actor,
                action=actor.operation,
                resource_type="persona_draft",
                resource_id=draft.id,
                reason_code=actor.reason_code,
                revision=draft.revision,
            )
            tx.advance_watermark(
                actor.tenant_id, agent_id, (("persona_draft", draft.id, draft.revision),)
            )
        return (
            actor.operation,
            json.dumps(
                {
                    "id": draft.id,
                    "revision": draft.revision,
                    "status": draft.status,
                    "published_revision_id": draft.published_revision_id,
                }
            ),
            refs,
        )
