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

import sqlite3
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
        vector_required: bool = False,
        vector_capability: Callable[[], bool] | None = None,
        profile_required: bool = False,
        graph_required: bool = False,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._gauge = gauge
        self._scheduler_lag = scheduler_lag
        self._vector_required = vector_required
        self._profile_required = profile_required
        self._graph_required = graph_required
        #: Live capability probe (e.g. the vector projection's
        #: ``capability_available``: FAISS importable AND the embedding
        #: provider answers). Without this a ``vector_required`` deployment
        #: would report ready while every vector route degrades on a dead
        #: provider (ADR-0015 §11).
        self._vector_capability = vector_capability

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

        # Phase 6 (§31.4): the FTS projection state is REPORTED, never fatal —
        # an unbuilt or rebuild-pending index degrades the FTS route only;
        # structured recall keeps serving (ADR-0014 §9). ``never_built`` is the
        # normal fresh-install state (FTS is optional until an admin builds),
        # so it does not mark readiness degraded; ``pending_rebuild`` (a
        # restore owes a rebuild) and an unreadable projection do.
        try:
            with self._uow.read() as tx:
                checks["fts_projection_state"] = tx.fts.projection_state()
        except Exception:
            checks["fts_projection_state"] = "unavailable"
        if checks["fts_projection_state"] in ("pending_rebuild", "unavailable"):
            degraded = True
            reasons.append("fts_projection_rebuild_pending")

        # Phase 7 (ADR-0015 §11): the vector projection follows the same
        # optional-capability semantics as FTS, split by configuration —
        # ``never_built`` never degrades (fresh install); ``pending_rebuild``
        # or an unreadable projection degrades an OPTIONAL vector capability
        # but makes readiness NOT READY when the deployment declared the
        # vector capability REQUIRED (``vector_required``). A database still
        # on Schema 7 (its startup migration has not run) reports the normal
        # ``never_built`` shape rather than a misleading degradation. The
        # response carries the state string only — no paths, no identifiers.
        try:
            with self._uow.read() as tx:
                checks["vector_projection_state"] = tx.vector.projection_state()
        except sqlite3.OperationalError:
            # Schema 7 database whose startup migration has not run yet: the
            # vector tables do not exist; the migration owns this state.
            checks["vector_projection_state"] = "never_built"
        except Exception:
            checks["vector_projection_state"] = "unavailable"
        if checks["vector_projection_state"] in ("pending_rebuild", "unavailable"):
            if self._vector_required:
                fatal = True
                reasons.append("vector_projection_required")
            else:
                degraded = True
                reasons.append("vector_projection_rebuild_pending")

        # Phase 8 (ADR-0016 §10): profile/graph follow the same
        # optional-capability semantics — ``never_built`` is the fresh-install
        # normal state; ``pending_rebuild``/``unavailable`` degrade an
        # OPTIONAL capability and go NOT READY when the deployment declared
        # it REQUIRED. No live capability probe: both projections are pure
        # SQLite with no external provider dependency.
        for kind, repository_attr, required, reason in (
            (
                "profile",
                "profile",
                self._profile_required,
                "profile_projection_required",
            ),
            (
                "graph",
                "graph",
                self._graph_required,
                "graph_projection_required",
            ),
        ):
            try:
                with self._uow.read() as tx:
                    checks[f"{kind}_projection_state"] = getattr(
                        tx, repository_attr
                    ).projection_state()
            except sqlite3.OperationalError:
                # Pre-Schema-9 database whose startup migration has not run:
                # the tables do not exist; the migration owns this state.
                checks[f"{kind}_projection_state"] = "never_built"
            except Exception:
                checks[f"{kind}_projection_state"] = "unavailable"
            if checks[f"{kind}_projection_state"] in ("pending_rebuild", "unavailable"):
                if required:
                    fatal = True
                    reasons.append(reason)
                else:
                    degraded = True
                    reasons.append(f"{kind}_projection_rebuild_pending")

        # Phase 7 capability probe (ADR-0015 §11): the projection STATE only
        # says what the database owes — a required vector capability is only
        # real when the runtime can actually serve it (FAISS importable and
        # the embedding provider answering; the probe is cached/cooldown-
        # bounded by the projection service). Optional deployments REPORT the
        # state without degrading on it; required deployments go NOT READY.
        if self._vector_capability is not None:
            try:
                capability_ok = bool(self._vector_capability())
            except Exception:
                capability_ok = False
            checks["vector_capability"] = "ok" if capability_ok else "unavailable"
            if not capability_ok and self._vector_required:
                fatal = True
                reasons.append("vector_capability_unavailable")

        status = NOT_READY if fatal else (DEGRADED if degraded else READY)
        return ReadinessReport(status=status, checks=checks, reasons=tuple(reasons))
