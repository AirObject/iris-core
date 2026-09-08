"""Governed cognitive provider adapters for Phase 10.

The wrapper owns bounded time, concurrency, rate, cost and circuit state.  It
never logs submitted content and it exposes only low-sensitivity outcomes to
the caller.  Provider implementations receive the effective socket/model
timeout and are required to honor it; the wrapper also places a caller-side
deadline around faulty implementations used in tests.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar, cast

from iris_memory_core.application.ports.clock import Clock
from iris_memory_core.application.ports.transaction import UnitOfWork
from iris_memory_core.domain.errors import ProviderUnavailableError
from iris_memory_core.domain.reflection import ProviderKind, ProviderOutcome

_T = TypeVar("_T")


class _InjectedProviderError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__("injected provider failure")
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class CognitiveProviderLimits:
    timeout_seconds: float = 5.0
    max_retries: int = 3
    max_qps: float = 5.0
    daily_budget_microunits: int = 1_000_000
    concurrency: int = 2
    breaker_failures: int = 5
    breaker_cooldown_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.timeout_seconds > 300:
            raise ValueError("provider timeout must be within (0,300]")
        if not 0 <= self.max_retries <= 20:
            raise ValueError("provider retries must be within 0..20")
        if self.max_qps <= 0:
            raise ValueError("provider max_qps must be positive")
        if self.daily_budget_microunits < 0:
            raise ValueError("provider daily budget must be non-negative")
        if not 1 <= self.concurrency <= 128:
            raise ValueError("provider concurrency must be within 1..128")
        if self.breaker_failures < 1 or self.breaker_cooldown_seconds <= 0:
            raise ValueError("provider breaker configuration is invalid")


class ProviderOutcomeSink:
    def record_provider_outcome(
        self,
        *,
        tenant_id: str,
        agent_id: str | None,
        job_kind: str,
        provider_kind: ProviderKind,
        model_id: str,
        prompt_version: str,
        provider_schema_version: str,
        outcome: ProviderOutcome,
    ) -> str:  # pragma: no cover - protocol-like runtime seam
        raise NotImplementedError


class DurableProviderState:
    """SQLite-backed admission state shared by every worker process.

    Probe reservation and budget charging use single conditional UPDATEs, so
    two workers cannot both consume the sole half-open probe or overspend the
    configured daily ceiling.
    """

    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    def circuit_state(self, tenant_id: str, kind: ProviderKind) -> str:
        with self._uow.read() as tx:
            row = tx.reflection.provider_circuit_state(tenant_id, kind)
        if row is None:
            return "closed"
        state = str(row["state"])
        opened_until = row.get("opened_until_us")
        if (
            state == "open"
            and opened_until is not None
            and cast(int, opened_until) <= self._clock.now_us()
        ):
            return "half_open"
        return state

    def before_circuit(self, tenant_id: str, kind: ProviderKind) -> bool:
        now_us = self._clock.now_us()
        with self._uow.read() as tx:
            row = tx.reflection.provider_circuit_state(tenant_id, kind)
        if row is None or str(row["state"]) == "closed":
            return False
        opened_until = row.get("opened_until_us")
        if opened_until is not None and cast(int, opened_until) > now_us:
            raise ProviderUnavailableError(reason_code="circuit_open")
        with self._uow.write() as tx:
            reserved = tx.reflection.reserve_provider_probe(tenant_id, kind, now_us=now_us)
        if not reserved:
            raise ProviderUnavailableError(reason_code="circuit_open")
        return True

    def record_circuit(
        self,
        tenant_id: str,
        kind: ProviderKind,
        *,
        success: bool,
        transport_failure: bool,
        failure_threshold: int,
        cooldown_seconds: float,
    ) -> None:
        now_us = self._clock.now_us()
        with self._uow.write() as tx:
            current = tx.reflection.provider_circuit_state(tenant_id, kind)
            failures = cast(int, current["consecutive_failures"]) if current else 0
            state = str(current["state"]) if current else "closed"
            opened_until: int | None = (
                cast(int, current["opened_until_us"])
                if current and current.get("opened_until_us") is not None
                else None
            )
            if success:
                failures, state, opened_until = 0, "closed", None
            elif transport_failure:
                failures += 1
                if failures >= failure_threshold:
                    state = "open"
                    opened_until = now_us + int(cooldown_seconds * 1_000_000)
            tx.reflection.update_provider_circuit(
                tenant_id,
                kind,
                state=state,
                consecutive_failures=failures,
                opened_until_us=opened_until,
                probe_in_flight=False,
                now_us=now_us,
            )

    def charge_budget(
        self,
        tenant_id: str,
        kind: ProviderKind,
        *,
        amount_microunits: int,
        limit_microunits: int,
        budget_day: int,
    ) -> None:
        with self._uow.write() as tx:
            charged = tx.reflection.charge_provider_budget(
                tenant_id,
                kind,
                budget_day=budget_day,
                amount_microunits=amount_microunits,
                limit_microunits=limit_microunits,
                now_us=self._clock.now_us(),
            )
        if not charged:
            raise ProviderUnavailableError(reason_code="budget_exhausted")

    def refund_budget(
        self,
        tenant_id: str,
        kind: ProviderKind,
        *,
        amount_microunits: int,
        budget_day: int,
    ) -> None:
        with self._uow.write() as tx:
            tx.reflection.refund_provider_budget(
                tenant_id,
                kind,
                budget_day=budget_day,
                amount_microunits=amount_microunits,
                now_us=self._clock.now_us(),
            )

    def budget_spent(self, tenant_id: str, kind: ProviderKind, *, budget_day: int) -> int:
        with self._uow.read() as tx:
            return tx.reflection.provider_budget_spent(tenant_id, kind, budget_day=budget_day)


@dataclass(slots=True)
class _Breaker:
    failures: int = 0
    opened_at: float | None = None
    probe_in_flight: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


class ProviderGovernance:
    """Per-kind independent governance with bounded half-open recovery.

    Admission rotates `(tenant, agent)` keys at each call.  It is intentionally
    small: durable job order remains the Outbox's responsibility; this layer
    prevents one admitted key from consuming all per-kind provider slots.
    """

    def __init__(
        self,
        limits: Mapping[ProviderKind, CognitiveProviderLimits] | None = None,
        *,
        metrics: object | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
        durable_state: DurableProviderState | None = None,
    ) -> None:
        default = CognitiveProviderLimits()
        configured = limits or {}
        kinds: tuple[ProviderKind, ...] = (
            "extraction",
            "summarization",
            "reconciliation",
            "persona_evolution",
        )
        self._limits: dict[ProviderKind, CognitiveProviderLimits] = {
            kind: configured.get(kind) or default for kind in kinds
        }
        self._metrics = metrics
        self._monotonic = monotonic
        self._wall_time = wall_time
        self._durable_state = durable_state
        self._semaphores = {
            kind: threading.BoundedSemaphore(config.concurrency)
            for kind, config in self._limits.items()
        }
        self._breakers: dict[tuple[str, ProviderKind], _Breaker] = defaultdict(_Breaker)
        self._last_call: dict[ProviderKind, float] = defaultdict(float)
        self._rate_locks: dict[ProviderKind, threading.Lock] = defaultdict(threading.Lock)
        self._budgets: dict[tuple[int, str, ProviderKind], int] = defaultdict(int)
        self._budget_lock = threading.Lock()
        self._fair_order: dict[ProviderKind, deque[tuple[str, str | None]]] = defaultdict(deque)
        self._fair_lock = threading.Lock()

    def limits_for(self, kind: ProviderKind) -> CognitiveProviderLimits:
        return self._limits[kind]

    def circuit_state(self, tenant_id: str, kind: ProviderKind) -> str:
        if self._durable_state is not None:
            return self._durable_state.circuit_state(tenant_id, kind)
        breaker = self._breakers[(tenant_id, kind)]
        limits = self._limits[kind]
        with breaker.lock:
            if breaker.opened_at is None:
                return "closed"
            if self._monotonic() - breaker.opened_at >= limits.breaker_cooldown_seconds:
                return "half_open"
            return "open"

    def budget_spent(self, tenant_id: str, kind: ProviderKind) -> int:
        day = int(self._wall_time() // 86_400)
        if self._durable_state is not None:
            return self._durable_state.budget_spent(tenant_id, kind, budget_day=day)
        with self._budget_lock:
            return self._budgets[(day, tenant_id, kind)]

    def _before_breaker(self, tenant_id: str, kind: ProviderKind) -> bool:
        if self._durable_state is not None:
            return self._durable_state.before_circuit(tenant_id, kind)
        breaker = self._breakers[(tenant_id, kind)]
        limits = self._limits[kind]
        with breaker.lock:
            if breaker.opened_at is None:
                return False
            if self._monotonic() - breaker.opened_at < limits.breaker_cooldown_seconds:
                raise ProviderUnavailableError(reason_code="circuit_open")
            if breaker.probe_in_flight:
                raise ProviderUnavailableError(reason_code="circuit_open")
            breaker.probe_in_flight = True
            return True

    def _record_breaker(
        self, tenant_id: str, kind: ProviderKind, *, success: bool, transport_failure: bool
    ) -> None:
        if self._durable_state is not None:
            limits = self._limits[kind]
            self._durable_state.record_circuit(
                tenant_id,
                kind,
                success=success,
                transport_failure=transport_failure,
                failure_threshold=limits.breaker_failures,
                cooldown_seconds=limits.breaker_cooldown_seconds,
            )
            return
        breaker = self._breakers[(tenant_id, kind)]
        limits = self._limits[kind]
        with breaker.lock:
            breaker.probe_in_flight = False
            if success:
                breaker.failures = 0
                breaker.opened_at = None
            elif transport_failure:
                breaker.failures += 1
                if breaker.failures >= limits.breaker_failures:
                    breaker.opened_at = self._monotonic()

    def _charge_budget(
        self, tenant_id: str, kind: ProviderKind, estimated_cost_microunits: int
    ) -> None:
        if estimated_cost_microunits < 0:
            raise ValueError("estimated provider cost cannot be negative")
        limit = self._limits[kind].daily_budget_microunits
        day = int(self._wall_time() // 86_400)
        if self._durable_state is not None:
            self._durable_state.charge_budget(
                tenant_id,
                kind,
                amount_microunits=estimated_cost_microunits,
                limit_microunits=limit,
                budget_day=day,
            )
            return
        key = (day, tenant_id, kind)
        with self._budget_lock:
            if self._budgets[key] + estimated_cost_microunits > limit:
                raise ProviderUnavailableError(reason_code="budget_exhausted")
            self._budgets[key] += estimated_cost_microunits

    def _refund_budget(
        self, tenant_id: str, kind: ProviderKind, estimated_cost_microunits: int
    ) -> None:
        day = int(self._wall_time() // 86_400)
        if self._durable_state is not None:
            self._durable_state.refund_budget(
                tenant_id,
                kind,
                amount_microunits=estimated_cost_microunits,
                budget_day=day,
            )
            return
        with self._budget_lock:
            key = (day, tenant_id, kind)
            self._budgets[key] = max(0, self._budgets[key] - estimated_cost_microunits)

    def _rate_limit(self, kind: ProviderKind) -> None:
        interval = 1.0 / self._limits[kind].max_qps
        lock = self._rate_locks[kind]
        with lock:
            now = self._monotonic()
            remaining = interval - (now - self._last_call[kind])
            if remaining > 0:
                time.sleep(remaining)
            self._last_call[kind] = self._monotonic()

    def _fair_admit(self, kind: ProviderKind, key: tuple[str, str | None]) -> None:
        # A deterministic round-robin record.  The semaphore is the hard
        # bound; the deque prevents a hot key from remaining at the head.
        with self._fair_lock:
            queue = self._fair_order[kind]
            if key in queue:
                queue.remove(key)
            queue.append(key)
            if len(queue) > 10_000:
                queue.popleft()

    def call(
        self,
        kind: ProviderKind,
        *,
        tenant_id: str,
        agent_id: str | None,
        request_material: Mapping[str, object],
        estimated_cost_microunits: int,
        invoke: Callable[[float], _T],
    ) -> tuple[_T, ProviderOutcome]:
        """Run with a bounded number of transport retries.

        Admission failures (budget, circuit, concurrency) are final for this
        job attempt. Only timeout/server transport failures are retried; the
        durable Outbox remains the outer retry and Dead Letter authority.
        """
        limits = self._limits[kind]
        last: ProviderUnavailableError | None = None
        charged_cost = 0
        for attempt in range(limits.max_retries + 1):
            try:
                value, outcome = self._call_once(
                    kind,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    request_material={**request_material, "attempt": attempt},
                    estimated_cost_microunits=estimated_cost_microunits,
                    invoke=invoke,
                )
                return value, replace(
                    outcome, cost_microunits=outcome.cost_microunits + charged_cost
                )
            except ProviderUnavailableError as error:
                charged_cost += int(getattr(error, "charged_cost_microunits", 0))
                error.charged_cost_microunits = charged_cost
                last = error
                reason = str(error.details.get("reason_code", ""))
                if (
                    not error.retryable
                    or reason not in {"timeout", "server_error"}
                    or attempt >= limits.max_retries
                ):
                    raise
        assert last is not None
        raise last

    def _call_once(
        self,
        kind: ProviderKind,
        *,
        tenant_id: str,
        agent_id: str | None,
        request_material: Mapping[str, object],
        estimated_cost_microunits: int,
        invoke: Callable[[float], _T],
    ) -> tuple[_T, ProviderOutcome]:
        limits = self._limits[kind]
        request_hash = hashlib.sha256(repr(sorted(request_material.items())).encode()).hexdigest()
        probe_reserved = self._before_breaker(tenant_id, kind)
        try:
            self._charge_budget(tenant_id, kind, estimated_cost_microunits)
        except BaseException:
            if probe_reserved:
                self._record_breaker(tenant_id, kind, success=False, transport_failure=False)
            raise
        self._fair_admit(kind, (tenant_id, agent_id))
        acquired = self._semaphores[kind].acquire(timeout=limits.timeout_seconds)
        if not acquired:
            self._refund_budget(tenant_id, kind, estimated_cost_microunits)
            self._record_breaker(tenant_id, kind, success=False, transport_failure=False)
            raise ProviderUnavailableError(reason_code="concurrency_exhausted")
        started = self._monotonic()
        try:
            self._rate_limit(kind)
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"iris-{kind}")
            future = executor.submit(invoke, limits.timeout_seconds)
            try:
                result = future.result(timeout=limits.timeout_seconds)
            except FutureTimeout:
                future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                duration = int((self._monotonic() - started) * 1_000_000)
                self._record_breaker(tenant_id, kind, success=False, transport_failure=True)
                self._emit(kind, "timeout", duration)
                raise ProviderUnavailableError(reason_code="timeout") from None
            except ProviderUnavailableError as error:
                executor.shutdown(wait=False, cancel_futures=True)
                duration = int((self._monotonic() - started) * 1_000_000)
                reason = str(error.details.get("reason_code", "server_error"))
                self._record_breaker(
                    tenant_id, kind, success=False, transport_failure=error.retryable
                )
                self._emit(kind, reason, duration)
                raise
            except Exception as error:
                executor.shutdown(wait=False, cancel_futures=True)
                duration = int((self._monotonic() - started) * 1_000_000)
                self._record_breaker(tenant_id, kind, success=False, transport_failure=True)
                self._emit(kind, "server_error", duration)
                raise ProviderUnavailableError(
                    reason_code=getattr(error, "reason_code", "server_error")
                ) from None
            else:
                executor.shutdown(wait=True)
            duration = int((self._monotonic() - started) * 1_000_000)
            response_hash = hashlib.sha256(repr(result).encode()).hexdigest()
            outcome = ProviderOutcome(
                provider_kind=kind,
                outcome="success",
                retryable=False,
                request_hash=request_hash,
                response_hash=response_hash,
                cost_microunits=estimated_cost_microunits,
                duration_us=duration,
            )
            self._record_breaker(tenant_id, kind, success=True, transport_failure=False)
            self._emit(kind, "success", duration)
            return result, outcome
        except ProviderUnavailableError as error:
            # Every started attempt retains its conservative budget charge,
            # including invalid responses and timeouts. Admission failures
            # before this block have not started a billable network attempt.
            error.charged_cost_microunits = estimated_cost_microunits
            raise
        finally:
            self._semaphores[kind].release()

    def _emit(self, kind: ProviderKind, outcome: str, duration_us: int) -> None:
        if self._metrics is None:
            return
        request = getattr(self._metrics, "provider_request", None)
        duration = getattr(self._metrics, "provider_duration", None)
        if callable(request):
            request(kind, outcome)
        if callable(duration):
            duration(kind, duration_us / 1_000_000)


class DeterministicCognitiveProvider:
    """Deterministic fake implementing all four cognitive provider ports."""

    model_id = "deterministic-fake-v1"

    def __init__(
        self,
        candidates: Sequence[Mapping[str, Any]] = (),
        *,
        summary: Mapping[str, Any] | None = None,
        failures: Sequence[str] = (),
        delay_seconds: float = 0.0,
    ) -> None:
        self._candidates = tuple(dict(item) for item in candidates)
        self._summary = dict(summary or {"title": "", "summary": ""})
        self._failures = deque(failures)
        self._delay = delay_seconds
        self.calls = 0

    def _before(self, timeout_seconds: float) -> None:
        self.calls += 1
        if self._delay:
            time.sleep(min(self._delay, timeout_seconds + 0.05))
        if self._failures:
            reason = self._failures.popleft()
            raise _InjectedProviderError(reason)

    def extract(
        self,
        observations: Sequence[Mapping[str, object]],
        *,
        prompt_version: str,
        schema_version: str,
        timeout_seconds: float,
    ) -> Sequence[Mapping[str, Any]]:
        del observations, prompt_version, schema_version
        self._before(timeout_seconds)
        return tuple(dict(item) for item in self._candidates)

    def summarize(
        self,
        observations: Sequence[Mapping[str, object]],
        *,
        prompt_version: str,
        schema_version: str,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        del observations, prompt_version, schema_version
        self._before(timeout_seconds)
        return dict(self._summary)

    def reconcile(
        self,
        candidates: Sequence[Mapping[str, object]],
        *,
        prompt_version: str,
        schema_version: str,
        timeout_seconds: float,
    ) -> Sequence[Mapping[str, Any]]:
        del prompt_version, schema_version
        self._before(timeout_seconds)
        return tuple(sorted((dict(item) for item in candidates), key=repr))

    def propose(
        self,
        evidence: Sequence[Mapping[str, object]],
        *,
        prompt_version: str,
        schema_version: str,
        timeout_seconds: float,
    ) -> Sequence[Mapping[str, Any]]:
        del evidence, prompt_version, schema_version
        self._before(timeout_seconds)
        return tuple(
            dict(item) for item in self._candidates if item.get("type") == "persona_proposal"
        )


__all__ = [
    "CognitiveProviderLimits",
    "DeterministicCognitiveProvider",
    "DurableProviderState",
    "ProviderGovernance",
    "ProviderKind",
    "ProviderOutcome",
]
