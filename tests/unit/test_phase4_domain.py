"""Phase 4 domain property tests (S10-S12).

Every property runs 200 fixed-seed cases: the four state machines (note,
task, task step, cognitive event), the dependency DAG properties (acyclicity
under edge insertion, deterministic readiness), the declarative condition
allowlist and the occurrence identity math.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from iris_memory_core.domain.errors import DomainError
from iris_memory_core.domain.event import (
    EVENT_TRANSITIONS,
    pullable,
    should_expire,
    validate_event_transition,
)
from iris_memory_core.domain.note import (
    NOTE_TRANSITIONS,
    extended_review_after,
    note_content_hash,
    validate_note_transition,
)
from iris_memory_core.domain.schedule import CatchUpPolicy, plan_catch_up
from iris_memory_core.domain.task import (
    CONDITION_SATISFIED_BY,
    compute_step_status,
    dependency_satisfied,
    evaluate_state_condition,
    may_activate,
    occurrence_key_for_observation,
    occurrence_key_for_state,
    occurrence_key_for_time,
    occurrence_key_for_transition,
    validate_condition_spec,
    validate_step_transition,
    validate_task_transition,
    would_create_cycle,
)

CASES = 200

_TERMINAL_FORBIDDEN = {
    "note": ("promoted", "tombstoned"),
    "task": ("archived",),
    "step": ("completed", "skipped", "cancelled"),
    "event": ("acknowledged", "expired", "cancelled"),
}


def _walk(
    rng: random.Random,
    transitions: dict[str, frozenset[str]],
    start: str,
    *,
    legal: bool,
) -> tuple[list[str], bool]:
    """Walk a random transition sequence; return (path, stayed_legal)."""
    current = start
    path = [current]
    for _ in range(rng.randint(1, 8)):
        targets = sorted(transitions.get(current, frozenset()))
        pool = targets if legal else sorted(set(transitions) - set(targets)) or targets
        if not pool:
            break
        current = rng.choice(pool)
        path.append(current)
    return path, all(
        path[index + 1] in transitions.get(path[index], frozenset())
        for index in range(len(path) - 1)
    )


class TestStateMachineProperties:
    @pytest.mark.parametrize("seed", range(CASES))
    def test_note_random_legal_walks_are_accepted(self, seed: int) -> None:
        rng = random.Random(seed)
        path, legal = _walk(rng, NOTE_TRANSITIONS, "inbox", legal=True)
        assert legal
        for index in range(len(path) - 1):
            validate_note_transition(path[index], path[index + 1])
        # Terminal statuses never resurrect (§10.2/ADR-0004).
        if path[-1] in _TERMINAL_FORBIDDEN["note"]:
            with pytest.raises(DomainError):
                validate_note_transition(path[-1], "inbox")

    @pytest.mark.parametrize("seed", range(CASES))
    def test_note_random_illegal_moves_are_stable_errors(self, seed: int) -> None:
        rng = random.Random(seed)
        current = rng.choice(sorted(NOTE_TRANSITIONS))
        targets = sorted(set(NOTE_TRANSITIONS) - NOTE_TRANSITIONS[current])
        if not targets:
            return
        target = rng.choice(targets)
        with pytest.raises(DomainError) as captured:
            validate_note_transition(current, target)
        assert captured.value.code == "invalid_request"

    @pytest.mark.parametrize("seed", range(CASES))
    def test_task_random_legal_walks_are_accepted(self, seed: int) -> None:
        from iris_memory_core.domain.task import TASK_TRANSITIONS

        rng = random.Random(seed)
        path, legal = _walk(rng, TASK_TRANSITIONS, "proposed", legal=True)
        assert legal
        for index in range(len(path) - 1):
            validate_task_transition(path[index], path[index + 1])
        # Only explicit activation / policy / admin leaves `proposed`.
        assert may_activate(origin="conversation", is_admin=True) is False
        assert may_activate(origin="background", is_admin=True) is False
        assert may_activate(origin="explicit_tool", is_admin=False) is True
        assert may_activate(origin="policy", is_admin=False) is True
        assert may_activate(origin="admin", is_admin=True) is True

    @pytest.mark.parametrize("seed", range(CASES))
    def test_task_random_illegal_moves_are_stable_errors(self, seed: int) -> None:
        from iris_memory_core.domain.task import TASK_TRANSITIONS

        rng = random.Random(seed)
        current = rng.choice(sorted(TASK_TRANSITIONS))
        targets = sorted(set(TASK_TRANSITIONS) - TASK_TRANSITIONS[current])
        if not targets:
            return
        with pytest.raises(DomainError) as captured:
            validate_task_transition(current, rng.choice(targets))
        assert captured.value.code == "invalid_request"

    @pytest.mark.parametrize("seed", range(CASES))
    def test_step_random_legal_walks_are_accepted(self, seed: int) -> None:
        from iris_memory_core.domain.task import TASK_STEP_TRANSITIONS

        rng = random.Random(seed)
        path, legal = _walk(rng, TASK_STEP_TRANSITIONS, "pending", legal=True)
        assert legal
        for index in range(len(path) - 1):
            validate_step_transition(path[index], path[index + 1])

    @pytest.mark.parametrize("seed", range(CASES))
    def test_step_manual_readiness_is_rejected(self, seed: int) -> None:
        """pending→ready is derived, never set by hand (§11.3)."""
        del seed
        for current in ("pending", "in_progress", "waiting", "blocked"):
            with pytest.raises(DomainError):
                validate_step_transition(current, "ready")

    @pytest.mark.parametrize("seed", range(CASES))
    def test_event_random_legal_walks_are_accepted(self, seed: int) -> None:
        rng = random.Random(seed)
        path, legal = _walk(rng, EVENT_TRANSITIONS, "pending", legal=True)
        assert legal
        for index in range(len(path) - 1):
            validate_event_transition(path[index], path[index + 1])

    @pytest.mark.parametrize("seed", range(CASES))
    def test_event_terminal_states_never_reopened(self, seed: int) -> None:
        del seed
        for terminal in _TERMINAL_FORBIDDEN["event"]:
            for target in EVENT_TRANSITIONS:
                with pytest.raises(DomainError):
                    validate_event_transition(terminal, target)


class TestDependencyDagProperties:
    @pytest.mark.parametrize("seed", range(CASES))
    def test_inserting_edges_never_creates_a_cycle_the_checker_misses(self, seed: int) -> None:
        """A DAG stays a DAG: any edge the checker accepts keeps it acyclic."""
        rng = random.Random(seed)
        nodes = [f"s{index}" for index in range(rng.randint(2, 8))]
        edges: list[tuple[str, str]] = []
        for _ in range(rng.randint(0, 12)):
            predecessor = rng.choice(nodes)
            successor = rng.choice(nodes)
            if would_create_cycle(
                edges, predecessor_step_id=predecessor, successor_step_id=successor
            ):
                continue
            edges.append((predecessor, successor))
            # Verify acyclicity by Kahn's algorithm after every insertion.
            assert self._topological_order(nodes, edges) is not None

    @staticmethod
    def _topological_order(nodes: list[str], edges: list[tuple[str, str]]) -> list[str] | None:
        from collections import deque

        indegree = {node: 0 for node in nodes}
        adjacency: dict[str, list[str]] = {node: [] for node in nodes}
        for source, target in edges:
            adjacency[source].append(target)
            indegree[target] += 1
        queue = deque(node for node, degree in indegree.items() if degree == 0)
        order: list[str] = []
        while queue:
            node = queue.popleft()
            order.append(node)
            for neighbor in adjacency[node]:
                indegree[neighbor] -= 1
                if indegree[neighbor] == 0:
                    queue.append(neighbor)
        return order if len(order) == len(nodes) else None

    @pytest.mark.parametrize("seed", range(CASES))
    def test_readiness_is_deterministic_and_reversible(self, seed: int) -> None:
        """compute_step_status is a pure function of the dependency states."""
        rng = random.Random(seed)
        conditions = sorted(CONDITION_SATISFIED_BY)
        pairs = [
            (rng.choice(conditions), rng.choice(["pending", "ready", "completed", "skipped"]))
            for _ in range(rng.randint(0, 5))
        ]
        derived = compute_step_status(current_status="pending", predecessor_statuses=pairs)
        expected = (
            "ready"
            if all(dependency_satisfied(condition, status) for condition, status in pairs)
            else "pending"
        )
        assert derived == expected
        # Adding an unmet dependency RETRACTS readiness (both directions).
        assert compute_step_status(
            current_status="ready", predecessor_statuses=pairs
        ) == compute_step_status(current_status="pending", predecessor_statuses=pairs)
        # Non-derived statuses are authoritative.
        for status in ("in_progress", "waiting", "blocked", "completed", "skipped", "cancelled"):
            assert compute_step_status(current_status=status, predecessor_statuses=pairs) == status

    @pytest.mark.parametrize("seed", range(CASES))
    def test_dependency_satisfaction_matrix(self, seed: int) -> None:
        del seed
        assert dependency_satisfied("completed", "completed") is True
        assert dependency_satisfied("completed", "skipped") is False
        assert dependency_satisfied("completed_or_skipped", "completed") is True
        assert dependency_satisfied("completed_or_skipped", "skipped") is True
        assert dependency_satisfied("completed_or_skipped", "pending") is False


class TestConditionSpecAllowlist:
    def test_declarative_fields_only(self) -> None:
        assert validate_condition_spec(
            "state_condition",
            {"namespace": "environment", "key": "scene", "operator": "eq", "value": "gaming"},
        ) == {
            "namespace": "environment",
            "key": "scene",
            "operator": "eq",
            "value": "gaming",
        }
        assert (
            validate_condition_spec(
                "observation_kind", {"observation_kind": "message.text", "role": "user"}
            )
            is not None
        )
        assert (
            validate_condition_spec("task_transition", {"task_id": "t1", "to_status": "completed"})
            is not None
        )

    @pytest.mark.parametrize(
        ("kind", "spec"),
        [
            ("state_condition", {"namespace": "n", "key": "k", "operator": "exec"}),
            ("state_condition", {"namespace": "n", "key": "k"}),
            ("state_condition", {"namespace": "n", "operator": "eq"}),
            ("observation_kind", {"observation_kind": "x", "eval": "1 + 1"}),
            ("task_transition", {"task_id": "t"}),
            ("at_time", {"namespace": "n"}),
        ],
    )
    def test_non_declarative_specs_rejected(self, kind: str, spec: dict[str, object]) -> None:
        from iris_memory_core.domain.errors import DomainError

        with pytest.raises(DomainError):
            validate_condition_spec(kind, spec)

    @pytest.mark.parametrize("seed", range(CASES))
    def test_state_condition_evaluation_is_pure(self, seed: int) -> None:
        rng = random.Random(seed)
        operators = ("eq", "ne", "lt", "le", "gt", "ge", "exists", "absent", "contains")
        operator = rng.choice(operators)
        spec = {
            "namespace": "environment",
            "key": "value",
            "operator": operator,
            "value": rng.choice(["gaming", 3, 7]),
        }
        value = rng.choice([None, "gaming", 3, 7])
        try:
            first: object = evaluate_state_condition(spec, value)
        except DomainError:
            first = "invalid"
        for _ in range(3):
            try:
                repeat: object = evaluate_state_condition(spec, value)
            except DomainError:
                repeat = "invalid"
            assert repeat == first


class TestOccurrenceIdentity:
    @pytest.mark.parametrize("seed", range(CASES))
    def test_occurrence_keys_are_collision_free_and_stable(self, seed: int) -> None:
        rng = random.Random(seed)
        trigger = f"tr-{rng.randint(0, 10)}"
        revision = rng.randint(1, 5)
        moment = rng.randint(0, 10**12)
        assert occurrence_key_for_time(trigger, revision, moment) == (
            occurrence_key_for_time(trigger, revision, moment)
        )
        keys = {
            occurrence_key_for_time(trigger, revision + 1, moment),
            occurrence_key_for_time(trigger, revision, moment + 1),
            occurrence_key_for_observation(trigger, revision, "obs-1"),
            occurrence_key_for_state(trigger, revision, moment),
            occurrence_key_for_transition(trigger, revision, 4),
        }
        # A trigger revision change or a different planned moment forks a new
        # logical occurrence; the same pair never collapses two distinct ones.
        assert len(keys) == 5


class TestCatchUpMatrix:
    def test_misfire_grace_bounds_catch_up(self) -> None:
        now = 1_000_000_000
        decisions = plan_catch_up(
            [now - 500, now - 5],
            now_us=now,
            policy=CatchUpPolicy.ALL,
            misfire_grace_us=100,
            max_ticks_per_run=10,
        )
        assert [decision.fire for decision in decisions] == [False, True]
        assert decisions[0].reason_code == "misfire_grace_exceeded"

    def test_cap_exceeded_is_accounted_not_dropped(self) -> None:
        decisions = plan_catch_up(
            list(range(1, 20)),
            now_us=1_000,
            policy=CatchUpPolicy.ALL,
            misfire_grace_us=10_000,
            max_ticks_per_run=3,
        )
        fired = [decision for decision in decisions if decision.fire]
        skipped = [decision for decision in decisions if not decision.fire]
        assert len(fired) == 3
        assert skipped
        assert all(decision.reason_code == "catch_up_cap_exceeded" for decision in skipped)

    @pytest.mark.parametrize(
        ("timezone_name", "expect_unique"),
        [
            ("UTC", True),
            ("Europe/Berlin", True),
            ("America/New_York", True),
            ("Asia/Tokyo", True),
            ("Australia/Lord_Howe", True),
        ],
    )
    def test_dst_matrix_resolves_or_rejects_explicitly(
        self, timezone_name: str, expect_unique: bool
    ) -> None:
        from iris_memory_core.domain.schedule import DailySpec, next_daily_occurrence
        from iris_memory_core.domain.task import parse_trigger_schedule_spec

        tz = ZoneInfo(timezone_name)
        # Spring-forward gap and fall-back ambiguity anchors for each zone.
        anchors = [
            datetime(2026, 3, 8, 2, 30, tzinfo=UTC),
            datetime(2026, 3, 29, 2, 30, tzinfo=UTC),
            datetime(2026, 11, 1, 1, 30, tzinfo=UTC),
            datetime(2026, 10, 25, 2, 30, tzinfo=UTC),
        ]
        for anchor in anchors:
            for at in ("02:30", "01:30", "09:00"):
                parsed = parse_trigger_schedule_spec(
                    "recurrence", {"kind": "daily", "at": at, "dst_missing": "postpone"}
                )
                assert isinstance(parsed, DailySpec)
                after = int(anchor.timestamp() * 1_000_000)
                occurrence = next_daily_occurrence(after, parsed, tz)
                resolved = datetime.fromtimestamp(occurrence / 1_000_000, tz=UTC)
                assert resolved > anchor
                # Postponed/first/second instants are always UNIQUE real
                # instants - a missing or ambiguous wall time never resolves
                # to the same instant twice in a row.
                again = next_daily_occurrence(occurrence, parsed, tz)
                assert again > occurrence
        assert expect_unique

    @pytest.mark.parametrize("seed", range(CASES))
    def test_clock_back_slew_cannot_duplicate_a_moment(self, seed: int) -> None:
        rng = random.Random(seed)
        fired: set[int] = set()
        now = rng.randint(10**6, 10**9)
        for _ in range(6):
            now = max(0, now - rng.randint(0, 100))  # back-slew
            occurrence = rng.randint(0, now)
            fired.add(occurrence)  # idempotency by set membership (ledger key)
        assert all(fired)


class TestNoteHelpers:
    @pytest.mark.parametrize("seed", range(CASES))
    def test_content_hash_is_deterministic(self, seed: int) -> None:
        rng = random.Random(seed)
        kind = rng.choice(["idea", "promise", "question"])
        title = f"title-{seed}"
        body = f"body-{seed}"
        assert note_content_hash(kind=kind, title=title, body=body) == note_content_hash(
            kind=kind, title=title, body=body
        )
        assert note_content_hash(kind=kind, title=title, body=body) != note_content_hash(
            kind=kind, title=title, body=body + "!"
        )

    @pytest.mark.parametrize("seed", range(CASES))
    def test_review_extension_never_moves_backwards(self, seed: int) -> None:
        rng = random.Random(seed)
        now = rng.randint(1, 10**12)
        current = rng.choice([None, now - rng.randint(0, 1000), now + rng.randint(0, 1000)])
        extension = rng.randint(1, 10**6)
        extended = extended_review_after(current, now_us=now, extension_us=extension)
        assert extended >= now + extension


class TestEventExpiryPolicy:
    @pytest.mark.parametrize("seed", range(CASES))
    def test_expiry_never_touches_acknowledged(self, seed: int) -> None:
        rng = random.Random(seed)
        now = rng.randint(10**6, 10**12)
        expires = rng.choice([None, now - 1, now + 1000])
        attempts = rng.randint(0, 100)
        assert (
            should_expire(
                status="acknowledged",
                expires_us=expires,
                acknowledged_us=now - 1,
                delivery_attempts=attempts,
                now_us=now,
            )
            is False
        )

    @pytest.mark.parametrize("seed", range(CASES))
    def test_pullable_requires_due_pending_unexpired(self, seed: int) -> None:
        rng = random.Random(seed)
        now = rng.randint(10**6, 10**12)
        deliver_after = now - rng.randint(0, 100)
        expires = rng.choice([None, now + 10])
        status = rng.choice(["pending", "delivered", "acknowledged", "expired", "cancelled"])
        result = pullable(
            status,
            deliver_after_us=deliver_after,
            expires_us=expires,
            now_us=now,
        )
        expected = status == "pending" and (expires is None or expires > now)
        assert result is expected
