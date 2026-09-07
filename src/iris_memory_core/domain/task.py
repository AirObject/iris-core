"""Task, TaskStep, TaskDependency and TaskTrigger domain rules (§11).

Tasks are formal, persistent plans. Conversation/background extraction can
only create ``proposed`` tasks; activation requires an explicit tool call, a
deterministic tenant policy or an administrator (§11.5). Step completion is
an independent transition, and a step that declares an external
``expected_effect`` must cite committed successful Observations (or
equivalent evidence) — delivery, ACK and expiry of a CognitiveEvent are
NEVER completion evidence.

Dependencies are same-task only in v1; writes run cycle detection, and
``ready`` is computed deterministically from the stored dependency states,
never inferred from natural language.

Triggers are declarative only: allowlisted fields and operators, no eval,
no scripts, no implicit tool execution (§11.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from iris_memory_core.domain.errors import InvalidRequestError, TaskDependencyCycleError
from iris_memory_core.domain.hashing import request_fingerprint
from iris_memory_core.domain.schedule import (
    ALLOWED_CATCH_UP_POLICIES,
    ScheduleSpec,
    parse_schedule_spec,
    validate_timezone,
)

MAX_TASK_TITLE_CHARS = 500
MAX_TASK_GOAL_CHARS = 8_000
MAX_STEP_KEY_CHARS = 256
MAX_NEXT_ACTION_CHARS = 2_000
MAX_PROGRESS_NOTE_CHARS = 8_000
MAX_STEP_DESCRIPTION_CHARS = 8_000
MAX_EXPECTED_EFFECT_CHARS = 2_000

#: Origins that may create a task at all. Conversation/background extraction
#: is pinned to ``proposed`` (§11.5) — see ``may_activate``.
TASK_ORIGINS = frozenset({"explicit_tool", "admin", "policy", "conversation", "background"})
ORIGINS_THAT_CANNOT_ACTIVATE = frozenset({"conversation", "background"})

#: Resource types accepted as completion evidence: REAL external effects
#: only. ``observation`` rows are validated against their committed effect
#: state by the application layer; a CognitiveEvent is a reminder, and an
#: ACK/Delivered/Expired event must never masquerade as an effect (§12.2).
#: ``artifact`` re-enters the set in Phase 5 together with its canonical
#: validator (ADR-0013 §5): the artifact repository provides existence,
#: tenant/agent, scope-envelope, status and tombstone checks, so an artifact
#: ref is validated against its own row — not the caller's say-so.
#: ``cognitive_event`` stays structurally excluded.
EVIDENCE_RESOURCE_TYPES = frozenset({"observation", "artifact"})


class TaskStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    WAITING = "waiting"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


#: §11.5 state machine. ``proposed`` only leaves via explicit activation or
#: cancellation; completed/cancelled tasks may be archived for bookkeeping;
#: archived is terminal (ADR-0004: history never rewinds).
TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    TaskStatus.PROPOSED.value: frozenset({TaskStatus.ACTIVE.value, TaskStatus.CANCELLED.value}),
    TaskStatus.ACTIVE.value: frozenset(
        {
            TaskStatus.WAITING.value,
            TaskStatus.BLOCKED.value,
            TaskStatus.COMPLETED.value,
            TaskStatus.CANCELLED.value,
        }
    ),
    TaskStatus.WAITING.value: frozenset(
        {
            TaskStatus.ACTIVE.value,
            TaskStatus.BLOCKED.value,
            TaskStatus.COMPLETED.value,
            TaskStatus.CANCELLED.value,
        }
    ),
    TaskStatus.BLOCKED.value: frozenset(
        {TaskStatus.ACTIVE.value, TaskStatus.WAITING.value, TaskStatus.CANCELLED.value}
    ),
    TaskStatus.COMPLETED.value: frozenset({TaskStatus.ARCHIVED.value}),
    TaskStatus.CANCELLED.value: frozenset({TaskStatus.ARCHIVED.value}),
    TaskStatus.ARCHIVED.value: frozenset(),
}


class TaskStepStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


#: Step transitions. ``pending → ready`` is NOT in the caller's hands: readiness
#: is derived from dependencies (``compute_step_status``) and only ever
#: entered from ``pending``; the transition endpoint rejects any manual move
#: to ``ready`` so the two can never disagree.
TASK_STEP_TRANSITIONS: dict[str, frozenset[str]] = {
    TaskStepStatus.PENDING.value: frozenset({TaskStepStatus.WAITING.value}),
    TaskStepStatus.READY.value: frozenset(
        {
            TaskStepStatus.IN_PROGRESS.value,
            TaskStepStatus.WAITING.value,
            TaskStepStatus.SKIPPED.value,
            TaskStepStatus.CANCELLED.value,
        }
    ),
    TaskStepStatus.IN_PROGRESS.value: frozenset(
        {
            TaskStepStatus.WAITING.value,
            TaskStepStatus.BLOCKED.value,
            TaskStepStatus.COMPLETED.value,
            TaskStepStatus.CANCELLED.value,
        }
    ),
    TaskStepStatus.WAITING.value: frozenset(
        {
            TaskStepStatus.PENDING.value,
            TaskStepStatus.IN_PROGRESS.value,
            TaskStepStatus.BLOCKED.value,
            TaskStepStatus.CANCELLED.value,
        }
    ),
    TaskStepStatus.BLOCKED.value: frozenset(
        {
            TaskStepStatus.PENDING.value,
            TaskStepStatus.IN_PROGRESS.value,
            TaskStepStatus.WAITING.value,
            TaskStepStatus.CANCELLED.value,
        }
    ),
    TaskStepStatus.COMPLETED.value: frozenset(),
    TaskStepStatus.SKIPPED.value: frozenset(),
    TaskStepStatus.CANCELLED.value: frozenset(),
}


class TaskOwnerKind(StrEnum):
    AGENT = "agent"
    JOINT = "joint"
    ENTITY = "entity"
    SPACE_GROUP = "space_group"


class DependencyCondition(StrEnum):
    COMPLETED = "completed"
    COMPLETED_OR_SKIPPED = "completed_or_skipped"


#: Satisfied predecessor statuses per condition.
CONDITION_SATISFIED_BY: dict[str, frozenset[str]] = {
    DependencyCondition.COMPLETED.value: frozenset({TaskStepStatus.COMPLETED.value}),
    DependencyCondition.COMPLETED_OR_SKIPPED.value: frozenset(
        {TaskStepStatus.COMPLETED.value, TaskStepStatus.SKIPPED.value}
    ),
}


class TriggerKind(StrEnum):
    AT_TIME = "at_time"
    RECURRENCE = "recurrence"
    OBSERVATION_KIND = "observation_kind"
    STATE_CONDITION = "state_condition"
    TASK_TRANSITION = "task_transition"


ALL_TRIGGER_KINDS = frozenset(item.value for item in TriggerKind)

#: The declarative operator allowlist for ``condition_spec`` (§11.4). Anything
#: outside this table is rejected — there is no eval, no script, no fallback.
CONDITION_OPERATORS = frozenset(
    {"eq", "ne", "lt", "le", "gt", "ge", "exists", "absent", "contains"}
)

#: Fields a condition_spec may use, per trigger kind. Unknown keys are
#: invalid_request, not ignored: a trigger is a stored program, and silently
#: dropping part of it would change its meaning.
_ALLOWED_SPEC_KEYS: dict[str, frozenset[str]] = {
    TriggerKind.OBSERVATION_KIND.value: frozenset({"observation_kind", "role"}),
    TriggerKind.STATE_CONDITION.value: frozenset({"namespace", "key", "operator", "value"}),
    TriggerKind.TASK_TRANSITION.value: frozenset(
        {"task_id", "task_step_id", "from_status", "to_status"}
    ),
}


class InvalidTaskError(InvalidRequestError):
    """Raised when a task write violates the §11 contract."""


class InvalidConditionSpecError(InvalidRequestError):
    """Malformed or non-declarative condition spec (§11.4)."""


# ---------------------------------------------------------------------------
# Tasks


def validate_task_transition(current: str, target: str) -> None:
    if current not in TASK_TRANSITIONS:
        raise InvalidTaskError(f"unknown task status: {current!r}")
    if target not in TASK_TRANSITIONS:
        raise InvalidTaskError(f"unknown task status: {target!r}")
    if target not in TASK_TRANSITIONS[current]:
        raise InvalidTaskError(f"task cannot transition from {current!r} to {target!r}")


def may_activate(*, origin: str, is_admin: bool) -> bool:
    """§11.5: only explicit tools, deterministic policy or admins activate."""
    if origin in ORIGINS_THAT_CANNOT_ACTIVATE:
        return False
    if origin in ("explicit_tool", "policy"):
        return True
    return is_admin


def task_scope_key(
    tenant_id: str,
    agent_id: str,
    space_group_id: str | None,
    space_id: str | None,
    session_id: str | None,
) -> str:
    return "|".join((tenant_id, agent_id, space_group_id or "", space_id or "", session_id or ""))


@dataclass(frozen=True, slots=True)
class TaskCurrent:
    id: str
    tenant_id: str
    agent_id: str
    space_group_id: str | None
    space_id: str | None
    session_id: str | None
    scope_key: str
    parent_task_id: str | None
    title: str
    owner_kind: str
    owner_entity_id: str | None
    status: str
    priority: int
    next_action: str | None
    due_at_us: int | None
    completed_us: int | None
    current_revision: int
    created_us: int
    updated_us: int


@dataclass(frozen=True, slots=True)
class TaskRevision:
    id: str
    task_id: str
    tenant_id: str
    revision: int
    title: str
    goal: str
    owner_kind: str
    owner_entity_id: str | None
    privacy_labels: tuple[str, ...]
    source_refs: tuple[dict[str, object], ...]
    status: str
    priority: int
    next_action: str | None
    progress_note: str | None
    due_at_us: int | None
    completed_us: int | None
    created_us: int
    created_by: str


# ---------------------------------------------------------------------------
# Steps


def validate_step_transition(current: str, target: str) -> None:
    if current not in TASK_STEP_TRANSITIONS:
        raise InvalidTaskError(f"unknown task step status: {current!r}")
    if target not in TASK_STEP_TRANSITIONS:
        raise InvalidTaskError(f"unknown task step status: {target!r}")
    if target not in TASK_STEP_TRANSITIONS[current]:
        raise InvalidTaskError(f"task step cannot transition from {current!r} to {target!r}")


@dataclass(frozen=True, slots=True)
class TaskStepCurrent:
    id: str
    task_id: str
    tenant_id: str
    stable_key: str
    title: str
    ordinal: int
    status: str
    current_revision: int
    started_us: int | None
    completed_us: int | None
    created_us: int
    updated_us: int


@dataclass(frozen=True, slots=True)
class TaskStepRevision:
    id: str
    step_id: str
    task_id: str
    tenant_id: str
    revision: int
    stable_key: str
    title: str
    description: str | None
    privacy_labels: tuple[str, ...]
    status: str
    ordinal: int
    expected_effect: str | None
    completion_evidence_refs: tuple[dict[str, object], ...]
    started_us: int | None
    completed_us: int | None
    created_us: int
    created_by: str


@dataclass(frozen=True, slots=True)
class TaskDependencyEdge:
    dependency_id: str
    task_id: str
    predecessor_step_id: str
    successor_step_id: str
    condition: str
    current_revision: int
    current_revision_id: str
    created_us: int
    updated_us: int
    status: str = "active"


def would_create_cycle(
    edges: list[tuple[str, str]],
    *,
    predecessor_step_id: str,
    successor_step_id: str,
) -> bool:
    """Would adding predecessor→successor create a cycle in the step DAG?

    Pure function over the existing edge list (v1: one task's steps only,
    §11.3). A cycle exists iff the new successor can already reach the new
    predecessor.
    """
    if predecessor_step_id == successor_step_id:
        return True
    adjacency: dict[str, list[str]] = {}
    for source, target in edges:
        adjacency.setdefault(source, []).append(target)
    stack = [successor_step_id]
    seen: set[str] = set()
    while stack:
        node = stack.pop()
        if node == predecessor_step_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adjacency.get(node, ()))
    return False


def dependency_satisfied(condition: str, predecessor_status: str) -> bool:
    satisfied = CONDITION_SATISFIED_BY.get(condition)
    if satisfied is None:
        raise InvalidTaskError(f"unknown dependency condition: {condition!r}")
    return predecessor_status in satisfied


def compute_step_status(
    *,
    current_status: str,
    predecessor_statuses: list[tuple[str, str]],
) -> str:
    """Deterministically derive ``pending`` vs ``ready`` from dependencies.

    ``predecessor_statuses`` is a list of ``(condition, status)`` pairs for
    every dependency whose successor is this step. Only ``pending`` and
    ``ready`` are derived — both directions: adding a dependency RETRACTS
    readiness just like completing a predecessor grants it. Every other
    status is authoritative and untouched — readiness never overrides an
    in-progress or terminal step.
    """
    if current_status not in (TaskStepStatus.PENDING.value, TaskStepStatus.READY.value):
        return current_status
    if all(dependency_satisfied(condition, status) for condition, status in predecessor_statuses):
        return TaskStepStatus.READY.value
    return TaskStepStatus.PENDING.value


# ---------------------------------------------------------------------------
# Triggers


@dataclass(frozen=True, slots=True)
class TaskTriggerCurrent:
    id: str
    task_id: str
    task_step_id: str | None
    tenant_id: str
    agent_id: str
    kind: str
    timezone: str
    catch_up_policy: str
    misfire_grace_us: int
    max_occurrences_per_run: int
    enabled: bool
    next_fire_at_us: int | None
    last_scan_us: int | None
    current_revision: int
    created_us: int
    updated_us: int


@dataclass(frozen=True, slots=True)
class TaskTriggerRevision:
    id: str
    trigger_id: str
    task_id: str
    tenant_id: str
    revision: int
    kind: str
    task_step_id: str | None
    schedule_spec: dict[str, object] | None
    condition_spec: dict[str, object] | None
    timezone: str
    catch_up_policy: str
    misfire_grace_us: int
    max_occurrences_per_run: int
    enabled: bool
    created_us: int
    created_by: str


@dataclass(frozen=True, slots=True)
class TriggerOccurrence:
    """One planned firing of one trigger revision (immutable, idempotent)."""

    id: str
    trigger_id: str
    trigger_revision: int
    tenant_id: str
    scheduled_at_us: int
    occurrence_key: str
    status: str
    reason_code: str | None
    cognitive_event_id: str | None
    created_us: int


def parse_trigger_schedule_spec(kind: str, spec: dict[str, object] | None) -> ScheduleSpec | None:
    """Parse the restricted recurrence/at-time schedule grammar (ADR-0009 §5).

    Recurrence delegates to the Phase 2 parser so ``kind`` and the DST
    policies (``dst_missing`` / ``dst_ambiguous``) carry their declared
    semantics — a wrapper that rebuilt the spec silently dropped them.
    """
    if kind == TriggerKind.RECURRENCE.value:
        if not isinstance(spec, dict):
            raise InvalidConditionSpecError("recurrence trigger requires schedule_spec")
        normalized = dict(spec)
        if "kind" not in normalized:
            # Bare legacy shapes stay accepted: the grammar is inferable.
            normalized["kind"] = (
                "interval" if isinstance(normalized.get("every_seconds"), int) else "daily"
            )
        return parse_schedule_spec(normalized)
    if kind == TriggerKind.AT_TIME.value:
        if not isinstance(spec, dict) or "at_us" not in spec:
            raise InvalidConditionSpecError("at_time trigger requires schedule_spec.at_us")
        return None
    return None


def validate_condition_spec(kind: str, spec: dict[str, object] | None) -> dict[str, object] | None:
    """Validate a declarative condition spec against the allowlist (§11.4).

    Accepts only known keys per kind and allowlisted operators; ``value`` is
    the one field with a typed grammar (string or number, narrowed by the
    operator). The result is the canonicalized spec — never executed, only
    compared.
    """
    if kind in (TriggerKind.AT_TIME.value, TriggerKind.RECURRENCE.value):
        if spec is not None:
            raise InvalidConditionSpecError(
                f"{kind} triggers take schedule_spec, not condition_spec"
            )
        return None
    if not isinstance(spec, dict):
        raise InvalidConditionSpecError(f"{kind} trigger requires condition_spec")
    allowed = _ALLOWED_SPEC_KEYS[kind]
    unknown = sorted(set(spec) - allowed)
    if unknown:
        raise InvalidConditionSpecError(
            f"condition_spec keys outside the allowlist: {unknown}", details={"keys": unknown}
        )
    for key, value in spec.items():
        if kind == TriggerKind.STATE_CONDITION.value and key == "value":
            continue  # typed below against the declared operator
        if not isinstance(value, str) or not value:
            raise InvalidConditionSpecError(f"condition_spec.{key} must be a non-empty string")
    if kind == TriggerKind.STATE_CONDITION.value:
        operator = spec.get("operator")
        if operator not in CONDITION_OPERATORS:
            raise InvalidConditionSpecError(
                f"operator must be one of {sorted(CONDITION_OPERATORS)}",
                details={"operator": operator if isinstance(operator, str) else None},
            )
        namespace_value = spec.get("namespace")
        key_value = spec.get("key")
        if not namespace_value or not key_value:
            raise InvalidConditionSpecError("state_condition requires namespace and key")
        if operator not in ("exists", "absent") and "value" not in spec:
            # A comparison without a comparand is a malformed program.
            raise InvalidConditionSpecError(f"operator {operator!r} requires a value")
        comparand = spec.get("value")
        # Ordered comparison is numeric-only: every allowlisted operator must
        # have at least one legal input shape.
        if operator in ("lt", "le", "gt", "ge") and (
            isinstance(comparand, bool) or not isinstance(comparand, (int, float))
        ):
            raise InvalidConditionSpecError(f"operator {operator!r} requires a numeric value")
        if operator == "contains" and not isinstance(comparand, str):
            raise InvalidConditionSpecError("operator 'contains' requires a string value")
    if kind == TriggerKind.TASK_TRANSITION.value and "to_status" not in spec:
        raise InvalidConditionSpecError("task_transition requires to_status")
    return dict(spec)


def evaluate_state_condition(spec: dict[str, object], value: object | None) -> bool:
    """Pure comparison of a state value against a declarative spec.

    ``value is None`` means "no current state record". Only scalar-friendly
    operators are supported; ``contains`` applies to string values.
    """
    operator = str(spec["operator"])
    if operator == "exists":
        return value is not None
    if operator == "absent":
        return value is None
    if value is None:
        return False
    expected = spec.get("value")
    if operator == "contains":
        return isinstance(value, str) and isinstance(expected, str) and expected in value
    # Narrow to comparable scalars; anything else compares by string form.
    left: float | int | str = value if isinstance(value, (int, float, str)) else str(value)
    right: float | int | str = (
        expected if isinstance(expected, (int, float, str)) else str(expected)
    )
    if isinstance(left, bool) or isinstance(right, bool):
        # Booleans only compare by equality; ordered comparison is meaningless.
        if operator in ("eq", "ne"):
            return (operator == "eq") == (left == right)
        return False
    try:
        if operator == "eq":
            return left == right
        if operator == "ne":
            return left != right
        if isinstance(left, str) or isinstance(right, str):
            # A stored program whose live state value is not orderable
            # against its numeric comparand simply does not match; raising
            # here would let one odd value abort the whole agent scan.
            return False
        if operator == "lt":
            return left < right
        if operator == "le":
            return left <= right
        if operator == "gt":
            return left > right
        if operator == "ge":
            return left >= right
    except TypeError as error:
        raise InvalidConditionSpecError("condition values are not comparable") from error
    raise InvalidConditionSpecError(f"unknown operator: {operator!r}")


def occurrence_key_for_time(trigger_id: str, trigger_revision: int, scheduled_at_us: int) -> str:
    """Stable identity of one time-planned occurrence (§11.4).

    The unique constraint covers (trigger_id, trigger_revision,
    scheduled_at_us, occurrence_key), so replays, restarts and clock
    back-slew collapse onto the same row.
    """
    return f"t:{trigger_id}:{trigger_revision}:{scheduled_at_us}"


def occurrence_key_for_observation(
    trigger_id: str, trigger_revision: int, observation_id: str
) -> str:
    """Identity of one observation-driven occurrence."""
    return f"o:{trigger_id}:{trigger_revision}:{observation_id}"


def occurrence_key_for_state(trigger_id: str, trigger_revision: int, observed_us: int) -> str:
    """Identity of one state-condition firing, keyed by the state revision time."""
    return f"s:{trigger_id}:{trigger_revision}:{observed_us}"


def occurrence_key_for_transition(
    trigger_id: str, trigger_revision: int, aggregate_revision: int
) -> str:
    """Identity of one task-transition firing, keyed by the target revision."""
    return f"x:{trigger_id}:{trigger_revision}:{aggregate_revision}"


def validate_trigger_common(
    *,
    kind: str,
    timezone: str,
    catch_up_policy: str,
    misfire_grace_us: int,
    max_occurrences_per_run: int,
    enabled: bool,
) -> None:
    if kind not in ALL_TRIGGER_KINDS:
        raise InvalidTaskError(f"unknown trigger kind: {kind!r}")
    validate_timezone(timezone)
    if catch_up_policy not in ALLOWED_CATCH_UP_POLICIES:
        raise InvalidTaskError(f"unknown catch_up_policy: {catch_up_policy!r}")
    if misfire_grace_us < 0:
        raise InvalidTaskError("misfire_grace_us must be non-negative")
    if not 1 <= max_occurrences_per_run <= 1_000:
        raise InvalidTaskError("max_occurrences_per_run must be within 1..1000")
    if not isinstance(enabled, bool):
        raise InvalidTaskError("enabled must be a boolean")


def trigger_fingerprint(
    *,
    task_id: str,
    task_step_id: str | None,
    kind: str,
    schedule_spec: dict[str, object] | None,
    condition_spec: dict[str, object] | None,
    timezone: str,
    catch_up_policy: str,
    misfire_grace_us: int,
    max_occurrences_per_run: int,
    enabled: bool,
) -> str:
    return request_fingerprint(
        "task:trigger",
        {
            "task_id": task_id,
            "task_step_id": task_step_id,
            "kind": kind,
            "schedule_spec": schedule_spec,
            "condition_spec": condition_spec,
            "timezone": timezone,
            "catch_up_policy": catch_up_policy,
            "misfire_grace_us": misfire_grace_us,
            "max_occurrences_per_run": max_occurrences_per_run,
            "enabled": enabled,
        },
    )


def task_write_fingerprint(operation: str, payload: dict[str, Any]) -> str:
    return request_fingerprint(operation, payload)


__all__ = [
    "ALL_TRIGGER_KINDS",
    "CONDITION_OPERATORS",
    "CONDITION_SATISFIED_BY",
    "EVIDENCE_RESOURCE_TYPES",
    "ORIGINS_THAT_CANNOT_ACTIVATE",
    "TASK_ORIGINS",
    "TASK_STEP_TRANSITIONS",
    "TASK_TRANSITIONS",
    "DependencyCondition",
    "InvalidConditionSpecError",
    "InvalidTaskError",
    "TaskCurrent",
    "TaskDependencyCycleError",
    "TaskDependencyEdge",
    "TaskOwnerKind",
    "TaskRevision",
    "TaskStatus",
    "TaskStepCurrent",
    "TaskStepRevision",
    "TaskStepStatus",
    "TaskTriggerCurrent",
    "TaskTriggerRevision",
    "TriggerKind",
    "TriggerOccurrence",
    "compute_step_status",
    "dependency_satisfied",
    "evaluate_state_condition",
    "may_activate",
    "occurrence_key_for_observation",
    "occurrence_key_for_state",
    "occurrence_key_for_time",
    "occurrence_key_for_transition",
    "parse_trigger_schedule_spec",
    "task_scope_key",
    "task_write_fingerprint",
    "trigger_fingerprint",
    "validate_condition_spec",
    "validate_step_transition",
    "validate_task_transition",
    "validate_trigger_common",
    "would_create_cycle",
]
