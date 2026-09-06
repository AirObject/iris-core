"""Deadline-bounded limiter waits and recovery probes after an open circuit."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from iris_memory_core.domain.vector import EmbeddingProviderError, VectorSpaceConfig
from iris_memory_core.indexing.vector import VectorProjectionService
from iris_memory_core.providers.embedding import (
    DeterministicEmbeddingProvider,
    EmbeddingProviderLimits,
    _RateLimiter,
)
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock


def test_rate_wait_obeys_deadline_and_never_calls_transport() -> None:
    provider = DeterministicEmbeddingProvider(
        VectorSpaceConfig(model="review", dimension=8),
        limits=EmbeddingProviderLimits(max_qps=1),
    )
    provider.embed_batch(["first"])
    started = time.monotonic()
    with pytest.raises(EmbeddingProviderError) as captured:
        provider.embed_batch(["deadline"], deadline_monotonic_us=int((started + 0.03) * 1_000_000))
    assert captured.value.reason_code == "timeout"
    assert time.monotonic() - started < 0.3
    assert provider.stats.vectors == 1


def test_short_deadline_does_not_wait_for_another_limiters_sleep() -> None:
    limiter = _RateLimiter(2)
    limiter.acquire()
    waiting = threading.Event()

    def long_wait() -> None:
        waiting.set()
        limiter.acquire()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(long_wait)
        assert waiting.wait(1)
        started = time.monotonic()
        with pytest.raises(EmbeddingProviderError):
            limiter.acquire(deadline_monotonic_us=int((started + 0.03) * 1_000_000))
        assert time.monotonic() - started < 0.3
        future.result(timeout=2)


@pytest.mark.parametrize("observe_open", [False, True])
def test_half_open_requires_fresh_successful_probe(
    clocked_store: Store,
    mutable_clock: MutableClock,
    observe_open: bool,
) -> None:
    class Provider:
        circuit_state = "closed"
        probes = 0
        healthy = True

        def probe(self) -> tuple[bool, str]:
            self.probes += 1
            if not self.healthy:
                self.circuit_state = "open"
                return False, "transport_error"
            self.circuit_state = "closed"
            return True, "ok"

    provider = Provider()
    projection = VectorProjectionService(
        clocked_store,
        mutable_clock,
        provider=provider,
        vector_root=Path(clocked_store.runtime.database).parent / "vector",
        space=VectorSpaceConfig(model="review", dimension=8),
    )
    assert projection._provider_available()
    assert provider.probes == 1
    provider.healthy = False
    provider.circuit_state = "open"
    if observe_open:
        assert not projection._provider_available()
    provider.circuit_state = "half_open"
    assert not projection._provider_available()
    assert provider.probes == 2
    assert not projection._provider_available()
    assert provider.probes == 2
    mutable_clock.advance(20_000_000)
    provider.healthy = True
    provider.circuit_state = "half_open"
    assert projection._provider_available()
    assert provider.probes == 3
