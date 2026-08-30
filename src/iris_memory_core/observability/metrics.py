"""Low-cardinality metrics registry (§31).

Only the frozen metric names and label keys are accepted; high-cardinality
identifiers (tenant/agent/space/entity/task/request ids) and free-form kind
strings are rejected or hashed. Snapshots feed /metrics and readiness —
counts, gauges and timings only, never content.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Any

# Frozen metric surface (§31). Extending requires a contract review.
METRIC_SPECS: dict[str, frozenset[str]] = {
    "iris_observations_total": frozenset({"role", "kind"}),
    "iris_outbox_jobs": frozenset({"status", "job_kind"}),
    "iris_outbox_oldest_age_seconds": frozenset({"job_kind"}),
    "iris_schedule_lag_seconds": frozenset({"job_kind"}),
    "iris_sqlite_busy_total": frozenset({"operation_class"}),
    "iris_storage_free_bytes": frozenset(),
    "iris_active_surface_leases": frozenset({"status"}),
}

#: Labels that must NEVER appear on a metric (§31 forbidden cardinality).
FORBIDDEN_LABELS = frozenset(
    {"tenant_id", "agent_id", "space_id", "entity_id", "task_id", "request_id"}
)

#: Label keys whose values are enum-like and pass through unhashed.
_LOW_CARDINALITY_VALUES = frozenset(
    {"role", "status", "operation_class", "lane", "mode", "job_kind"}
)


class InvalidMetricError(ValueError):
    """Unknown metric name, forbidden label key, or wrong label set."""


def hash_label_value(value: str) -> str:
    """Hash free-form values so kind strings neither leak nor explode cardinality."""
    return "h_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _is_bounded(key: str, value: str) -> bool:
    if key == "job_kind":
        from iris_memory_core.domain.jobs import JOB_KINDS

        return value in JOB_KINDS
    return True


def _normalize_labels(name: str, labels: dict[str, str]) -> dict[str, str]:
    expected = METRIC_SPECS[name]
    given = frozenset(labels)
    if given & FORBIDDEN_LABELS:
        raise InvalidMetricError(f"forbidden label keys: {sorted(given & FORBIDDEN_LABELS)}")
    if expected and given != expected:
        raise InvalidMetricError(
            f"metric {name} expects labels {sorted(expected)}, got {sorted(given)}"
        )
    if not expected and given:
        raise InvalidMetricError(f"metric {name} takes no labels, got {sorted(given)}")
    return {
        key: value
        if key in _LOW_CARDINALITY_VALUES and _is_bounded(key, value)
        else hash_label_value(value)
        for key, value in labels.items()
    }


def _check_name(name: str) -> None:
    if name not in METRIC_SPECS:
        raise InvalidMetricError(f"unknown metric name: {name}")


class Metrics:
    """Thread-safe counters and gauges with the frozen label surface."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

    def inc(self, name: str, labels: dict[str, str] | None = None, amount: int = 1) -> None:
        _check_name(name)
        normalized = _normalize_labels(name, labels or {})
        key = (name, tuple(sorted(normalized.items())))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + amount

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        _check_name(name)
        normalized = _normalize_labels(name, labels or {})
        key = (name, tuple(sorted(normalized.items())))
        with self._lock:
            self._gauges[key] = value

    # -- Phase 2 convenience emitters ---------------------------------------

    def observation_recorded(self, *, role: str, kind: str) -> None:
        self.inc("iris_observations_total", {"role": role, "kind": kind})

    def outbox_job_event(self, job_kind: str, status: str) -> None:
        self.inc("iris_outbox_jobs", {"status": status, "job_kind": job_kind})

    def sqlite_busy(self, operation_class: str) -> None:
        self.inc("iris_sqlite_busy_total", {"operation_class": operation_class})

    def storage_free(self, free_bytes: int) -> None:
        self.set_gauge("iris_storage_free_bytes", float(free_bytes))

    def outbox_oldest_age(self, job_kind: str, age_seconds: float) -> None:
        self.set_gauge("iris_outbox_oldest_age_seconds", age_seconds, {"job_kind": job_kind})

    def schedule_lag(self, job_kind: str, lag_seconds: float) -> None:
        self.set_gauge("iris_schedule_lag_seconds", lag_seconds, {"job_kind": job_kind})

    def surface_leases(self, status: str, count: int) -> None:
        self.set_gauge("iris_active_surface_leases", float(count), {"status": status})

    # -- output --------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = [
                {"name": name, "labels": dict(labels), "value": value}
                for (name, labels), value in sorted(self._counters.items())
            ]
            gauges = [
                {"name": name, "labels": dict(labels), "value": value}
                for (name, labels), value in sorted(self._gauges.items())
            ]
        return {"counters": counters, "gauges": gauges}

    def render_prometheus(self) -> str:
        """Text exposition; label values are already bounded or hashed."""
        lines: list[str] = []
        snapshot = self.snapshot()
        for item in [*snapshot["counters"], *snapshot["gauges"]]:
            labels = ",".join(f'{key}="{value}"' for key, value in item["labels"].items())
            suffix = f"{{{labels}}}" if labels else ""
            lines.append(f"{item['name']}{suffix} {item['value']}")
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()


__all__ = [
    "FORBIDDEN_LABELS",
    "METRIC_SPECS",
    "InvalidMetricError",
    "Metrics",
    "hash_label_value",
]
