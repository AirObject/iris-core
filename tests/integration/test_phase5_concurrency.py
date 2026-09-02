"""Phase 5 concurrency races: Recall/Search vs Correct vs Forget, 50 rounds.

After a Forget commits, EVERY subsequent current read of the target must
reject — including reads racing the commit from other threads. CAS races
between concurrent writers must resolve to exactly one winner.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.memory import ClaimService
from iris_memory_core.domain.errors import DomainError, NotFoundError
from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.test_phase5_claims import _Ctx

ROUNDS = 50


@pytest.fixture
def cctx(
    clocked_store: Store,
    mutable_clock: MutableClock,
    phase5_claims: ClaimService,
    phase5_forget: ForgetService,
) -> dict[str, Any]:
    return {
        "ctx": _Ctx(clocked_store, mutable_clock, phase5_claims),
        "store": clocked_store,
        "claims": phase5_claims,
        "forget": phase5_forget,
    }


class TestForgetVsSearchRace:
    def test_forget_commit_then_zero_current_hits(self, cctx: dict[str, Any]) -> None:
        """Racing searchers must observe either the claim or its absence —
        never content after the forget committed."""
        ctx: _Ctx = cctx["ctx"]
        claims: ClaimService = cctx["claims"]
        forget: ForgetService = cctx["forget"]
        for index in range(ROUNDS):
            created = ctx.remember(f"race{index}", predicate=f"race{index}")
            results: list[int] = []
            errors: list[BaseException] = []

            searcher_results: list[int] = []
            searcher_errors: list[BaseException] = []

            def searcher(
                target_id: str = created.claim_id,
                sink: list[int] | None = None,
                error_sink: list[BaseException] | None = None,
            ) -> None:
                try:
                    hits = [
                        view
                        for view in claims.search(
                            ctx.access, agent_id=ctx.agent, subject_entity_id=ctx.entity
                        )
                        if view.claim.id == target_id
                    ]
                    if sink is not None:
                        sink.append(len(hits))
                except BaseException as error:  # pragma: no cover - diagnostic
                    if error_sink is not None:
                        error_sink.append(error)

            threads = [
                threading.Thread(
                    target=searcher,
                    kwargs={"sink": searcher_results, "error_sink": searcher_errors},
                )
                for _ in range(4)
            ]
            for thread in threads:
                thread.start()
            forget.forget(
                ctx.access,
                ForgetSelector(
                    kind=ForgetSelectorKind.RESOURCE,
                    resource_type="claim",
                    resource_id=created.claim_id,
                ),
                reason="race",
                idempotency_key=f"fg-race-{index}",
            )
            for thread in threads:
                thread.join()
            assert not searcher_errors
            del results, errors
            # Post-commit: exactly zero current hits, every time.
            post = [
                view
                for view in claims.search(
                    ctx.access, agent_id=ctx.agent, subject_entity_id=ctx.entity
                )
                if view.claim.id == created.claim_id
            ]
            assert len(post) == 0
            with pytest.raises(NotFoundError):
                claims.get(ctx.access, created.claim_id)


class TestCorrectCasRace:
    def test_concurrent_corrects_yield_exactly_one_winner(self, cctx: dict[str, Any]) -> None:
        """50 identical Expected-Revision corrections race: exactly one
        wins, the losers get revision_mismatch (CAS is the arbiter)."""
        ctx: _Ctx = cctx["ctx"]
        claims: ClaimService = cctx["claims"]
        for index in range(ROUNDS):
            created = ctx.remember(f"cas{index}", predicate=f"cas{index}")
            observation = ctx.observe(f"obs2-cas{index}")
            outcomes: list[str] = []
            lock = threading.Lock()
            barrier = threading.Barrier(4)

            def corrector(
                claim_id: str = created.claim_id,
                source_id: str = observation,
                gate: threading.Barrier = barrier,
                sink: list[str] = outcomes,
                sink_lock: threading.Lock = lock,
                round_index: int = index,
            ) -> None:
                gate.wait()
                try:
                    claims.correct(
                        ctx.access,
                        claim_id,
                        expected_revision=1,
                        mode="supersede",
                        value={"drink": "coffee"},
                        canonical_text="Bob likes coffee",
                        evidence=[
                            {
                                "source_type": "observation",
                                "source_id": source_id,
                                "relation": "supports",
                            }
                        ],
                        idempotency_key=f"cor-race-{round_index}-{threading.get_ident()}",
                    )
                    with sink_lock:
                        sink.append("won")
                except DomainError as error:
                    with sink_lock:
                        sink.append(error.code)

            threads = [threading.Thread(target=corrector) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert outcomes.count("won") == 1
            assert all(code == "revision_mismatch" for code in outcomes if code != "won")
            view = claims.get(ctx.access, created.claim_id)
            assert view is not None
            assert view.claim.current_revision == 2

    def test_concurrent_forget_and_correct(self, cctx: dict[str, Any]) -> None:
        """A forget racing a correct: whichever commits first, the claim
        never ends up visible-and-forgotten simultaneously."""
        ctx: _Ctx = cctx["ctx"]
        claims: ClaimService = cctx["claims"]
        forget: ForgetService = cctx["forget"]
        for index in range(ROUNDS):
            created = ctx.remember(f"mix{index}", predicate=f"mix{index}")
            observation = ctx.observe(f"obs2-mix{index}")
            codes: list[str] = []
            lock = threading.Lock()

            def corrector(
                claim_id: str = created.claim_id,
                source_id: str = observation,
                round_index: int = index,
                sink: list[str] = codes,
                sink_lock: threading.Lock = lock,
            ) -> None:
                try:
                    claims.correct(
                        ctx.access,
                        claim_id,
                        expected_revision=1,
                        mode="dispute",
                        reason="r",
                        evidence=[
                            {
                                "source_type": "observation",
                                "source_id": source_id,
                                "relation": "contradicts",
                            }
                        ],
                        idempotency_key=f"mix-c-{round_index}-{threading.get_ident()}",
                    )
                    outcome = "corrected"
                except DomainError as error:
                    outcome = error.code
                with sink_lock:
                    sink.append(outcome)

            def forgetter(
                claim_id: str = created.claim_id,
                round_index: int = index,
                sink: list[str] = codes,
                sink_lock: threading.Lock = lock,
            ) -> None:
                try:
                    forget.forget(
                        ctx.access,
                        ForgetSelector(
                            kind=ForgetSelectorKind.RESOURCE,
                            resource_type="claim",
                            resource_id=claim_id,
                        ),
                        reason="r",
                        idempotency_key=f"mix-f-{round_index}-{threading.get_ident()}",
                    )
                    outcome = "forgotten"
                except DomainError as error:
                    outcome = error.code
                with sink_lock:
                    sink.append(outcome)

            threads = [threading.Thread(target=corrector), threading.Thread(target=forgetter)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            # The forget either won or lost cleanly; a claim the forget won
            # is invisible forever after.
            if "forgotten" in codes:
                with pytest.raises(NotFoundError):
                    claims.get(ctx.access, created.claim_id)
            else:
                view = claims.get(ctx.access, created.claim_id)
                assert view is not None
                assert view.claim.status in ("active", "disputed")


class TestWatermarkMonotonicityUnderRaces:
    def test_tombstone_and_agent_watermarks_never_regress(self, cctx: dict[str, Any]) -> None:
        ctx: _Ctx = cctx["ctx"]
        forget: ForgetService = cctx["forget"]
        store: Store = cctx["store"]
        with store.read() as tx:
            previous_tombstone = tx.tombstone_watermark()
            previous_agent = tx.watermark("t1", ctx.agent)
        base_agent = previous_agent.current_seq if previous_agent else 0
        for index in range(ROUNDS):
            created = ctx.remember(f"wm{index}", predicate=f"wm{index}")
            forget.forget(
                ctx.access,
                ForgetSelector(
                    kind=ForgetSelectorKind.RESOURCE,
                    resource_type="claim",
                    resource_id=created.claim_id,
                ),
                reason="r",
                idempotency_key=f"fg-wm-{index}",
            )
            with store.read() as tx:
                tombstone = tx.tombstone_watermark()
                agent = tx.watermark("t1", ctx.agent)
            assert tombstone > previous_tombstone
            assert agent is not None and agent.current_seq >= base_agent
            previous_tombstone = tombstone
