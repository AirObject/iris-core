"""Phase 6 usage report gates (§18.7, ADR-0014 §7).

Four-stage persistence, the subset property (≥200 generated cases),
forgery rejection (foreign/cross-tenant candidates = 0 successes), the
echo completeness rule, idempotent merge (100 repeats → one logical row) and
the guarantee that usage never mutates canonical confidence.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from iris_memory_core.application.recall import (
    RecallUsageReportInput,
    RecallUsageService,
)
from iris_memory_core.domain.errors import InvalidRequestError, NotReadyError, RevisionMismatchError
from tests.integration.test_phase6_recall import World, _recall

PROPERTY_CASES = 200
REPEATS = 100


@pytest.fixture
def usage_world(world: World) -> tuple[World, RecallUsageService, list[str]]:
    from tests.integration.test_phase6_recall import _deterministic_world

    _deterministic_world(world)
    service = RecallUsageService(world.store, world.clock)
    result = _recall(world, request_id="usage-req", topic="deterministic quantum")
    ids = [candidate.candidate_id for candidate in result.candidates]
    assert ids, "the recall must return candidates for the usage gates"
    return world, service, ids


def _report(
    ids: list[str],
    *,
    returned: list[str] | None = None,
    host_selected: list[str] | None = None,
    model_visible: list[str] | None = None,
    persona_revision: int = 1,
    host_cycle: str = "cycle-1",
) -> RecallUsageReportInput:
    return RecallUsageReportInput(
        request_id="usage-req",
        host_cycle_id=host_cycle,
        returned_candidate_ids=tuple(returned if returned is not None else ids),
        host_selected_candidate_ids=tuple(host_selected if host_selected is not None else ids),
        model_visible_candidate_ids=tuple(model_visible if model_visible is not None else ids),
        persona_revision=persona_revision,
        reported_at_us=1_700_000_100_000_000,
    )


class TestSubsetProperty:
    def test_two_hundred_generated_subset_chains_are_accepted(
        self, usage_world: tuple[World, RecallUsageService, list[str]]
    ) -> None:
        world, service, ids = usage_world
        rng = random.Random(20260902)
        accepted = 0
        for case in range(PROPERTY_CASES):
            size = rng.randint(0, len(ids))
            selected = rng.sample(ids, size)
            visible = rng.sample(selected, rng.randint(0, len(selected)))
            result = service.report(
                world.access,
                _report(
                    ids, host_selected=selected, model_visible=visible, host_cycle=f"cycle-{case}"
                ),
            )
            assert result.created
            assert result.model_visible_count == len(visible)
            accepted += 1
        assert accepted == PROPERTY_CASES

    def test_broken_chains_are_always_rejected(
        self, usage_world: tuple[World, RecallUsageService, list[str]]
    ) -> None:
        world, service, ids = usage_world
        rng = random.Random(42)
        rejected = 0
        for case in range(PROPERTY_CASES):
            size = rng.randint(0, max(0, len(ids) - 1))
            selected = rng.sample(ids, size)
            # Inject a chain break: model_visible names an id NOT in host_selected.
            break_id = next(i for i in ids if i not in selected)
            with pytest.raises(InvalidRequestError):
                service.report(
                    world.access,
                    _report(
                        ids,
                        host_selected=selected,
                        model_visible=[break_id],
                        host_cycle=f"bad-chain-{case}",
                    ),
                )
            rejected += 1
        assert rejected == PROPERTY_CASES

    def test_foreign_candidate_ids_are_always_rejected(
        self, usage_world: tuple[World, RecallUsageService, list[str]]
    ) -> None:
        world, service, ids = usage_world
        forged = "cand:" + "f" * 16
        for case in range(PROPERTY_CASES):
            with pytest.raises(InvalidRequestError):
                service.report(
                    world.access,
                    _report(ids, host_selected=[*ids, forged], host_cycle=f"forged-{case}"),
                )


class TestForgery:
    def test_cross_request_forgery_succeeds_zero_times(
        self, usage_world: tuple[World, RecallUsageService, list[str]]
    ) -> None:
        world, service, ids = usage_world
        # A second, different request whose returned set differs.
        other = _recall(world, request_id="usage-req-2", topic="usage probe two")
        other_ids = [candidate.candidate_id for candidate in other.candidates]
        success = 0
        for case in range(PROPERTY_CASES):
            # Report ids belonging to the OTHER request against this one.
            mixed = [*ids[:1], *other_ids[:1]]
            try:
                service.report(
                    world.access,
                    _report(ids, returned=mixed, host_selected=mixed, host_cycle=f"x-{case}"),
                )
                success += 1
            except InvalidRequestError:
                pass
        assert success == 0

    def test_cross_tenant_report_is_denied(self, usage_world) -> None:  # type: ignore[no-untyped-def]
        _world, service, ids = usage_world
        from tests.conftest import access_for

        stranger = access_for("t2", agent_ids=frozenset({"nope"}))
        with pytest.raises((NotReadyError, InvalidRequestError)):
            service.report(
                stranger.access if hasattr(stranger, "access") else stranger, _report(ids)
            )

    def test_incomplete_echo_is_rejected(
        self, usage_world: tuple[World, RecallUsageService, list[str]]
    ) -> None:
        world, service, ids = usage_world
        with pytest.raises(InvalidRequestError):
            service.report(world.access, _report(ids, returned=ids[:-1]))

    def test_persona_revision_mismatch_is_rejected(
        self, usage_world: tuple[World, RecallUsageService, list[str]]
    ) -> None:
        world, service, ids = usage_world
        with pytest.raises(RevisionMismatchError):
            service.report(world.access, _report(ids, persona_revision=99))


class TestIdempotentMerge:
    def test_hundred_repeats_produce_one_logical_report(
        self, usage_world: tuple[World, RecallUsageService, list[str]]
    ) -> None:
        world, service, ids = usage_world
        outcomes = [
            service.report(world.access, _report(ids, host_cycle="cycle-repeat"))
            for _ in range(REPEATS)
        ]
        assert outcomes[0].created is True
        assert all(outcome.created is False for outcome in outcomes[1:])
        assert len({outcome.report_id for outcome in outcomes}) == 1
        with world.store.read() as tx:
            rows = tx.usage.reports_for_request("t1", "usage-req")
        repeat_rows = [row for row in rows if str(row["host_cycle_id"]) == "cycle-repeat"]
        assert len(repeat_rows) == 1

    def test_unknown_request_is_not_ready(
        self, usage_world: tuple[World, RecallUsageService, list[str]]
    ) -> None:
        world, service, ids = usage_world
        report = RecallUsageReportInput(
            request_id="req-missing",
            host_cycle_id="cycle-x",
            returned_candidate_ids=tuple(ids),
            host_selected_candidate_ids=tuple(ids),
            model_visible_candidate_ids=tuple(ids),
            persona_revision=1,
            reported_at_us=1,
        )
        with pytest.raises(NotReadyError):
            service.report(world.access, report)


class TestUsageNeverMutatesCanonicalState:
    def test_usage_leaves_claims_byte_identical(
        self, usage_world: tuple[World, RecallUsageService, list[str]]
    ) -> None:
        world, service, ids = usage_world
        import sqlite3

        def snapshot() -> list[tuple[Any, ...]]:
            connection = sqlite3.connect(world.store.runtime.database)
            try:
                return list(
                    connection.execute(
                        "SELECT id, confidence, importance, accessibility FROM claims ORDER BY id"
                    )
                )
            finally:
                connection.close()

        before = snapshot()
        for cycle in range(20):
            service.report(world.access, _report(ids, host_cycle=f"mut-{cycle}"))
        assert snapshot() == before

    def test_usage_rows_store_no_candidate_text(
        self, usage_world: tuple[World, RecallUsageService, list[str]]
    ) -> None:
        world, service, ids = usage_world
        service.report(world.access, _report(ids, host_cycle="cycle-scan"))
        with world.store.read() as tx:
            payload = str(tx.raw().execute("SELECT * FROM recall_usage_reports").fetchall()) + str(
                tx.raw().execute("SELECT * FROM recall_requests").fetchall()
            )
        for candidate in _recall(
            world, request_id="usage-req-scan", topic="usage probe"
        ).candidates:
            if candidate.text:
                assert candidate.text not in payload
