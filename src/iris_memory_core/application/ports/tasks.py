"""Application ports for tasks; storage and provider adapters implement these contracts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from iris_memory_core.domain.task import (
    TaskCurrent,
    TaskDependencyEdge,
    TaskRevision,
    TaskStepCurrent,
    TaskStepRevision,
    TaskTriggerCurrent,
    TaskTriggerRevision,
    TriggerOccurrence,
)


class TaskSurface(Protocol):
    """Repository surface for tasks, steps, dependencies, triggers, occurrences."""

    def deletion_children(
        self, tenant_id: str, task_id: str, *, limit: int = 501
    ) -> tuple[tuple[str, str], ...]: ...
    def has_live_child_task(self, tenant_id: str, task_id: str) -> bool: ...
    def erase_task_content(self, task_id: str, *, now_us: int) -> None: ...

    def get_task(self, task_id: str) -> TaskCurrent: ...
    def get_task_revision(self, revision_id: str) -> TaskRevision: ...
    def current_task_revision_row(self, task_id: str) -> TaskRevision: ...
    def insert_task(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        scope_key: str,
        parent_task_id: str | None,
        title: str,
        owner_kind: str,
        owner_entity_id: str | None,
        status: str,
        priority: int,
        due_at_us: int | None,
    ) -> str: ...
    def insert_task_revision(
        self,
        *,
        task_id: str,
        tenant_id: str,
        revision: int,
        title: str,
        goal: str,
        owner_kind: str,
        owner_entity_id: str | None,
        privacy_labels: tuple[str, ...],
        source_refs: tuple[dict[str, object], ...],
        status: str,
        priority: int,
        next_action: str | None,
        progress_note: str | None,
        due_at_us: int | None,
        completed_us: int | None,
        created_by: str,
    ) -> str: ...
    def set_initial_task_pointer(self, task_id: str, revision_id: str) -> int: ...
    def advance_task_pointer(
        self,
        task_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        next_action: str | None = None,
        next_action_set: bool = False,
        due_at_us: int | None = None,
        due_at_set: bool = False,
        completed_us: int | None = None,
        completed_us_set: bool = False,
    ) -> int: ...
    def raise_task_pointer_mismatch(self, task_id: str, expected: int) -> None: ...
    def list_tasks(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        statuses: tuple[str, ...] = ("proposed", "active", "waiting", "blocked"),
        limit: int = 100,
    ) -> Sequence[TaskCurrent]: ...
    def due_tasks(
        self, tenant_id: str, agent_id: str, *, now_us: int, limit: int = 50
    ) -> Sequence[TaskCurrent]: ...
    def latest_task_transition(self, task_id: str) -> tuple[int, str, str | None]: ...

    def task_history(self, task_id: str, *, limit: int = 100) -> Sequence[TaskRevision]: ...
    def get_step(self, step_id: str) -> TaskStepCurrent: ...
    def get_step_revision(self, revision_id: str) -> TaskStepRevision: ...
    def current_step_revision_row(self, step_id: str) -> TaskStepRevision: ...
    def find_step_by_key(self, task_id: str, stable_key: str) -> TaskStepCurrent | None: ...
    def insert_step(
        self,
        *,
        task_id: str,
        tenant_id: str,
        stable_key: str,
        title: str,
        ordinal: int,
        status: str,
    ) -> str: ...
    def insert_step_revision(
        self,
        *,
        step_id: str,
        task_id: str,
        tenant_id: str,
        revision: int,
        stable_key: str,
        title: str,
        description: str | None,
        privacy_labels: tuple[str, ...],
        status: str,
        ordinal: int,
        expected_effect: str | None,
        completion_evidence_refs: tuple[dict[str, object], ...],
        started_us: int | None,
        completed_us: int | None,
        created_by: str,
    ) -> str: ...
    def set_initial_step_pointer(self, step_id: str, revision_id: str) -> int: ...
    def advance_step_pointer(
        self,
        step_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        started_us: int | None = None,
        started_us_set: bool = False,
        completed_us: int | None = None,
        completed_us_set: bool = False,
    ) -> int: ...
    def raise_step_pointer_mismatch(self, step_id: str, expected: int) -> None: ...
    def steps_for_task(self, task_id: str) -> Sequence[TaskStepCurrent]: ...
    def latest_step_transition(self, step_id: str) -> tuple[int, str, str | None]: ...

    def step_history(self, step_id: str, *, limit: int = 100) -> Sequence[TaskStepRevision]: ...
    def dependencies_for_task(self, task_id: str) -> Sequence[TaskDependencyEdge]: ...
    def dependency_at_revision(self, dependency_id: str, revision: int) -> TaskDependencyEdge: ...
    def get_dependency(self, dependency_id: str) -> TaskDependencyEdge: ...
    def insert_dependency(
        self,
        *,
        task_id: str,
        tenant_id: str,
        predecessor_step_id: str,
        successor_step_id: str,
        condition: str,
    ) -> str: ...
    def insert_dependency_revision(
        self,
        *,
        dependency_id: str,
        task_id: str,
        tenant_id: str,
        revision: int,
        predecessor_step_id: str,
        successor_step_id: str,
        condition: str,
        created_by: str,
        status: str = "active",
    ) -> str: ...
    def set_initial_dependency_pointer(self, dependency_id: str, revision_id: str) -> int: ...
    def advance_dependency_pointer(
        self,
        dependency_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        condition: str,
    ) -> int: ...
    def dependency_for_pair(
        self, task_id: str, predecessor_step_id: str, successor_step_id: str
    ) -> TaskDependencyEdge | None: ...
    def dependency_revision_number(self, revision_id: str) -> int: ...
    def get_trigger(self, trigger_id: str) -> TaskTriggerCurrent: ...
    def get_trigger_revision(self, revision_id: str) -> TaskTriggerRevision: ...
    def current_trigger_revision_row(self, trigger_id: str) -> TaskTriggerRevision: ...
    def insert_trigger(
        self,
        *,
        task_id: str,
        task_step_id: str | None,
        tenant_id: str,
        agent_id: str,
        kind: str,
        timezone: str,
        catch_up_policy: str,
        misfire_grace_us: int,
        max_occurrences_per_run: int,
        enabled: bool,
        next_fire_at_us: int | None,
    ) -> str: ...
    def insert_trigger_revision(
        self,
        *,
        trigger_id: str,
        task_id: str,
        tenant_id: str,
        revision: int,
        kind: str,
        task_step_id: str | None,
        schedule_spec: dict[str, object] | None,
        condition_spec: dict[str, object] | None,
        timezone: str,
        catch_up_policy: str,
        misfire_grace_us: int,
        max_occurrences_per_run: int,
        enabled: bool,
        created_by: str,
    ) -> str: ...
    def set_initial_trigger_pointer(self, trigger_id: str, revision_id: str) -> int: ...
    def advance_trigger_pointer(
        self,
        trigger_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        enabled: bool | None = None,
        spec: TaskTriggerRevision | None = None,
        next_fire_at_us: int | None = None,
    ) -> int: ...
    def raise_trigger_pointer_mismatch(self, trigger_id: str, expected: int) -> None: ...
    def set_trigger_schedule_state(
        self,
        trigger_id: str,
        *,
        expected_fire_at_us: int | None,
        next_fire_at_us: int | None,
        last_scan_us: int | None = None,
    ) -> int: ...
    def triggers_for_scan(
        self, tenant_id: str, agent_id: str, *, now_us: int, limit: int = 500
    ) -> Sequence[TaskTriggerCurrent]: ...
    def triggers_for_task(self, task_id: str) -> Sequence[TaskTriggerCurrent]: ...
    def insert_occurrence(
        self,
        *,
        trigger_id: str,
        trigger_revision: int,
        tenant_id: str,
        scheduled_at_us: int,
        occurrence_key: str,
        status: str,
        reason_code: str | None = None,
    ) -> tuple[str, bool]: ...
    def get_occurrence(self, occurrence_id: str) -> TriggerOccurrence: ...
    def attach_occurrence_event(self, occurrence_id: str, *, cognitive_event_id: str) -> int: ...
    def occurrences_for_trigger(
        self, trigger_id: str, *, limit: int = 200
    ) -> Sequence[TriggerOccurrence]: ...
