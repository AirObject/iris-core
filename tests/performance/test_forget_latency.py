"""Phase 5 performance gate: Forget canonical effect p95 <= 100 ms.

Measurement method (recorded in the verification report):
- hardware: the CI host running this test (Apple Silicon dev machine);
- selector scale: 1 resource / 20 / 100 resolved targets per forget;
- concurrency: sequential (single writer, matching the writer gate);
- metric: wall time of ForgetService.forget() from call to return, i.e.
  the full transaction including tombstones, erasure, ledger, watermark and
  the invalidation outbox — the synchronous canonical effect. Projection
  cleanup is deliberately excluded (asynchronous by design).
"""

from __future__ import annotations

import time

from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.memory import ClaimService
from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.memory.test_memory_claims import _Ctx

P95_BUDGET_MS = 100.0
SAMPLES = 40
TARGETS_PER_FORGET = 20


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


def test_forget_canonical_effect_p95_under_100ms(
    clocked_store: Store,
    mutable_clock: MutableClock,
    phase5_claims: ClaimService,
    phase5_forget: ForgetService,
) -> None:
    ctx = _Ctx(clocked_store, mutable_clock, phase5_claims)
    durations: list[float] = []
    # Warm-up round (page cache, schema prepared).
    warm = ctx.remember("warm", predicate="warm")
    phase5_forget.forget(
        ctx.access,
        ForgetSelector(
            kind=ForgetSelectorKind.RESOURCE, resource_type="claim", resource_id=warm.claim_id
        ),
        reason="warm",
        idempotency_key="perf-warm",
    )
    # Each sample is a DISTINCT request instant: the ledger identity
    # (selector, created_us, key, reason, mode) collapses true double-submit
    # replays only — successive forgets must each do their own real work.
    mutable_clock.advance(1)
    for sample in range(SAMPLES):
        for i in range(TARGETS_PER_FORGET):
            ctx.remember(f"perf{sample}-{i}", predicate=f"perf{sample}-{i}")
        selector = ForgetSelector(
            kind=ForgetSelectorKind.SUBJECT_PREDICATE,
            agent_id=ctx.agent,
            subject_entity_id=ctx.entity,
        )
        started = time.perf_counter()
        result = phase5_forget.forget(
            ctx.access, selector, reason="perf", idempotency_key=f"perf-{sample}"
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        mutable_clock.advance(1)
        assert result.erased_count >= TARGETS_PER_FORGET
        durations.append(elapsed_ms)
    p95 = _percentile(durations, 0.95)
    # The gate: p95 of the synchronous canonical effect stays under budget.
    assert p95 <= P95_BUDGET_MS, {
        "p95_ms": p95,
        "max_ms": max(durations),
        "samples": len(durations),
        "targets_per_forget": TARGETS_PER_FORGET,
    }


def test_single_resource_forget_p95_under_100ms(
    clocked_store: Store,
    mutable_clock: MutableClock,
    phase5_claims: ClaimService,
    phase5_forget: ForgetService,
) -> None:
    ctx = _Ctx(clocked_store, mutable_clock, phase5_claims)
    durations: list[float] = []
    for sample in range(SAMPLES):
        created = ctx.remember(f"single{sample}", predicate=f"single{sample}")
        started = time.perf_counter()
        phase5_forget.forget(
            ctx.access,
            ForgetSelector(
                kind=ForgetSelectorKind.RESOURCE,
                resource_type="claim",
                resource_id=created.claim_id,
            ),
            reason="perf",
            idempotency_key=f"perf-single-{sample}",
        )
        durations.append((time.perf_counter() - started) * 1000)
        mutable_clock.advance(1)
    p95 = _percentile(durations, 0.95)
    assert p95 <= P95_BUDGET_MS, {"p95_ms": p95, "samples": len(durations)}
