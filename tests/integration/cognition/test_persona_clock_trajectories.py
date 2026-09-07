"""W03: replay UTC jumps, worker pauses and stale expiry jobs three times."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

from iris_memory_core.application.outbox import OutboxService
from iris_memory_core.application.persona import PersonaService
from iris_memory_core.application.provisioning import ProvisioningService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.jobs.worker import OutboxWorker, phase9_handlers
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.cognition import test_persona

persona_world = test_persona.persona_world


def test_three_identical_utc_jump_pause_restart_trajectories(
    persona_world: dict[str, object],
) -> None:
    store = cast(Store, persona_world["store"])
    clock = cast(MutableClock, persona_world["clock"])
    initial_admin = cast(AccessContext, persona_world["admin"])
    initial_app = cast(AccessContext, persona_world["app"])
    start = int(datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC).timestamp() * 1_000_000)

    def run(attempt: int) -> list[tuple[Any, ...]]:
        clock.set(start)
        agent = ProvisioningService(store).create_agent(initial_admin, f"trajectory-{attempt}")
        admin = replace(initial_admin, agent_ids=frozenset({agent.id}))
        app = replace(initial_app, agent_ids=frozenset({agent.id}))
        service = PersonaService(store, clock, IdempotencyManager(store))
        worker = OutboxWorker(
            OutboxService(store, clock), phase9_handlers(clock), owner=f"before-restart-{attempt}"
        )
        trace: list[tuple[Any, ...]] = []

        def observe(label: str, revision: int, mood: float) -> None:
            current = service.current(admin, agent.id).state
            assert current is not None
            assert current.revision == revision, (attempt, label)
            assert json.loads(current.state_json) == {"mood": mood}, (attempt, label)
            trace.append(
                (label, clock.now_us() - start, revision, mood, current.expires_us - start)
            )

        service.update_state(
            app,
            agent.id,
            expected_revision=0,
            state={"mood": 0.8},
            baseline={"mood": 0.0},
            ttl_us=2_000_000,
        )
        observe("initial", 1, 0.8)
        clock.advance(750_000)
        service.update_state(
            app,
            agent.id,
            expected_revision=1,
            state={"mood": 0.4},
            baseline={"mood": -0.1},
            ttl_us=4_000_000,
        )
        observe("replaced", 2, 0.4)
        clock.set(start + 2_000_000)
        assert clock.now().isoformat() == "2026-04-01T00:00:01+00:00"
        assert worker.run_once()["completed"] == 1
        observe("old-job-fenced", 2, 0.4)
        clock.set(start + 1_000_000)
        assert worker.run_once()["completed"] == 0
        assert service.expire_due_states() == 0
        observe("clock-backward", 2, 0.4)
        # No worker runs across the forward jump: reads retain the stored Current.
        clock.set(start + 6_000_000)
        observe("worker-paused-past-expiry", 2, 0.4)
        clock.set(start + 3_000_000)
        assert service.expire_due_states() == 0
        observe("rollback-delays-expiry", 2, 0.4)
        # Restart uses the real deterministic scan, then drains the late old job.
        clock.set(start + 5_000_000)
        service = PersonaService(store, clock, IdempotencyManager(store))
        worker = OutboxWorker(
            OutboxService(store, clock), phase9_handlers(clock), owner=f"after-restart-{attempt}"
        )
        assert service.expire_due_states() == 1
        observe("restart-catch-up", 3, -0.1)
        assert worker.run_once()["completed"] == 1
        assert service.expire_due_states() == 0
        observe("late-job-fenced-after-catch-up", 3, -0.1)
        clock.set(start + 1_000_000)
        assert service.expire_due_states() == 0
        observe("rollback-cannot-resurrect", 3, -0.1)
        clock.advance(100_000)
        service.update_state(
            app,
            agent.id,
            expected_revision=3,
            state={"mood": 0.6},
            baseline={"mood": 0.2},
            ttl_us=6_000_000,
        )
        observe("newer-state-after-rollback", 4, 0.6)
        clock.set(start + 8_000_000)
        assert worker.run_once()["completed"] == 1
        observe("new-state-expires-once", 5, 0.2)
        assert service.expire_due_states() == 0
        assert worker.run_once()["completed"] == 0
        return trace

    trajectories = [run(attempt) for attempt in range(3)]
    assert trajectories[0] == trajectories[1] == trajectories[2]
    print(json.dumps({"W03_clock_trajectory": trajectories[0], "identical_replays": 3}))
