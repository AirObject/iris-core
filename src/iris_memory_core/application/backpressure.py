"""Configurable backpressure limits and degradation gauges (§16.5).

Limits cover pending job counts and payload bytes at global, tenant and agent
granularity, worker batch/concurrency/lease ceilings, and soft/hard disk
thresholds with a recovery hysteresis: once the hard floor trips, the store
stays "full" until free space climbs back above the soft floor. Safety-lane
 enqueue (Forget/Correct/security) keeps a bounded priority channel and is
never crowded out by queue pressure — only by the physical disk floor.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from iris_memory_core.domain.errors import StorageFullError
from iris_memory_core.domain.jobs import JobLane

MICROSECONDS_PER_SECOND = 1_000_000


@dataclass(frozen=True, slots=True)
class BackpressureConfig:
    max_pending_jobs_global: int = 100_000
    max_pending_jobs_per_tenant: int = 20_000
    max_pending_jobs_per_agent: int = 10_000
    max_pending_bytes_global: int = 2_000_000_000
    max_pending_bytes_per_tenant: int = 400_000_000
    max_pending_bytes_per_agent: int = 200_000_000
    soft_disk_free_bytes: int = 2_000_000_000
    hard_disk_free_bytes: int = 500_000_000
    recovery_hysteresis_bytes: int = 200_000_000
    max_safety_pending_jobs: int = 1_000
    worker_batch_size: int = 16
    worker_max_concurrency: int = 4
    worker_lease_us: int = 30_000_000
    max_worker_leases: int = 128
    claim_fairness_per_tenant: int = 4
    retry_base_delay_us: int = 1_000_000
    retry_max_delay_us: int = 300_000_000
    expired_lease_requeue_delay_us: int = 0
    ready_max_queue_lag_us: int = 300_000_000
    ready_max_oldest_pending_us: int = 3_600_000_000
    ready_max_dead_letters: int = 100

    def __post_init__(self) -> None:
        if self.hard_disk_free_bytes >= self.soft_disk_free_bytes:
            raise ValueError("hard disk floor must be below the soft threshold")
        if self.recovery_hysteresis_bytes < 0:
            raise ValueError("recovery hysteresis must be non-negative")
        if self.worker_batch_size < 1 or self.worker_max_concurrency < 1:
            raise ValueError("worker batch and concurrency must be at least 1")
        if self.claim_fairness_per_tenant < 1:
            raise ValueError("claim fairness quota must be at least 1")


class DiskProbe(Protocol):
    def free_bytes(self, path: Path) -> int: ...


class SystemDiskProbe:
    def free_bytes(self, path: Path) -> int:
        return shutil.disk_usage(path).free


class FixedDiskProbe:
    """Test double with a scriptable free-space sequence."""

    def __init__(self, free_bytes: int) -> None:
        self._free = free_bytes

    def set_free_bytes(self, free_bytes: int) -> None:
        self._free = free_bytes

    def free_bytes(self, path: Path) -> int:
        return self._free


@dataclass(frozen=True, slots=True)
class PressureReading:
    jobs: int
    bytes_: int
    disk_free_bytes: int
    soft: bool
    hard: bool
    writable: bool
    reasons: tuple[str, ...] = ()


class BackpressureGauge:
    """Evaluates limits and holds the in-process trip state for hysteresis.

    The trip state is deliberately per-process: the durable facts (queue
    depth, disk space) are re-measured on every check, and the hysteresis
    only smooths flapping around the threshold inside one writer process.
    """

    def __init__(
        self,
        config: BackpressureConfig,
        *,
        probe: DiskProbe | None = None,
        database_path: Path | None = None,
    ) -> None:
        self._config = config
        self._probe = probe or SystemDiskProbe()
        self._database_path = database_path
        self._disk_tripped = False

    @property
    def config(self) -> BackpressureConfig:
        return self._config

    def evaluate(
        self,
        *,
        pressure: dict[str, int],
        lane: JobLane = JobLane.NORMAL,
        tenant_pressure: dict[str, int] | None = None,
        agent_pressure: dict[str, int] | None = None,
        safety_pressure: dict[str, int] | None = None,
    ) -> PressureReading:
        cfg = self._config
        target = Path(self._database_path or ".")
        disk_free = self._probe.free_bytes(target)
        reasons: list[str] = []

        # Disk hysteresis: trip below the hard floor, clear only once free
        # space has climbed back above hard + recovery_hysteresis_bytes —
        # the gap between the two is what stops flapping around either
        # threshold inside one writer process.
        if disk_free < cfg.hard_disk_free_bytes:
            self._disk_tripped = True
        elif disk_free >= cfg.hard_disk_free_bytes + cfg.recovery_hysteresis_bytes:
            self._disk_tripped = False
        disk_hard = self._disk_tripped

        jobs = pressure.get("jobs", 0)
        payload_bytes = pressure.get("bytes", 0)
        queue_hard = False
        queue_soft = False
        if lane is JobLane.SAFETY:
            # Bounded priority channel: safety work is only crowded out by the
            # physical disk floor, never by the normal-lane queue (ADR-0005).
            # Its quota counts safety-lane jobs only. Callers pass the
            # projected count including the incoming job; the limit allows
            # exactly ``max_safety_pending_jobs`` settled jobs.
            safety_jobs = safety_pressure.get("jobs", 0) if safety_pressure else jobs
            queue_hard = safety_jobs > cfg.max_safety_pending_jobs
            queue_soft = safety_jobs > cfg.max_safety_pending_jobs // 2
            if queue_hard:
                reasons.append("safety_queue_full")
        else:
            queue_hard = (
                jobs > cfg.max_pending_jobs_global or payload_bytes > cfg.max_pending_bytes_global
            )
            queue_soft = (
                jobs > cfg.max_pending_jobs_global // 2
                or payload_bytes > cfg.max_pending_bytes_global // 2
            )
            if queue_hard and jobs > cfg.max_pending_jobs_global:
                reasons.append("pending_jobs_global")
            if queue_hard and payload_bytes > cfg.max_pending_bytes_global:
                reasons.append("pending_bytes_global")
        if tenant_pressure is not None:
            if tenant_pressure.get("jobs", 0) > cfg.max_pending_jobs_per_tenant:
                queue_hard = queue_hard or lane is not JobLane.SAFETY
                reasons.append("pending_jobs_tenant")
            if tenant_pressure.get("bytes", 0) > cfg.max_pending_bytes_per_tenant:
                queue_hard = queue_hard or lane is not JobLane.SAFETY
                reasons.append("pending_bytes_tenant")
        if agent_pressure is not None:
            if agent_pressure.get("jobs", 0) > cfg.max_pending_jobs_per_agent:
                queue_hard = queue_hard or lane is not JobLane.SAFETY
                reasons.append("pending_jobs_agent")
            if agent_pressure.get("bytes", 0) > cfg.max_pending_bytes_per_agent:
                queue_hard = queue_hard or lane is not JobLane.SAFETY
                reasons.append("pending_bytes_agent")
        if disk_hard:
            reasons.append("disk_below_hard")

        hard = queue_hard or disk_hard
        soft = queue_soft or disk_hard or disk_free < cfg.soft_disk_free_bytes
        return PressureReading(
            jobs=jobs,
            bytes_=payload_bytes,
            disk_free_bytes=disk_free,
            soft=soft and not hard,
            hard=hard,
            writable=not hard,
            reasons=tuple(reasons),
        )

    def require_accepts(
        self,
        *,
        pressure: dict[str, int],
        lane: JobLane = JobLane.NORMAL,
        tenant_pressure: dict[str, int] | None = None,
        agent_pressure: dict[str, int] | None = None,
        safety_pressure: dict[str, int] | None = None,
    ) -> PressureReading:
        reading = self.evaluate(
            pressure=pressure,
            lane=lane,
            tenant_pressure=tenant_pressure,
            agent_pressure=agent_pressure,
            safety_pressure=safety_pressure,
        )
        if reading.hard:
            raise StorageFullError(
                "hard backpressure threshold reached; normal writes rejected with storage_full",
                details={"reasons": list(reading.reasons)},
            )
        return reading


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    """Low-cardinality queue state for readiness and metrics (§2.3, §31)."""

    status_counts: dict[str, int]
    oldest_pending_us: int | None
    dead_letters: int
    disk_free_bytes: int
    writable: bool
    degraded: bool
    queue_lag_us: int | None = None
    oldest_pending_age_us: int | None = None
    reasons: tuple[str, ...] = ()


def queue_snapshot(
    *,
    status_counts: dict[str, int],
    oldest_pending_us: int | None,
    now_us: int,
    gauge: BackpressureGauge,
    pending_jobs: int,
    pending_bytes: int,
) -> QueueSnapshot:
    reading = gauge.evaluate(pressure={"jobs": pending_jobs, "bytes": pending_bytes})
    dead = status_counts.get("dead", 0)
    lag = (now_us - oldest_pending_us) if oldest_pending_us is not None else None
    cfg = gauge.config
    degraded = (
        reading.soft
        or (lag is not None and lag > cfg.ready_max_queue_lag_us)
        or dead > cfg.ready_max_dead_letters
    )
    return QueueSnapshot(
        status_counts=status_counts,
        oldest_pending_us=oldest_pending_us,
        dead_letters=dead,
        disk_free_bytes=reading.disk_free_bytes,
        writable=reading.writable,
        degraded=degraded,
        queue_lag_us=lag,
        oldest_pending_age_us=lag,
        reasons=reading.reasons,
    )


def backoff_delay_us(
    attempt: int,
    *,
    base_us: int = 1_000_000,
    max_us: int = 300_000_000,
    jitter: float = 0.0,
) -> int:
    """Jittered exponential backoff for retries (§16.3); jitter in [0, 1)."""
    capped_attempts = min(attempt, 30)
    delay = min(base_us * (2 ** max(0, capped_attempts - 1)), max_us)
    spread = int(delay * jitter) if jitter > 0 else 0
    result: int = delay - spread
    return result


__all__ = [
    "BackpressureConfig",
    "BackpressureGauge",
    "DiskProbe",
    "FixedDiskProbe",
    "PressureReading",
    "QueueSnapshot",
    "SystemDiskProbe",
    "backoff_delay_us",
    "queue_snapshot",
]
