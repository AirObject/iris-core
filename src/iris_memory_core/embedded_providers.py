"""Public host adapters implementing the existing provider ports.

Callbacks execute on the host loop; the synchronous application engine waits
on its own bounded worker. The host owns callbacks and their clients.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from iris_memory_core.application.ports.providers import (
    EmbeddingProvider as EmbeddingProvider,
)
from iris_memory_core.application.ports.providers import (
    ExtractionProvider as ExtractionProvider,
)
from iris_memory_core.application.ports.providers import (
    SummarizationProvider as SummarizationProvider,
)
from iris_memory_core.domain.errors import ProviderUnavailableError
from iris_memory_core.domain.vector import (
    EmbeddingProviderError,
)
from iris_memory_core.domain.vector import (
    VectorSpaceConfig as VectorSpaceConfig,
)
from iris_memory_core.providers.cognitive import CognitiveProviderLimits as CognitiveProviderLimits

__all__ = [
    "AsyncCognitiveAdapter",
    "AsyncEmbeddingAdapter",
    "CognitiveProviderLimits",
    "EmbeddingProvider",
    "ExtractionProvider",
    "SummarizationProvider",
    "VectorSpaceConfig",
]


@dataclass(frozen=True)
class AsyncEmbeddingAdapter:
    space: VectorSpaceConfig
    embed: Callable[[Sequence[str]], Awaitable[list[Sequence[float]]]]
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not 0 < self.timeout_seconds <= 300:
            raise ValueError("provider timeout must be within (0,300]")


@dataclass(frozen=True)
class AsyncCognitiveAdapter:
    model_id: str
    extract: Callable[..., Awaitable[Sequence[Mapping[str, Any]]]]
    summarize: Callable[..., Awaitable[Mapping[str, Any]]]
    limits: CognitiveProviderLimits = field(
        default_factory=lambda: CognitiveProviderLimits(max_retries=0, concurrency=1)
    )


class _HostBridge:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.tasks: set[asyncio.Task[Any]] = set()
        self.closed = False

    def call(self, callback: Callable[[], Awaitable[Any]], timeout: float) -> Any:
        async def invoke() -> Any:
            if self.closed or self.tasks:
                raise asyncio.CancelledError
            task = asyncio.current_task()
            assert task is not None
            self.tasks.add(task)
            try:
                async with asyncio.timeout(timeout):
                    return await callback()
            finally:
                self.tasks.discard(task)

        if self.closed or timeout <= 0:
            raise TimeoutError
        future = asyncio.run_coroutine_threadsafe(invoke(), self.loop)
        try:
            return future.result(timeout=timeout)
        except (TimeoutError, concurrent.futures.CancelledError):
            future.cancel()
            raise TimeoutError from None

    def cancel(self) -> None:
        self.closed = True
        for task in tuple(self.tasks):
            task.cancel()


class _Embedding:
    def __init__(self, adapter: AsyncEmbeddingAdapter, bridge: _HostBridge) -> None:
        self.adapter = adapter
        self.bridge = bridge
        self.space = adapter.space

    def embed_batch(
        self,
        texts: Sequence[str],
        *,
        deadline_monotonic_us: int | None = None,
    ) -> list[Sequence[float]]:
        timeout = self.adapter.timeout_seconds
        if deadline_monotonic_us is not None:
            timeout = min(timeout, deadline_monotonic_us / 1_000_000 - time.monotonic())
        try:
            value: list[Sequence[float]] = self.bridge.call(
                lambda: self.adapter.embed(texts), timeout
            )
            return value
        except TimeoutError:
            raise EmbeddingProviderError("provider_timeout") from None
        except Exception:
            raise EmbeddingProviderError("provider_unavailable") from None


class _Cognitive:
    def __init__(self, adapter: AsyncCognitiveAdapter, bridge: _HostBridge) -> None:
        self.adapter = adapter
        self.bridge = bridge
        self.model_id = adapter.model_id

    def _call(self, method: Any, observations: Any, options: dict[str, Any]) -> Any:
        try:
            return self.bridge.call(
                lambda: method(observations, **options),
                float(options["timeout_seconds"]),
            )
        except TimeoutError:
            raise ProviderUnavailableError(reason_code="timeout") from None
        except Exception:
            raise ProviderUnavailableError(reason_code="server_error") from None

    def extract(
        self,
        observations: Sequence[Mapping[str, object]],
        *,
        prompt_version: str,
        schema_version: str,
        timeout_seconds: float,
    ) -> Sequence[Mapping[str, Any]]:
        return self._call(  # type: ignore[no-any-return]
            self.adapter.extract,
            observations,
            {
                "prompt_version": prompt_version,
                "schema_version": schema_version,
                "timeout_seconds": timeout_seconds,
            },
        )

    def summarize(
        self,
        observations: Sequence[Mapping[str, object]],
        *,
        prompt_version: str,
        schema_version: str,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        return self._call(  # type: ignore[no-any-return]
            self.adapter.summarize,
            observations,
            {
                "prompt_version": prompt_version,
                "schema_version": schema_version,
                "timeout_seconds": timeout_seconds,
            },
        )
