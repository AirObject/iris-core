"""FocusItem application service (§9.3, Phase 3.3).

Focus is Canonical: creates and transitions write immutable revisions and CAS
the current pointer with Expected Revision; dismiss/expire/promote keep the
full revision history and never delete sources. Capacity (item count, kind
quotas, token budget) is enforced by evicting the LOWEST-attention active
items to dormant — deterministically, never by deletion. Time decay is a pure
function of the activation base and the last activation time, so maintenance
is idempotent and cannot raise confidence or touch sources. ``affect`` items
are ordinary focus: nothing here writes Persona Trait/Core.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, cast

from iris_memory_core.application.ports.clock import Clock
from iris_memory_core.application.ports.transaction import (
    IdempotencyRunner,
    Transaction,
    UnitOfWork,
)
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.application.write_support import require_surface_online_in_tx
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.console import CommandActor
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    ConflictError,
    IdempotencyUnavailableError,
    InvalidRequestError,
    InvalidTransitionError,
    NotFoundError,
    ScopeViolationError,
    require_reason,
)
from iris_memory_core.domain.focus import (
    ALL_FOCUS_KINDS,
    DEFAULT_FOCUS_CAPACITY,
    FocusCapacityPolicy,
    FocusItemCurrent,
    FocusKind,
    FocusRevision,
    FocusStatus,
    InvalidFocusItemError,
    clamp01,
    decayed_activation,
    focus_scope_key,
    validate_promotion_target,
    validate_scores,
    validate_summary,
    validate_transition,
)
from iris_memory_core.domain.hashing import request_fingerprint
from iris_memory_core.domain.privacy import InvalidPrivacyLabelError, evaluate_privacy, parse_label
from iris_memory_core.domain.recent import DefaultTokenEstimator, TokenEstimator
from iris_memory_core.domain.scope import Scope, scope_allows

_SOURCE_REF_TYPES = frozenset(
    {
        "observation",
        "episode",
        "claim",
        "entity",
        "external_identity",
        "binding",
        "relation",
        "state_record",
        "focus_item",
        "note",
        "task",
        "task_step",
        "task_trigger",
        "cognitive_event",
        "persona_revision",
        "persona_state",
        "persona_proposal",
        "reflection_record",
        "artifact",
        "tombstone",
        "audit_event",
    }
)


def _hash_id(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class FocusCreateResult:
    item_id: str
    revision: int
    evicted_item_ids: tuple[str, ...]
    replayed: bool


@dataclass(frozen=True, slots=True)
class FocusMaintenanceReport:
    """Low-cardinality outcome of one decay/tidy sweep."""

    expired: int
    dormanted: int
    refreshed: int
    unchanged: int


def _authorize_scope(
    tx: Transaction,
    access: AccessContext,
    *,
    agent_id: str,
    space_id: str | None,
    session_id: str | None,
) -> Scope:
    agent = tx.get_agent(agent_id)
    if agent.tenant_id != access.tenant_id:
        raise AccessDeniedError("agent belongs to another tenant")
    if agent_id not in access.agent_ids:
        raise AccessDeniedError("agent is outside the access context")
    space_group_id: str | None = None
    if space_id is not None:
        space = tx.get_space(space_id)
        if space.tenant_id != access.tenant_id:
            raise AccessDeniedError("space belongs to another tenant")
        if space_id not in access.allowed_space_ids:
            raise AccessDeniedError("space is outside the access context")
        if space.agent_id is not None and space.agent_id != agent_id:
            raise AccessDeniedError("space belongs to a different agent")
    if session_id is not None:
        if space_id is None:
            raise InvalidRequestError("session_id requires space_id")
        session = tx.get_session(session_id)
        if session.tenant_id != access.tenant_id:
            raise AccessDeniedError("session belongs to another tenant")
        if session.space_id != space_id:
            raise InvalidRequestError("session does not belong to the given space")
    scope = Scope(
        tenant_id=access.tenant_id,
        agent_id=agent_id,
        space_group_id=space_group_id,
        space_id=space_id,
        session_id=session_id,
    )
    return access.authorize_scope(scope)


def _parse_source_refs(raw: list[dict[str, Any]] | None) -> tuple[dict[str, object], ...]:
    refs: list[dict[str, object]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            raise InvalidRequestError("source_refs entries must be objects")
        resource_type = item.get("resource_type")
        resource_id = item.get("resource_id")
        if resource_type not in _SOURCE_REF_TYPES:
            raise InvalidRequestError(f"unknown source ref type: {resource_type!r}")
        if not isinstance(resource_id, str) or not resource_id:
            raise InvalidRequestError("source_refs[].resource_id must be a non-empty string")
        revision = item.get("revision")
        if revision is not None and (not isinstance(revision, int) or revision < 1):
            raise InvalidRequestError("source_refs[].revision must be a positive integer")
        refs.append(
            {
                "resource_type": resource_type,
                "resource_id": resource_id,
                **({"revision": revision} if revision is not None else {}),
            }
        )
    return tuple(refs)


def _parse_privacy_labels(raw: list[str] | None) -> tuple[str, ...]:
    labels: list[str] = []
    for label in raw or []:
        try:
            parse_label(label)
        except InvalidPrivacyLabelError as error:
            raise InvalidRequestError(str(error)) from error
        labels.append(label)
    return tuple(labels)


class FocusService:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        *,
        capacity: FocusCapacityPolicy | None = None,
        estimator: TokenEstimator | None = None,
        idempotency: IdempotencyRunner | None = None,
        surface: SurfaceCoordinatorService | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._capacity = capacity or DEFAULT_FOCUS_CAPACITY
        self._estimator = estimator or DefaultTokenEstimator()
        self._idempotency = idempotency
        self._surface = surface

    def _gate(
        self,
        tx: Transaction,
        access: AccessContext,
        agent_id: str,
        lease_id: str | None,
        lease_epoch: int | None,
    ) -> None:
        require_surface_online_in_tx(
            self._surface,
            tx,
            access.tenant_id,
            agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )

    def _item_preflight(
        self,
        access: AccessContext,
        item_id: str,
        lease_id: str | None,
        lease_epoch: int | None,
    ) -> None:
        if self._surface is not None:
            with self._uow.read() as tx:
                item = tx.focus.get(item_id)
                _require_item_content_access(tx, access, item)
                self._gate(tx, access, item.agent_id, lease_id, lease_epoch)

    def create_for_command(
        self,
        tx: Transaction,
        actor: CommandActor,
        fields: dict[str, Any],
        *,
        privacy_labels: list[str],
        source_refs: list[dict[str, Any]],
    ) -> tuple[str, str, list[str]]:
        from iris_memory_core.application.console.commands import CommandTarget, command_access
        from iris_memory_core.application.console.resources import ResourceRef

        if actor.operation != "focus.create" or not actor.scope.agent_id:
            raise InvalidRequestError("invalid managed focus creation")
        if not {"kind", "summary"} <= fields.keys() or fields.keys() - {
            "kind",
            "summary",
            "salience",
            "importance",
            "activation",
            "promotion_policy",
            "expires_us",
            "structured_payload",
        }:
            raise InvalidRequestError("invalid managed focus fields")
        labels, refs = _parse_privacy_labels(privacy_labels), _parse_source_refs(source_refs)
        access = command_access(
            tx,
            actor,
            CommandTarget(
                "focus_item",
                actor.scope,
                privacy_labels=labels,
                source_refs=tuple(
                    ResourceRef(
                        str(ref["resource_type"]),
                        str(ref["resource_id"]),
                        cast(int | None, ref.get("revision")),
                    )
                    for ref in refs
                ),
            ),
            now_us=self._clock.now_us(),
        )
        payload = {
            "agent_id": actor.scope.agent_id,
            "space_id": actor.scope.space_id,
            "session_id": actor.scope.session_id,
            "salience": 0.5,
            "importance": 0.5,
            "activation": 0.5,
            "promotion_policy": "",
            "expires_us": None,
            "structured_payload": None,
            **fields,
            "privacy_labels": list(labels),
            "source_refs": [dict(ref) for ref in refs],
        }
        if payload["kind"] not in ALL_FOCUS_KINDS:
            raise InvalidRequestError("invalid managed focus kind")
        try:
            validate_summary(payload["summary"])
            validate_scores(
                salience=payload["salience"],
                importance=payload["importance"],
                activation=payload["activation"],
            )
        except InvalidFocusItemError as error:
            raise InvalidRequestError(str(error)) from error
        if payload["expires_us"] is not None and payload["expires_us"] <= self._clock.now_us():
            raise InvalidRequestError("focus expiry must be in the future")
        return self._execute_create(tx, access, payload, command_actor=actor)

    def _command_item_access(
        self, tx: Transaction, actor: CommandActor, item: FocusItemCurrent, operation: str
    ) -> AccessContext:
        from iris_memory_core.application.console.commands import CommandTarget, command_access
        from iris_memory_core.application.console.resources import ResourceRef

        if actor.operation != operation or actor.scope != _item_scope(item):
            raise AccessDeniedError("focus command scope or operation mismatch")
        current = tx.focus.current_revision_row(item.id)
        target = CommandTarget(
            "focus_item",
            _item_scope(item),
            privacy_labels=current.privacy_labels,
            source_refs=tuple(
                ResourceRef(
                    str(ref["resource_type"]),
                    str(ref["resource_id"]),
                    cast(int | None, ref.get("revision")),
                )
                for ref in current.source_refs
            ),
            resource_id=item.id,
        )
        return command_access(tx, actor, target, now_us=self._clock.now_us())

    def mutate_for_command(
        self,
        tx: Transaction,
        actor: CommandActor,
        item_id: str,
        *,
        expected_revision: int,
        fields: dict[str, Any],
    ) -> tuple[str, str, list[str]]:
        if type(expected_revision) is not int or expected_revision < 1:
            raise InvalidRequestError("expected revision must be positive")
        item = tx.focus.get(item_id)
        access = self._command_item_access(tx, actor, item, actor.operation)
        payload = {
            "item_id": item_id,
            "expected_revision": expected_revision,
            "reason": actor.reason_code,
        }
        if actor.operation == "focus.activate" and not fields:
            return self._execute_activate(tx, access, payload, command_actor=actor)
        if actor.operation == "focus.update":
            return self._update_for_command(tx, actor, item, expected_revision, fields)
        if (
            actor.operation != "focus.transition"
            or "target_status" not in fields
            or set(fields) - {"target_status", "promotion_target_type"}
            or fields["target_status"] not in {"dormant", "dismissed", "expired", "promoted"}
        ):
            raise InvalidRequestError("invalid managed focus transition")
        payload.update(
            target=fields["target_status"],
            promotion_target_type=fields.get("promotion_target_type"),
        )
        return self._execute_transition(
            tx, access, payload, FocusStatus(fields["target_status"]), command_actor=actor
        )

    def _update_for_command(
        self,
        tx: Transaction,
        actor: CommandActor,
        item: FocusItemCurrent,
        expected_revision: int,
        fields: dict[str, Any],
    ) -> tuple[str, str, list[str]]:
        access = self._command_item_access(tx, actor, item, "focus.update")
        current = _require_item_content_access(tx, access, item, managed=True)
        if item.status not in {"active", "dormant"}:
            raise InvalidTransitionError("terminal focus cannot be edited")
        if not fields or fields.keys() - {
            "summary",
            "structured_payload",
            "salience",
            "importance",
        }:
            raise InvalidRequestError("invalid managed focus update fields")
        summary = fields.get("summary", current.summary)
        salience, importance = (
            fields.get("salience", current.salience),
            fields.get("importance", current.importance),
        )
        try:
            validate_summary(summary)
            validate_scores(salience=salience, importance=importance, activation=current.activation)
        except InvalidFocusItemError as error:
            raise InvalidRequestError(str(error)) from error
        if item.current_revision != expected_revision:
            tx.focus.raise_pointer_mismatch(item.id, expected_revision)
        evicted = (
            self._enforce_capacity(
                tx,
                _item_scope(item),
                incoming_kind=item.kind,
                incoming_tokens=self._estimator.estimate(summary),
                exclude_item_id=item.id,
                command_actor=actor,
            )
            if item.status == "active"
            else []
        )
        activation = decayed_activation(
            item.activation_base,
            item.last_activated_us,
            self._clock.now_us(),
            half_life_us=self._capacity.half_life_us,
        )
        revision = item.current_revision + 1
        revision_id = tx.focus.insert_revision(
            item_id=item.id,
            tenant_id=item.tenant_id,
            revision=revision,
            kind=FocusKind(current.kind),
            summary=summary,
            structured_payload=fields.get("structured_payload", current.structured_payload),
            privacy_labels=current.privacy_labels,
            source_refs=current.source_refs,
            salience=salience,
            importance=importance,
            activation=activation,
            activation_base=item.activation_base,
            status=FocusStatus(item.status),
            promotion_policy=current.promotion_policy,
            promotion_target_type=current.promotion_target_type,
            promotion_target_id=current.promotion_target_id,
            last_activated_us=item.last_activated_us,
            expires_us=current.expires_us,
            created_by=actor.audit_actor,
        )
        if (
            tx.focus.advance_pointer(
                item.id,
                expected_revision=expected_revision,
                revision=revision,
                revision_id=revision_id,
                status=item.status,
                activation=activation,
            )
            != 1
        ):
            tx.focus.raise_pointer_mismatch(item.id, expected_revision)
        tx.advance_watermark(item.tenant_id, item.agent_id, [("focus_item", item.id, revision)])
        tx.audit(
            tenant_id=item.tenant_id,
            actor=actor.audit_actor,
            action="focus.updated",
            resource_type="focus_item",
            resource_id=item.id,
            reason_code=actor.reason_code,
            details={"fields": sorted(fields)},
            revision=revision,
        )
        return (
            "focus.updated",
            json.dumps({"revision_id": revision_id}),
            [f"focus_item:{item.id}", *[f"focus_item:{identifier}" for identifier in evicted]],
        )

    # -- create ---------------------------------------------------------------

    def create(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        kind: str,
        summary: str,
        space_id: str | None = None,
        session_id: str | None = None,
        salience: float = 0.5,
        importance: float = 0.5,
        activation: float = 0.5,
        promotion_policy: str = "",
        expires_us: int | None = None,
        privacy_labels: list[str] | None = None,
        source_refs: list[dict[str, Any]] | None = None,
        structured_payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
    ) -> FocusCreateResult:
        if idempotency_key is None:
            raise InvalidRequestError("focus creation requires an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        if kind not in ALL_FOCUS_KINDS:
            raise InvalidRequestError(f"unknown focus kind: {kind!r}")
        try:
            validate_summary(summary)
            validate_scores(salience=salience, activation=activation, importance=importance)
        except InvalidFocusItemError as error:
            raise InvalidRequestError(str(error)) from error
        refs = _parse_source_refs(source_refs)
        labels = _parse_privacy_labels(privacy_labels)
        if self._surface is not None:
            with self._uow.read() as tx:
                scope = _authorize_scope(
                    tx,
                    access,
                    agent_id=agent_id,
                    space_id=space_id,
                    session_id=session_id,
                )
                if not evaluate_privacy(labels, scope, scope, access):
                    raise AccessDeniedError("focus privacy labels are outside the access context")
                self._gate(tx, access, agent_id, lease_id, lease_epoch)
        now_us = self._clock.now_us()
        if expires_us is not None and expires_us <= now_us:
            raise InvalidRequestError("expires_us must be in the future")
        payload = {
            "agent_id": agent_id,
            "kind": kind,
            "summary": summary,
            "space_id": space_id,
            "session_id": session_id,
            "salience": salience,
            "importance": importance,
            "activation": activation,
            "promotion_policy": promotion_policy,
            "expires_us": expires_us,
            "privacy_labels": list(labels),
            "source_refs": [dict(ref) for ref in refs],
            "structured_payload": structured_payload,
        }
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="focus:create",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint("focus:create", payload),
            execute=lambda tx: self._execute_create(tx, access, payload, lease_id, lease_epoch),
        )
        body = json.loads(result.body)
        # Creation replay is also a by-ID outcome: current authorization and
        # Forget state outrank the cached success record.
        with self._uow.read() as tx:
            item = tx.focus.get(body["item_id"])
            _require_item_content_access(tx, access, item)
            if result.replayed:
                self._gate(tx, access, item.agent_id, lease_id, lease_epoch)
        return FocusCreateResult(
            item_id=body["item_id"],
            revision=int(body["revision"]),
            evicted_item_ids=tuple(body.get("evicted", [])),
            replayed=result.replayed,
        )

    def _execute_create(
        self,
        tx: Transaction,
        access: AccessContext,
        payload: dict[str, Any],
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        *,
        command_actor: CommandActor | None = None,
    ) -> tuple[str, str, list[str]]:
        from iris_memory_core.application.write_support import authorize_scope

        scope = authorize_scope(
            tx,
            access,
            agent_id=payload["agent_id"],
            space_id=payload["space_id"],
            session_id=payload["session_id"],
            space_group_id=command_actor.scope.space_group_id if command_actor else None,
        )
        if command_actor is not None and scope != command_actor.scope:
            raise AccessDeniedError("managed focus scope mismatch")
        labels = tuple(
            label
            for label in payload["privacy_labels"]
            if command_actor is None or label != "restricted"
        )
        if not evaluate_privacy(labels, scope, scope, access):
            raise AccessDeniedError("focus privacy labels are outside the access context")
        if command_actor is None:
            self._gate(tx, access, payload["agent_id"], lease_id, lease_epoch)
        now_us = self._clock.now_us()
        evicted = self._enforce_capacity(
            tx,
            scope,
            incoming_kind=payload["kind"],
            incoming_tokens=self._estimator.estimate(payload["summary"]),
            command_actor=command_actor,
        )
        scope_key = focus_scope_key(
            scope.tenant_id,
            scope.agent_id or "",
            scope.space_id,
            scope.session_id,
            scope.space_group_id,
        )
        # The revision row carries the item FK, so the item row lands first
        # with a placeholder pointer; the pointer is wired right after. Item
        # ids are fresh UUIDv7s, so creation cannot race here.
        item_id = tx.focus.insert(
            tenant_id=scope.tenant_id,
            agent_id=scope.agent_id or "",
            space_group_id=scope.space_group_id,
            space_id=scope.space_id,
            session_id=scope.session_id,
            scope_key=scope_key,
            kind=FocusKind(payload["kind"]),
            summary=payload["summary"],
            status=FocusStatus.ACTIVE,
            revision_id="",
            activation=payload["activation"],
            activation_base=payload["activation"],
            last_activated_us=now_us,
            expires_us=payload["expires_us"],
        )
        revision_id = tx.focus.insert_revision(
            item_id=item_id,
            tenant_id=scope.tenant_id,
            revision=1,
            kind=FocusKind(payload["kind"]),
            summary=payload["summary"],
            structured_payload=payload["structured_payload"],
            privacy_labels=tuple(payload["privacy_labels"]),
            source_refs=tuple(payload["source_refs"]),
            salience=payload["salience"],
            activation=payload["activation"],
            activation_base=payload["activation"],
            importance=payload["importance"],
            status=FocusStatus.ACTIVE,
            promotion_policy=payload["promotion_policy"],
            promotion_target_type=None,
            promotion_target_id=None,
            last_activated_us=now_us,
            expires_us=payload["expires_us"],
            created_by=command_actor.audit_actor
            if command_actor
            else f"access:{access.app_instance_id}",
        )
        wired = tx.focus.set_initial_pointer(item_id, revision_id)
        if wired != 1:
            raise ConflictError("focus item creation raced inside the transaction")
        tx.advance_watermark(scope.tenant_id, scope.agent_id or "", [("focus_item", item_id, 1)])
        tx.audit(
            tenant_id=scope.tenant_id,
            actor=command_actor.audit_actor
            if command_actor
            else f"access:{access.app_instance_id}",
            action="focus.created",
            resource_type="focus_item",
            resource_id=item_id,
            reason_code=command_actor.reason_code if command_actor else "attention_capture",
            details={
                "kind": payload["kind"],
                "evicted": len(evicted),
                "summary_hash": _hash_id(payload["summary"]),
            },
            revision=1,
        )
        body = json.dumps({"item_id": item_id, "revision": 1, "evicted": list(evicted)})
        return (
            "focus.created",
            body,
            [
                f"focus_item:{item_id}",
                *([f"focus_item:{identifier}" for identifier in evicted] if command_actor else []),
            ],
        )

    def _enforce_capacity(
        self,
        tx: Transaction,
        scope: Scope,
        *,
        incoming_kind: str,
        incoming_tokens: int,
        command_actor: CommandActor | None = None,
        exclude_item_id: str | None = None,
    ) -> list[str]:
        """Evict lowest-attention active items to dormant until the new item fits.

        Eviction order is (activation, created_us, id) ascending — fully
        deterministic. Dormant items do not count against the working-set
        limits; dismissed/expired/promoted never do.
        """
        active = [
            item
            for item in tx.focus.active_items(scope.tenant_id, scope.agent_id or "")
            if item.id != exclude_item_id
        ]
        quota = self._capacity.kind_quotas or {}
        kind_quota = quota.get(incoming_kind, self._capacity.max_items)
        summaries = {item.id: tx.focus.current_revision_row(item.id).summary for item in active}
        active_tokens = (
            sum(self._estimator.estimate(summary) for summary in summaries.values())
            + incoming_tokens
        )
        evicted: list[str] = []
        while (
            len(active) + 1 > self._capacity.max_items
            or sum(1 for item in active if item.kind == incoming_kind) + 1 > kind_quota
            or active_tokens > self._capacity.token_budget
        ):
            # Prefer evicting within the over-quota kind; otherwise the lowest
            # (activation, created_us, id) active item — the repository's
            # ordering is exactly that.
            victim = next((item for item in active if item.kind == incoming_kind), None)
            if victim is None:
                victim = active[0] if active else None
            if victim is None:
                raise ConflictError(
                    "focus capacity cannot admit the item",
                    details={"kind": incoming_kind},
                )
            if command_actor is not None:
                self._command_item_access(
                    tx,
                    replace(command_actor, scope=_item_scope(victim), operation="focus.capacity"),
                    victim,
                    "focus.capacity",
                )
            self._transition(
                tx,
                scope.tenant_id,
                victim,
                FocusStatus.DORMANT,
                actor=command_actor.audit_actor if command_actor else "focus:capacity",
                reason_code="focus_capacity_eviction",
                now_us=self._clock.now_us(),
                activation_override=decayed_activation(
                    victim.activation_base,
                    victim.last_activated_us,
                    self._clock.now_us(),
                    half_life_us=self._capacity.half_life_us,
                ),
            )
            evicted.append(victim.id)
            active_tokens -= self._estimator.estimate(summaries.pop(victim.id, ""))
            active = [item for item in active if item.id != victim.id]
        return evicted

    # -- transitions -------------------------------------------------------------

    def activate(
        self,
        access: AccessContext,
        item_id: str,
        *,
        expected_revision: int,
        reason: str | None = None,
        idempotency_key: str | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
    ) -> FocusRevision:
        """Explicit activation: refresh + bounded boost of the activation base.

        Only explicit activation (or later verified real usage) may strengthen
        activation — plain retrieval/return never calls this.
        """
        if idempotency_key is None:
            raise InvalidRequestError("focus activation requires an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        self._item_preflight(access, item_id, lease_id, lease_epoch)
        reason_code = require_reason(reason)
        payload = {
            "item_id": item_id,
            "expected_revision": expected_revision,
            "reason": reason_code,
        }
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="focus:activate",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint("focus:activate", payload),
            execute=lambda tx: self._execute_activate(tx, access, payload, lease_id, lease_epoch),
        )
        body = json.loads(result.body)
        # Revision rows are immutable, so a replay re-reads the exact row the
        # first execution wrote instead of replaying the CAS. Authorization and
        # Tombstone checks are intentionally repeated: a completed idempotency
        # record must not preserve access that has since been revoked or
        # resurrect content that has since been forgotten.
        with self._uow.read() as tx:
            item = tx.focus.get(item_id)
            revision = tx.focus.get_revision(body["revision_id"])
            visible = _require_item_content_access(tx, access, item, revision=revision)
            if result.replayed:
                self._gate(tx, access, item.agent_id, lease_id, lease_epoch)
            return visible

    def _execute_activate(
        self,
        tx: Transaction,
        access: AccessContext,
        payload: dict[str, Any],
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        *,
        command_actor: CommandActor | None = None,
    ) -> tuple[str, str, list[str]]:
        item_id = payload["item_id"]
        expected_revision = payload["expected_revision"]
        item = tx.focus.get(item_id)
        if command_actor is not None:
            access = self._command_item_access(tx, command_actor, item, "focus.activate")
        current = _require_item_content_access(tx, access, item, managed=command_actor is not None)
        if command_actor is None:
            self._gate(tx, access, item.agent_id, lease_id, lease_epoch)
        if item.status not in (FocusStatus.ACTIVE.value, FocusStatus.DORMANT.value):
            raise InvalidTransitionError(
                f"focus item cannot be activated from {item.status!r}",
                details={"from": item.status, "to": "active"},
            )
        evicted = (
            self._enforce_capacity(
                tx,
                _item_scope(item),
                incoming_kind=item.kind,
                incoming_tokens=self._estimator.estimate(current.summary),
                exclude_item_id=item.id,
                command_actor=command_actor,
            )
            if command_actor is not None and item.status == FocusStatus.DORMANT.value
            else []
        )
        now_us = self._clock.now_us()
        boost = self._capacity.activation_boost
        new_base = clamp01(item.activation_base + boost)
        revision_id = tx.focus.insert_revision(
            item_id=item.id,
            tenant_id=item.tenant_id,
            revision=item.current_revision + 1,
            kind=FocusKind(current.kind),
            summary=current.summary,
            structured_payload=current.structured_payload,
            privacy_labels=current.privacy_labels,
            source_refs=current.source_refs,
            salience=current.salience,
            activation=new_base,
            activation_base=new_base,
            importance=current.importance,
            status=FocusStatus.ACTIVE,
            promotion_policy=current.promotion_policy,
            promotion_target_type=current.promotion_target_type,
            promotion_target_id=current.promotion_target_id,
            last_activated_us=now_us,
            expires_us=current.expires_us,
            created_by=command_actor.audit_actor
            if command_actor
            else f"access:{access.app_instance_id}",
        )
        updated = tx.focus.advance_pointer(
            item.id,
            expected_revision=expected_revision,
            revision=item.current_revision + 1,
            revision_id=revision_id,
            status=FocusStatus.ACTIVE,
            activation=new_base,
            activation_base=new_base,
            last_activated_us=now_us,
        )
        if updated != 1:
            tx.focus.raise_pointer_mismatch(item.id, expected_revision)
        tx.advance_watermark(
            item.tenant_id, item.agent_id, [("focus_item", item.id, item.current_revision + 1)]
        )
        tx.audit(
            tenant_id=item.tenant_id,
            actor=command_actor.audit_actor
            if command_actor
            else f"access:{access.app_instance_id}",
            action="focus.activated",
            resource_type="focus_item",
            resource_id=item.id,
            reason_code=payload["reason"],
            details={"activation_base": new_base},
            revision=item.current_revision + 1,
        )
        body = json.dumps({"revision_id": revision_id})
        return (
            "focus.activated",
            body,
            [f"focus_item:{item.id}", *[f"focus_item:{identifier}" for identifier in evicted]],
        )

    def transition(
        self,
        access: AccessContext,
        item_id: str,
        target: str,
        *,
        expected_revision: int,
        reason: str | None = None,
        promotion_target_type: str | None = None,
        idempotency_key: str | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
    ) -> FocusRevision:
        """dormant | dismiss | expire | promote (action aliases) with CAS.

        Both the action spelling (``promote``) and the status spelling
        (``promoted``) are accepted; everything else is ``invalid_request``.
        """
        if idempotency_key is None:
            raise InvalidRequestError("focus transitions require an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        self._item_preflight(access, item_id, lease_id, lease_epoch)
        reason_code = require_reason(reason)
        alias = {"dismiss": "dismissed", "expire": "expired", "promote": "promoted"}
        canonical = alias.get(target, target)
        try:
            target_status = FocusStatus(canonical)
        except ValueError:
            raise InvalidRequestError(f"unknown focus status: {target!r}") from None
        try:
            validate_promotion_target(
                promotion_target_type if target_status is FocusStatus.PROMOTED else None
            )
        except InvalidFocusItemError as error:
            raise InvalidRequestError(str(error)) from error
        # All state-dependent checks (authorization, state-machine legality,
        # promotion arguments) run INSIDE the idempotent execution: a
        # byte-identical replay short-circuits to the first outcome without
        # re-observing a world the first execution already changed.
        payload = {
            "item_id": item_id,
            "target": target,
            "expected_revision": expected_revision,
            "reason": reason_code,
            "promotion_target_type": promotion_target_type,
        }
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="focus:transition",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint("focus:transition", payload),
            execute=lambda tx: self._execute_transition(
                tx, access, payload, target_status, lease_id, lease_epoch
            ),
        )
        body = json.loads(result.body)
        with self._uow.read() as tx:
            item = tx.focus.get(item_id)
            revision = tx.focus.get_revision(body["revision_id"])
            visible = _require_item_content_access(tx, access, item, revision=revision)
            if result.replayed:
                self._gate(tx, access, item.agent_id, lease_id, lease_epoch)
            if revision.promotion_target_id and revision.promotion_target_type:
                from iris_memory_core.application.promotion import (
                    PromotionSource,
                    focus_promotion_evidence,
                    require_promotion_references,
                )

                source = PromotionSource.from_focus(item, revision)
                focus_promotion_evidence(tx, access, source, revision.promotion_target_type)
                require_promotion_references(
                    tx,
                    access,
                    source.scope,
                    (
                        {
                            "resource_type": revision.promotion_target_type,
                            "resource_id": revision.promotion_target_id,
                        },
                    ),
                )
            return visible

    def _execute_transition(
        self,
        tx: Transaction,
        access: AccessContext,
        payload: dict[str, Any],
        target_status: FocusStatus,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        *,
        command_actor: CommandActor | None = None,
    ) -> tuple[str, str, list[str]]:
        item_id = payload["item_id"]
        expected_revision = payload["expected_revision"]
        promotion_target_type = payload["promotion_target_type"]
        item = tx.focus.get(item_id)
        if command_actor is not None:
            access = self._command_item_access(tx, command_actor, item, "focus.transition")
        current = _require_item_content_access(tx, access, item, managed=command_actor is not None)
        if command_actor is None:
            self._gate(tx, access, item.agent_id, lease_id, lease_epoch)
        # State-machine legality runs BEFORE argument checks so an illegal
        # move always reports invalid_state_transition, whatever the caller
        # did or did not supply. It also runs on the row the CAS will target,
        # INSIDE the serialized write transaction: a caller that predicted the
        # next revision cannot push an item out of a terminal state even by
        # winning the race to write.
        try:
            validate_transition(item.status, target_status.value)
        except InvalidFocusItemError as error:
            if target_status is FocusStatus.ACTIVE:
                raise InvalidTransitionError(
                    f"focus item cannot be activated from {item.status!r}",
                    details={"from": item.status, "to": "active"},
                ) from error
            raise InvalidTransitionError(
                str(error), details={"from": item.status, "to": target_status.value}
            ) from error
        if target_status is FocusStatus.PROMOTED and promotion_target_type is None:
            raise InvalidRequestError("promotion requires a promotion_target_type")
        if target_status is not FocusStatus.PROMOTED and promotion_target_type is not None:
            raise InvalidRequestError("promotion_target_type is only valid for promote")
        if target_status is FocusStatus.PROMOTED:
            try:
                validate_promotion_target(promotion_target_type)
            except InvalidFocusItemError as error:
                raise InvalidRequestError(str(error)) from error
        if item.current_revision != expected_revision:
            tx.focus.raise_pointer_mismatch(item.id, expected_revision)
        promotion_target_id = None
        extra_refs: list[str] = []
        if target_status is FocusStatus.PROMOTED:
            from iris_memory_core.application.promotion import PromotionSource, materialize_focus

            promotion_target_id = materialize_focus(
                tx,
                access,
                PromotionSource.from_focus(item, current),
                str(promotion_target_type),
                actor=command_actor.audit_actor
                if command_actor
                else f"access:{access.app_instance_id}",
                now_us=self._clock.now_us(),
                command_actor=command_actor,
            )
            extra_refs.append(f"{promotion_target_type}:{promotion_target_id}")
        now_us = self._clock.now_us()
        new_activation = decayed_activation(
            item.activation_base,
            item.last_activated_us,
            now_us,
            half_life_us=self._capacity.half_life_us,
        )
        revision_id = tx.focus.insert_revision(
            item_id=item.id,
            tenant_id=item.tenant_id,
            revision=item.current_revision + 1,
            kind=FocusKind(current.kind),
            summary=current.summary,
            structured_payload=current.structured_payload,
            privacy_labels=current.privacy_labels,
            source_refs=current.source_refs,
            salience=current.salience,
            activation=new_activation,
            activation_base=item.activation_base,
            importance=current.importance,
            status=target_status,
            promotion_policy=current.promotion_policy,
            promotion_target_type=promotion_target_type,
            promotion_target_id=promotion_target_id,
            last_activated_us=item.last_activated_us,
            expires_us=current.expires_us,
            created_by=command_actor.audit_actor
            if command_actor
            else f"access:{access.app_instance_id}",
        )
        updated = tx.focus.advance_pointer(
            item.id,
            expected_revision=expected_revision,
            revision=item.current_revision + 1,
            revision_id=revision_id,
            status=target_status,
            activation=new_activation,
        )
        if updated != 1:
            tx.focus.raise_pointer_mismatch(item.id, expected_revision)
        tx.advance_watermark(
            item.tenant_id, item.agent_id, [("focus_item", item.id, item.current_revision + 1)]
        )
        action = {
            FocusStatus.DORMANT: "focus.set_dormant",
            FocusStatus.DISMISSED: "focus.dismissed",
            FocusStatus.EXPIRED: "focus.expired",
            FocusStatus.PROMOTED: "focus.promoted",
        }[target_status]
        details: dict[str, object] = {"activation": new_activation}
        if target_status is FocusStatus.PROMOTED:
            details["promotion_target_type"] = promotion_target_type
            details["promotion_target_id"] = promotion_target_id
            details["promotion_policy"] = current.promotion_policy
        tx.audit(
            tenant_id=item.tenant_id,
            actor=command_actor.audit_actor
            if command_actor
            else f"access:{access.app_instance_id}",
            action=action,
            resource_type="focus_item",
            resource_id=item.id,
            reason_code=payload["reason"],
            details=details,
            revision=item.current_revision + 1,
        )
        body = json.dumps({"revision_id": revision_id})
        return action, body, [f"focus_item:{item.id}", *extra_refs]

    def _transition(
        self,
        tx: Transaction,
        tenant_id: str,
        item: FocusItemCurrent,
        target: FocusStatus,
        *,
        actor: str,
        reason_code: str,
        now_us: int,
        activation_override: float | None = None,
        allow_same_status: bool = False,
    ) -> None:
        """Internal transition (capacity eviction, maintenance) inside a tx.

        ``allow_same_status`` exists for the maintenance decay refresh: a new
        revision that only recomputes Activation keeps the status — that is a
        revision write, not a state-machine move.
        """
        if allow_same_status and item.status == target.value:
            pass  # refresh path: same status, new activation revision
        else:
            try:
                validate_transition(item.status, target.value)
            except InvalidFocusItemError:
                return
        new_activation = (
            activation_override
            if activation_override is not None
            else decayed_activation(
                item.activation_base,
                item.last_activated_us,
                now_us,
                half_life_us=self._capacity.half_life_us,
            )
        )
        current = tx.focus.current_revision_row(item.id)
        revision_id = tx.focus.insert_revision(
            item_id=item.id,
            tenant_id=tenant_id,
            revision=item.current_revision + 1,
            kind=FocusKind(current.kind),
            summary=current.summary,
            structured_payload=current.structured_payload,
            privacy_labels=current.privacy_labels,
            source_refs=current.source_refs,
            salience=current.salience,
            activation=new_activation,
            activation_base=item.activation_base,
            importance=current.importance,
            status=target,
            promotion_policy=current.promotion_policy,
            promotion_target_type=current.promotion_target_type,
            promotion_target_id=current.promotion_target_id,
            last_activated_us=item.last_activated_us,
            expires_us=current.expires_us,
            created_by=actor,
        )
        updated = tx.focus.advance_pointer(
            item.id,
            expected_revision=item.current_revision,
            revision=item.current_revision + 1,
            revision_id=revision_id,
            status=target,
            activation=new_activation,
        )
        if updated != 1:
            tx.focus.raise_pointer_mismatch(item.id, item.current_revision)
        tx.advance_watermark(
            tenant_id, item.agent_id, [("focus_item", item.id, item.current_revision + 1)]
        )
        tx.audit(
            tenant_id=tenant_id,
            actor=actor,
            action=f"focus.{target.value}",
            resource_type="focus_item",
            resource_id=item.id,
            reason_code=reason_code,
            details={"activation": new_activation},
            revision=item.current_revision + 1,
        )

    # -- maintenance --------------------------------------------------------------

    def maintenance_sweep(
        self, tx: Transaction, *, tenant_id: str, agent_id: str, now_us: int | None = None
    ) -> FocusMaintenanceReport:
        """Deterministic decay/tidy pass — idempotent at the same instant.

        Expiry wins over decay (an expired item is expired regardless of its
        activation); active items whose decayed activation fell below the
        dormant floor go dormant; stored activation drifting from the pure
        function's value is refreshed. Nothing is deleted and sources are
        never modified.
        """
        now = now_us if now_us is not None else self._clock.now_us()
        expired = dormanted = refreshed = unchanged = 0
        items = tx.focus.maintenance_items(tenant_id, agent_id, now_us=now)
        for item in items:
            if item.expires_us is not None and item.expires_us <= now:
                before = item.current_revision
                self._transition(
                    tx,
                    tenant_id,
                    item,
                    FocusStatus.EXPIRED,
                    actor="focus:maintenance",
                    reason_code="focus_ttl_reached",
                    now_us=now,
                )
                if tx.focus.get(item.id).current_revision > before:
                    expired += 1
                continue
            decayed = decayed_activation(
                item.activation_base,
                item.last_activated_us,
                now,
                half_life_us=self._capacity.half_life_us,
            )
            if item.status == FocusStatus.ACTIVE.value and decayed < self._capacity.dormant_floor:
                before = item.current_revision
                self._transition(
                    tx,
                    tenant_id,
                    item,
                    FocusStatus.DORMANT,
                    actor="focus:maintenance",
                    reason_code="activation_below_floor",
                    now_us=now,
                    activation_override=decayed,
                )
                if tx.focus.get(item.id).current_revision > before:
                    dormanted += 1
                continue
            if abs(item.activation - decayed) > 1e-9:
                before = item.current_revision
                self._transition(
                    tx,
                    tenant_id,
                    item,
                    FocusStatus(item.status),
                    actor="focus:maintenance",
                    reason_code="activation_decay_refresh",
                    now_us=now,
                    activation_override=decayed,
                    allow_same_status=True,
                )
                if tx.focus.get(item.id).current_revision > before:
                    refreshed += 1
                continue
            unchanged += 1
        return FocusMaintenanceReport(
            expired=expired, dormanted=dormanted, refreshed=refreshed, unchanged=unchanged
        )

    # -- reads -------------------------------------------------------------------

    def get(
        self,
        access: AccessContext,
        item_id: str,
        *,
        include_dormant: bool = True,
    ) -> tuple[FocusItemCurrent, FocusRevision] | None:
        with self._uow.read() as tx:
            found = self.get_in_tx(tx, access, item_id, include_dormant=include_dormant)
            return found

    def get_in_tx(
        self,
        tx: Transaction,
        access: AccessContext,
        item_id: str,
        *,
        include_dormant: bool = True,
    ) -> tuple[FocusItemCurrent, FocusRevision] | None:
        try:
            item = tx.focus.get(item_id)
        except NotFoundError:
            return None
        _same_item_access(tx, access, item)
        if tx.is_tombstoned(item.tenant_id, "focus_item", item.id):
            return None
        revision = tx.focus.current_revision_row(item.id)
        now_us = self._clock.now_us()
        # Final checks before returning: terminal statuses and expiry govern
        # visibility for working-set readers.
        if revision.status == FocusStatus.EXPIRED.value or (
            revision.expires_us is not None and revision.expires_us <= now_us
        ):
            return None
        if revision.status == FocusStatus.DISMISSED.value:
            return None
        if revision.status == FocusStatus.DORMANT.value and not include_dormant:
            return None
        # The structural scope gate for by-ID reads lives in
        # _same_item_access (space inside the envelope). Privacy still runs
        # here: with the item's own scope as the request stand-in, labels
        # re-check the access grants (agent:<id> ∈ agent_ids, space:<id> ∈
        # allowed_space_ids, restricted → admin, custom → granted, entity →
        # consent) instead of comparing the item's scope with itself.
        item_scope = _item_scope(item)
        if not evaluate_privacy(revision.privacy_labels, item_scope, item_scope, access):
            return None
        return item, (
            _require_item_content_access(tx, access, item, revision=revision)
            if revision.promotion_target_id
            else revision
        )

    def list_items(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        statuses: tuple[str, ...] = ("active",),
        kind: str | None = None,
        space_id: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[tuple[FocusItemCurrent, FocusRevision]]:
        with self._uow.read() as tx:
            return self.list_items_in_tx(
                tx,
                access,
                agent_id=agent_id,
                statuses=statuses,
                kind=kind,
                space_id=space_id,
                session_id=session_id,
                limit=limit,
            )

    def list_items_in_tx(
        self,
        tx: Transaction,
        access: AccessContext,
        *,
        agent_id: str,
        statuses: tuple[str, ...] = ("active",),
        kind: str | None = None,
        space_id: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[tuple[FocusItemCurrent, FocusRevision]]:
        _authorize_scope(tx, access, agent_id=agent_id, space_id=space_id, session_id=session_id)
        rows = tx.focus.items_for_agent(
            access.tenant_id, agent_id, statuses=statuses, kind=kind, limit=limit
        )
        request = Scope(
            tenant_id=access.tenant_id,
            agent_id=agent_id,
            space_group_id=None,
            space_id=space_id,
            session_id=session_id,
        )
        now_us = self._clock.now_us()
        visible: list[tuple[FocusItemCurrent, FocusRevision]] = []
        for item in rows:
            if tx.is_tombstoned(item.tenant_id, "focus_item", item.id):
                continue
            revision = tx.focus.current_revision_row(item.id)
            if revision.expires_us is not None and revision.expires_us <= now_us:
                continue
            if revision.status not in statuses:
                continue
            data_scope = Scope(
                tenant_id=item.tenant_id,
                agent_id=item.agent_id,
                space_group_id=item.space_group_id,
                space_id=item.space_id,
                session_id=item.session_id,
            )
            if not scope_allows(data_scope, request):
                continue
            if not evaluate_privacy(revision.privacy_labels, data_scope, request, access):
                continue
            visible.append(
                (
                    item,
                    _require_item_content_access(tx, access, item, revision=revision)
                    if revision.promotion_target_id
                    else revision,
                )
            )
        return visible

    def history(
        self, access: AccessContext, item_id: str, *, limit: int = 100
    ) -> list[FocusRevision]:
        with self._uow.read() as tx:
            item = tx.focus.get(item_id)
            _require_item_content_access(tx, access, item)
            revisions = list(tx.focus.history(item.id, limit=limit))
            return [
                _require_item_content_access(tx, access, item, revision=revision)
                for revision in revisions
            ]


def _item_scope(item: FocusItemCurrent) -> Scope:
    return Scope(
        tenant_id=item.tenant_id,
        agent_id=item.agent_id,
        space_group_id=item.space_group_id,
        space_id=item.space_id,
        session_id=item.session_id,
    )


def _require_item_content_access(
    tx: Transaction,
    access: AccessContext,
    item: FocusItemCurrent,
    *,
    revision: FocusRevision | None = None,
    managed: bool = False,
) -> FocusRevision:
    """Authorize one content-bearing Focus read or mutation at call time.

    Idempotent replays pass the exact historical revision returned by the
    first execution. Scope grants, Privacy and Tombstones are current security
    state, so they are deliberately re-evaluated instead of cached.
    """

    _same_item_access(tx, access, item)
    if tx.is_tombstoned(item.tenant_id, "focus_item", item.id):
        raise NotFoundError("focus item not found")
    current = tx.focus.current_revision_row(item.id)
    checked = revision or current
    if checked.item_id != item.id or checked.tenant_id != item.tenant_id:
        raise ConflictError("focus revision does not belong to the requested item")
    item_scope = _item_scope(item)
    for candidate in (current, checked):
        labels = tuple(
            label for label in candidate.privacy_labels if not managed or label != "restricted"
        )
        if not evaluate_privacy(labels, item_scope, item_scope, access):
            raise AccessDeniedError("focus item's privacy labels are outside the access context")
    if checked.promotion_target_id and checked.promotion_target_type:
        from iris_memory_core.application.promotion import require_promotion_references

        try:
            require_promotion_references(
                tx,
                access,
                item_scope,
                (
                    {
                        "resource_type": checked.promotion_target_type,
                        "resource_id": checked.promotion_target_id,
                    },
                ),
            )
        except (NotFoundError, AccessDeniedError, ScopeViolationError):
            return replace(checked, promotion_target_id=None, promotion_target_type=None)
    return checked


def _same_item_access(tx: Transaction, access: AccessContext, item: FocusItemCurrent) -> None:
    """By-ID gate for get/activate/transition/history (tenant + agent + SPACE).

    A by-ID caller expresses no request scope, so the item's own scope dims
    must lie inside the access envelope: a space/session-scoped item is only
    reachable when its space is authorized. Comparing the item's scope with
    itself (as an earlier draft did) is always true and authorizes nothing.
    """
    if item.tenant_id != access.tenant_id:
        raise AccessDeniedError("focus item belongs to another tenant")
    if item.agent_id not in access.agent_ids:
        raise AccessDeniedError("focus item's agent is outside the access context")
    if item.space_id is not None and item.space_id not in access.allowed_space_ids:
        raise AccessDeniedError("focus item's space is outside the access context")


__all__ = [
    "FocusCapacityPolicy",
    "FocusCreateResult",
    "FocusMaintenanceReport",
    "FocusService",
]
