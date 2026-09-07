"""Complete Persona application service (Phase 9).

All model-originated material enters through structured values and is treated
as untrusted data.  Publication is a short transaction containing the new
immutable revision, pointer CAS, audit, watermark and notification outbox.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, cast

from iris_memory_core.application.outbox import enqueue_with_pressure
from iris_memory_core.application.ports.clock import Clock
from iris_memory_core.application.ports.transaction import (
    IdempotencyRunner,
    Transaction,
    UnitOfWork,
)
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.console import CommandActor, OperatorPrincipal
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    IdempotencyUnavailableError,
    InvalidRequestError,
    InvalidTransitionError,
    NotFoundError,
    PersonaBaseRevisionStaleError,
    PersonaPolicyDeniedError,
    RevisionMismatchError,
    require_reason,
)
from iris_memory_core.domain.hashing import canonical_json, request_fingerprint
from iris_memory_core.domain.jobs import JOB_PAYLOAD_VERSION, NewOutboxJob
from iris_memory_core.domain.model import record_restore, snapshot_json
from iris_memory_core.domain.persona import (
    PersonaFieldDelta,
    PersonaPolicy,
    PersonaPolicyMode,
    PersonaProposal,
    PersonaProposalStatus,
    PersonaRecord,
    PersonaState,
    field_magnitude,
    flatten_patch,
    persona_content_hash,
    validate_content_layer,
    validate_state,
    validate_state_timing,
)
from iris_memory_core.domain.privacy import evaluate_privacy
from iris_memory_core.domain.scope import Scope

PERSONA_READ_CAPABILITY = "persona.read.v1"
PERSONA_STATE_CAPABILITY = "persona.state.write.v1"
PERSONA_MANAGE_CAPABILITY = "persona.manage.v1"
PERSONA_REVIEW_CAPABILITY = "persona.review.v1"
PERSONA_PROPOSAL_TTL_US = 30 * 24 * 60 * 60 * 1_000_000
EVIDENCE_TYPES = frozenset({"claim", "episode", "relation", "task", "persona_state"})
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class PersonaView:
    revision: PersonaRecord
    policy: PersonaPolicy
    state: PersonaState | None


@dataclass(frozen=True, slots=True)
class _Evidence:
    ref: dict[str, object]
    source_key: str
    occurred_us: int


class PersonaMetrics(Protocol):
    def persona_proposal(self, outcome: str) -> None: ...


def _agent_access(access: AccessContext, agent_id: str) -> None:
    if agent_id not in access.agent_ids and not access.admin:
        raise AccessDeniedError("agent is outside the access context")


def _capability(access: AccessContext, capability: str, *, admin: bool = False) -> None:
    if capability not in access.capabilities or (admin and not access.admin):
        raise AccessDeniedError(f"operation requires {capability}")


def normalize_persona_layer(raw: str) -> dict[str, Any]:
    if raw == "":
        return {}
    parsed = json.loads(raw)
    # ADR-0008 bootstrap bytes are frozen as ``[]`` for the three empty
    # layers.  Treat that one legacy empty shape as an empty object without
    # rewriting its stored bytes or hash.
    if parsed == []:
        return {}
    if not isinstance(parsed, dict):
        raise InvalidRequestError("stored Persona content is not an object")
    return parsed


class PersonaService:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        idempotency: IdempotencyRunner | None = None,
        *,
        metrics: PersonaMetrics | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._idempotency = idempotency
        self._metrics = metrics

    def _write(
        self,
        access: AccessContext,
        *,
        operation: str,
        idempotency_key: str | None,
        payload: Mapping[str, object],
        record_type: type[_T],
        execute: Callable[[Transaction], _T],
    ) -> _T:
        if idempotency_key is None:
            with self._uow.write() as tx:
                return execute(tx)
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )

        def op(tx: Transaction) -> tuple[str, str, Sequence[str]]:
            record = execute(tx)
            resource_id = getattr(record, "id", None)
            return "ok", snapshot_json(record), [str(resource_id)] if resource_id else []

        outcome = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation=operation,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint(operation, dict(payload)),
            execute=op,
        )
        return cast(_T, record_restore(record_type, json.loads(outcome.body)))

    def current(self, access: AccessContext, agent_id: str) -> PersonaView:
        _agent_access(access, agent_id)
        _capability(access, PERSONA_READ_CAPABILITY)
        with self._uow.read() as tx:
            revision = tx.personas.current(agent_id)
            if revision.tenant_id != access.tenant_id:
                raise AccessDeniedError("cross-tenant Persona read is denied")
            return PersonaView(
                revision=revision,
                policy=tx.personas.current_policy(agent_id),
                state=tx.personas.current_state(agent_id),
            )

    def history(
        self, access: AccessContext, agent_id: str, *, limit: int = 100
    ) -> tuple[PersonaRecord, ...]:
        _agent_access(access, agent_id)
        _capability(access, PERSONA_READ_CAPABILITY)
        if not 1 <= limit <= 500:
            raise InvalidRequestError("Persona history limit must be within 1..500")
        with self._uow.read() as tx:
            current = tx.personas.current(agent_id)
            if current.tenant_id != access.tenant_id:
                raise AccessDeniedError("cross-tenant Persona read is denied")
            return tx.personas.history(agent_id, limit=limit)

    def replace_policy(
        self,
        access: AccessContext,
        agent_id: str,
        *,
        expected_revision: int,
        config: Mapping[str, object],
        reason: str,
        idempotency_key: str | None = None,
    ) -> PersonaPolicy:
        _agent_access(access, agent_id)
        _capability(access, PERSONA_MANAGE_CAPABILITY, admin=True)
        reason_code = require_reason(reason)
        normalized = _validate_policy_config(config)

        def op(tx: Transaction) -> PersonaPolicy:
            current = tx.personas.current(agent_id)
            if current.tenant_id != access.tenant_id:
                raise AccessDeniedError("cross-tenant Persona management is denied")
            policy = tx.personas.replace_policy(
                tenant_id=access.tenant_id,
                agent_id=agent_id,
                expected_revision=expected_revision,
                config=normalized,
                created_by=access.app_instance_id,
                reason_code=reason_code,
            )
            tx.audit(
                tenant_id=access.tenant_id,
                actor=access.app_instance_id,
                action="persona.policy_replaced",
                resource_type="persona_policy",
                resource_id=policy.id,
                reason_code=reason_code,
                revision=policy.revision,
            )
            tx.advance_watermark(
                access.tenant_id, agent_id, (("persona_policy", policy.id, policy.revision),)
            )
            return policy

        result = self._write(
            access,
            operation="persona.replace_policy",
            idempotency_key=idempotency_key,
            payload={
                "agent_id": agent_id,
                "expected_revision": expected_revision,
                "config": dict(normalized),
                "reason": reason_code,
            },
            record_type=PersonaPolicy,
            execute=op,
        )
        return result

    def replace_policy_for_command(
        self,
        tx: Transaction,
        actor: CommandActor,
        agent_id: str,
        *,
        expected_revision: int,
        config: Mapping[str, object],
    ) -> tuple[str, str, list[str]]:
        from iris_memory_core.application.console.commands import CommandTarget, command_access

        if (
            actor.operation != "persona.policy.replace"
            or type(expected_revision) is not int
            or expected_revision < 1
        ):
            raise InvalidRequestError("invalid managed Policy replacement")
        command_access(
            tx,
            actor,
            CommandTarget("agent", Scope(actor.tenant_id, agent_id), resource_id=agent_id),
            now_us=self._clock.now_us(),
        )
        normalized = _validate_policy_config(config)
        policy = tx.personas.replace_policy(
            tenant_id=actor.tenant_id,
            agent_id=agent_id,
            expected_revision=expected_revision,
            config=normalized,
            created_by=actor.audit_actor,
            reason_code=actor.reason_code,
        )
        tx.audit(
            tenant_id=actor.tenant_id,
            actor=actor.audit_actor,
            action="persona.policy_replaced",
            resource_type="persona_policy",
            resource_id=policy.id,
            reason_code=actor.reason_code,
            revision=policy.revision,
        )
        tx.advance_watermark(
            actor.tenant_id, agent_id, (("persona_policy", policy.id, policy.revision),)
        )
        return (
            "persona.policy_replaced",
            json.dumps(
                {
                    "id": policy.id,
                    "revision": policy.revision,
                    "content_hash": policy.content_hash,
                }
            ),
            [f"agent:{agent_id}"],
        )

    def mutate_for_command(
        self,
        tx: Transaction,
        actor: CommandActor,
        agent_id: str,
        *,
        expected_revision: int,
        expected_policy_revision: int,
        fields: Mapping[str, Any],
        source_refs: Sequence[Mapping[str, object]] = (),
        target_revision: int | None = None,
    ) -> tuple[str, str, list[str]]:
        from iris_memory_core.application.console.commands import CommandTarget, command_access
        from iris_memory_core.application.console.resources import ResourceRef

        if (
            actor.operation not in {"persona.publish", "persona.rollback"}
            or type(expected_revision) is not int
            or expected_revision < 1
            or type(expected_policy_revision) is not int
            or expected_policy_revision < 1
        ):
            raise InvalidRequestError("invalid managed Persona command")
        target = CommandTarget("agent", Scope(actor.tenant_id, agent_id), resource_id=agent_id)
        access = command_access(tx, actor, target, now_us=self._clock.now_us())
        current = tx.personas.current(agent_id)
        policy = tx.personas.current_policy(agent_id)
        if current.revision != expected_revision:
            raise RevisionMismatchError(
                "persona_revision", agent_id, expected_revision, current.revision
            )
        if policy.revision != expected_policy_revision:
            raise RevisionMismatchError(
                "persona_policy", policy.id, expected_policy_revision, policy.revision
            )
        # A stable Agent target keeps replay identity unchanged. Reading the
        # current content and rollback target still requires their provenance.
        command_access(
            tx,
            actor,
            CommandTarget(
                "agent",
                target.scope,
                resource_id=agent_id,
                source_refs=(ResourceRef("persona_revision", current.id),),
            ),
            now_us=self._clock.now_us(),
        )
        if actor.operation == "persona.publish":
            if (
                set(fields) != {"core", "traits", "narrative"}
                or target_revision is not None
                or any(not isinstance(value, Mapping) for value in fields.values())
            ):
                raise InvalidRequestError("invalid Persona publication fields")
            layers = {name: validate_content_layer(name, fields[name]) for name in fields}
            refs = self._validate_evidence(
                tx, access, agent_id, source_refs, required=False, command_actor=actor
            )
            result = self._publish(
                tx,
                access,
                current=current,
                expected_revision=expected_revision,
                core=layers["core"],
                traits=layers["traits"],
                narrative=layers["narrative"],
                policy=policy,
                refs=refs,
                reason_code=actor.reason_code,
                source="console",
            )
        else:
            if fields or source_refs or type(target_revision) is not int or target_revision < 1:
                raise InvalidRequestError("invalid Persona rollback target")
            previous = tx.personas.by_revision(agent_id, target_revision)
            if previous.status.value not in {"published", "superseded"}:
                raise PersonaPolicyDeniedError("revoked Persona is not a rollback target")
            command_access(
                tx,
                actor,
                CommandTarget(
                    "agent",
                    target.scope,
                    resource_id=agent_id,
                    source_refs=(ResourceRef("persona_revision", previous.id),),
                ),
                now_us=self._clock.now_us(),
            )
            previous_refs = json.loads(previous.source_refs)
            if not isinstance(previous_refs, list) or any(
                not isinstance(ref, dict) for ref in previous_refs
            ):
                raise PersonaPolicyDeniedError("stored Persona Evidence is invalid")
            refs = self._validate_evidence(
                tx, access, agent_id, previous_refs, required=False, command_actor=actor
            )
            result = self._publish_encoded(
                tx,
                access,
                current=current,
                expected_revision=expected_revision,
                core_json=previous.core,
                traits_json=previous.traits,
                narrative_json=previous.narrative,
                digest=previous.content_hash,
                policy=policy,
                source_refs_json=canonical_json([ref.ref for ref in refs]),
                reason_code=actor.reason_code,
                source=f"console.rollback:{previous.revision}",
            )
            self._notification(
                tx,
                kind="persona.revision_invalidated",
                record=result,
                invalidated_revision=current.revision,
                reason_code=actor.reason_code,
            )
        return (
            "persona.published",
            json.dumps(
                {"id": result.id, "revision": result.revision, "content_hash": result.content_hash}
            ),
            [f"persona_revision:{result.id}"],
        )

    def publish_revision(
        self,
        access: AccessContext,
        agent_id: str,
        *,
        expected_revision: int,
        core: Mapping[str, Any],
        traits: Mapping[str, Any],
        narrative: Mapping[str, Any],
        source_refs: Sequence[Mapping[str, object]] = (),
        reason: str,
        idempotency_key: str | None = None,
    ) -> PersonaRecord:
        _agent_access(access, agent_id)
        _capability(access, PERSONA_MANAGE_CAPABILITY, admin=True)
        reason_code = require_reason(reason)
        core_value = validate_content_layer("core", core)
        trait_value = validate_content_layer("traits", traits)
        narrative_value = validate_content_layer("narrative", narrative)

        def op(tx: Transaction) -> PersonaRecord:
            current = tx.personas.current(agent_id)
            if current.tenant_id != access.tenant_id:
                raise AccessDeniedError("cross-tenant Persona publication is denied")
            refs = self._validate_evidence(tx, access, agent_id, source_refs, required=False)
            policy = tx.personas.current_policy(agent_id)
            return self._publish(
                tx,
                access,
                current=current,
                expected_revision=expected_revision,
                core=core_value,
                traits=trait_value,
                narrative=narrative_value,
                policy=policy,
                refs=refs,
                reason_code=reason_code,
                source="admin",
            )

        result = self._write(
            access,
            operation="persona.publish_revision",
            idempotency_key=idempotency_key,
            payload={
                "agent_id": agent_id,
                "expected_revision": expected_revision,
                "core": core_value,
                "traits": trait_value,
                "narrative": narrative_value,
                "source_refs": [dict(item) for item in source_refs],
                "reason": reason_code,
            },
            record_type=PersonaRecord,
            execute=op,
        )
        return result

    def rollback(
        self,
        access: AccessContext,
        agent_id: str,
        *,
        target_revision: int,
        expected_revision: int,
        reason: str,
        idempotency_key: str | None = None,
    ) -> PersonaRecord:
        _agent_access(access, agent_id)
        _capability(access, PERSONA_MANAGE_CAPABILITY, admin=True)
        reason_code = require_reason(reason)

        def op(tx: Transaction) -> PersonaRecord:
            current = tx.personas.current(agent_id)
            target = tx.personas.by_revision(agent_id, target_revision)
            if current.tenant_id != access.tenant_id or target.tenant_id != access.tenant_id:
                raise AccessDeniedError("cross-tenant Persona rollback is denied")
            policy = tx.personas.current_policy(agent_id)
            # A rollback is a new revision whose content bytes and hash are an
            # exact copy of the selected immutable revision.  This preserves
            # the frozen Phase 1 bootstrap encoding as well as its semantics.
            record = self._publish_encoded(
                tx,
                access,
                current=current,
                expected_revision=expected_revision,
                core_json=target.core,
                traits_json=target.traits,
                narrative_json=target.narrative,
                digest=target.content_hash,
                policy=policy,
                source_refs_json="[]",
                reason_code=reason_code,
                source=f"rollback:{target.revision}",
            )
            self._notification(
                tx,
                kind="persona.revision_invalidated",
                record=record,
                invalidated_revision=current.revision,
                reason_code=reason_code,
            )
            return record

        return self._write(
            access,
            operation="persona.rollback",
            idempotency_key=idempotency_key,
            payload={
                "agent_id": agent_id,
                "target_revision": target_revision,
                "expected_revision": expected_revision,
                "reason": reason_code,
            },
            record_type=PersonaRecord,
            execute=op,
        )

    def mutate_state_for_command(
        self,
        tx: Transaction,
        actor: CommandActor,
        agent_id: str,
        *,
        expected_revision: int,
        fields: Mapping[str, Any],
        source_refs: Sequence[Mapping[str, object]] = (),
    ) -> tuple[str, str, list[str]]:
        from iris_memory_core.application.console.commands import CommandTarget, command_access
        from iris_memory_core.application.console.resources import ResourceRef

        if actor.operation not in {"persona.state.update", "persona.state.clear"} or (
            type(expected_revision) is not int or expected_revision < 0
        ):
            raise InvalidRequestError("invalid managed Persona State command")
        scope = Scope(actor.tenant_id, agent_id)
        access = command_access(
            tx,
            actor,
            CommandTarget("agent", scope, resource_id=agent_id),
            now_us=self._clock.now_us(),
        )
        current = tx.personas.current_state(agent_id)
        actual_revision = current.revision if current is not None else 0
        if actual_revision != expected_revision:
            raise RevisionMismatchError(
                "persona_state", agent_id, expected_revision, actual_revision
            )
        if current is not None:
            command_access(
                tx,
                actor,
                CommandTarget(
                    "agent",
                    scope,
                    resource_id=agent_id,
                    source_refs=(ResourceRef("persona_state", current.id),),
                ),
                now_us=self._clock.now_us(),
            )
        now = self._clock.now_us()
        if actor.operation == "persona.state.update":
            if set(fields) - {"state", "baseline", "ttl_us"} or not {"state", "ttl_us"} <= set(
                fields
            ):
                raise InvalidRequestError("invalid Persona State fields")
            if not isinstance(fields["state"], Mapping) or not isinstance(
                fields.get("baseline", {}), Mapping
            ):
                raise InvalidRequestError("Persona State layers must be objects")
            ttl_us = fields["ttl_us"]
            if type(ttl_us) is not int:
                raise InvalidRequestError("Persona State TTL must be integer microseconds")
            started, expires = now, now + ttl_us
            validate_state_timing(started_us=started, expires_us=expires)
            state = validate_state(fields["state"])
            baseline = validate_state(fields.get("baseline", {}))
            refs = self._validate_evidence(
                tx, access, agent_id, source_refs, required=False, command_actor=actor
            )
            refs_json = canonical_json([item.ref for item in refs])
            action = "persona.state_updated"
        else:
            if fields or source_refs:
                raise InvalidRequestError("Persona State clear accepts no content")
            if current is None:
                raise NotFoundError("Persona State not found")
            if current.expires_us > now:
                raise InvalidTransitionError("only an expired Persona State can be cleared")
            baseline = validate_state(normalize_persona_layer(current.baseline_json))
            state = baseline
            started, expires = current.expires_us, current.expires_us + 1
            refs_json = "[]"
            action = "persona.state_cleared"
        result = tx.personas.put_state(
            tenant_id=actor.tenant_id,
            agent_id=agent_id,
            expected_revision=expected_revision,
            state_json=canonical_json(state),
            baseline_json=canonical_json(baseline),
            source_refs_json=refs_json,
            started_us=started,
            expires_us=expires,
            created_by=actor.audit_actor,
        )
        tx.audit(
            tenant_id=actor.tenant_id,
            actor=actor.audit_actor,
            action=action,
            resource_type="persona_state",
            resource_id=result.id,
            reason_code=actor.reason_code,
            revision=result.revision,
        )
        tx.advance_watermark(
            actor.tenant_id, agent_id, (("persona_state", result.id, result.revision),)
        )
        if actor.operation == "persona.state.update":
            self._state_expiry_job(tx, result)
        return (
            action,
            json.dumps({"id": result.id, "revision": result.revision}),
            [f"persona_state:{result.id}"],
        )

    def update_state(
        self,
        access: AccessContext,
        agent_id: str,
        *,
        expected_revision: int,
        state: Mapping[str, Any],
        baseline: Mapping[str, Any] | None,
        ttl_us: int,
        source_refs: Sequence[Mapping[str, object]] = (),
        idempotency_key: str | None = None,
    ) -> PersonaState:
        _agent_access(access, agent_id)
        _capability(access, PERSONA_STATE_CAPABILITY)
        state_value = validate_state(state)
        baseline_value = validate_state(baseline or {})
        started_us = self._clock.now_us()
        expires_us = started_us + ttl_us
        validate_state_timing(started_us=started_us, expires_us=expires_us)

        def op(tx: Transaction) -> PersonaState:
            persona = tx.personas.current(agent_id)
            if persona.tenant_id != access.tenant_id:
                raise AccessDeniedError("cross-tenant Persona state write is denied")
            refs = self._validate_evidence(tx, access, agent_id, source_refs, required=False)
            result = tx.personas.put_state(
                tenant_id=access.tenant_id,
                agent_id=agent_id,
                expected_revision=expected_revision,
                state_json=canonical_json(state_value),
                baseline_json=canonical_json(baseline_value),
                source_refs_json=canonical_json([item.ref for item in refs]),
                started_us=started_us,
                expires_us=expires_us,
                created_by=access.app_instance_id,
            )
            tx.audit(
                tenant_id=access.tenant_id,
                actor=access.app_instance_id,
                action="persona.state_updated",
                resource_type="persona_state",
                resource_id=result.id,
                reason_code="state_update",
                revision=result.revision,
            )
            tx.advance_watermark(
                access.tenant_id, agent_id, (("persona_state", result.id, result.revision),)
            )
            self._state_expiry_job(tx, result)
            return result

        return self._write(
            access,
            operation="persona.update_state",
            idempotency_key=idempotency_key,
            payload={
                "agent_id": agent_id,
                "expected_revision": expected_revision,
                "state": state_value,
                "baseline": baseline_value,
                "ttl_us": ttl_us,
                "source_refs": [dict(item) for item in source_refs],
            },
            record_type=PersonaState,
            execute=op,
        )

    def expire_due_states(self, *, limit: int = 100) -> int:
        """Deterministic restart catch-up: expired active state becomes baseline."""
        now_us = self._clock.now_us()
        changed = 0
        with self._uow.write() as tx:
            for state in tx.personas.due_states(now_us=now_us, limit=limit):
                baseline = validate_state(normalize_persona_layer(state.baseline_json))
                result = tx.personas.put_state(
                    tenant_id=state.tenant_id,
                    agent_id=state.agent_id,
                    expected_revision=state.revision,
                    state_json=canonical_json(baseline),
                    baseline_json=canonical_json(baseline),
                    source_refs_json="[]",
                    started_us=state.expires_us,
                    expires_us=state.expires_us + 1,
                    created_by="persona-state-decay",
                )
                tx.audit(
                    tenant_id=state.tenant_id,
                    actor="persona-state-decay",
                    action="persona.state_expired",
                    resource_type="persona_state",
                    resource_id=result.id,
                    reason_code="ttl_expired",
                    revision=result.revision,
                )
                tx.advance_watermark(
                    state.tenant_id,
                    state.agent_id,
                    (("persona_state", result.id, result.revision),),
                )
                changed += 1
        return changed

    def mutate_proposal_for_command(
        self,
        tx: Transaction,
        actor: CommandActor,
        agent_id: str,
        *,
        base_revision: int,
        expected_policy_revision: int | None,
        fields: Mapping[str, Any],
        evidence_refs: Sequence[Mapping[str, object]] = (),
        proposal_id: str | None = None,
    ) -> tuple[str, str, list[str]]:
        from iris_memory_core.application.console.commands import CommandTarget, command_access
        from iris_memory_core.application.console.resources import ResourceRef

        operation = actor.operation
        if (
            operation
            not in {
                "persona.proposal.create",
                "persona.proposal.approve",
                "persona.proposal.reject",
            }
            or type(base_revision) is not int
            or base_revision < 1
        ):
            raise InvalidRequestError("invalid managed Proposal command")
        scope = Scope(actor.tenant_id, agent_id)
        access = command_access(
            tx,
            actor,
            CommandTarget("agent", scope, resource_id=agent_id),
            now_us=self._clock.now_us(),
        )
        current = tx.personas.current(agent_id)
        if operation != "persona.proposal.reject":
            if type(expected_policy_revision) is not int or expected_policy_revision < 1:
                raise InvalidRequestError("reviewed Policy revision is required")
            policy = tx.personas.current_policy(agent_id)
            if policy.revision != expected_policy_revision:
                raise RevisionMismatchError(
                    "persona_policy", policy.id, expected_policy_revision, policy.revision
                )
            if current.revision != base_revision:
                raise PersonaBaseRevisionStaleError("proposal base revision is no longer current")
            command_access(
                tx,
                actor,
                CommandTarget(
                    "agent",
                    scope,
                    resource_id=agent_id,
                    source_refs=(ResourceRef("persona_revision", current.id),),
                ),
                now_us=self._clock.now_us(),
            )
        elif expected_policy_revision is not None:
            raise InvalidRequestError("Proposal rejection accepts no Policy revision")

        if operation == "persona.proposal.create":
            if proposal_id is not None or set(fields) != {"patch", "confidence", "ttl_us"}:
                raise InvalidRequestError("invalid Proposal creation fields")
            patch, confidence, ttl = fields["patch"], fields["confidence"], fields["ttl_us"]
            if (
                not isinstance(patch, Mapping)
                or isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not 0.0 <= confidence <= 1.0
                or type(ttl) is not int
                or not 1 <= ttl <= PERSONA_PROPOSAL_TTL_US
            ):
                raise InvalidRequestError("invalid Proposal patch, confidence or TTL")
            proposal = self._execute_create_proposal(
                tx,
                access,
                agent_id,
                base_revision=base_revision,
                patch=patch,
                evidence_refs=evidence_refs,
                confidence=float(confidence),
                generator=actor.audit_actor,
                generator_version="console-v1",
                expires_us=self._clock.now_us() + ttl,
                command_actor=actor,
            )
            action = "persona.proposal_created"
        else:
            if not isinstance(proposal_id, str) or not proposal_id or fields or evidence_refs:
                raise InvalidRequestError("invalid Proposal review fields")
            proposal = tx.personas.proposal(proposal_id)
            self._proposal_scope(access, agent_id, proposal)
            command_access(
                tx,
                actor,
                CommandTarget(
                    "agent",
                    scope,
                    resource_id=agent_id,
                    source_refs=(ResourceRef("persona_proposal", proposal.id),),
                ),
                now_us=self._clock.now_us(),
            )
            if proposal.base_revision != base_revision:
                raise RevisionMismatchError(
                    "persona_proposal", proposal.id, base_revision, proposal.base_revision
                )
            if operation == "persona.proposal.approve":
                if proposal.expires_us <= self._clock.now_us():
                    raise InvalidTransitionError("Persona proposal has expired")
                proposal = tx.personas.transition_proposal(
                    proposal.id,
                    expected_status=PersonaProposalStatus.PROPOSED,
                    target=PersonaProposalStatus.APPROVED,
                    actor=actor.audit_actor,
                    reason_code=actor.reason_code,
                )
                proposal = self._publish_proposal(
                    tx, access, proposal, actor.reason_code, command_actor=actor
                )
                action = "persona.proposal_published"
            else:
                proposal = tx.personas.transition_proposal(
                    proposal.id,
                    expected_status=PersonaProposalStatus.PROPOSED,
                    target=PersonaProposalStatus.REJECTED,
                    actor=actor.audit_actor,
                    reason_code=actor.reason_code,
                )
                action = "persona.proposal_rejected"
            tx.audit(
                tenant_id=actor.tenant_id,
                actor=actor.audit_actor,
                action=action,
                resource_type="persona_proposal",
                resource_id=proposal.id,
                reason_code=actor.reason_code,
            )
        # Proposal status has no numeric revision. Advance the Agent sequence;
        # publication separately records the actual new Persona revision.
        tx.advance_watermark(actor.tenant_id, agent_id, ())
        refs = [f"persona_proposal:{proposal.id}"]
        if proposal.published_revision_id:
            refs.append(f"persona_revision:{proposal.published_revision_id}")
        return (
            action,
            json.dumps(
                {
                    "id": proposal.id,
                    "status": proposal.status.value,
                    "published_revision_id": proposal.published_revision_id,
                }
            ),
            refs,
        )

    def create_proposal(
        self,
        access: AccessContext,
        agent_id: str,
        *,
        base_revision: int,
        patch: Mapping[str, Any],
        evidence_refs: Sequence[Mapping[str, object]],
        confidence: float,
        generator: str,
        generator_version: str,
        expires_us: int | None = None,
        idempotency_key: str | None = None,
    ) -> PersonaProposal:
        _agent_access(access, agent_id)
        _capability(access, PERSONA_REVIEW_CAPABILITY)
        if not 0.0 <= confidence <= 1.0:
            raise InvalidRequestError("Persona proposal confidence must be within [0, 1]")
        if not generator or not generator_version:
            raise InvalidRequestError("Persona proposal generator identity is required")

        def op(tx: Transaction) -> PersonaProposal:
            return self._execute_create_proposal(
                tx,
                access,
                agent_id,
                base_revision=base_revision,
                patch=patch,
                evidence_refs=evidence_refs,
                confidence=confidence,
                generator=generator,
                generator_version=generator_version,
                expires_us=expires_us,
            )

        result = self._write(
            access,
            operation="persona.create_proposal",
            idempotency_key=idempotency_key,
            payload={
                "agent_id": agent_id,
                "base_revision": base_revision,
                "patch": dict(patch),
                "evidence_refs": [dict(item) for item in evidence_refs],
                "confidence": confidence,
                "generator": generator,
                "generator_version": generator_version,
                "expires_us": expires_us,
            },
            record_type=PersonaProposal,
            execute=op,
        )
        if self._metrics is not None:
            self._metrics.persona_proposal(result.status.value)
        return result

    def _execute_create_proposal(
        self,
        tx: Transaction,
        access: AccessContext,
        agent_id: str,
        *,
        base_revision: int,
        patch: Mapping[str, Any],
        evidence_refs: Sequence[Mapping[str, object]],
        confidence: float,
        generator: str,
        generator_version: str,
        expires_us: int | None = None,
        command_actor: CommandActor | None = None,
    ) -> PersonaProposal:
        """Phase 9 policy boundary for transaction-local callers.

        Background reflection calls this inside the fenced Outbox commit;
        policy evaluation and optional bounded-auto publication therefore
        remain identical to the public application path.
        """
        if command_actor is None:
            _agent_access(access, agent_id)
            _capability(access, PERSONA_REVIEW_CAPABILITY)
        else:
            from iris_memory_core.application.console.commands import CommandTarget, command_access

            if command_actor.operation != "persona.proposal.create":
                raise InvalidRequestError("invalid proposal creation actor")
            access = command_access(
                tx,
                command_actor,
                CommandTarget(
                    "agent", Scope(command_actor.tenant_id, agent_id), resource_id=agent_id
                ),
                now_us=self._clock.now_us(),
            )
        if not 0.0 <= confidence <= 1.0:
            raise InvalidRequestError("Persona proposal confidence must be within [0, 1]")
        if not generator or not generator_version:
            raise InvalidRequestError("Persona proposal generator identity is required")
        flattened = flatten_patch(patch)
        current = tx.personas.current(agent_id)
        if current.tenant_id != access.tenant_id:
            raise AccessDeniedError("cross-tenant Persona proposal is denied")
        if current.revision != base_revision:
            raise PersonaBaseRevisionStaleError(
                "proposal base revision is no longer current",
                details={
                    "base_revision": base_revision,
                    "current_revision": current.revision,
                },
            )
        policy = tx.personas.current_policy(agent_id)
        refs = self._validate_evidence(
            tx, access, agent_id, evidence_refs, required=True, command_actor=command_actor
        )
        deltas = _field_deltas(current, flattened, refs)
        now_us = self._clock.now_us()
        previous_delta = tx.personas.published_delta_total(
            agent_id, since_us=now_us - policy.cumulative_window_us
        )
        last_publication_us = tx.personas.last_proposal_publication_us(agent_id)
        evaluation = _evaluate(
            policy,
            deltas,
            refs,
            confidence,
            now_us=now_us,
            previous_delta=previous_delta,
            last_publication_us=last_publication_us,
        )
        if command_actor is not None:
            evaluation = {**evaluation, "decision": "manual_review", "requires_review": True}
        proposal = tx.personas.insert_proposal(
            tenant_id=access.tenant_id,
            agent_id=agent_id,
            base_revision=base_revision,
            target_fields=tuple(flattened),
            patch_json=canonical_json(dict(patch)),
            field_deltas_json=canonical_json([_delta_json(item) for item in deltas]),
            evidence_refs_json=canonical_json([item.ref for item in refs]),
            confidence=confidence,
            generator=generator,
            generator_version=generator_version,
            policy_evaluation_json=canonical_json(evaluation),
            expires_us=expires_us or self._clock.now_us() + PERSONA_PROPOSAL_TTL_US,
            actor=access.app_instance_id,
        )
        tx.audit(
            tenant_id=access.tenant_id,
            actor=access.app_instance_id,
            action="persona.proposal_created",
            resource_type="persona_proposal",
            resource_id=proposal.id,
            reason_code=command_actor.reason_code if command_actor else "policy_accepted",
        )
        if (
            command_actor is None
            and policy.mode is PersonaPolicyMode.BOUNDED_AUTO
            and not evaluation["requires_review"]
        ):
            proposal = self._publish_proposal(tx, access, proposal, "bounded_auto")
        return proposal

    def approve(
        self,
        access: AccessContext,
        agent_id: str,
        proposal_id: str,
        *,
        reason: str,
        idempotency_key: str | None = None,
    ) -> PersonaProposal:
        _agent_access(access, agent_id)
        _capability(access, PERSONA_REVIEW_CAPABILITY, admin=True)
        reason_code = require_reason(reason)

        def op(tx: Transaction) -> PersonaProposal:
            proposal = tx.personas.proposal(proposal_id)
            self._proposal_scope(access, agent_id, proposal)
            if proposal.expires_us <= self._clock.now_us():
                tx.personas.transition_proposal(
                    proposal_id,
                    expected_status=PersonaProposalStatus.PROPOSED,
                    target=PersonaProposalStatus.EXPIRED,
                    actor=access.app_instance_id,
                    reason_code="expired",
                )
                raise InvalidTransitionError("Persona proposal has expired")
            current = tx.personas.current(agent_id)
            if current.revision != proposal.base_revision:
                raise PersonaBaseRevisionStaleError(
                    "proposal base revision is no longer current",
                    details={
                        "base_revision": proposal.base_revision,
                        "current_revision": current.revision,
                    },
                )
            tx.personas.transition_proposal(
                proposal_id,
                expected_status=PersonaProposalStatus.PROPOSED,
                target=PersonaProposalStatus.APPROVED,
                actor=access.app_instance_id,
                reason_code=reason_code,
            )
            return self._publish_proposal(
                tx, access, tx.personas.proposal(proposal_id), reason_code
            )

        result = self._write(
            access,
            operation="persona.approve_proposal",
            idempotency_key=idempotency_key,
            payload={"agent_id": agent_id, "proposal_id": proposal_id, "reason": reason_code},
            record_type=PersonaProposal,
            execute=op,
        )
        if self._metrics is not None:
            self._metrics.persona_proposal(result.status.value)
        return result

    def reject(
        self,
        access: AccessContext,
        agent_id: str,
        proposal_id: str,
        *,
        reason: str,
        idempotency_key: str | None = None,
    ) -> PersonaProposal:
        _agent_access(access, agent_id)
        _capability(access, PERSONA_REVIEW_CAPABILITY, admin=True)
        reason_code = require_reason(reason)

        def op(tx: Transaction) -> PersonaProposal:
            proposal = tx.personas.proposal(proposal_id)
            self._proposal_scope(access, agent_id, proposal)
            rejected = tx.personas.transition_proposal(
                proposal_id,
                expected_status=PersonaProposalStatus.PROPOSED,
                target=PersonaProposalStatus.REJECTED,
                actor=access.app_instance_id,
                reason_code=reason_code,
            )
            tx.audit(
                tenant_id=access.tenant_id,
                actor=access.app_instance_id,
                action="persona.proposal_rejected",
                resource_type="persona_proposal",
                resource_id=proposal_id,
                reason_code=reason_code,
            )
            return rejected

        result = self._write(
            access,
            operation="persona.reject_proposal",
            idempotency_key=idempotency_key,
            payload={"agent_id": agent_id, "proposal_id": proposal_id, "reason": reason_code},
            record_type=PersonaProposal,
            execute=op,
        )
        if self._metrics is not None:
            self._metrics.persona_proposal(result.status.value)
        return result

    def _publish_proposal(
        self,
        tx: Transaction,
        access: AccessContext,
        proposal: PersonaProposal,
        reason_code: str,
        *,
        command_actor: CommandActor | None = None,
    ) -> PersonaProposal:
        current = tx.personas.current(proposal.agent_id)
        if current.revision != proposal.base_revision:
            raise PersonaBaseRevisionStaleError("proposal base revision is no longer current")
        policy = tx.personas.current_policy(proposal.agent_id)
        if policy.mode is PersonaPolicyMode.LOCKED:
            raise PersonaPolicyDeniedError("locked policy forbids Proposal publication")
        patch = normalize_persona_layer(proposal.patch_json)
        flattened = flatten_patch(patch)
        raw_refs = json.loads(proposal.evidence_refs_json)
        if not isinstance(raw_refs, list) or any(not isinstance(item, dict) for item in raw_refs):
            raise PersonaPolicyDeniedError("stored Persona Evidence is invalid")
        refs = self._validate_evidence(
            tx,
            access,
            proposal.agent_id,
            cast(list[dict[str, object]], raw_refs),
            required=True,
            command_actor=command_actor,
        )
        deltas = _field_deltas(current, flattened, refs)
        now_us = self._clock.now_us()
        _evaluate(
            policy,
            deltas,
            refs,
            proposal.confidence,
            now_us=now_us,
            previous_delta=tx.personas.published_delta_total(
                proposal.agent_id, since_us=now_us - policy.cumulative_window_us
            ),
            last_publication_us=tx.personas.last_proposal_publication_us(proposal.agent_id),
        )
        traits = normalize_persona_layer(current.traits)
        narrative = normalize_persona_layer(current.narrative)
        if isinstance(patch.get("traits"), dict):
            traits.update(patch["traits"])
        if isinstance(patch.get("narrative"), dict):
            narrative.update(patch["narrative"])
        record = self._publish(
            tx,
            access,
            current=current,
            expected_revision=proposal.base_revision,
            core=normalize_persona_layer(current.core),
            traits=validate_content_layer("traits", traits),
            narrative=validate_content_layer("narrative", narrative),
            policy=policy,
            refs=refs,
            reason_code=reason_code,
            source=f"proposal:{proposal.id}",
        )
        expected_status = (
            PersonaProposalStatus.APPROVED
            if proposal.status is PersonaProposalStatus.APPROVED
            else PersonaProposalStatus.PROPOSED
        )
        published = tx.personas.transition_proposal(
            proposal.id,
            expected_status=expected_status,
            target=PersonaProposalStatus.PUBLISHED,
            actor=access.app_instance_id,
            reason_code=reason_code,
            published_revision_id=record.id,
        )
        return published

    def _publish(
        self,
        tx: Transaction,
        access: AccessContext,
        *,
        current: PersonaRecord,
        expected_revision: int,
        core: Mapping[str, Any],
        traits: Mapping[str, Any],
        narrative: Mapping[str, Any],
        policy: PersonaPolicy,
        refs: Sequence[_Evidence],
        reason_code: str,
        source: str,
    ) -> PersonaRecord:
        digest = persona_content_hash(core, traits, narrative)
        return self._publish_encoded(
            tx,
            access,
            current=current,
            expected_revision=expected_revision,
            core_json=canonical_json(dict(core)),
            traits_json=canonical_json(dict(traits)),
            narrative_json=canonical_json(dict(narrative)),
            digest=digest,
            policy=policy,
            source_refs_json=canonical_json([item.ref for item in refs]),
            reason_code=reason_code,
            source=source,
        )

    def _publish_encoded(
        self,
        tx: Transaction,
        access: AccessContext,
        *,
        current: PersonaRecord,
        expected_revision: int,
        core_json: str,
        traits_json: str,
        narrative_json: str,
        digest: str,
        policy: PersonaPolicy,
        source_refs_json: str,
        reason_code: str,
        source: str,
    ) -> PersonaRecord:
        record = tx.personas.publish(
            tenant_id=access.tenant_id,
            agent_id=current.agent_id,
            expected_revision=expected_revision,
            core_json=core_json,
            traits_json=traits_json,
            narrative_json=narrative_json,
            digest=digest,
            policy_id=policy.id,
            source_refs_json=source_refs_json,
            change_reason=reason_code,
            created_by=access.app_instance_id,
            source=source,
        )
        tx.audit(
            tenant_id=access.tenant_id,
            actor=access.app_instance_id,
            action="persona.revision_published",
            resource_type="persona_revision",
            resource_id=record.id,
            reason_code=reason_code,
            details={"previous_revision": current.revision, "source": source},
            revision=record.revision,
        )
        tx.advance_watermark(
            access.tenant_id,
            current.agent_id,
            (("persona_revision", record.id, record.revision),),
        )
        self._notification(tx, kind="persona.revised", record=record, reason_code=reason_code)
        return record

    def _notification(
        self,
        tx: Transaction,
        *,
        kind: str,
        record: PersonaRecord,
        reason_code: str,
        invalidated_revision: int | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "version": JOB_PAYLOAD_VERSION,
            "event_schema": f"{kind}.v1",
            "agent_id": record.agent_id,
            "persona_revision": record.revision,
            "persona_content_hash": record.content_hash,
            "reason_code": reason_code,
        }
        if invalidated_revision is not None:
            payload["invalidated_revision"] = invalidated_revision
        enqueue_with_pressure(
            tx,
            NewOutboxJob(
                tenant_id=record.tenant_id,
                agent_id=record.agent_id,
                job_kind=kind,
                aggregate_type="persona_revision",
                aggregate_id=record.id,
                source_revision=record.revision,
                payload=payload,
                dedupe_key=f"{kind}:{record.agent_id}:{record.revision}",
                priority=2,
            ),
            None,
        )

    def _state_expiry_job(self, tx: Transaction, state: PersonaState) -> None:
        enqueue_with_pressure(
            tx,
            NewOutboxJob(
                tenant_id=state.tenant_id,
                agent_id=state.agent_id,
                job_kind="persona.state_expire",
                aggregate_type="persona_state",
                aggregate_id=state.id,
                source_revision=state.revision,
                payload={
                    "version": JOB_PAYLOAD_VERSION,
                    "agent_id": state.agent_id,
                    "state_id": state.id,
                    "state_revision": state.revision,
                    "expires_us": state.expires_us,
                },
                dedupe_key=f"persona.state_expire:{state.id}:{state.revision}",
                available_at_us=state.expires_us,
                priority=4,
            ),
            None,
        )

    def _validate_evidence(
        self,
        tx: Transaction,
        access: AccessContext,
        agent_id: str,
        refs: Sequence[Mapping[str, object]],
        *,
        required: bool,
        command_actor: CommandActor | None = None,
    ) -> tuple[_Evidence, ...]:
        if required and not refs:
            raise PersonaPolicyDeniedError("Persona proposal requires Evidence")
        validated: list[_Evidence] = []
        command_reader = None
        if command_actor is not None:
            from iris_memory_core.application.console.commands import CommandTarget, command_access
            from iris_memory_core.application.console.reads import ResourceReader
            from iris_memory_core.application.console.resources import ResourceRef

            command_access(
                tx,
                command_actor,
                CommandTarget("agent", Scope(access.tenant_id, agent_id), resource_id=agent_id),
                now_us=self._clock.now_us(),
            )
            operator_key = tx.console.key(command_actor.key_id)
            session = tx.console.session(command_actor.session_id)
            assert operator_key is not None and session is not None
            command_reader = ResourceReader(
                tx, OperatorPrincipal(operator_key, session), self._clock.now_us()
            )
        if command_actor is not None and len(refs) > 100:
            raise InvalidRequestError("too many Persona Evidence references")
        seen: set[tuple[str, str, int | None]] = set()
        for raw in refs:
            resource_type = raw.get("resource_type")
            resource_id = raw.get("resource_id")
            revision = raw.get("revision")
            if (
                resource_type not in EVIDENCE_TYPES
                or not isinstance(resource_id, str)
                or not resource_id
            ):
                raise PersonaPolicyDeniedError("unknown or invalid Persona Evidence reference")
            if revision is not None and (
                isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
            ):
                raise PersonaPolicyDeniedError("Persona Evidence revision is invalid")
            key = (str(resource_type), resource_id, revision if isinstance(revision, int) else None)
            if key in seen:
                continue
            seen.add(key)
            if tx.is_tombstoned(access.tenant_id, str(resource_type), resource_id):
                raise PersonaPolicyDeniedError("deleted Persona Evidence is not admissible")
            if (
                command_reader is not None
                and command_reader.get(ResourceRef(str(resource_type), resource_id, revision))
                is None
            ):
                raise PersonaPolicyDeniedError("Persona Evidence is not visible to this operator")
            record: object
            revision_row: object | None
            try:
                if resource_type == "claim":
                    record = tx.claims.get(resource_id)
                elif resource_type == "episode":
                    record = tx.episodes.get(resource_id)
                elif resource_type == "relation":
                    record = tx.relations.get(resource_id)
                elif resource_type == "task":
                    record = tx.tasks.get_task(resource_id)
                else:
                    state = tx.personas.current_state(agent_id)
                    if state is None or state.id != resource_id:
                        raise PersonaPolicyDeniedError("Persona State Evidence is not current")
                    if command_actor is not None and state.expires_us <= self._clock.now_us():
                        raise PersonaPolicyDeniedError("Persona State Evidence has expired")
                    record = state
            except NotFoundError as error:
                raise PersonaPolicyDeniedError(
                    "unknown Persona Evidence is not admissible"
                ) from error
            if getattr(record, "tenant_id", None) != access.tenant_id:
                raise PersonaPolicyDeniedError("cross-tenant Persona Evidence is denied")
            record_agent = getattr(record, "agent_id", agent_id)
            if record_agent != agent_id:
                raise PersonaPolicyDeniedError("cross-agent Persona Evidence is denied")
            space_id = getattr(record, "space_id", None)
            if (
                space_id is not None
                and command_reader is None
                and space_id not in access.allowed_space_ids
                and not access.admin
            ):
                raise PersonaPolicyDeniedError("Persona Evidence is outside the access scope")
            current_revision = getattr(
                record,
                "current_revision",
                getattr(record, "revision", None) if resource_type == "persona_state" else None,
            )
            if (
                revision is not None
                and current_revision is not None
                and revision != current_revision
            ):
                raise PersonaPolicyDeniedError("Persona Evidence revision is not current")
            if resource_type == "claim":
                revision_row = tx.claims.current_revision_row(resource_id)
                allowed_statuses = {"active", "disputed"}
            elif resource_type == "episode":
                revision_row = tx.episodes.current_revision_row(resource_id)
                allowed_statuses = {"open", "sealed"}
            elif resource_type == "relation":
                revision_row = tx.relations.current_revision_row(resource_id)
                allowed_statuses = {"active", "disputed"}
            elif resource_type == "task":
                revision_row = tx.tasks.current_task_revision_row(resource_id)
                allowed_statuses = {"active", "waiting", "blocked", "completed"}
            else:
                revision_row = None
                allowed_statuses = {"published"}
            status = str(getattr(record, "status", "published"))
            if status not in allowed_statuses:
                raise PersonaPolicyDeniedError("inactive Persona Evidence is not admissible")
            if revision_row is not None and command_reader is None:
                data_scope = Scope(
                    tenant_id=access.tenant_id,
                    agent_id=agent_id,
                    space_group_id=getattr(record, "space_group_id", None),
                    space_id=space_id,
                    session_id=getattr(record, "session_id", None),
                )
                if not evaluate_privacy(
                    tuple(getattr(revision_row, "privacy_labels", ())),
                    data_scope,
                    data_scope,
                    access,
                ):
                    raise PersonaPolicyDeniedError(
                        "Persona Evidence privacy labels are outside the access context"
                    )
            occurred_us = int(
                getattr(
                    record,
                    "recorded_at_us",
                    getattr(record, "created_us", getattr(record, "started_us", 0)),
                )
            )
            normalized = {
                "resource_type": resource_type,
                "resource_id": resource_id,
                **({"revision": revision} if revision is not None else {}),
            }
            validated.append(_Evidence(normalized, f"{resource_type}:{resource_id}", occurred_us))
        return tuple(validated)

    @staticmethod
    def _proposal_scope(access: AccessContext, agent_id: str, proposal: PersonaProposal) -> None:
        if proposal.tenant_id != access.tenant_id or proposal.agent_id != agent_id:
            raise AccessDeniedError("Persona proposal is outside the access context")


def _validate_policy_config(config: Mapping[str, object]) -> dict[str, object]:
    allowed_keys = {
        "mode",
        "allowed_fields",
        "max_single_delta",
        "max_cumulative_delta",
        "cumulative_window_us",
        "min_evidence",
        "min_distinct_sources",
        "min_evidence_span_us",
        "min_confidence",
        "cooldown_us",
        "observation_us",
        "sensitive_fields",
        "rollback_threshold",
    }
    unknown = set(config) - allowed_keys
    if unknown:
        raise InvalidRequestError(f"unknown Persona policy fields: {sorted(unknown)}")
    mode = str(config.get("mode", "locked"))
    if mode not in {item.value for item in PersonaPolicyMode}:
        raise InvalidRequestError("unknown Persona policy mode")
    raw_paths = config.get("allowed_fields", ())
    raw_sensitive = config.get("sensitive_fields", ())
    if not isinstance(raw_paths, (list, tuple)) or not isinstance(raw_sensitive, (list, tuple)):
        raise InvalidRequestError("Persona policy field lists must be arrays")
    paths = tuple(str(item) for item in raw_paths)
    sensitive = tuple(str(item) for item in raw_sensitive)
    legal_paths = {
        f"traits.{item}" for item in ("style", "interests", "habits", "tendencies", "weights")
    }
    legal_paths |= {
        f"narrative.{item}" for item in ("summary", "experiences", "relationships", "goals")
    }
    if set(paths) - legal_paths or set(sensitive) - legal_paths:
        raise InvalidRequestError("Persona policy contains an unknown field path")
    numeric_defaults: dict[str, float] = {
        "max_single_delta": 0.0,
        "max_cumulative_delta": 0.0,
        "min_confidence": 1.0,
        "rollback_threshold": 0.0,
    }
    result: dict[str, object] = {
        "mode": mode,
        "allowed_fields": paths,
        "sensitive_fields": sensitive,
    }
    for name, default in numeric_defaults.items():
        raw = config.get(name, default)
        if (
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not 0.0 <= float(raw) <= 1.0
        ):
            raise InvalidRequestError(f"Persona policy {name} must be within [0, 1]")
        result[name] = float(raw)
    integer_defaults = {
        "cumulative_window_us": 0,
        "min_evidence": 1,
        "min_distinct_sources": 1,
        "min_evidence_span_us": 0,
        "cooldown_us": 0,
        "observation_us": 0,
    }
    for name, default in integer_defaults.items():
        raw = config.get(name, default)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise InvalidRequestError(f"Persona policy {name} must be a non-negative integer")
        result[name] = raw
    if mode == PersonaPolicyMode.LOCKED.value and paths:
        raise InvalidRequestError("locked Persona policy cannot allow evolution fields")
    return result


def _field_deltas(
    current: PersonaRecord, flattened: Mapping[str, Any], refs: Sequence[_Evidence]
) -> tuple[PersonaFieldDelta, ...]:
    layers = {
        "traits": normalize_persona_layer(current.traits),
        "narrative": normalize_persona_layer(current.narrative),
    }
    return tuple(
        PersonaFieldDelta(
            field=field,
            old_value=layers[field.split(".", 1)[0]].get(field.split(".", 1)[1]),
            new_value=value,
            magnitude=field_magnitude(
                layers[field.split(".", 1)[0]].get(field.split(".", 1)[1]), value
            ),
            evidence_refs=tuple(item.source_key for item in refs),
            reason_code="evidence_supported_change",
        )
        for field, value in flattened.items()
    )


def _evaluate(
    policy: PersonaPolicy,
    deltas: Sequence[PersonaFieldDelta],
    refs: Sequence[_Evidence],
    confidence: float,
    *,
    now_us: int,
    previous_delta: float,
    last_publication_us: int | None,
) -> dict[str, object]:
    if policy.mode is PersonaPolicyMode.LOCKED:
        raise PersonaPolicyDeniedError("locked policy forbids Persona proposals")
    fields = {item.field for item in deltas}
    if fields - set(policy.allowed_fields):
        raise PersonaPolicyDeniedError("proposal changes a field outside the policy allowlist")
    if confidence < policy.min_confidence:
        raise PersonaPolicyDeniedError("proposal confidence is below policy minimum")
    if len(refs) < policy.min_evidence:
        raise PersonaPolicyDeniedError("proposal has insufficient Evidence")
    distinct = len({item.source_key for item in refs})
    if distinct < policy.min_distinct_sources:
        raise PersonaPolicyDeniedError("proposal has insufficient Evidence diversity")
    required_span = max(policy.min_evidence_span_us, policy.observation_us)
    if (
        refs
        and max(item.occurred_us for item in refs) - min(item.occurred_us for item in refs)
        < required_span
    ):
        raise PersonaPolicyDeniedError("proposal Evidence time span is too short")
    if any(item.magnitude > policy.max_single_delta for item in deltas):
        raise PersonaPolicyDeniedError("proposal exceeds the per-change magnitude limit")
    new_delta = sum(item.magnitude for item in deltas)
    if previous_delta + new_delta > policy.max_cumulative_delta:
        raise PersonaPolicyDeniedError("proposal exceeds the cumulative magnitude limit")
    if last_publication_us is not None and now_us - last_publication_us < policy.cooldown_us:
        raise PersonaPolicyDeniedError("Persona evolution policy cooldown is active")
    requires_review = (
        bool(fields & set(policy.sensitive_fields)) or policy.mode is PersonaPolicyMode.MANUAL
    )
    return {
        "decision": "manual_review" if requires_review else "auto_publish",
        "requires_review": requires_review,
        "policy_id": policy.id,
        "policy_revision": policy.revision,
        "evidence_count": len(refs),
        "distinct_sources": distinct,
        "confidence": confidence,
        "previous_window_delta": previous_delta,
        "proposed_delta": new_delta,
        "evidence_span_us": (
            max(item.occurred_us for item in refs) - min(item.occurred_us for item in refs)
            if refs
            else 0
        ),
    }


def _delta_json(delta: PersonaFieldDelta) -> dict[str, object]:
    return {
        "field": delta.field,
        "old_value": delta.old_value,
        "new_value": delta.new_value,
        "magnitude": delta.magnitude,
        "evidence_refs": list(delta.evidence_refs),
        "reason_code": delta.reason_code,
    }


__all__ = [
    "PERSONA_MANAGE_CAPABILITY",
    "PERSONA_READ_CAPABILITY",
    "PERSONA_REVIEW_CAPABILITY",
    "PERSONA_STATE_CAPABILITY",
    "PersonaService",
    "PersonaView",
    "normalize_persona_layer",
]
