"""Current-Grant statistics, partial reads and explicit coverage/freshness."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import defaultdict
from dataclasses import replace
from typing import Any

from iris_memory_core.application.console.authorization import ConsoleAuthorization
from iris_memory_core.application.console.execution_context import ExecutionContext
from iris_memory_core.application.console.reads import ResourceReader
from iris_memory_core.application.console.resources import ReadRecord, ResourceRef
from iris_memory_core.application.console.security import authorize, denied
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.domain.console import OperatorGrant, OperatorPrincipal
from iris_memory_core.domain.errors import InvalidRequestError, NotReadyError
from iris_memory_core.domain.scope import Scope
from iris_memory_core.domain.statistics import (
    BY_METRIC,
    DAY_US,
    DIMENSIONS,
    GRANULARITIES,
    HOUR_US,
    LATENCY_BOUNDS_US,
    METRICS,
    PANELS,
    MetricSpec,
    bucket_start,
)

INSTANCE_STARTED_MONOTONIC = time.monotonic()
MAX_ATOMS = 4000


def authorize_statistics(
    tx: Transaction,
    principal: OperatorPrincipal,
    now: int,
    *,
    system: bool = False,
    recent: bool = False,
) -> OperatorPrincipal:
    fresh = authorize(tx, principal, now, "stats.read", recent=recent)
    if "console.manage" not in fresh.key.grant.data_purposes or (
        system and "system.read" not in fresh.permissions
    ):
        raise denied("permission_denied")
    return fresh


def scope_fingerprint(principal: OperatorPrincipal) -> str:
    material = (
        f"{principal.key.tenant_id}:{principal.key.id}:{principal.key.revision}:"
        f"{principal.key.grant.fingerprint}:{principal.session.id}:{principal.session.epoch}"
    )
    return "sf_" + hashlib.sha256(material.encode()).hexdigest()


class Statistics:
    def __init__(self, context: ExecutionContext) -> None:
        self.context = context

    def registry(self, principal: OperatorPrincipal) -> tuple[list[MetricSpec], int]:
        with self.context.uow.read() as tx:
            fresh = authorize_statistics(tx, principal, self.context.clock.now_us())
            with tx.statistics.budget():
                coverage = tx.statistics.coverage_from_us()
            return [
                metric
                for metric in METRICS
                if not metric.system or "system.read" in fresh.permissions
            ], coverage

    @staticmethod
    def _visible(reader: ResourceReader, labels: dict[str, Any], metric: str) -> bool:
        grant = reader.principal.key.grant
        if labels.get("unresolved_scope"):
            return False
        if labels.get("operator_key_id"):
            key = reader.tx.console.key(labels["operator_key_id"])
            if (
                key is None
                or key.tenant_id != reader.principal.key.tenant_id
                or not grant.includes(key.grant)
            ):
                return False
        if (
            not set(labels.get("required_subjects", ())) <= grant.subject_entity_ids
            or not set(labels.get("required_custom_labels", ())) <= grant.custom_privacy_labels
        ):
            return False
        scope = Scope(**labels["scope"])
        synthetic = ReadRecord(
            "statistic",
            "statistic",
            scope,
            1,
            "active",
            {},
            tuple(labels.get("privacy_labels", ())),
            (),
            0,
            0,
        )
        if not reader.authority.visible(reader.tx, synthetic):
            return False
        for kind, identifier in labels.get("resources", ()):
            if metric == "memory.tombstones":
                # Deletion metadata retains its own privacy envelope; canonical
                # scope and privacy changes still have to match today's Grant.
                from iris_memory_core.application.console.resources import TYPE_TO_COLLECTION

                collection = TYPE_TO_COLLECTION.get(kind)
                record = (
                    reader.tx.console_reads.get(collection, scope.tenant_id, identifier)
                    if collection
                    else None
                )
                if record is None or not reader.authority.visible(
                    reader.tx, replace(record, resource_type="statistic", status="active")
                ):
                    return False
            elif reader.get(ResourceRef(kind, identifier)) is None:
                return False
        return True

    @staticmethod
    def _value(metric: MetricSpec, values: list[dict[str, Any]]) -> str | None:
        if metric.metric_id.startswith("recall.latency_p"):
            bins = [0] * (len(LATENCY_BOUNDS_US) + 1)
            for value in values:
                for index, count in enumerate(value["histogram"]):
                    bins[index] += count
            total = sum(bins)
            if not total:
                return None
            quantile = int(metric.metric_id.rsplit("p", 1)[1]) / 100
            target = math.ceil(total * quantile)
            cumulative = 0
            for index, count in enumerate(bins):
                cumulative += count
                if cumulative >= target:
                    return str(LATENCY_BOUNDS_US[index]) if index < len(LATENCY_BOUNDS_US) else None
            return None
        if metric.unit == "ratio":
            numerator = sum(v["numerator"] for v in values)
            denominator = sum(v["denominator"] for v in values)
            return str(numerator / denominator) if denominator else None
        return str(sum(value.get("count", 0) for value in values))

    def _rollup(
        self,
        tx: Transaction,
        fresh: OperatorPrincipal,
        metric: MetricSpec,
        group_by: str | None,
        filters: dict[str, str],
        bucket: int | None,
        granularity: str,
        now: int,
        warnings: set[str],
    ) -> tuple[list[dict[str, Any]], int | None]:
        if metric.metric_id.startswith("provider."):
            warnings.add("projection_unavailable")
            return [self._point(metric, None, bucket=bucket)], None
        source_metric = metric.metric_id
        if source_metric in {"memory.created_24h", "memory.created_7d"}:
            source_metric = "memory.present"
        snapshot = source_metric.startswith(
            ("memory.present", "memory.tombstones", "active.", "pending.", "storage.")
        )
        build = tx.statistics.current_build(
            fresh.key.tenant_id,
            bucket_us=bucket if not snapshot else None,
            end_us=bucket + GRANULARITIES[granularity] if bucket is not None else None,
        )
        if build is None:
            warnings.add("projection_unavailable")
            return [self._point(metric, None, bucket=bucket)], None
        atoms = tx.statistics.atoms(
            fresh.key.tenant_id,
            build["id"],
            source_metric,
            "snapshot" if snapshot else granularity,
            fresh.key.grant,
            bucket=bucket if not snapshot else None,
        )
        if len(atoms) > MAX_ATOMS:
            warnings.add("stats_row_limit")
        reader = ResourceReader(tx, fresh, now)
        reader.deadline = time.monotonic() + 0.250
        grouped: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
        for atom in atoms[:MAX_ATOMS]:
            labels = json.loads(atom["labels_json"])
            if not self._visible(reader, labels, source_metric):
                warnings.add("scope_truncated")
                continue
            if any(labels["scope"].get(key) != value for key, value in filters.items()):
                continue
            if metric.metric_id.startswith("memory.created_"):
                width = DAY_US if metric.metric_id.endswith("24h") else 7 * DAY_US
                if labels["created_us"] < now - width:
                    continue
            group = (
                labels["scope"].get(group_by)
                if group_by in DIMENSIONS
                else labels.get(group_by)
                if group_by
                else None
            )
            groups = (
                labels.get("privacy_labels", []) or ["public"]
                if group_by == "privacy_label"
                else [group]
            )
            for visible_group in groups:
                grouped[str(visible_group) if visible_group is not None else None].append(
                    json.loads(atom["value_json"])
                )
        if not grouped and group_by is None:
            grouped[None] = []
        return [
            self._point(metric, self._value(metric, values), bucket=bucket, group=group)
            for group, values in sorted(grouped.items(), key=lambda item: item[0] or "")
        ], build["completed_us"]

    @staticmethod
    def _point(
        metric: MetricSpec,
        value: str | None,
        *,
        bucket: int | None = None,
        group: str | None = None,
        labels: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "id": f"{metric.metric_id}:{bucket}:{group}",
            "metric_id": metric.metric_id,
            "value": value,
            "bucket_us": bucket,
            "group": group,
            "approximate": metric.approximate,
            "histogram_bounds_us": [str(value) for value in LATENCY_BOUNDS_US]
            if metric.metric_id.startswith("recall.latency_p")
            else None,
            "labels": labels or {},
        }

    @staticmethod
    def _scope_record(tenant: str, row: dict[str, Any]) -> ReadRecord:
        return ReadRecord(
            str(row["id"]),
            "statistic",
            Scope(tenant, **{key: row.get(key) for key in DIMENSIONS}),
            1,
            "active",
            {},
            (),
            (),
            0,
            0,
        )

    def _projections(
        self,
        tx: Transaction,
        principal: OperatorPrincipal,
        metric: MetricSpec,
        now: int,
        warnings: set[str],
    ) -> list[dict[str, Any]]:
        result = []
        reader = ResourceReader(tx, principal, now)
        reader.deadline = time.monotonic() + 0.250
        tenant_wide = all(
            getattr(principal.key.grant, key[:-3] + "_selector").mode == "all" for key in DIMENSIONS
        )
        for kind in ("fts", "vector", "profile", "graph", "recent_context"):
            metadata, rows = tx.statistics.projection(principal.key.tenant_id, kind)
            if metadata is None or (kind == "recent_context" and not rows):
                warnings.add("projection_unavailable")
                result.append(
                    self._point(metric, None, group=kind, labels={"degraded_reason": "never_built"})
                )
                continue
            if len(rows) > 1000:
                warnings.add("stats_row_limit")
            count = 0
            source_watermarks = []
            for row in rows[:1000]:
                if kind == "profile":
                    refs = json.loads(row["source_refs_json"])
                    refs = [
                        (ref["resource_type"], ref["resource_id"])
                        if isinstance(ref, dict)
                        else tuple(ref[:2])
                        for ref in refs
                    ]
                    record = self._scope_record(principal.key.tenant_id, {"id": "profile", **row})
                    if not reader.authority.visible(
                        tx,
                        replace(
                            record, privacy_labels=tuple(json.loads(row["privacy_labels_json"]))
                        ),
                    ) or any(reader.get(ResourceRef(*ref)) is None for ref in refs):
                        warnings.add("scope_truncated")
                        continue
                    count += 1
                elif kind == "recent_context":
                    record = self._scope_record(principal.key.tenant_id, row)
                    refs = json.loads(row["hot_observation_refs"])
                    if not reader.authority.visible(tx, record) or (
                        row["expires_us"] is not None and row["expires_us"] <= now
                    ):
                        warnings.add("scope_truncated")
                        continue
                    visible = []
                    for ref in refs:
                        identifier = ref["observation_id"] if isinstance(ref, dict) else ref
                        if reader.get(ResourceRef("observation", identifier)) is not None:
                            visible.append(identifier)
                        else:
                            warnings.add("scope_truncated")
                    count += len(visible)
                    source_watermarks.append(row["source_watermark"])
                elif reader.get(ResourceRef(row["resource_type"], row["resource_id"])) is not None:
                    count += 1
                else:
                    warnings.add("scope_truncated")
            labels: dict[str, Any] = {
                "degraded_reason": None if metadata["state"] == "ready" else metadata["state"]
            }
            if tenant_wide:
                labels.update(
                    generation_id=metadata.get("id"),
                    last_rebuild_us=str(metadata["created_us"])
                    if metadata.get("created_us")
                    else None,
                )
            else:
                warnings.add("scope_truncated")
            field = metric.metric_id.split(".", 1)[1]
            value: str | None = None
            if field == "documents":
                value = str(count)
            elif field == "source_watermark" and tenant_wide:
                value = str(metadata.get("source_watermark", max(source_watermarks, default=0)))
            elif (
                field == "tombstone_watermark"
                and tenant_wide
                and metadata.get("tombstone_watermark") is not None
            ):
                value = str(metadata["tombstone_watermark"])
            elif field == "lag" and metadata.get("agent_watermarks_json"):
                lag = []
                for agent_id, watermark in json.loads(metadata["agent_watermarks_json"]).items():
                    if reader.get(ResourceRef("agent", agent_id)) is None:
                        continue
                    current = tx.watermark(principal.key.tenant_id, agent_id)
                    if current is not None:
                        lag.append(max(0, current.current_seq - int(watermark)))
                value = str(max(lag, default=0))
            result.append(self._point(metric, value, group=kind, labels=labels))
        return result

    def _live(
        self,
        tx: Transaction,
        principal: OperatorPrincipal,
        metric: MetricSpec,
        group_by: str | None,
        filters: dict[str, str],
        now: int,
        warnings: set[str],
    ) -> list[dict[str, Any]]:
        name = metric.metric_id
        if name.startswith("projection."):
            return self._projections(tx, principal, metric, now, warnings)
        authority = ConsoleAuthorization(principal.key.tenant_id, principal.key.grant)
        if name in {"storage.database_bytes", "storage.wal_bytes", "storage.free_bytes"}:
            return [
                self._point(
                    metric,
                    str(tx.statistics.file_sizes()[name]),
                    labels={"scope": "instance_local"},
                )
            ]
        if name in {"storage.staging_bytes", "storage.vector_bytes"}:
            size = tx.statistics.directory_size(name)
            if size is None:
                warnings.add("projection_unavailable")
            return [
                self._point(
                    metric,
                    str(size) if size is not None else None,
                    labels={"scope": "instance_local"},
                )
            ]
        source = (
            "login"
            if name in {"security.login_failures", "security.lockouts"}
            else "jobs"
            if name.startswith("pipeline.")
            else "keys"
            if name.startswith("security.")
            else None
        )
        if name == "pipeline.schedule_lag_us":
            source = "schedules"
        if name == "security.holds":
            source = "holds"
        if name == "security.retention":
            source = "retention"
        if name.startswith(("provider.", "projection.")):
            # No persistent observations exist for these sources in this candidate.
            warnings.add("projection_unavailable")
            return [self._point(metric, None)]
        assert source is not None
        rows = tx.statistics.live(principal.key.tenant_id, source)
        if len(rows) > 1000:
            warnings.add("stats_row_limit")
        visible = []
        reader = ResourceReader(tx, principal, now)
        reader.deadline = time.monotonic() + 0.250
        for row in rows[:1000]:
            if source in {"keys", "login"}:
                if not principal.key.grant.includes(
                    OperatorGrant.parse(json.loads(row["grants_json"]))
                ):
                    warnings.add("scope_truncated")
                    continue
            else:
                record = self._scope_record(principal.key.tenant_id, row)
                labels: tuple[str, ...] = ()
                if source == "holds" and row["subject_entity_id"]:
                    labels = (f"entity:{row['subject_entity_id']}:private",)
                if source == "retention" and row["privacy_label"]:
                    labels = (row["privacy_label"],)
                if not authority.visible(tx, replace(record, privacy_labels=labels)):
                    warnings.add("scope_truncated")
                    continue
                if source == "jobs":
                    # Tenant-global management jobs have no scoped resource. Their
                    # counts are visible only to their exact current owner.
                    if row["aggregate_type"] == "console_operation":
                        operation = tx.console_operations.get(
                            principal.key.tenant_id, row["aggregate_id"]
                        )
                        if operation is None or (
                            operation.key_id,
                            operation.key_revision,
                            operation.grant_fingerprint,
                        ) != (
                            principal.key.id,
                            principal.key.revision,
                            principal.key.grant.fingerprint,
                        ):
                            warnings.add("scope_truncated")
                            continue
                    elif (
                        row["agent_id"] is None
                        or reader.get(ResourceRef("agent", row["agent_id"])) is None
                    ):
                        warnings.add("scope_truncated")
                        continue
            if any(row.get(key) != value for key, value in filters.items()):
                continue
            visible.append(row)
        if source == "login":
            action = (
                "console.login.failed"
                if name == "security.login_failures"
                else "console.login.locked"
            )
            return [self._point(metric, str(sum(row["action"] == action for row in visible)))]
        if name == "pipeline.heartbeat_us":
            stamps = [
                row["last_heartbeat_us"] for row in visible if row["last_heartbeat_us"] is not None
            ]
            return [self._point(metric, str(max(0, now - max(stamps))) if stamps else None)]
        if name == "pipeline.dlq":
            dead = [row for row in visible if row["status"] == "dead"]
            sample_labels = {
                f"sample_{index + 1}_{field}": row[field]
                for index, row in enumerate(dead[:10])
                for field in ("job_kind", "last_error_code")
            }
            return [self._point(metric, str(len(dead)), labels=sample_labels)]
        if name == "security.sessions":
            return [
                self._point(
                    metric,
                    str(tx.statistics.active_sessions(tuple(row["id"] for row in visible), now)),
                )
            ]
        if name == "security.expiring_keys":
            visible = [
                row
                for row in visible
                if now < row["expires_us"] <= now + 30 * DAY_US and row["status"] == "active"
            ]
        if name == "security.holds":
            visible = [row for row in visible if row["released_us"] is None]
        if name == "security.retention":
            visible = [row for row in visible if row["enabled"]]
        if name == "pipeline.dlq":
            visible = [row for row in visible if row["status"] == "dead"]
        if name == "pipeline.leases":
            visible = [
                row
                for row in visible
                if row["status"] == "leased" and (row["lease_expires_us"] or 0) > now
            ]
        if name == "pipeline.oldest_pending_us":
            ages = [
                max(0, now - row["created_us"])
                for row in visible
                if row["status"] in {"pending", "retryable"}
            ]
            return [self._point(metric, str(max(ages, default=0)))]
        groups: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
        for row in visible:
            groups[str(row.get(group_by)) if group_by else None].append(row)
        if not groups and group_by is None:
            groups[None] = []
        return [
            self._point(
                metric,
                str(
                    max(
                        (max(0, now - row["next_tick_at_us"]) for row in rows if row["enabled"]),
                        default=0,
                    )
                )
                if name == "pipeline.schedule_lag_us"
                else str(len(rows)),
                group=group,
            )
            for group, rows in sorted(groups.items(), key=lambda item: item[0] or "")
        ]

    def query(
        self,
        principal: OperatorPrincipal,
        panel: str,
        *,
        metric_id: str | None = None,
        granularity: str = "hour",
        lower: int | None = None,
        upper: int | None = None,
        group_by: str | None = None,
        filters: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        now = self.context.clock.now_us()
        filters = filters or {}
        if (
            panel not in PANELS
            or granularity not in GRANULARITIES
            or set(filters) - set(DIMENSIONS)
        ):
            raise InvalidRequestError("unknown statistics query")
        selected = (
            [BY_METRIC[metric_id]]
            if metric_id in BY_METRIC
            else [metric for metric in METRICS if metric.panel == panel]
        )
        if metric_id is not None and metric_id not in BY_METRIC:
            raise InvalidRequestError("unknown statistics metric")
        if panel != "timeseries" and any(metric.panel != panel for metric in selected):
            raise InvalidRequestError("metric does not belong to panel")
        if group_by and any(group_by not in metric.group_by for metric in selected):
            raise InvalidRequestError("unsupported statistics group")
        if panel == "timeseries" and metric_id is None:
            selected = [BY_METRIC["memory.created"]]
        if panel == "timeseries" and any(metric.source != "rollup" for metric in selected):
            raise InvalidRequestError("metric has no time series")
        lower = bucket_start(now - 24 * HOUR_US, granularity) if lower is None else lower
        upper = (
            bucket_start(now, granularity) + GRANULARITIES[granularity] if upper is None else upper
        )
        if (
            lower < 0
            or lower >= upper
            or (upper - lower) // GRANULARITIES[granularity] > 744
            or lower != bucket_start(lower, granularity)
            or upper != bucket_start(upper, granularity)
        ):
            raise InvalidRequestError("statistics interval must contain 1..744 aligned buckets")
        deadline = time.monotonic() + 1.0
        warnings: set[str] = set()
        points = []
        computed = []
        with self.context.uow.read() as tx:
            fresh = authorize_statistics(
                tx, principal, now, system=any(metric.system for metric in selected)
            )
            coverage = tx.statistics.coverage_from_us()
            if any(
                getattr(fresh.key.grant, key[:-3] + "_selector").mode == "ids" for key in DIMENSIONS
            ):
                warnings.add("scope_truncated")
            for metric in selected:
                buckets = (
                    list(range(lower, upper, GRANULARITIES[granularity]))
                    if panel == "timeseries"
                    else [None]
                )
                for bucket in buckets:
                    if time.monotonic() >= deadline:
                        warnings.add("stats_timeout")
                        points.append(self._point(metric, None, bucket=bucket))
                        continue
                    if metric.metric_id.startswith("recall.") and (
                        (bucket is not None and bucket < coverage)
                        or (bucket is None and upper <= coverage)
                    ):
                        points.append(self._point(metric, None, bucket=bucket))
                        warnings.add("no_historical_coverage")
                        continue
                    stamp: int | None
                    try:
                        with tx.statistics.budget():
                            if metric.source == "live":
                                values = self._live(
                                    tx, fresh, metric, group_by, filters, now, warnings
                                )
                                stamp = now
                            else:
                                values, stamp = self._rollup(
                                    tx,
                                    fresh,
                                    metric,
                                    group_by,
                                    filters,
                                    bucket,
                                    granularity,
                                    now,
                                    warnings,
                                )
                        points.extend(values)
                        if stamp is not None:
                            computed.append(stamp)
                    except NotReadyError:
                        warnings.add("stats_timeout")
                        points.append(self._point(metric, None, bucket=bucket))
            computed_at = min(computed) if computed else None
            lag = max(0, now - computed_at) if computed_at is not None else None
            stale = lag is not None and lag > 2 * HOUR_US
            if stale:
                warnings.add("rollup_lagging")
            return {
                "data": points,
                "meta": {
                    "as_of_us": now,
                    "computed_at_us": computed_at,
                    "coverage_from_us": coverage,
                    "scope_fingerprint": scope_fingerprint(fresh),
                    "source": "mixed"
                    if len({metric.source for metric in selected}) > 1
                    else selected[0].source,
                    "stale": stale,
                    "rollup_lag_us": str(lag) if lag is not None else None,
                    "warnings": sorted(warnings),
                    "partial": bool(warnings & {"stats_timeout", "stats_row_limit"}),
                    "instance_local": any(
                        metric.metric_id.startswith("storage.") and metric.source == "live"
                        for metric in selected
                    ),
                },
            }
