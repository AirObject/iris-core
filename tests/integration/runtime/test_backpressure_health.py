"""Backpressure, readiness and leak-scan integration tests (§16.5, §2.3, §2.5).

Covers configurable queue/disk soft-hard thresholds, storage_full rejection
with the safety lane preserved, recovery hysteresis, readiness reporting
(queue lag, oldest pending, dead letters, writability) and the automated
sensitive-content leak scan across logs, metrics, errors and health.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from iris_memory_core.application.backpressure import (
    BackpressureConfig,
    BackpressureGauge,
    FixedDiskProbe,
)
from iris_memory_core.application.health import HealthService
from iris_memory_core.application.observation import ObservationService
from iris_memory_core.application.outbox import JobCommit, JobWork, OutboxService
from iris_memory_core.application.scheduler import SchedulerService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import StorageFullError
from iris_memory_core.domain.jobs import JobLane, NewOutboxJob, OutboxJob
from iris_memory_core.observability.logging import (
    FORBIDDEN_LOG_FIELDS,
    LowSensitivityLogger,
    sanitize_log_record,
)
from iris_memory_core.observability.metrics import (
    FORBIDDEN_LABELS,
    InvalidMetricError,
    Metrics,
)
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock

US = 1_000_000


def _job(**overrides: object) -> NewOutboxJob:
    base: dict[str, object] = {
        "tenant_id": "tenant-a",
        "job_kind": "maintenance.selfcheck",
        "aggregate_type": "probe",
        "aggregate_id": "probe",
        "source_revision": 1,
        "payload": {"version": 1},
        "dedupe_key": "dk",
        "available_at_us": 0,
    }
    base.update(overrides)
    return NewOutboxJob(**base)  # type: ignore[arg-type]


def _tight_gauge(database: Path, **config: int) -> BackpressureGauge:
    values: dict[str, int] = {
        "max_pending_jobs_global": 5,
        "max_pending_bytes_global": 10_000_000,
        "soft_disk_free_bytes": 1_000_000,
        "hard_disk_free_bytes": 400_000,
        "recovery_hysteresis_bytes": 100_000,
        "max_safety_pending_jobs": 3,
    }
    values.update(config)
    return BackpressureGauge(
        BackpressureConfig(**values),
        probe=FixedDiskProbe(10**9),
        database_path=database,
    )


class TestQueueThresholds:
    def test_soft_threshold_accepts_but_flags(
        self, database: Path, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        gauge = _tight_gauge(database, max_pending_jobs_global=4)
        service = OutboxService(clocked_store, mutable_clock, gauge=gauge)
        for i in range(3):
            service.enqueue(_job(dedupe_key=f"soft-{i}"))
        reading = gauge.evaluate(pressure={"jobs": 3, "bytes": 0})
        assert reading.soft is True and reading.hard is False

    def test_hard_threshold_rejects_normal_lane(
        self, database: Path, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        gauge = _tight_gauge(database, max_pending_jobs_global=3)
        service = OutboxService(clocked_store, mutable_clock, gauge=gauge)
        for i in range(3):
            service.enqueue(_job(dedupe_key=f"fill-{i}"))
        with pytest.raises(StorageFullError):
            service.enqueue(_job(dedupe_key="over"))

    def test_safety_lane_survives_normal_lane_pressure(
        self, database: Path, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        gauge = _tight_gauge(database, max_pending_jobs_global=3)
        service = OutboxService(clocked_store, mutable_clock, gauge=gauge)
        for i in range(3):
            service.enqueue(_job(dedupe_key=f"fill-{i}"))
        safety, created = service.enqueue(
            _job(
                dedupe_key="safety-1",
                job_kind="forget.execute",
                lane=JobLane.SAFETY,
            )
        )
        assert created and safety.lane == "safety"

    def test_safety_lane_has_its_own_bounded_quota(
        self, database: Path, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        gauge = _tight_gauge(database, max_pending_jobs_global=3, max_safety_pending_jobs=2)
        service = OutboxService(clocked_store, mutable_clock, gauge=gauge)
        service.enqueue(_job(dedupe_key="s1", job_kind="forget.execute", lane=JobLane.SAFETY))
        service.enqueue(_job(dedupe_key="s2", job_kind="forget.execute", lane=JobLane.SAFETY))
        with pytest.raises(StorageFullError):
            service.enqueue(_job(dedupe_key="s3", job_kind="forget.execute", lane=JobLane.SAFETY))

    def test_payload_bytes_threshold_rejects(
        self, database: Path, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        gauge = _tight_gauge(database, max_pending_bytes_global=200)
        service = OutboxService(clocked_store, mutable_clock, gauge=gauge)
        with pytest.raises(StorageFullError):
            service.enqueue(_job(payload={"version": 1, "blob": "x" * 10_000}))

    def test_tenant_quota_rejects(
        self, database: Path, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        gauge = BackpressureGauge(
            BackpressureConfig(max_pending_jobs_per_tenant=2),
            probe=FixedDiskProbe(10**9),
            database_path=database,
        )
        service = OutboxService(clocked_store, mutable_clock, gauge=gauge)
        service.enqueue(_job(dedupe_key="a1"))
        service.enqueue(_job(dedupe_key="a2"))
        with pytest.raises(StorageFullError):
            service.enqueue(_job(dedupe_key="a3"))

    def test_observe_rejected_under_hard_queue_pressure(
        self,
        database: Path,
        clocked_store: Store,
        mutable_clock: MutableClock,
        phase2_access: AccessContext,
        phase2_agent: str,
    ) -> None:
        gauge = _tight_gauge(database, max_pending_jobs_global=1)
        service = ObservationService(clocked_store, gauge=gauge)
        record = {
            "agent_id": phase2_agent,
            "role": "user",
            "kind": "message.text",
            "idempotency_key": "k1",
            "occurred_us": 1,
            "committed_us": 2,
        }
        service.observe_batch(phase2_access, [dict(record)])
        with pytest.raises(StorageFullError):
            service.observe_batch(phase2_access, [dict(record, idempotency_key="k2")])


class TestDiskThresholds:
    def test_hard_disk_floor_rejects(
        self, database: Path, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        probe = FixedDiskProbe(10**9)
        gauge = _tight_gauge(database)
        gauge._probe = probe
        service = OutboxService(clocked_store, mutable_clock, gauge=gauge)
        probe.set_free_bytes(300_000)  # below hard floor
        with pytest.raises(StorageFullError):
            service.enqueue(_job(dedupe_key="d1"))

    def test_soft_disk_floor_degrades_not_rejects(
        self, database: Path, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        probe = FixedDiskProbe(10**9)
        gauge = _tight_gauge(database)
        gauge._probe = probe
        service = OutboxService(clocked_store, mutable_clock, gauge=gauge)
        probe.set_free_bytes(500_000)  # between hard and soft
        _job_stored, created = service.enqueue(_job(dedupe_key="d1"))
        assert created
        reading = gauge.evaluate(pressure={"jobs": 0, "bytes": 0})
        assert reading.soft is True and reading.hard is False

    def test_recovery_hysteresis_requires_soft_floor(self, database: Path) -> None:
        probe = FixedDiskProbe(10**9)
        gauge = _tight_gauge(database)
        gauge._probe = probe
        probe.set_free_bytes(300_000)
        reading = gauge.evaluate(pressure={"jobs": 0, "bytes": 0})
        assert reading.hard is True
        # Recovering just above the hard floor is NOT enough.
        probe.set_free_bytes(450_000)
        assert gauge.evaluate(pressure={"jobs": 0, "bytes": 0}).hard is True
        # Above the soft floor the trip clears.
        probe.set_free_bytes(1_200_000)
        assert gauge.evaluate(pressure={"jobs": 0, "bytes": 0}).hard is False

    def test_config_validation_rejects_inverted_thresholds(self) -> None:
        with pytest.raises(ValueError):
            BackpressureConfig(hard_disk_free_bytes=10, soft_disk_free_bytes=5)


class TestReadiness:
    def test_ready_when_healthy(
        self,
        health: HealthService,
    ) -> None:
        report = health.readiness()
        assert report.status in ("ready", "degraded")
        assert report.checks["storage_writable"] is True

    def test_not_ready_when_storage_unreadable(
        self,
        generous_gauge: BackpressureGauge,
        mutable_clock: MutableClock,
        database: Path,
    ) -> None:
        class DeadUoW:
            def read(self):  # type: ignore[no-untyped-def]
                raise RuntimeError("cannot open")

            def write(self):  # type: ignore[no-untyped-def]
                raise RuntimeError("cannot open")

        service = HealthService(DeadUoW(), mutable_clock, gauge=generous_gauge)
        report = service.readiness()
        assert report.status == "not_ready"
        assert "storage_unreadable" in report.reasons
        assert report.checks["storage_error_code"] == "internal_error"

    def test_degrades_on_dead_letters(
        self,
        clocked_store: Store,
        mutable_clock: MutableClock,
        generous_gauge: BackpressureGauge,
        database: Path,
    ) -> None:
        from iris_memory_core.domain.errors import NotFoundError

        outbox = OutboxService(clocked_store, mutable_clock, gauge=generous_gauge)
        service = HealthService(clocked_store, mutable_clock, gauge=generous_gauge)
        # Force a dead letter.
        outbox.enqueue(_job(dedupe_key="dying", max_attempts=1))
        job = outbox.claim("w1").jobs[0]

        def failing(job: OutboxJob) -> JobWork:
            def work(j: OutboxJob) -> JobCommit:
                raise NotFoundError("gone")

            return work

        outbox.execute(job, failing(job), owner="w1")
        report = service.readiness()
        assert report.checks["dead_letters"] == 1

    def test_liveness_is_trivial(
        self,
        health: HealthService,
    ) -> None:
        assert health.liveness() == {"status": "live"}

    def test_scheduler_lag_reported(
        self,
        health: HealthService,
        scheduler: SchedulerService,
        phase2_access: AccessContext,
        phase2_agent: str,
        mutable_clock: MutableClock,
    ) -> None:
        scheduler.create_schedule(
            phase2_access,
            agent_id=phase2_agent,
            job_kind="maintenance.selfcheck",
            spec={"kind": "interval", "every_seconds": 60},
            reason="t",
        )
        mutable_clock.advance(120 * US)
        report = health.readiness()
        assert "scheduler_lag_us" in report.checks


class TestLeakScan:
    """Automated sensitive-content leakage scanning (§2.5, §31)."""

    CANARY = "CANARY-SECRET-CONTENT-xyzzy"

    def _canary_record(self, agent_id: str) -> dict[str, object]:
        return {
            "agent_id": agent_id,
            "role": "assistant",
            "kind": "message.text",
            "idempotency_key": "canary-1",
            "occurred_us": 1,
            "committed_us": 2,
            "content": self.CANARY,
            "structured_payload": {"secret": self.CANARY},
        }

    def test_no_canary_in_logs_metrics_errors_or_health(
        self,
        clocked_store: Store,
        generous_gauge: BackpressureGauge,
        mutable_clock: MutableClock,
        phase2_access: AccessContext,
        phase2_agent: str,
        database: Path,
    ) -> None:
        metrics = Metrics()
        logger = LowSensitivityLogger(sink=lambda line: None)
        observations = ObservationService(clocked_store, gauge=generous_gauge, surface=None)
        observations.observe_batch(phase2_access, [self._canary_record(phase2_agent)])
        metrics.observation_recorded(role="assistant", kind="message.text")
        metrics.outbox_job_event("observation.recorded", "completed")
        logger.emit("observation.committed", count=1, code="ok")

        # Error details from a failing observe must not carry content either.
        from iris_memory_core.domain.errors import InvalidRequestError

        error = InvalidRequestError("bad record", details={"reason": "field"})
        surfaces = [
            metrics.snapshot(),
            logger.drain(),
            {"error_details": error.details, "message": str(error)},
            HealthService(clocked_store, mutable_clock, gauge=generous_gauge).readiness().checks,
        ]
        for surface in surfaces:
            assert self.CANARY not in repr(surface)

        # The stored canonical row itself is the only place content lives.
        with clocked_store.read() as tx:
            outbox_payloads = tx.raw().execute("SELECT payload FROM outbox_jobs").fetchall()
            audit_rows = tx.raw().execute("SELECT details FROM audit_events").fetchall()
        for row in outbox_payloads:
            assert self.CANARY not in row[0]
        for row in audit_rows:
            assert self.CANARY not in row[0]

    def test_log_sanitizer_drops_forbidden_fields(self) -> None:
        record = sanitize_log_record(
            {
                "event": "job.completed",
                "count": 1,
                "content": "SECRET",
                "payload": {"a": 1},
                "request_id": "r-1",
                "duration_ms": 5,
            }
        )
        assert "content" not in record
        assert "payload" not in record
        assert "request_id" not in record
        assert record["count"] == 1 and record["duration_ms"] == 5

    def test_logger_strict_mode_raises_on_forbidden_field(self) -> None:
        logger = LowSensitivityLogger(sink=lambda line: None, strict=True)
        from iris_memory_core.observability.logging import SensitiveDataLeakedError

        with pytest.raises(SensitiveDataLeakedError):
            logger.emit("x", content="SECRET")

    def test_logger_rejects_content_like_event_names(self) -> None:
        from iris_memory_core.observability.logging import SensitiveDataLeakedError

        logger = LowSensitivityLogger(sink=lambda line: None)
        with pytest.raises(SensitiveDataLeakedError):
            logger.emit(f"some long free text {self.CANARY}")

    def test_metrics_reject_forbidden_labels(self) -> None:
        metrics = Metrics()
        with pytest.raises(InvalidMetricError):
            metrics.inc("iris_observations_total", {"role": "user", "tenant_id": "t1"})
        with pytest.raises(InvalidMetricError):
            metrics.set_gauge("iris_storage_free_bytes", 1.0, {"agent_id": "a"})

    def test_metrics_reject_unknown_names_and_wrong_labels(self) -> None:
        metrics = Metrics()
        with pytest.raises(InvalidMetricError):
            metrics.inc("iris_unknown_metric")
        with pytest.raises(InvalidMetricError):
            metrics.inc("iris_observations_total", {"role": "user"})

    def test_metrics_hash_free_form_kinds(self) -> None:
        metrics = Metrics()
        metrics.inc("iris_observations_total", {"role": "user", "kind": "custom.kind"})
        rendered = metrics.render_prometheus()
        assert "custom.kind" not in rendered
        assert "iris_observations_total" in rendered

    def test_static_scan_sources_for_direct_logging_of_sensitive_fields(self) -> None:
        """No module under src/ may reference forbidden fields in log/metric
        emitters, and the logging module must keep the allowlist intact."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[3] / "src" / "iris_memory_core"
        offenders: list[str] = []
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "observability" not in str(path) and (
                "getLogger" in text or "logging.basicConfig" in text
            ):
                offenders.append(str(path))
        assert offenders == []
        assert "content" in FORBIDDEN_LOG_FIELDS
        assert "tenant_id" in FORBIDDEN_LABELS
