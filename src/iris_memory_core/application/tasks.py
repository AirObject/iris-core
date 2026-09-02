"""Task application service (§11, Phase 4.2/4.3).

Tasks use immutable revisions plus a Current Pointer (ADR-0004). Every
modification runs inside one transaction that writes the revision, CASes the
pointer, advances the watermark, writes audit and enqueues the change job
(§16.1). Conversation/background origins create ``proposed`` tasks only;
activation demands an explicit tool call, a deterministic policy or an
administrator (§11.5). Step completion is an independent transition and a
step declaring ``expected_effect`` must cite committed Observations. This
service NEVER interprets delivery/ACK/expiry as completion.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from iris_memory_core.application.ports import (
    Clock,
    IdempotencyRunner,
    Transaction,
    UnitOfWork,
)
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.application.write_support import (
    authorize_scope,
    enqueue_change_job,
    parse_privacy_labels,
    parse_source_refs,
    require_same_tenant_agent,
    require_surface_online,
    require_surface_online_in_tx,
)
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    ConflictError,
    IdempotencyUnavailableError,
    InvalidRequestError,
    InvalidTransitionError,
    NotFoundError,
    TaskDependencyCycleError,
    require_reason,
)
from iris_memory_core.domain.event import DEFAULT_EVENT_TTL_US
from iris_memory_core.domain.hashing import request_fingerprint
from iris_memory_core.domain.note import NoteCurrent, NoteRevision
from iris_memory_core.domain.observation import StoredObservation
from iris_memory_core.domain.privacy import evaluate_privacy
from iris_memory_core.domain.schedule import ScheduleSpec
from iris_memory_core.domain.scope import Scope, scope_allows
from iris_memory_core.domain.task import (
    EVIDENCE_RESOURCE_TYPES,
    TASK_ORIGINS,
    DependencyCondition,
    TaskCurrent,
    TaskDependencyEdge,
    TaskRevision,
    TaskStatus,
    TaskStepCurrent,
    TaskStepRevision,
    TaskStepStatus,
    TaskTriggerCurrent,
    TaskTriggerRevision,
    TriggerKind,
    compute_step_status,
    evaluate_state_condition,
    may_activate,
    occurrence_key_for_observation,
    occurrence_key_for_state,
    occurrence_key_for_time,
    occurrence_key_for_transition,
    parse_trigger_schedule_spec,
    task_scope_key,
    validate_condition_spec,
    validate_step_transition,
    validate_task_transition,
    validate_trigger_common,
    would_create_cycle,
)

#: One trigger-scan observation batch. The cursor only ever advances past
#: rows this batch actually read, so a larger backlog converges over runs.
OBSERVATION_SCAN_BATCH = 500


@dataclass(frozen=True, slots=True)
class TaskWriteResult:
    task_id: str
    revision: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class StepWriteResult:
    step_id: str
    revision: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class TriggerWriteResult:
    trigger_id: str
    revision: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class TriggerScanReport:
    """Low-cardinality outcome of one trigger scan."""

    occurrences_created: int
    events_created: int
    occurrences_absorbed: int
    skipped: int


def _task_scope(task: TaskCurrent) -> Scope:
    return Scope(
        tenant_id=task.tenant_id,
        agent_id=task.agent_id,
        space_group_id=task.space_group_id,
        space_id=task.space_id,
        session_id=task.session_id,
    )


def _require_task_access(
    tx: Transaction,
    access: AccessContext,
    task: TaskCurrent,
    *,
    revision_id: str | None = None,
) -> TaskRevision:
    require_same_tenant_agent(
        access, tenant_id=task.tenant_id, agent_id=task.agent_id, space_id=task.space_id
    )
    if tx.is_tombstoned(task.tenant_id, "task", task.id):
        raise NotFoundError("task not found")
    current = tx.tasks.current_task_revision_row(task.id)
    checked = tx.tasks.get_task_revision(revision_id) if revision_id else current
    if checked.task_id != task.id or checked.tenant_id != task.tenant_id:
        raise ConflictError("task revision does not belong to the requested task")
    task_scope = _task_scope(task)
    for candidate in (current, checked):
        if not evaluate_privacy(candidate.privacy_labels, task_scope, task_scope, access):
            raise AccessDeniedError("task's privacy labels are outside the access context")
    return checked


def _require_step_access(
    tx: Transaction,
    access: AccessContext,
    step: TaskStepCurrent,
    *,
    revision_id: str | None = None,
) -> TaskStepRevision:
    task = tx.tasks.get_task(step.task_id)
    require_same_tenant_agent(
        access, tenant_id=step.tenant_id, agent_id=task.agent_id, space_id=task.space_id
    )
    if tx.is_tombstoned(step.tenant_id, "task", task.id):
        raise NotFoundError("task not found")
    if tx.is_tombstoned(step.tenant_id, "task_step", step.id):
        raise NotFoundError("task step not found")
    current = tx.tasks.current_step_revision_row(step.id)
    checked = tx.tasks.get_step_revision(revision_id) if revision_id else current
    if checked.step_id != step.id or checked.tenant_id != step.tenant_id:
        raise ConflictError("step revision does not belong to the requested step")
    task_scope = _task_scope(task)
    for candidate in (current, checked):
        if not evaluate_privacy(candidate.privacy_labels, task_scope, task_scope, access):
            raise AccessDeniedError("step privacy labels are outside the access context")
    return checked


class TaskService:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        *,
        idempotency: IdempotencyRunner | None = None,
        event_ttl_us: int = DEFAULT_EVENT_TTL_US,
        surface: SurfaceCoordinatorService | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._idempotency = idempotency
        # Optional §25.3 online-plane coordinator: under ``required`` every
        # application-plane write below must present the caller's live lease.
        self._surface = surface
        # The trigger scan creates CognitiveEvents; it must inherit the SAME
        # clock and event TTL configuration as the event service, not fall
        # back to the system clock and the default TTL.
        self._event_ttl_us = event_ttl_us

    # -- task create/list/patch/transition ---------------------------------

    def create(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        title: str,
        goal: str = "",
        origin: str = "explicit_tool",
        owner_kind: str = "agent",
        owner_entity_id: str | None = None,
        priority: int = 5,
        due_at_us: int | None = None,
        next_action: str | None = None,
        space_id: str | None = None,
        session_id: str | None = None,
        privacy_labels: list[str] | None = None,
        source_refs: list[dict[str, Any]] | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        idempotency_key: str | None = None,
    ) -> TaskWriteResult:
        if idempotency_key is None:
            raise InvalidRequestError("task creation requires an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        if origin not in TASK_ORIGINS:
            raise InvalidRequestError(f"unknown task origin: {origin!r}")
        if not title or len(title) > 500:
            raise InvalidRequestError("title must be 1..500 characters")
        if len(goal) > 8_000:
            raise InvalidRequestError("goal must be at most 8000 characters")
        if not 0 <= priority <= 9:
            raise InvalidRequestError("priority must be within 0..9")
        refs = parse_source_refs(source_refs)
        labels = parse_privacy_labels(privacy_labels)
        # Authorize the named Agent/Space/Session and privacy envelope before
        # consulting Surface state; otherwise an ungranted Agent becomes a
        # lease-mode oracle. The write transaction repeats the checks.
        with self._uow.read() as tx:
            request_scope = authorize_scope(
                tx,
                access,
                agent_id=agent_id,
                space_id=space_id,
                session_id=session_id,
            )
            if not evaluate_privacy(labels, request_scope, request_scope, access):
                raise AccessDeniedError("task privacy labels are outside the access context")
        # §25.3 gate BEFORE the idempotency cache; the proof is a per-call
        # credential (validated on replays too), never fingerprint material
        # — a retry after a lease rotation must replay, not collide.
        require_surface_online(
            self._surface,
            access.tenant_id,
            agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        payload: dict[str, Any] = {
            "agent_id": agent_id,
            "title": title,
            "goal": goal,
            "origin": origin,
            "owner_kind": owner_kind,
            "owner_entity_id": owner_entity_id,
            "priority": priority,
            "due_at_us": due_at_us,
            "next_action": next_action,
            "space_id": space_id,
            "session_id": session_id,
            "privacy_labels": list(labels),
            "source_refs": [dict(ref) for ref in refs],
        }
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="task:create",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint("task:create", payload),
            execute=lambda tx: self._execute_create(
                tx,
                access,
                payload,
                lease_id=lease_id,
                lease_epoch=lease_epoch,
            ),
        )
        body = json.loads(result.body)
        with self._uow.read() as tx:
            task = tx.tasks.get_task(body["task_id"])
            _require_task_access(tx, access, task)
        return TaskWriteResult(
            task_id=body["task_id"], revision=int(body["revision"]), replayed=result.replayed
        )

    def _execute_create(
        self,
        tx: Transaction,
        access: AccessContext,
        payload: dict[str, Any],
        *,
        lease_id: str | None,
        lease_epoch: int | None,
    ) -> tuple[str, str, list[str]]:
        scope = authorize_scope(
            tx,
            access,
            agent_id=payload["agent_id"],
            space_id=payload["space_id"],
            session_id=payload["session_id"],
        )
        if not evaluate_privacy(tuple(payload["privacy_labels"]), scope, scope, access):
            raise AccessDeniedError("task privacy labels are outside the access context")
        lease_warning = require_surface_online_in_tx(
            self._surface,
            tx,
            access.tenant_id,
            payload["agent_id"],
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        origin = payload["origin"]
        # §11.5: conversation/background extraction is pinned to proposed.
        status = (
            TaskStatus.PROPOSED.value
            if not may_activate(origin=origin, is_admin=access.admin)
            else TaskStatus.ACTIVE.value
        )
        if payload["owner_kind"] == "entity" and not payload["owner_entity_id"]:
            raise InvalidRequestError("owner_kind entity requires owner_entity_id")
        scope_key = task_scope_key(
            scope.tenant_id,
            scope.agent_id or "",
            scope.space_group_id,
            scope.space_id,
            scope.session_id,
        )
        task_id = tx.tasks.insert_task(
            tenant_id=scope.tenant_id,
            agent_id=scope.agent_id or "",
            space_group_id=scope.space_group_id,
            space_id=scope.space_id,
            session_id=scope.session_id,
            scope_key=scope_key,
            parent_task_id=None,
            title=payload["title"],
            owner_kind=payload["owner_kind"],
            owner_entity_id=payload["owner_entity_id"],
            status=status,
            priority=payload["priority"],
            due_at_us=payload["due_at_us"],
        )
        revision_id = tx.tasks.insert_task_revision(
            task_id=task_id,
            tenant_id=scope.tenant_id,
            revision=1,
            title=payload["title"],
            goal=payload["goal"],
            owner_kind=payload["owner_kind"],
            owner_entity_id=payload["owner_entity_id"],
            privacy_labels=tuple(payload["privacy_labels"]),
            source_refs=tuple(payload["source_refs"]),
            status=status,
            priority=payload["priority"],
            next_action=payload["next_action"],
            progress_note=None,
            due_at_us=payload["due_at_us"],
            completed_us=None,
            created_by=f"access:{access.app_instance_id}",
        )
        if tx.tasks.set_initial_task_pointer(task_id, revision_id) != 1:
            raise ConflictError("task creation raced inside the transaction")
        tx.advance_watermark(scope.tenant_id, scope.agent_id or "", [("task", task_id, 1)])
        tx.audit(
            tenant_id=scope.tenant_id,
            actor=f"access:{access.app_instance_id}",
            action="task.created",
            resource_type="task",
            resource_id=task_id,
            reason_code=f"origin:{origin}",
            details={"origin": origin, "status": status, "lease_warning": lease_warning},
            revision=1,
        )
        enqueue_change_job(
            tx,
            tenant_id=scope.tenant_id,
            agent_id=scope.agent_id or "",
            job_kind="task.changed",
            aggregate_type="task",
            aggregate_id=task_id,
            source_revision=1,
            payload={"task_id": task_id, "revision": 1},
        )
        return (
            "task.created",
            json.dumps({"task_id": task_id, "revision": 1}),
            [f"task:{task_id}"],
        )

    def patch(
        self,
        access: AccessContext,
        task_id: str,
        *,
        expected_revision: int,
        title: str | None = None,
        goal: str | None = None,
        priority: int | None = None,
        next_action: str | None = None,
        progress_note: str | None = None,
        due_at_us: int | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        idempotency_key: str | None = None,
    ) -> TaskRevision:
        if idempotency_key is None:
            raise InvalidRequestError("task patches require an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        with self._uow.read() as tx:
            task = tx.tasks.get_task(task_id)
            # Pre-read resolves the task's own tenant/agent for the §25.3
            # gate, which runs BEFORE the idempotency cache (round-4 P0).
            _require_task_access(tx, access, task)
        require_surface_online(
            self._surface,
            task.tenant_id,
            task.agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        payload = {
            "task_id": task_id,
            "expected_revision": expected_revision,
            "title": title,
            "goal": goal,
            "priority": priority,
            "next_action": next_action,
            "progress_note": progress_note,
            "due_at_us": due_at_us,
        }
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="task:patch",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint("task:patch", payload),
            execute=lambda tx: self._execute_patch(
                tx,
                access,
                payload,
                lease_id=lease_id,
                lease_epoch=lease_epoch,
            ),
        )
        body = json.loads(result.body)
        with self._uow.read() as tx:
            task = tx.tasks.get_task(task_id)
            return _require_task_access(tx, access, task, revision_id=body["revision_id"])

    def _execute_patch(
        self,
        tx: Transaction,
        access: AccessContext,
        payload: dict[str, Any],
        *,
        lease_id: str | None,
        lease_epoch: int | None,
    ) -> tuple[str, str, list[str]]:
        task = tx.tasks.get_task(payload["task_id"])
        current = _require_task_access(tx, access, task)
        lease_warning = require_surface_online_in_tx(
            self._surface,
            tx,
            task.tenant_id,
            task.agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        if task.status not in (
            TaskStatus.PROPOSED.value,
            TaskStatus.ACTIVE.value,
            TaskStatus.WAITING.value,
            TaskStatus.BLOCKED.value,
        ):
            raise InvalidTransitionError(
                f"task in status {task.status!r} cannot be edited",
                details={"from": task.status},
            )
        new_title = payload["title"] if payload["title"] is not None else current.title
        new_goal = payload["goal"] if payload["goal"] is not None else current.goal
        new_priority = payload["priority"] if payload["priority"] is not None else current.priority
        new_next = (
            payload["next_action"] if payload["next_action"] is not None else current.next_action
        )
        new_progress = (
            payload["progress_note"]
            if payload["progress_note"] is not None
            else current.progress_note
        )
        new_due = payload["due_at_us"] if payload["due_at_us"] is not None else current.due_at_us
        if not new_title or len(new_title) > 500:
            raise InvalidRequestError("title must be 1..500 characters")
        if not 0 <= new_priority <= 9:
            raise InvalidRequestError("priority must be within 0..9")
        revision = task.current_revision + 1
        revision_id = tx.tasks.insert_task_revision(
            task_id=task.id,
            tenant_id=task.tenant_id,
            revision=revision,
            title=new_title,
            goal=new_goal,
            owner_kind=current.owner_kind,
            owner_entity_id=current.owner_entity_id,
            privacy_labels=current.privacy_labels,
            source_refs=current.source_refs,
            status=task.status,
            priority=new_priority,
            next_action=new_next,
            progress_note=new_progress,
            due_at_us=new_due,
            completed_us=current.completed_us,
            created_by=f"access:{access.app_instance_id}",
        )
        if (
            tx.tasks.advance_task_pointer(
                task.id,
                expected_revision=payload["expected_revision"],
                revision=revision,
                revision_id=revision_id,
                status=task.status,
                next_action=new_next,
                next_action_set=True,
                due_at_us=new_due,
                due_at_set=True,
            )
            != 1
        ):
            tx.tasks.raise_task_pointer_mismatch(task.id, payload["expected_revision"])
        tx.advance_watermark(task.tenant_id, task.agent_id, [("task", task.id, revision)])
        tx.audit(
            tenant_id=task.tenant_id,
            actor=f"access:{access.app_instance_id}",
            action="task.patched",
            resource_type="task",
            resource_id=task.id,
            reason_code="plan_edit",
            details={"revision": revision, "lease_warning": lease_warning},
            revision=revision,
        )
        enqueue_change_job(
            tx,
            tenant_id=task.tenant_id,
            agent_id=task.agent_id,
            job_kind="task.changed",
            aggregate_type="task",
            aggregate_id=task.id,
            source_revision=revision,
            payload={"task_id": task.id, "revision": revision},
        )
        return (
            "task.patched",
            json.dumps({"revision_id": revision_id}),
            [f"task:{task.id}"],
        )

    def transition(
        self,
        access: AccessContext,
        task_id: str,
        target: str,
        *,
        expected_revision: int,
        origin: str = "explicit_tool",
        reason: str | None = None,
        completion_evidence_refs: list[dict[str, Any]] | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        idempotency_key: str | None = None,
    ) -> TaskRevision:
        """activate | wait | block | complete | cancel | archive with CAS."""
        if idempotency_key is None:
            raise InvalidRequestError("task transitions require an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        reason_code = require_reason(reason)
        alias = {
            "activate": "active",
            "wait": "waiting",
            "block": "blocked",
            "complete": "completed",
            "cancel": "cancelled",
            "archive": "archived",
            "unblock": "active",
            "resume": "active",
        }
        canonical = alias.get(target, target)
        try:
            TaskStatus(canonical)
        except ValueError:
            raise InvalidRequestError(f"unknown task status: {target!r}") from None
        with self._uow.read() as tx:
            task = tx.tasks.get_task(task_id)
            # Same pre-read + pre-cache gate as patch (round-4 P0).
            _require_task_access(tx, access, task)
        require_surface_online(
            self._surface,
            task.tenant_id,
            task.agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        payload = {
            "task_id": task_id,
            "target": canonical,
            "expected_revision": expected_revision,
            "origin": origin,
            "reason": reason_code,
            "completion_evidence_refs": completion_evidence_refs or [],
        }
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="task:transition",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint("task:transition", payload),
            execute=lambda tx: self._execute_transition(
                tx,
                access,
                payload,
                lease_id=lease_id,
                lease_epoch=lease_epoch,
            ),
        )
        body = json.loads(result.body)
        with self._uow.read() as tx:
            task = tx.tasks.get_task(task_id)
            return _require_task_access(tx, access, task, revision_id=body["revision_id"])

    def _execute_transition(
        self,
        tx: Transaction,
        access: AccessContext,
        payload: dict[str, Any],
        *,
        lease_id: str | None,
        lease_epoch: int | None,
    ) -> tuple[str, str, list[str]]:
        task = tx.tasks.get_task(payload["task_id"])
        current = _require_task_access(tx, access, task)
        lease_warning = require_surface_online_in_tx(
            self._surface,
            tx,
            task.tenant_id,
            task.agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        target = payload["target"]
        try:
            validate_task_transition(task.status, target)
        except InvalidRequestError as error:
            # Concurrent identical request already moved the aggregate: the CAS
            # is the authoritative arbiter, so a stale expected_revision reports
            # revision_mismatch rather than invalid_transition.
            if task.status == target and task.current_revision > payload["expected_revision"]:
                tx.tasks.raise_task_pointer_mismatch(task.id, payload["expected_revision"])
            raise InvalidTransitionError(
                str(error), details={"from": task.status, "to": target}
            ) from error
        if (
            target == TaskStatus.ACTIVE.value
            and task.status == TaskStatus.PROPOSED.value
            and not may_activate(origin=payload["origin"], is_admin=access.admin)
        ):
            # §11.5: activation is a privilege, checked inside the write tx.
            raise AccessDeniedError(
                "task activation requires an explicit tool, deterministic policy or admin",
            )
        now_us = self._clock.now_us()
        completed_us: int | None = None
        # Details carry only non-sensitive metadata; the caller's reason
        # lives in the audit reason_code column, never duplicated here.
        details: dict[str, object] = {}
        if target == TaskStatus.COMPLETED.value:
            steps = tx.tasks.steps_for_task(task.id)
            unresolved = [
                step.stable_key
                for step in steps
                if step.status
                not in (
                    TaskStepStatus.COMPLETED.value,
                    TaskStepStatus.SKIPPED.value,
                    TaskStepStatus.CANCELLED.value,
                )
            ]
            if unresolved:
                raise InvalidTransitionError(
                    "task completion requires every step to be completed, skipped or cancelled",
                    details={"unresolved_steps": unresolved},
                )
            refs = parse_source_refs(payload["completion_evidence_refs"])
            if refs:
                _validate_evidence(tx, task, refs, access)
                details["evidence_count"] = len(refs)
            completed_us = now_us
        revision = task.current_revision + 1
        revision_id = tx.tasks.insert_task_revision(
            task_id=task.id,
            tenant_id=task.tenant_id,
            revision=revision,
            title=current.title,
            goal=current.goal,
            owner_kind=current.owner_kind,
            owner_entity_id=current.owner_entity_id,
            privacy_labels=current.privacy_labels,
            source_refs=current.source_refs,
            status=target,
            priority=current.priority,
            next_action=current.next_action,
            progress_note=current.progress_note,
            due_at_us=current.due_at_us,
            completed_us=completed_us,
            created_by=f"access:{access.app_instance_id}",
        )
        if (
            tx.tasks.advance_task_pointer(
                task.id,
                expected_revision=payload["expected_revision"],
                revision=revision,
                revision_id=revision_id,
                status=target,
                completed_us=completed_us,
                completed_us_set=completed_us is not None,
            )
            != 1
        ):
            tx.tasks.raise_task_pointer_mismatch(task.id, payload["expected_revision"])
        tx.advance_watermark(task.tenant_id, task.agent_id, [("task", task.id, revision)])
        tx.audit(
            tenant_id=task.tenant_id,
            actor=f"access:{access.app_instance_id}",
            action=f"task.{target}",
            resource_type="task",
            resource_id=task.id,
            reason_code=payload["reason"],
            details={**details, "lease_warning": lease_warning},
            revision=revision,
        )
        enqueue_change_job(
            tx,
            tenant_id=task.tenant_id,
            agent_id=task.agent_id,
            job_kind="task.changed",
            aggregate_type="task",
            aggregate_id=task.id,
            source_revision=revision,
            payload={"task_id": task.id, "revision": revision},
        )
        # Task transitions may arm task_transition triggers on the NEXT scan;
        # nothing here fires events inline (the scan is the only producer).
        return (
            f"task.{target}",
            json.dumps({"revision_id": revision_id}),
            [f"task:{task.id}"],
        )

    # -- steps ----------------------------------------------------------------

    def create_step(
        self,
        access: AccessContext,
        task_id: str,
        *,
        stable_key: str,
        title: str,
        description: str | None = None,
        ordinal: int = 0,
        expected_effect: str | None = None,
        privacy_labels: list[str] | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        idempotency_key: str | None = None,
    ) -> StepWriteResult:
        if idempotency_key is None:
            raise InvalidRequestError("task step creation requires an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        if not stable_key or len(stable_key) > 256:
            raise InvalidRequestError("stable_key must be 1..256 characters")
        if not title or len(title) > 500:
            raise InvalidRequestError("title must be 1..500 characters")
        if ordinal < 0:
            raise InvalidRequestError("ordinal must be non-negative")
        if expected_effect is not None and len(expected_effect) > 2_000:
            raise InvalidRequestError("expected_effect must be at most 2000 characters")
        labels = parse_privacy_labels(privacy_labels)
        with self._uow.read() as tx:
            task = tx.tasks.get_task(task_id)
            # Pre-read resolves the owning task's tenant/agent for the §25.3
            # gate, which runs BEFORE the idempotency cache (round-4 P0).
            _require_task_access(tx, access, task)
        require_surface_online(
            self._surface,
            task.tenant_id,
            task.agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        payload = {
            "task_id": task_id,
            "stable_key": stable_key,
            "title": title,
            "description": description,
            "ordinal": ordinal,
            "expected_effect": expected_effect,
            "privacy_labels": list(labels),
        }
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="task:create_step",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint("task:create_step", payload),
            execute=lambda tx: self._execute_create_step(
                tx,
                access,
                payload,
                lease_id=lease_id,
                lease_epoch=lease_epoch,
            ),
        )
        body = json.loads(result.body)
        with self._uow.read() as tx:
            step = tx.tasks.get_step(body["step_id"])
            _require_step_access(tx, access, step)
        return StepWriteResult(
            step_id=body["step_id"], revision=int(body["revision"]), replayed=result.replayed
        )

    def _execute_create_step(
        self,
        tx: Transaction,
        access: AccessContext,
        payload: dict[str, Any],
        *,
        lease_id: str | None,
        lease_epoch: int | None,
    ) -> tuple[str, str, list[str]]:
        task = tx.tasks.get_task(payload["task_id"])
        _require_task_access(tx, access, task)
        lease_warning = require_surface_online_in_tx(
            self._surface,
            tx,
            task.tenant_id,
            task.agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        if task.status not in (
            TaskStatus.PROPOSED.value,
            TaskStatus.ACTIVE.value,
            TaskStatus.WAITING.value,
            TaskStatus.BLOCKED.value,
        ):
            raise InvalidTransitionError(
                f"task in status {task.status!r} cannot take new steps",
                details={"from": task.status},
            )
        if tx.tasks.find_step_by_key(task.id, payload["stable_key"]) is not None:
            raise ConflictError(
                "stable_key already exists in this task",
                details={"stable_key": payload["stable_key"]},
            )
        step_id = tx.tasks.insert_step(
            task_id=task.id,
            tenant_id=task.tenant_id,
            stable_key=payload["stable_key"],
            title=payload["title"],
            ordinal=payload["ordinal"],
            status=TaskStepStatus.PENDING.value,
        )
        revision_id = tx.tasks.insert_step_revision(
            step_id=step_id,
            task_id=task.id,
            tenant_id=task.tenant_id,
            revision=1,
            stable_key=payload["stable_key"],
            title=payload["title"],
            description=payload["description"],
            privacy_labels=tuple(payload["privacy_labels"]),
            status=TaskStepStatus.PENDING.value,
            ordinal=payload["ordinal"],
            expected_effect=payload["expected_effect"],
            completion_evidence_refs=(),
            started_us=None,
            completed_us=None,
            created_by=f"access:{access.app_instance_id}",
        )
        if tx.tasks.set_initial_step_pointer(step_id, revision_id) != 1:
            raise ConflictError("task step creation raced inside the transaction")
        tx.advance_watermark(task.tenant_id, task.agent_id, [("task_step", step_id, 1)])
        tx.audit(
            tenant_id=task.tenant_id,
            actor=f"access:{access.app_instance_id}",
            action="task.step_created",
            resource_type="task_step",
            resource_id=step_id,
            reason_code="plan_edit",
            details={
                "stable_key": payload["stable_key"],
                "ordinal": payload["ordinal"],
                "lease_warning": lease_warning,
            },
            revision=1,
        )
        enqueue_change_job(
            tx,
            tenant_id=task.tenant_id,
            agent_id=task.agent_id,
            job_kind="task.changed",
            aggregate_type="task_step",
            aggregate_id=step_id,
            source_revision=1,
            payload={"task_id": task.id, "step_id": step_id, "revision": 1},
        )
        # Dependencies added BEFORE this step may already release it: recompute
        # readiness deterministically from the stored graph.
        self._recompute_step(tx, task, step_id, actor=f"access:{access.app_instance_id}")
        current_revision = tx.tasks.get_step(step_id).current_revision
        return (
            "task.step_created",
            json.dumps({"step_id": step_id, "revision": current_revision}),
            [f"task_step:{step_id}"],
        )

    def transition_step(
        self,
        access: AccessContext,
        task_id: str,
        step_id: str,
        target: str,
        *,
        expected_revision: int,
        reason: str | None = None,
        completion_evidence_refs: list[dict[str, Any]] | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        idempotency_key: str | None = None,
    ) -> TaskStepRevision:
        """start | wait | block | complete | skip | cancel | requeue with CAS."""
        if idempotency_key is None:
            raise InvalidRequestError("task step transitions require an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        reason_code = require_reason(reason)
        alias = {
            "start": "in_progress",
            "wait": "waiting",
            "block": "blocked",
            "complete": "completed",
            "skip": "skipped",
            "cancel": "cancelled",
            "requeue": "pending",
        }
        canonical = alias.get(target, target)
        try:
            TaskStepStatus(canonical)
        except ValueError:
            raise InvalidRequestError(f"unknown task step status: {target!r}") from None
        with self._uow.read() as tx:
            step = tx.tasks.get_step(step_id)
            if step.task_id != task_id:
                raise InvalidRequestError("step does not belong to the given task")
            # Pre-read resolves the owning task's agent for the §25.3 gate,
            # which runs BEFORE the idempotency cache (round-4 P0).
            _require_step_access(tx, access, step)
            step_agent_id = tx.tasks.get_task(step.task_id).agent_id
        require_surface_online(
            self._surface,
            access.tenant_id,
            step_agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        payload = {
            "task_id": task_id,
            "step_id": step_id,
            "target": canonical,
            "expected_revision": expected_revision,
            "reason": reason_code,
            "completion_evidence_refs": completion_evidence_refs or [],
        }
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="task:transition_step",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint("task:transition_step", payload),
            execute=lambda tx: self._execute_transition_step(
                tx,
                access,
                payload,
                lease_id=lease_id,
                lease_epoch=lease_epoch,
            ),
        )
        body = json.loads(result.body)
        with self._uow.read() as tx:
            step = tx.tasks.get_step(step_id)
            return _require_step_access(tx, access, step, revision_id=body["revision_id"])

    def _execute_transition_step(
        self,
        tx: Transaction,
        access: AccessContext,
        payload: dict[str, Any],
        *,
        lease_id: str | None,
        lease_epoch: int | None,
    ) -> tuple[str, str, list[str]]:
        step = tx.tasks.get_step(payload["step_id"])
        if step.task_id != payload["task_id"]:
            raise InvalidRequestError("step does not belong to the given task")
        current = _require_step_access(tx, access, step)
        task = tx.tasks.get_task(step.task_id)
        lease_warning = require_surface_online_in_tx(
            self._surface,
            tx,
            task.tenant_id,
            task.agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        target = payload["target"]
        try:
            validate_step_transition(step.status, target)
        except InvalidRequestError as error:
            if step.status == target and step.current_revision > payload["expected_revision"]:
                tx.tasks.raise_step_pointer_mismatch(step.id, payload["expected_revision"])
            raise InvalidTransitionError(
                str(error), details={"from": step.status, "to": target}
            ) from error
        now_us = self._clock.now_us()
        evidence: tuple[dict[str, object], ...] = ()
        started_us: int | None = None
        completed_us: int | None = None
        if target == TaskStepStatus.READY.value:
            raise InvalidTransitionError(
                "step readiness is derived from dependencies, not set manually",
                details={"from": step.status, "to": target},
            )
        if target == TaskStepStatus.IN_PROGRESS.value:
            started_us = now_us
        if target in (
            TaskStepStatus.COMPLETED.value,
            TaskStepStatus.SKIPPED.value,
            TaskStepStatus.CANCELLED.value,
        ):
            completed_us = now_us
        if target == TaskStepStatus.COMPLETED.value and current.expected_effect:
            # §11.2: a step with an external effect completes only with real
            # committed evidence — never with a delivery, an ACK or an expiry.
            evidence = parse_source_refs(payload["completion_evidence_refs"])
            if not evidence:
                raise InvalidRequestError(
                    "completing a step with expected_effect requires completion_evidence_refs"
                )
            _validate_evidence(tx, task, evidence, access)
        elif payload["completion_evidence_refs"]:
            raise InvalidRequestError(
                "completion_evidence_refs is only valid when completing a step"
            )
        revision = step.current_revision + 1
        revision_id = tx.tasks.insert_step_revision(
            step_id=step.id,
            task_id=step.task_id,
            tenant_id=step.tenant_id,
            revision=revision,
            stable_key=current.stable_key,
            title=current.title,
            description=current.description,
            privacy_labels=current.privacy_labels,
            status=target,
            ordinal=current.ordinal,
            expected_effect=current.expected_effect,
            completion_evidence_refs=evidence or current.completion_evidence_refs,
            started_us=started_us if started_us is not None else current.started_us,
            completed_us=completed_us,
            created_by=f"access:{access.app_instance_id}",
        )
        if (
            tx.tasks.advance_step_pointer(
                step.id,
                expected_revision=payload["expected_revision"],
                revision=revision,
                revision_id=revision_id,
                status=target,
                started_us=started_us,
                started_us_set=started_us is not None,
                completed_us=completed_us,
                completed_us_set=completed_us is not None,
            )
            != 1
        ):
            tx.tasks.raise_step_pointer_mismatch(step.id, payload["expected_revision"])
        tx.advance_watermark(
            step.tenant_id, _task_agent(tx, step), [("task_step", step.id, revision)]
        )
        details: dict[str, object] = {"reason": payload["reason"]}
        if evidence:
            details["evidence_count"] = len(evidence)
        if lease_warning is not None:
            details["lease_warning"] = lease_warning
        tx.audit(
            tenant_id=step.tenant_id,
            actor=f"access:{access.app_instance_id}",
            action=f"task.step_{target}",
            resource_type="task_step",
            resource_id=step.id,
            reason_code=payload["reason"],
            details=details,
            revision=revision,
        )
        enqueue_change_job(
            tx,
            tenant_id=step.tenant_id,
            agent_id=_task_agent(tx, step),
            job_kind="task.changed",
            aggregate_type="task_step",
            aggregate_id=step.id,
            source_revision=revision,
            payload={"task_id": step.task_id, "step_id": step.id, "revision": revision},
        )
        # Completing/skipping a step may release successors deterministically.
        self._recompute_successors(
            tx, task, step_id=step.id, actor=f"access:{access.app_instance_id}"
        )
        return (
            f"task.step_{target}",
            json.dumps({"revision_id": revision_id}),
            [f"task_step:{step.id}"],
        )

    # -- dependencies -----------------------------------------------------------

    def add_dependency(
        self,
        access: AccessContext,
        task_id: str,
        *,
        predecessor_step_id: str,
        successor_step_id: str,
        condition: str = DependencyCondition.COMPLETED.value,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        idempotency_key: str | None = None,
    ) -> TaskDependencyEdge:
        if idempotency_key is None:
            raise InvalidRequestError("dependency writes require an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        if condition not in (
            DependencyCondition.COMPLETED.value,
            DependencyCondition.COMPLETED_OR_SKIPPED.value,
        ):
            raise InvalidRequestError(f"unknown dependency condition: {condition!r}")
        with self._uow.read() as tx:
            task = tx.tasks.get_task(task_id)
            # Pre-read resolves the task's own tenant/agent for the §25.3
            # gate, which runs BEFORE the idempotency cache (round-4 P0).
            _require_task_access(tx, access, task)
        require_surface_online(
            self._surface,
            task.tenant_id,
            task.agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        payload = {
            "task_id": task_id,
            "predecessor_step_id": predecessor_step_id,
            "successor_step_id": successor_step_id,
            "condition": condition,
        }
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="task:add_dependency",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint("task:add_dependency", payload),
            execute=lambda tx: self._execute_add_dependency(
                tx,
                access,
                payload,
                lease_id=lease_id,
                lease_epoch=lease_epoch,
            ),
        )
        body = json.loads(result.body)
        with self._uow.read() as tx:
            edge = tx.tasks.get_dependency(body["dependency_id"])
            task = tx.tasks.get_task(edge.task_id)
            _require_task_access(tx, access, task)
            return edge

    def _execute_add_dependency(
        self,
        tx: Transaction,
        access: AccessContext,
        payload: dict[str, Any],
        *,
        lease_id: str | None,
        lease_epoch: int | None,
    ) -> tuple[str, str, list[str]]:
        task = tx.tasks.get_task(payload["task_id"])
        _require_task_access(tx, access, task)
        lease_warning = require_surface_online_in_tx(
            self._surface,
            tx,
            task.tenant_id,
            task.agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        predecessor = tx.tasks.get_step(payload["predecessor_step_id"])
        successor = tx.tasks.get_step(payload["successor_step_id"])
        # v1 (§11.3): dependencies stay INSIDE one task.
        if predecessor.task_id != task.id or successor.task_id != task.id:
            raise InvalidRequestError("v1 dependencies only connect steps of the same task (§11.3)")
        edges = [
            (edge.predecessor_step_id, edge.successor_step_id)
            for edge in tx.tasks.dependencies_for_task(task.id)
        ]
        if would_create_cycle(
            edges,
            predecessor_step_id=payload["predecessor_step_id"],
            successor_step_id=payload["successor_step_id"],
        ):
            raise TaskDependencyCycleError(
                "adding this dependency would create a cycle in the task's step graph"
            )
        dependency_id = tx.tasks.insert_dependency(
            task_id=task.id,
            tenant_id=task.tenant_id,
            predecessor_step_id=payload["predecessor_step_id"],
            successor_step_id=payload["successor_step_id"],
            condition=payload["condition"],
        )
        revision_id = tx.tasks.insert_dependency_revision(
            dependency_id=dependency_id,
            task_id=task.id,
            tenant_id=task.tenant_id,
            revision=1,
            predecessor_step_id=payload["predecessor_step_id"],
            successor_step_id=payload["successor_step_id"],
            condition=payload["condition"],
            created_by=f"access:{access.app_instance_id}",
        )
        if tx.tasks.set_initial_dependency_pointer(dependency_id, revision_id) != 1:
            raise ConflictError("dependency creation raced inside the transaction")
        tx.advance_watermark(task.tenant_id, task.agent_id, [("task_dependency", dependency_id, 1)])
        tx.audit(
            tenant_id=task.tenant_id,
            actor=f"access:{access.app_instance_id}",
            action="task.dependency_added",
            resource_type="task_dependency",
            resource_id=dependency_id,
            reason_code="plan_edit",
            details={
                "predecessor_step_id": payload["predecessor_step_id"],
                "successor_step_id": payload["successor_step_id"],
                "condition": payload["condition"],
                "lease_warning": lease_warning,
            },
            revision=1,
        )
        enqueue_change_job(
            tx,
            tenant_id=task.tenant_id,
            agent_id=task.agent_id,
            job_kind="task.changed",
            aggregate_type="task_dependency",
            aggregate_id=dependency_id,
            source_revision=1,
            payload={"task_id": task.id, "dependency_id": dependency_id, "revision": 1},
        )
        # The new edge may retract readiness of the successor: recompute it.
        self._recompute_step(
            tx, task, payload["successor_step_id"], actor=f"access:{access.app_instance_id}"
        )
        return (
            "task.dependency_added",
            json.dumps({"dependency_id": dependency_id}),
            [f"task_dependency:{dependency_id}"],
        )

    # -- triggers ----------------------------------------------------------------

    def create_trigger(
        self,
        access: AccessContext,
        task_id: str,
        *,
        kind: str,
        schedule_spec: dict[str, Any] | None = None,
        condition_spec: dict[str, Any] | None = None,
        task_step_id: str | None = None,
        timezone_name: str = "UTC",
        catch_up_policy: str = "all",
        misfire_grace_us: int = 86_400_000_000,
        max_occurrences_per_run: int = 100,
        enabled: bool = True,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        idempotency_key: str | None = None,
    ) -> TriggerWriteResult:
        if idempotency_key is None:
            raise InvalidRequestError("trigger creation requires an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        validate_trigger_common(
            kind=kind,
            timezone=timezone_name,
            catch_up_policy=catch_up_policy,
            misfire_grace_us=misfire_grace_us,
            max_occurrences_per_run=max_occurrences_per_run,
            enabled=enabled,
        )
        canonical_condition = validate_condition_spec(kind, condition_spec)
        canonical_schedule = self._canonical_schedule(kind, schedule_spec)
        with self._uow.read() as tx:
            task = tx.tasks.get_task(task_id)
            # Pre-read resolves the task's own tenant/agent for the §25.3
            # gate, which runs BEFORE the idempotency cache (round-4 P0).
            _require_task_access(tx, access, task)
        require_surface_online(
            self._surface,
            task.tenant_id,
            task.agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        payload = {
            "task_id": task_id,
            "kind": kind,
            "task_step_id": task_step_id,
            "schedule_spec": canonical_schedule,
            "condition_spec": canonical_condition,
            "timezone": timezone_name,
            "catch_up_policy": catch_up_policy,
            "misfire_grace_us": misfire_grace_us,
            "max_occurrences_per_run": max_occurrences_per_run,
            "enabled": enabled,
        }
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="task:create_trigger",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint("task:create_trigger", payload),
            execute=lambda tx: self._execute_create_trigger(
                tx,
                access,
                payload,
                lease_id=lease_id,
                lease_epoch=lease_epoch,
            ),
        )
        body = json.loads(result.body)
        with self._uow.read() as tx:
            trigger = tx.tasks.get_trigger(body["trigger_id"])
            task = tx.tasks.get_task(trigger.task_id)
            _require_task_access(tx, access, task)
        return TriggerWriteResult(
            trigger_id=body["trigger_id"], revision=int(body["revision"]), replayed=result.replayed
        )

    @staticmethod
    def _canonical_schedule(
        kind: str, schedule_spec: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if kind in (TriggerKind.AT_TIME.value, TriggerKind.RECURRENCE.value):
            if not isinstance(schedule_spec, dict):
                raise InvalidRequestError(f"{kind} trigger requires schedule_spec")
            canonical: dict[str, Any] = dict(schedule_spec)
            if kind == TriggerKind.AT_TIME.value:
                at_us = canonical.get("at_us")
                if not isinstance(at_us, int) or isinstance(at_us, bool) or at_us <= 0:
                    raise InvalidRequestError("at_time trigger requires positive at_us")
            else:
                parse_trigger_schedule_spec(kind, canonical)
            return canonical
        if schedule_spec is not None:
            raise InvalidRequestError(f"{kind} triggers take condition_spec, not schedule_spec")
        return None

    def _execute_create_trigger(
        self,
        tx: Transaction,
        access: AccessContext,
        payload: dict[str, Any],
        *,
        lease_id: str | None,
        lease_epoch: int | None,
    ) -> tuple[str, str, list[str]]:
        task = tx.tasks.get_task(payload["task_id"])
        _require_task_access(tx, access, task)
        lease_warning = require_surface_online_in_tx(
            self._surface,
            tx,
            task.tenant_id,
            task.agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        kind = payload["kind"]
        task_step_id = payload["task_step_id"]
        if task_step_id is not None:
            step = tx.tasks.get_step(task_step_id)
            if step.task_id != task.id:
                raise InvalidRequestError("trigger's task_step_id belongs to another task")
        next_fire_at = self._initial_next_fire(payload, task)
        trigger_id = tx.tasks.insert_trigger(
            task_id=task.id,
            task_step_id=task_step_id,
            tenant_id=task.tenant_id,
            agent_id=task.agent_id,
            kind=kind,
            timezone=payload["timezone"],
            catch_up_policy=payload["catch_up_policy"],
            misfire_grace_us=payload["misfire_grace_us"],
            max_occurrences_per_run=payload["max_occurrences_per_run"],
            enabled=payload["enabled"],
            next_fire_at_us=next_fire_at,
        )
        revision_id = tx.tasks.insert_trigger_revision(
            trigger_id=trigger_id,
            task_id=task.id,
            tenant_id=task.tenant_id,
            revision=1,
            kind=kind,
            task_step_id=task_step_id,
            schedule_spec=payload["schedule_spec"],
            condition_spec=payload["condition_spec"],
            timezone=payload["timezone"],
            catch_up_policy=payload["catch_up_policy"],
            misfire_grace_us=payload["misfire_grace_us"],
            max_occurrences_per_run=payload["max_occurrences_per_run"],
            enabled=payload["enabled"],
            created_by=f"access:{access.app_instance_id}",
        )
        if tx.tasks.set_initial_trigger_pointer(trigger_id, revision_id) != 1:
            raise ConflictError("trigger creation raced inside the transaction")
        tx.advance_watermark(task.tenant_id, task.agent_id, [("task_trigger", trigger_id, 1)])
        tx.audit(
            tenant_id=task.tenant_id,
            actor=f"access:{access.app_instance_id}",
            action="task.trigger_created",
            resource_type="task_trigger",
            resource_id=trigger_id,
            reason_code="plan_edit",
            details={
                "kind": kind,
                "timezone": payload["timezone"],
                "catch_up_policy": payload["catch_up_policy"],
                "enabled": payload["enabled"],
                "lease_warning": lease_warning,
            },
            revision=1,
        )
        enqueue_change_job(
            tx,
            tenant_id=task.tenant_id,
            agent_id=task.agent_id,
            job_kind="task.changed",
            aggregate_type="task_trigger",
            aggregate_id=trigger_id,
            source_revision=1,
            payload={"task_id": task.id, "trigger_id": trigger_id, "revision": 1},
        )
        return (
            "task.trigger_created",
            json.dumps({"trigger_id": trigger_id, "revision": 1}),
            [f"task_trigger:{trigger_id}"],
        )

    def set_trigger_enabled(
        self,
        tx: Transaction,
        trigger: TaskTriggerCurrent,
        *,
        enabled: bool,
        actor: str,
        reason_code: str,
    ) -> None:
        """Enable/disable inside an existing transaction (new spec revision)."""
        current = tx.tasks.current_trigger_revision_row(trigger.id)
        revision = trigger.current_revision + 1
        revision_id = tx.tasks.insert_trigger_revision(
            trigger_id=trigger.id,
            task_id=trigger.task_id,
            tenant_id=trigger.tenant_id,
            revision=revision,
            kind=current.kind,
            task_step_id=current.task_step_id,
            schedule_spec=current.schedule_spec,
            condition_spec=current.condition_spec,
            timezone=current.timezone,
            catch_up_policy=current.catch_up_policy,
            misfire_grace_us=current.misfire_grace_us,
            max_occurrences_per_run=current.max_occurrences_per_run,
            enabled=enabled,
            created_by=actor,
        )
        if (
            tx.tasks.advance_trigger_pointer(
                trigger.id,
                expected_revision=trigger.current_revision,
                revision=revision,
                revision_id=revision_id,
                enabled=enabled,
            )
            != 1
        ):
            tx.tasks.raise_trigger_pointer_mismatch(trigger.id, trigger.current_revision)
        tx.advance_watermark(
            trigger.tenant_id, trigger.agent_id, [("task_trigger", trigger.id, revision)]
        )
        tx.audit(
            tenant_id=trigger.tenant_id,
            actor=actor,
            action="task.trigger_enabled" if enabled else "task.trigger_disabled",
            resource_type="task_trigger",
            resource_id=trigger.id,
            reason_code=reason_code,
            details={},
            revision=revision,
        )
        enqueue_change_job(
            tx,
            tenant_id=trigger.tenant_id,
            agent_id=trigger.agent_id,
            job_kind="task.changed",
            aggregate_type="task_trigger",
            aggregate_id=trigger.id,
            source_revision=revision,
            payload={"task_id": trigger.task_id, "trigger_id": trigger.id, "revision": revision},
        )

    def _initial_next_fire(self, payload: dict[str, Any], task: TaskCurrent) -> int | None:
        from iris_memory_core.domain.schedule import next_occurrence

        kind = payload["kind"]
        if kind == TriggerKind.AT_TIME.value:
            return int(payload["schedule_spec"]["at_us"])
        if kind == TriggerKind.RECURRENCE.value:
            spec = parse_trigger_schedule_spec(kind, payload["schedule_spec"])
            assert spec is not None
            return next_occurrence(self._clock.now_us(), spec, ZoneInfo(payload["timezone"]))
        # Condition kinds fire from the scan, not from a schedule position.
        del task
        return None

    # -- readiness --------------------------------------------------------------

    def _recompute_successors(
        self, tx: Transaction, task: TaskCurrent, *, step_id: str, actor: str
    ) -> None:
        for edge in tx.tasks.dependencies_for_task(task.id):
            if edge.predecessor_step_id == step_id:
                self._recompute_step(tx, task, edge.successor_step_id, actor=actor)

    def _recompute_step(
        self, tx: Transaction, task: TaskCurrent, step_id: str, *, actor: str
    ) -> None:
        """Derive pending↔ready from the stored dependency graph (§11.3)."""
        step = tx.tasks.get_step(step_id)
        pairs = [
            (edge.condition, tx.tasks.get_step(edge.predecessor_step_id).status)
            for edge in tx.tasks.dependencies_for_task(task.id)
            if edge.successor_step_id == step_id
        ]
        derived = compute_step_status(current_status=step.status, predecessor_statuses=pairs)
        if derived == step.status:
            return
        current = tx.tasks.current_step_revision_row(step_id)
        revision = step.current_revision + 1
        revision_id = tx.tasks.insert_step_revision(
            step_id=step_id,
            task_id=step.task_id,
            tenant_id=step.tenant_id,
            revision=revision,
            stable_key=current.stable_key,
            title=current.title,
            description=current.description,
            privacy_labels=current.privacy_labels,
            status=derived,
            ordinal=current.ordinal,
            expected_effect=current.expected_effect,
            completion_evidence_refs=current.completion_evidence_refs,
            started_us=current.started_us,
            completed_us=current.completed_us,
            created_by=actor,
        )
        if (
            tx.tasks.advance_step_pointer(
                step_id,
                expected_revision=step.current_revision,
                revision=revision,
                revision_id=revision_id,
                status=derived,
            )
            != 1
        ):
            tx.tasks.raise_step_pointer_mismatch(step_id, step.current_revision)
        tx.advance_watermark(
            step.tenant_id, _task_agent(tx, step), [("task_step", step_id, revision)]
        )
        tx.audit(
            tenant_id=step.tenant_id,
            actor=actor,
            action=f"task.step_{derived}",
            resource_type="task_step",
            resource_id=step_id,
            reason_code="dependency_readiness_recomputed",
            details={},
            revision=revision,
        )
        enqueue_change_job(
            tx,
            tenant_id=step.tenant_id,
            agent_id=_task_agent(tx, step),
            job_kind="task.changed",
            aggregate_type="task_step",
            aggregate_id=step_id,
            source_revision=revision,
            payload={"task_id": step.task_id, "step_id": step_id, "revision": revision},
        )

    # -- trigger scan (job handler body) --------------------------------------

    def trigger_scan(
        self, tx: Transaction, *, tenant_id: str, agent_id: str, now_us: int | None = None
    ) -> TriggerScanReport:
        """Compute due occurrences and create their CognitiveEvents.

        Idempotent by construction: each occurrence lands under
        UNIQUE(trigger, revision, scheduled_at, key); an occurrence that
        already carries its event is absorbed, never duplicated.
        """
        from iris_memory_core.application.events import CognitiveEventService
        from iris_memory_core.domain.schedule import CatchUpPolicy, next_occurrence, plan_catch_up

        now = now_us if now_us is not None else self._clock.now_us()
        created = absorbed = skipped = events = 0
        for trigger in tx.tasks.triggers_for_scan(tenant_id, agent_id, now_us=now):
            if tx.is_tombstoned(trigger.tenant_id, "task", trigger.task_id):
                continue
            spec_row = tx.tasks.current_trigger_revision_row(trigger.id)
            task = tx.tasks.get_task(trigger.task_id)
            step = tx.tasks.get_step(trigger.task_step_id) if trigger.task_step_id else None
            object_type = "task_step" if step is not None else "task"
            object_id = step.id if step is not None else task.id

            def _emit(
                scheduled_at_us: int,
                key: str,
                reason: str | None,
                *,
                trigger: TaskTriggerCurrent = trigger,
                task: TaskCurrent = task,
                object_type: str = object_type,
                object_id: str = object_id,
            ) -> bool:
                """Record one occurrence (+ its event); True if newly created.

                The loop variables are bound as defaults: the closure runs
                strictly within its own iteration, and the binding keeps that
                invariant explicit.
                """
                nonlocal created, absorbed, events
                occurrence_id, is_new = tx.tasks.insert_occurrence(
                    trigger_id=trigger.id,
                    trigger_revision=trigger.current_revision,
                    tenant_id=trigger.tenant_id,
                    scheduled_at_us=scheduled_at_us,
                    occurrence_key=key,
                    status="enqueued",
                    reason_code=reason,
                )
                if not is_new:
                    absorbed += 1
                    return False
                created += 1
                event_id = CognitiveEventService.create_internal(
                    tx,
                    tenant_id=trigger.tenant_id,
                    agent_id=trigger.agent_id,
                    space_group_id=task.space_group_id,
                    space_id=task.space_id,
                    session_id=task.session_id,
                    kind="task.due",
                    object_type=object_type,
                    object_id=object_id,
                    occurrence_id=occurrence_id,
                    scheduled_at_us=scheduled_at_us,
                    deliver_after_us=max(scheduled_at_us, now),
                    reason_code=reason or "trigger_fired",
                    now_us=now,
                    ttl_us=self._event_ttl_us,
                )
                tx.tasks.attach_occurrence_event(occurrence_id, cognitive_event_id=event_id)
                events += 1
                return True

            if trigger.kind in (TriggerKind.AT_TIME.value, TriggerKind.RECURRENCE.value):
                tz = ZoneInfo(trigger.timezone)
                if trigger.kind == TriggerKind.AT_TIME.value:
                    raw_at = (spec_row.schedule_spec or {}).get("at_us")
                    at_us = raw_at if isinstance(raw_at, int) else 0
                    due = [at_us] if trigger.next_fire_at_us is not None else []
                else:
                    spec = parse_trigger_schedule_spec(trigger.kind, spec_row.schedule_spec or {})
                    assert spec is not None
                    due = self._catch_up_occurrences(trigger, spec, tz, now_us=now)
                decisions = plan_catch_up(
                    due,
                    now_us=now,
                    policy=CatchUpPolicy(trigger.catch_up_policy),
                    misfire_grace_us=trigger.misfire_grace_us,
                    max_ticks_per_run=trigger.max_occurrences_per_run,
                )
                for decision in decisions:
                    key = occurrence_key_for_time(
                        trigger.id, trigger.current_revision, decision.scheduled_at_us
                    )
                    if decision.fire:
                        _emit(decision.scheduled_at_us, key, decision.reason_code)
                    else:
                        # Skipped occurrences are still accounted in the ledger
                        # (misfire / catch-up policy), just without an event.
                        _, is_new = tx.tasks.insert_occurrence(
                            trigger_id=trigger.id,
                            trigger_revision=trigger.current_revision,
                            tenant_id=trigger.tenant_id,
                            scheduled_at_us=decision.scheduled_at_us,
                            occurrence_key=key,
                            status="skipped",
                            reason_code=decision.reason_code,
                        )
                        if is_new:
                            skipped += 1
                # Advance the schedule position (monotonic; the SPEC revision
                # is untouched so occurrence identity cannot fork). A skipped
                # one-shot is still RETIRED: the ledger row already accounts
                # for it, so keeping the due marker would re-plan the same
                # occurrence on every tick forever.
                next_fire: int | None = trigger.next_fire_at_us
                if trigger.kind == TriggerKind.RECURRENCE.value:
                    assert spec is not None
                    if due:
                        next_fire = next_occurrence(max(due), spec, tz)
                    elif trigger.next_fire_at_us is None:
                        next_fire = next_occurrence(now, spec, tz)
                elif trigger.kind == TriggerKind.AT_TIME.value and decisions:
                    next_fire = None
                tx.tasks.set_trigger_schedule_state(
                    trigger.id,
                    expected_fire_at_us=trigger.next_fire_at_us,
                    next_fire_at_us=next_fire,
                    last_scan_us=now,
                )
            elif trigger.kind == TriggerKind.OBSERVATION_KIND.value:
                condition = spec_row.condition_spec or {}
                want_kind = condition.get("observation_kind")
                want_role = condition.get("role")
                # Already-ledgered observations are excluded in SQL so the
                # batch limit bounds NEW work; replays of the same window are
                # therefore free and a backlog converges across runs.
                scanned = tx.observations.for_trigger_scan(
                    tenant_id,
                    agent_id,
                    want_kind=str(want_kind) if want_kind else None,
                    want_role=str(want_role) if want_role else None,
                    after_us=trigger.last_scan_us,
                    limit=OBSERVATION_SCAN_BATCH,
                    trigger_id=trigger.id,
                    trigger_revision=trigger.current_revision,
                )
                for observation in scanned:
                    key = occurrence_key_for_observation(
                        trigger.id, trigger.current_revision, observation.id
                    )
                    if tx.is_tombstoned(observation.tenant_id, "observation", observation.id):
                        # Tombstone re-check (the spine query only filters
                        # committed): the skip is ledgered so the row leaves
                        # the candidate set instead of being re-read forever.
                        tx.tasks.insert_occurrence(
                            trigger_id=trigger.id,
                            trigger_revision=trigger.current_revision,
                            tenant_id=trigger.tenant_id,
                            scheduled_at_us=observation.occurred_us,
                            occurrence_key=key,
                            status="skipped",
                            reason_code="observation_tombstoned",
                        )
                        skipped += 1
                        continue
                    if not scope_allows(_observation_scope(observation), _task_scope(task)):
                        # Scope final check (the spine query filters by
                        # tenant+agent only): an observation of another
                        # space_group/space/session never fires this task's
                        # trigger. The skip is ledgered so the row leaves the
                        # candidate set instead of refilling every batch.
                        tx.tasks.insert_occurrence(
                            trigger_id=trigger.id,
                            trigger_revision=trigger.current_revision,
                            tenant_id=trigger.tenant_id,
                            scheduled_at_us=observation.occurred_us,
                            occurrence_key=key,
                            status="skipped",
                            reason_code="observation_out_of_scope",
                        )
                        skipped += 1
                        continue
                    _emit(observation.occurred_us, key, "observation_matched")
                # Cursor discipline: advance only past what this run actually
                # read. A truncated batch parks the cursor just below the last
                # row's committed_us so same-timestamp stragglers stay visible;
                # ledgered rows never re-enter the batch, so the next run
                # spends its whole batch on NEW rows.
                if len(scanned) >= OBSERVATION_SCAN_BATCH:
                    next_last_scan = max(row.committed_us for row in scanned) - 1
                else:
                    next_last_scan = now
                tx.tasks.set_trigger_schedule_state(
                    trigger.id,
                    expected_fire_at_us=trigger.next_fire_at_us,
                    next_fire_at_us=trigger.next_fire_at_us,
                    last_scan_us=next_last_scan,
                )
            elif trigger.kind == TriggerKind.STATE_CONDITION.value:
                condition = spec_row.condition_spec or {}
                record = tx.states.find(
                    _state_scope_key(tenant_id, agent_id),
                    str(condition.get("namespace", "")),
                    str(condition.get("key", "")),
                )
                observed_us = 0
                value: object | None = None
                if record is not None:
                    state_revision = tx.states.current_revision(record.current_revision_id)
                    live = not (
                        state_revision.expires_us is not None and state_revision.expires_us <= now
                    )
                    if live:
                        observed_us = state_revision.observed_us
                        try:
                            decoded = json.loads(state_revision.value_json)
                            value = (
                                decoded if not isinstance(decoded, dict) else decoded.get("value")
                            )
                        except json.JSONDecodeError:
                            value = None
                    # An expired record reads as absent: stale data must not
                    # arm a trigger (final lifecycle check).
                if evaluate_state_condition(condition, value):
                    _emit(
                        observed_us,
                        occurrence_key_for_state(trigger.id, trigger.current_revision, observed_us),
                        "state_condition_met",
                    )
                tx.tasks.set_trigger_schedule_state(
                    trigger.id,
                    expected_fire_at_us=trigger.next_fire_at_us,
                    next_fire_at_us=trigger.next_fire_at_us,
                    last_scan_us=now,
                )
            else:  # task_transition
                condition = spec_row.condition_spec or {}
                to_status = str(condition.get("to_status", ""))
                from_status = condition.get("from_status")
                watched: TaskCurrent | None = None
                aggregate_revision = 0
                status_now = ""
                previous_status: str | None = None
                if condition.get("task_step_id"):
                    try:
                        step_watched = tx.tasks.get_step(str(condition["task_step_id"]))
                        watched = tx.tasks.get_task(step_watched.task_id)
                    except NotFoundError:
                        watched = None
                    if watched is not None:
                        step_history_rows = tx.tasks.step_history(step_watched.id, limit=2)
                        aggregate_revision = (
                            step_history_rows[0].revision if step_history_rows else 0
                        )
                        status_now = step_history_rows[0].status if step_history_rows else ""
                        previous_status = (
                            step_history_rows[1].status if len(step_history_rows) > 1 else None
                        )
                else:
                    watched_task_id = (
                        str(condition["task_id"]) if condition.get("task_id") else task.id
                    )
                    try:
                        watched = tx.tasks.get_task(watched_task_id)
                    except NotFoundError:
                        watched = None
                    if watched is not None:
                        task_history_rows = tx.tasks.task_history(watched.id, limit=2)
                        aggregate_revision = (
                            task_history_rows[0].revision if task_history_rows else 0
                        )
                        status_now = task_history_rows[0].status if task_history_rows else ""
                        previous_status = (
                            task_history_rows[1].status if len(task_history_rows) > 1 else None
                        )
                # Final scope/lifecycle re-check: the condition may name any
                # task/step id, so the watched aggregate must still belong to
                # this trigger's tenant+agent, sit inside the trigger task's
                # space_group/space/session, and be alive — a cross-scope or
                # deleted aggregate (task OR watched step) never fires
                # (fail closed, per trigger).
                watched_in_scope = (
                    watched is not None
                    and watched.tenant_id == trigger.tenant_id
                    and watched.agent_id == trigger.agent_id
                    and scope_allows(_task_scope(watched), _task_scope(task))
                    and not tx.is_tombstoned(watched.tenant_id, "task", watched.id)
                    and (
                        not condition.get("task_step_id")
                        or not tx.is_tombstoned(
                            watched.tenant_id,
                            "task_step",
                            str(condition["task_step_id"]),
                        )
                    )
                )
                matched = (
                    watched_in_scope
                    and status_now == to_status
                    and (from_status is None or previous_status == from_status)
                )
                if matched:
                    _emit(
                        now,
                        occurrence_key_for_transition(
                            trigger.id, trigger.current_revision, aggregate_revision
                        ),
                        "task_transition_matched",
                    )
                tx.tasks.set_trigger_schedule_state(
                    trigger.id,
                    expected_fire_at_us=trigger.next_fire_at_us,
                    next_fire_at_us=trigger.next_fire_at_us,
                    last_scan_us=now,
                )
        return TriggerScanReport(
            occurrences_created=created,
            events_created=events,
            occurrences_absorbed=absorbed,
            skipped=skipped,
        )

    def _catch_up_occurrences(
        self,
        trigger: TaskTriggerCurrent,
        spec: ScheduleSpec,
        tz: ZoneInfo,
        *,
        now_us: int,
    ) -> list[int]:
        """All due scheduled times since the last known position (bounded).

        The bound guards against unbounded task storms after long offline
        periods (§17.3); occurrences beyond it are classified ``skipped`` by
        the catch-up planner, never silently dropped.
        """
        from iris_memory_core.domain.schedule import next_occurrence

        times: list[int] = []
        if trigger.next_fire_at_us is not None:
            times.append(trigger.next_fire_at_us)
        cursor = trigger.next_fire_at_us
        if cursor is None:
            return times
        bound = max(1_000, trigger.max_occurrences_per_run * 10)
        for _ in range(bound):
            candidate = next_occurrence(cursor, spec, tz)
            if candidate > now_us or candidate <= cursor:
                break
            times.append(candidate)
            cursor = candidate
        return [time for time in times if time <= now_us]

    # -- reads -------------------------------------------------------------------

    def due_tasks_for_route(
        self,
        tx: Transaction,
        access: AccessContext,
        *,
        agent_id: str,
        space_id: str,
        session_id: str | None,
        limit: int = 20,
    ) -> list[tuple[TaskCurrent, TaskRevision]]:
        """Due, non-terminal tasks visible at the recall request scope.

        The Recall tasks route input (§18/Phase 4): only active/waiting tasks
        whose due time has arrived, re-checked against scope/privacy/
        tombstone before they become candidates.
        """
        authorize_scope(tx, access, agent_id=agent_id, space_id=space_id, session_id=session_id)
        request = Scope(
            tenant_id=access.tenant_id,
            agent_id=agent_id,
            space_group_id=None,
            space_id=space_id,
            session_id=session_id,
        )
        now_us = self._clock.now_us()
        visible: list[tuple[TaskCurrent, TaskRevision]] = []
        for task in tx.tasks.due_tasks(access.tenant_id, agent_id, now_us=now_us, limit=limit):
            if tx.is_tombstoned(task.tenant_id, "task", task.id):
                continue
            revision = tx.tasks.current_task_revision_row(task.id)
            data_scope = _task_scope(task)
            if not scope_allows(data_scope, request):
                continue
            if not evaluate_privacy(revision.privacy_labels, data_scope, request, access):
                continue
            visible.append((task, revision))
        return visible

    def get_task_view(
        self, access: AccessContext, task_id: str
    ) -> tuple[TaskCurrent, TaskRevision] | None:
        with self._uow.read() as tx:
            try:
                task = tx.tasks.get_task(task_id)
                revision = _require_task_access(tx, access, task)
            except NotFoundError:
                return None
            return task, revision

    def list_tasks(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        statuses: tuple[str, ...] = ("proposed", "active", "waiting", "blocked"),
        space_id: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[tuple[TaskCurrent, TaskRevision]]:
        with self._uow.read() as tx:
            return self.list_tasks_in_tx(
                tx,
                access,
                agent_id=agent_id,
                statuses=statuses,
                space_id=space_id,
                session_id=session_id,
                limit=limit,
            )

    def list_tasks_in_tx(
        self,
        tx: Transaction,
        access: AccessContext,
        *,
        agent_id: str,
        statuses: tuple[str, ...] = ("proposed", "active", "waiting", "blocked"),
        space_id: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[tuple[TaskCurrent, TaskRevision]]:
        authorize_scope(tx, access, agent_id=agent_id, space_id=space_id, session_id=session_id)
        request = Scope(
            tenant_id=access.tenant_id,
            agent_id=agent_id,
            space_group_id=None,
            space_id=space_id,
            session_id=session_id,
        )
        visible: list[tuple[TaskCurrent, TaskRevision]] = []
        for task in tx.tasks.list_tasks(access.tenant_id, agent_id, statuses=statuses, limit=limit):
            if tx.is_tombstoned(task.tenant_id, "task", task.id):
                continue
            revision = tx.tasks.current_task_revision_row(task.id)
            data_scope = _task_scope(task)
            if not scope_allows(data_scope, request):
                continue
            if not evaluate_privacy(revision.privacy_labels, data_scope, request, access):
                continue
            visible.append((task, revision))
        return visible

    def list_steps(
        self, access: AccessContext, task_id: str
    ) -> list[tuple[TaskStepCurrent, TaskStepRevision]]:
        with self._uow.read() as tx:
            task = tx.tasks.get_task(task_id)
            _require_task_access(tx, access, task)
            result: list[tuple[TaskStepCurrent, TaskStepRevision]] = []
            for step in tx.tasks.steps_for_task(task.id):
                if tx.is_tombstoned(step.tenant_id, "task_step", step.id):
                    continue
                result.append((step, tx.tasks.current_step_revision_row(step.id)))
            return result

    def list_triggers(
        self, access: AccessContext, task_id: str
    ) -> list[tuple[TaskTriggerCurrent, TaskTriggerRevision]]:
        with self._uow.read() as tx:
            task = tx.tasks.get_task(task_id)
            _require_task_access(tx, access, task)
            result: list[tuple[TaskTriggerCurrent, TaskTriggerRevision]] = []
            for trigger in tx.tasks.triggers_for_task(task.id):
                if tx.is_tombstoned(trigger.tenant_id, "task_trigger", trigger.id):
                    continue
                result.append((trigger, tx.tasks.current_trigger_revision_row(trigger.id)))
            return result

    # -- promotion materialization (NoteService seam) ---------------------------

    @staticmethod
    def _create_promoted_task(
        tx: Transaction,
        *,
        note: NoteCurrent,
        current: NoteRevision,
        actor: str,
        now_us: int,
    ) -> str:
        """Create the PROPOSED task a note promotion materializes (§10.3)."""
        del now_us
        scope_key = task_scope_key(
            note.tenant_id,
            note.agent_id,
            note.space_group_id,
            note.space_id,
            note.session_id,
        )
        task_id = tx.tasks.insert_task(
            tenant_id=note.tenant_id,
            agent_id=note.agent_id,
            space_group_id=note.space_group_id,
            space_id=note.space_id,
            session_id=note.session_id,
            scope_key=scope_key,
            parent_task_id=None,
            title=current.title[:500],
            owner_kind="agent",
            owner_entity_id=None,
            status=TaskStatus.PROPOSED.value,
            priority=5,
            due_at_us=note.due_at_us,
        )
        revision_id = tx.tasks.insert_task_revision(
            task_id=task_id,
            tenant_id=note.tenant_id,
            revision=1,
            title=current.title[:500],
            goal=current.body[:8_000],
            owner_kind="agent",
            owner_entity_id=None,
            privacy_labels=current.privacy_labels,
            source_refs=(
                {
                    "resource_type": "note",
                    "resource_id": note.id,
                    "revision": note.current_revision,
                },
            ),
            status=TaskStatus.PROPOSED.value,
            priority=5,
            next_action=None,
            progress_note=None,
            due_at_us=note.due_at_us,
            completed_us=None,
            created_by=actor,
        )
        if tx.tasks.set_initial_task_pointer(task_id, revision_id) != 1:
            raise ConflictError("promoted task creation raced inside the transaction")
        tx.advance_watermark(note.tenant_id, note.agent_id, [("task", task_id, 1)])
        tx.insert_resource_link(
            tenant_id=note.tenant_id,
            source_type="note",
            source_id=note.id,
            target_type="task",
            target_id=task_id,
            relation="promoted_to",
        )
        tx.audit(
            tenant_id=note.tenant_id,
            actor=actor,
            action="task.created",
            resource_type="task",
            resource_id=task_id,
            reason_code="note_promotion",
            details={"source_note_id": note.id, "status": TaskStatus.PROPOSED.value},
            revision=1,
        )
        enqueue_change_job(
            tx,
            tenant_id=note.tenant_id,
            agent_id=note.agent_id,
            job_kind="task.changed",
            aggregate_type="task",
            aggregate_id=task_id,
            source_revision=1,
            payload={"task_id": task_id, "revision": 1},
        )
        return task_id


def _task_agent(tx: Transaction, step: TaskStepCurrent) -> str:
    return tx.tasks.get_task(step.task_id).agent_id


def _state_scope_key(tenant_id: str, agent_id: str) -> str:
    from iris_memory_core.domain.state import state_scope_key

    return state_scope_key(
        Scope(
            tenant_id=tenant_id,
            agent_id=agent_id,
            space_group_id=None,
            space_id=None,
            session_id=None,
        )
    )


def _observation_scope(observation: StoredObservation) -> Scope:
    return Scope(
        tenant_id=observation.tenant_id,
        agent_id=observation.agent_id,
        space_group_id=observation.space_group_id,
        space_id=observation.space_id,
        session_id=observation.session_id,
    )


def _validate_evidence(
    tx: Transaction, task: TaskCurrent, refs: tuple[dict[str, object], ...], access: AccessContext
) -> None:
    """Evidence must be REAL: committed observations or canonical artifacts
    inside the task's scope.

    The source is fetched by ID, so its own scope dims — not the caller's
    say-so — decide admissibility: a resource belonging to another tenant or
    agent is never evidence for this task, and neither is one from another
    space_group/space/session (a space-B effect must not complete a space-A
    task). Observation evidence additionally requires the committed effect
    state — generated-but-unsent, failed or partial output is not an effect
    (§15.2) — and its own privacy labels must be visible to the access
    context. Artifact evidence goes through the Phase 5 canonical artifact
    validator (existence, envelope, status, tombstone, PRIVACY; ADR-0013 §5)
    — CognitiveEvent is structurally excluded and can never complete a step.
    """
    for ref in refs:
        resource_type = str(ref.get("resource_type"))
        resource_id = str(ref.get("resource_id"))
        if resource_type not in EVIDENCE_RESOURCE_TYPES:
            raise InvalidRequestError(f"evidence resource type not allowed: {resource_type!r}")
        if resource_type == "observation":
            from iris_memory_core.domain.observation import EffectState

            observation = tx.observations.get(resource_id)
            if observation.tenant_id != task.tenant_id or observation.agent_id != task.agent_id:
                raise InvalidRequestError(
                    "observation evidence must belong to the task's tenant and agent"
                )
            if not scope_allows(_observation_scope(observation), _task_scope(task)):
                raise InvalidRequestError(
                    "observation evidence is outside the task's scope "
                    "(space_group/space/session must match)"
                )
            if observation.effect_state is not EffectState.COMMITTED:
                raise InvalidRequestError("observation evidence must be committed (not partial)")
            if tx.is_tombstoned(observation.tenant_id, "observation", observation.id):
                raise InvalidRequestError("observation evidence is tombstoned")
            if not evaluate_privacy(
                observation.privacy_labels,
                _observation_scope(observation),
                _task_scope(task),
                access,
            ):
                raise AccessDeniedError(
                    "observation evidence privacy is outside the access context"
                )
        elif resource_type == "artifact":
            artifact = tx.artifacts.get(resource_id)
            if artifact.tenant_id != task.tenant_id or artifact.agent_id != task.agent_id:
                raise InvalidRequestError(
                    "artifact evidence must belong to the task's tenant and agent"
                )
            artifact_scope = Scope(
                tenant_id=artifact.tenant_id,
                agent_id=artifact.agent_id,
                space_group_id=artifact.space_group_id,
                space_id=artifact.space_id,
                session_id=artifact.session_id,
            )
            if not scope_allows(artifact_scope, _task_scope(task)):
                raise InvalidRequestError(
                    "artifact evidence is outside the task's scope "
                    "(space_group/space/session must match)"
                )
            if artifact.status == "tombstoned" or tx.is_tombstoned(
                artifact.tenant_id, "artifact", artifact.id
            ):
                raise InvalidRequestError("artifact evidence is tombstoned")
            if not evaluate_privacy(
                artifact.privacy_labels, artifact_scope, _task_scope(task), access
            ):
                raise AccessDeniedError("artifact evidence privacy is outside the access context")


__all__ = [
    "StepWriteResult",
    "TaskService",
    "TaskWriteResult",
    "TriggerScanReport",
    "TriggerWriteResult",
]
