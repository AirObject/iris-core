"""Liveness and readiness application contract (§2.5, §31).

Readiness aggregates the Phase 2 reliability checks. Schema and runtime
gates surface through the store itself: an out-of-window schema or a
disallowed SQLite runtime fails the probe read with a stable error code
(``schema_incompatible`` / ``sqlite_runtime_not_allowed``), which the report
carries as ``storage_error_code``. Queue lag / oldest pending age,
dead-letter count, disk thresholds and scheduler lag come from the
backpressure gauge. The report carries counts, codes and timings only —
never payloads or identifiers (§31).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from iris_memory_core.application.backpressure import BackpressureGauge, queue_snapshot
from iris_memory_core.application.ports import Clock, UnitOfWork

READY = "ready"
DEGRADED = "degraded"
NOT_READY = "not_ready"


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    status: str
    checks: dict[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()


class HealthService:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        *,
        gauge: BackpressureGauge,
        scheduler_lag: Callable[[], dict[str, int]] | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._gauge = gauge
        self._scheduler_lag = scheduler_lag

    def liveness(self) -> dict[str, str]:
        """Unversioned liveness: process up, no dependency checks (§23.2)."""
        return {"status": "live"}

    def readiness(self) -> ReadinessReport:
        """Aggregate readiness; never raises — degradation is reported."""
        checks: dict[str, Any] = {}
        reasons: list[str] = []
        fatal = False
        degraded = False

        try:
            with self._uow.read() as tx:
                counts = tx.outbox.status_counts()
                oldest = tx.outbox.oldest_pending_us()
                pending = tx.outbox.pressure()
        except Exception as error:
            fatal = True
            reasons.append("storage_unreadable")
            checks["storage_error_code"] = getattr(error, "code", "internal_error")
            counts = {}
            oldest = None
            pending = {"jobs": 0, "bytes": 0}

        now_us = self._clock.now_us()
        snapshot = queue_snapshot(
            status_counts=counts,
            oldest_pending_us=oldest,
            now_us=now_us,
            gauge=self._gauge,
            pending_jobs=pending["jobs"],
            pending_bytes=pending["bytes"],
        )
        # A real write probe, not just the disk gauge: opening a write
        # transaction acquires the SQLite write lock and touches the file —
        # a read-only filesystem or an unwritable database fails here and
        # readiness must say so (§2.5 storage-writability).
        write_probe_ok = True
        if not fatal:
            try:
                with self._uow.write() as tx:
                    tx.outbox.status_counts()
            except Exception:
                write_probe_ok = False
                fatal = True
                reasons.append("storage_not_writable")
        checks["storage_writable"] = snapshot.writable and write_probe_ok
        checks["disk_free_bytes"] = snapshot.disk_free_bytes
        checks["queue_lag_us"] = snapshot.queue_lag_us
        checks["oldest_pending_age_us"] = snapshot.oldest_pending_age_us
        checks["dead_letters"] = snapshot.dead_letters
        checks["pending_jobs"] = pending["jobs"]
        if not snapshot.writable:
            fatal = True
            reasons.extend(snapshot.reasons or ["storage_full"])
        elif snapshot.degraded:
            degraded = True
            if snapshot.queue_lag_us is not None and snapshot.queue_lag_us > (
                self._gauge.config.ready_max_queue_lag_us
            ):
                reasons.append("queue_lag_exceeded")
            if snapshot.dead_letters > self._gauge.config.ready_max_dead_letters:
                reasons.append("dead_letter_count_exceeded")

        if self._scheduler_lag is not None:
            try:
                lag = self._scheduler_lag()
                checks["scheduler_lag_us"] = lag
                if lag:
                    worst = max(lag.values())
                    if worst > self._gauge.config.ready_max_queue_lag_us:
                        degraded = True
                        reasons.append("scheduler_lag_exceeded")
            except Exception:
                degraded = True
                reasons.append("scheduler_unavailable")

        status = NOT_READY if fatal else (DEGRADED if degraded else READY)
        return ReadinessReport(status=status, checks=checks, reasons=tuple(reasons))
