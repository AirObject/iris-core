"""Management command boundary, without a bearer credential or Surface lease.

The callbacks are internal domain commands, never client-selected functions.
Domain mutations and this boundary's audit share the idempotency transaction.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from iris_memory_core.application.console.reads import ResourceReader
from iris_memory_core.application.console.resources import ReadRecord, ResourceRef
from iris_memory_core.application.console.security import OperatorSecurity, authorize, denied
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.console import CommandActor, OperatorPrincipal
from iris_memory_core.domain.errors import (
    IdempotencyUnavailableError,
    InvalidRequestError,
    NotFoundError,
)
from iris_memory_core.domain.hashing import request_fingerprint
from iris_memory_core.domain.model import IdempotentResult
from iris_memory_core.domain.scope import Scope

# Each new domain command has to enter this policy explicitly. No generic
# service lookup, method dispatch, arbitrary SQL or online effect operations.
COMMAND_PERMISSIONS = {
    "persona.draft.create": "persona.publish",
    "persona.draft.update": "persona.publish",
    "persona.draft.publish": "persona.publish",
    "persona.draft.discard": "persona.publish",
    "persona.policy.replace": "persona.publish",
    "persona.proposal.create": "memory.write",
    "persona.proposal.approve": "persona.publish",
    "persona.proposal.reject": "persona.publish",
    "persona.state.update": "memory.write",
    "persona.state.clear": "memory.write",
    "persona.publish": "persona.publish",
    "persona.rollback": "persona.publish",
    "cognitive_event.dismiss": "memory.write",
    "entity.forget": "memory.forget",
    "note.forget": "memory.forget",
    "observation.forget": "memory.forget",
    "claim.forget": "memory.forget",
    "episode.forget": "memory.forget",
    "relation.forget": "memory.forget",
    "artifact.forget": "memory.forget",
    "entity.create": "memory.write",
    "entity.attribute": "memory.write",
    "entity.redirect": "memory.write",
    "identity.create": "memory.write",
    "binding.create": "memory.write",
    "binding.confirm": "memory.write",
    "binding.revoke": "memory.write",
    "artifact.create": "memory.write",
    "artifact.upload": "memory.write",
    "relation.create": "memory.write",
    "relation.correct": "memory.write",
    "relation.transition": "memory.write",
    "episode.create": "memory.write",
    "episode.update": "memory.write",
    "episode.transition": "memory.write",
    "claim.create": "memory.write",
    "claim.correct": "memory.write",
    "observation.create": "memory.write",
    "observation.annotate": "memory.write",
    "note.create": "memory.write",
    "note.update": "memory.write",
    "note.transition": "memory.write",
    "focus_item.forget": "memory.forget",
    "focus.create": "memory.write",
    "focus.update": "memory.write",
    "focus.activate": "memory.write",
    "focus.transition": "memory.write",
    "focus.capacity": "memory.write",
    "state_record.forget": "memory.forget",
    "state.create": "memory.write",
    "state.update": "memory.write",
    "state.expire": "memory.write",
    "task.forget": "memory.forget",
    "task.create": "memory.write",
    "task.update": "memory.write",
    "task.transition": "memory.write",
    "task.step.create": "memory.write",
    "task.step.transition": "memory.write",
    "task.dependency.create": "memory.write",
    "task.dependency.remove": "memory.write",
    "task.trigger.create": "memory.write",
    "task.trigger.update": "memory.write",
    "task.trigger.enabled": "memory.write",
}


@dataclass(frozen=True, slots=True)
class CommandTarget:
    resource_type: str
    scope: Scope
    privacy_labels: tuple[str, ...] = ()
    source_refs: tuple[ResourceRef, ...] = ()
    resource_id: str | None = None
    namespace: str | None = None


def authorize_command(
    tx: Transaction,
    principal: OperatorPrincipal,
    *,
    operation: str,
    target: CommandTarget,
    now_us: int,
    reason: str,
) -> CommandActor:
    if (
        operation not in COMMAND_PERMISSIONS
        or reason != "operator_request"
        or target.resource_type
        != {
            "entity": "entity",
            "identity": "external_identity",
            "binding": "binding",
            "artifact": "artifact",
            "relation": "relation",
            "episode": "episode",
            "claim": "claim",
            "observation": "observation",
            "note": "note",
            "focus": "focus_item",
            "focus_item": "focus_item",
            "state": "state_record",
            "state_record": "state_record",
            "task": "task",
            "cognitive_event": "cognitive_event",
            "persona": "agent",
        }.get(operation.partition(".")[0])
        or (
            operation
            in {
                "entity.create",
                "identity.create",
                "binding.create",
                "artifact.create",
                "artifact.upload",
                "relation.create",
                "episode.create",
                "claim.create",
                "observation.create",
                "note.create",
                "focus.create",
                "state.create",
                "task.create",
            }
        )
        != (target.resource_id is None)
    ):
        raise InvalidRequestError("unsupported management command or reason")
    fresh = authorize(
        tx,
        principal,
        now_us,
        COMMAND_PERMISSIONS[operation],
        recent=operation
        in {
            "persona.publish",
            "persona.rollback",
            "persona.proposal.approve",
            "persona.proposal.reject",
            "persona.policy.replace",
            "persona.draft.publish",
            "persona.draft.discard",
        },
    )
    if (
        "console.manage" not in fresh.key.grant.data_purposes
        or "memory.read" not in fresh.permissions
    ):
        raise denied("permission_denied")
    if target.scope.tenant_id != fresh.key.tenant_id:
        raise NotFoundError("command scope not found")
    reader = ResourceReader(tx, fresh, now_us)
    if target.resource_id is not None:
        current = reader.get(ResourceRef(target.resource_type, target.resource_id))
        if current is None or current.scope != target.scope:
            raise NotFoundError("command resource not found")
    else:
        current = ReadRecord(
            id="__new_management_resource__",
            resource_type=target.resource_type,
            scope=target.scope,
            revision=0,
            status="active",
            fields={},
            privacy_labels=target.privacy_labels,
            source_refs=(),
            created_us=now_us,
            updated_us=now_us,
        )
    if target.resource_type == "state_record":
        from iris_memory_core.domain.state import resolve_namespace_policy

        if not target.namespace or (
            target.resource_id is not None and current.fields.get("namespace") != target.namespace
        ):
            raise InvalidRequestError("invalid state command namespace")
        policy = resolve_namespace_policy(
            target.namespace, tx.states.policy(fresh.key.tenant_id, target.namespace)
        )
        if "user" not in policy.allowed_source_authorities and operation != "state_record.forget":
            raise denied("permission_denied")
    if not reader.authority.mutable(tx, current):
        raise denied("permission_denied")
    if operation == "task.forget":
        from iris_memory_core.application.task_deletion import children

        task = tx.tasks.get_task(current.id)
        for kind, identifier in children(tx, task):
            child = reader.get(ResourceRef(kind, identifier))
            if child is None or not reader.authority.mutable(tx, child):
                raise denied("permission_denied")

    # A Task can retain 100 original sources plus 100 completion proofs and an owner.
    # HTTP input arrays remain independently capped at 100.
    if len(target.source_refs) > (201 if target.resource_type == "task" else 100):
        raise InvalidRequestError("too many command source references")
    for ref in target.source_refs:
        if reader.get(ref) is None:
            raise NotFoundError("command source not found")
    return CommandActor(
        tenant_id=fresh.key.tenant_id,
        key_id=fresh.key.id,
        key_revision=fresh.key.revision,
        grant_fingerprint=fresh.key.grant.fingerprint,
        session_id=fresh.session.id,
        session_epoch=fresh.session.epoch,
        scope=target.scope,
        operation=operation,
        reason_code=reason,
    )


def command_access(
    tx: Transaction,
    actor: CommandActor,
    target: CommandTarget,
    *,
    now_us: int,
) -> AccessContext:
    """Recheck an internal actor at the domain seam; never elevate to admin."""
    key, session = tx.console.key(actor.key_id), tx.console.session(actor.session_id)
    if actor.origin != "console" or key is None or session is None:
        raise denied()
    fresh = authorize_command(
        tx,
        OperatorPrincipal(key, session),
        operation=actor.operation,
        target=target,
        now_us=now_us,
        reason=actor.reason_code,
    )
    if fresh != actor:
        raise denied("permission_denied")
    scope = actor.scope
    return AccessContext(
        tenant_id=actor.tenant_id,
        app_instance_id=actor.audit_actor,
        agent_ids=frozenset({scope.agent_id}) if scope.agent_id else frozenset(),
        allowed_space_group_ids=frozenset({scope.space_group_id})
        if scope.space_group_id
        else frozenset(),
        allowed_space_ids=frozenset({scope.space_id}) if scope.space_id else frozenset(),
        consent_subject_entity_ids=key.grant.subject_entity_ids,
        granted_custom_labels=key.grant.custom_privacy_labels,
        data_purposes=frozenset({"console.manage"}),
    )


class ConsoleCommandExecutor:
    def __init__(self, security: OperatorSecurity) -> None:
        self.security = security

    def run(
        self,
        principal: OperatorPrincipal,
        *,
        operation: str,
        target: CommandTarget,
        payload: dict[str, Any],
        idempotency_key: str,
        execute: Callable[[Transaction, CommandActor], tuple[str, str, list[str]]],
        reason: str = "operator_request",
    ) -> IdempotentResult:
        if not idempotency_key:
            raise InvalidRequestError("management command requires an idempotency key")
        runner = self.security.idempotency
        if runner is None:
            raise IdempotencyUnavailableError("management idempotency is unavailable")

        def actor_in(tx: Transaction) -> CommandActor:
            return authorize_command(
                tx,
                principal,
                operation=operation,
                target=target,
                now_us=self.security.clock.now_us(),
                reason=reason,
            )

        with self.security.uow.read() as tx:
            initial = actor_in(tx)

        def check_results(tx: Transaction, refs: tuple[str, ...] | list[str]) -> None:
            if not refs or len(refs) > 100:
                raise InvalidRequestError("management result references are required and bounded")
            fresh = authorize(
                tx,
                principal,
                self.security.clock.now_us(),
                COMMAND_PERMISSIONS[operation],
            )
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            for value in refs:
                kind, separator, identifier = value.partition(":")
                if not separator or reader.get(ResourceRef(kind, identifier)) is None:
                    raise NotFoundError("command result not found")

        def mutate(tx: Transaction) -> tuple[str, str, list[str]]:
            actor = actor_in(tx)
            if (actor.key_revision, actor.grant_fingerprint) != (
                initial.key_revision,
                initial.grant_fingerprint,
            ):
                raise denied("permission_denied")
            code, body, refs = execute(tx, actor)
            # Reject a non-JSON result before committing its business writes.
            json.loads(body)
            check_results(tx, refs)
            tx.audit(
                tenant_id=actor.tenant_id,
                actor=actor.audit_actor,
                action="console.command.committed",
                resource_type="console_command",
                resource_id=operation,
                reason_code=reason,
                details={
                    "origin": actor.origin,
                    "key_revision": actor.key_revision,
                    "grant_fingerprint": actor.grant_fingerprint,
                },
            )
            return code, body, refs

        result = runner.run(
            tenant_id=initial.tenant_id,
            app_instance_id=initial.audit_actor,
            operation="console.command:" + operation,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint(
                operation,
                {
                    "reason": reason,
                    "resource_type": target.resource_type,
                    "resource_id": target.resource_id,
                    **({"namespace": target.namespace} if target.namespace is not None else {}),
                    "privacy_labels": list(target.privacy_labels),
                    "source_refs": [
                        {
                            "resource_type": ref.resource_type,
                            "resource_id": ref.resource_id,
                            "revision": ref.revision,
                        }
                        for ref in target.source_refs
                    ],
                    "scope": {
                        name: getattr(target.scope, name)
                        for name in (
                            "tenant_id",
                            "agent_id",
                            "space_group_id",
                            "space_id",
                            "session_id",
                        )
                    },
                    "payload": payload,
                },
            ),
            execute=mutate,
        )
        # Replays never inherit a historical grant; authorization runs again
        # after reading the completed outcome and before publishing a response.
        with self.security.uow.read() as tx:
            actor_in(tx)
            check_results(tx, result.resource_refs)
        return result
