"""Canonical-derived, resumable statistics projection using bounded batches."""

from __future__ import annotations

import json
from typing import Any

from iris_memory_core.application.console.resources import ReadQuery, ReadRecord
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.domain.console import OperatorGrant, Selector
from iris_memory_core.domain.errors import ConflictError, InvalidRequestError
from iris_memory_core.domain.statistics import (
    DAY_US,
    MEMORY_COLLECTIONS,
    bucket_start,
    histogram,
)

BATCH_SIZE = 200
COLLECTIONS = (*MEMORY_COLLECTIONS, "agents", "spaces", "sessions")
STAGES = (*COLLECTIONS, "recall", "audit", "aggregate")
INTERNAL_SCAN = OperatorGrant(
    frozenset(), Selector("all"), Selector("all"), Selector("all"), Selector("all")
)


def _labels(record: ReadRecord) -> dict[str, Any]:
    return {
        "scope": record.scope.as_dict(),
        "privacy_labels": list(record.privacy_labels),
        "resources": [[record.resource_type, record.id]],
        "resource_type": record.resource_type,
        "status": record.status,
        "decision": record.status,
        "source_authority": record.fields.get("source_authority"),
        "created_us": record.created_us,
    }


class StatisticsRollup:
    """Each step writes <= 200 source rows. Replayed keys replace rather than add."""

    @staticmethod
    def validate_range(lower: int, upper: int) -> None:
        if (
            type(lower) is not int
            or type(upper) is not int
            or lower < 0
            or lower >= upper
            or upper - lower > 31 * DAY_US
            or bucket_start(lower, "hour") != lower
            or bucket_start(upper, "hour") != upper
        ):
            raise InvalidRequestError("statistics backfill requires 1..744 complete UTC hours")

    @staticmethod
    def _put(
        tx: Transaction,
        build: dict[str, Any],
        metric: str,
        identifier: str,
        labels: dict[str, Any],
        value: dict[str, Any],
        *,
        created: int | None = None,
    ) -> None:
        if created is not None and not build["from_us"] <= created < build["to_us"]:
            return
        tx.statistics.put(
            build_id=build["id"],
            tenant_id=build["tenant_id"],
            bucket=bucket_start(created, "hour") if created is not None else 0,
            granularity="hour" if created is not None else "snapshot",
            metric=metric,
            atom_id=identifier,
            labels=labels,
            value=value,
        )

    def _memory(
        self,
        tx: Transaction,
        build: dict[str, Any],
        collection: str,
        after: tuple[int, str] | None,
        now: int,
    ) -> tuple[int, str] | None:
        rows = tx.console_reads.scan(
            collection,
            build["tenant_id"],
            INTERNAL_SCAN,
            ReadQuery(limit=BATCH_SIZE + 1, ceiling_us=build["started_us"], after=after),
        )
        for record in rows[:BATCH_SIZE]:
            labels = _labels(record)
            identifier = record.resource_type + ":" + record.id
            deleted = tx.is_tombstoned(build["tenant_id"], record.resource_type, record.id)
            if collection in {"agents", "spaces", "sessions"}:
                if not deleted:
                    self._put(tx, build, "active." + collection, identifier, labels, {"count": 1})
                continue
            if deleted or record.status in {"tombstoned", "erased"}:
                self._put(tx, build, "memory.tombstones", identifier, labels, {"count": 1})
                continue
            if (
                record.fields.get("expires_us") is not None
                and int(record.fields["expires_us"]) <= now
            ):
                continue
            self._put(tx, build, "memory.present", identifier, labels, {"count": 1})
            self._put(
                tx,
                build,
                "memory.created",
                identifier,
                labels,
                {"count": 1},
                created=record.created_us,
            )
            self._put(tx, build, "storage.rows", identifier, labels, {"count": 1})
            # Encoded row representation is an estimate, never SQLite page allocation.
            estimated = len(json.dumps(record.fields, ensure_ascii=False, default=str).encode())
            self._put(
                tx, build, "storage.estimated_bytes", identifier, labels, {"count": estimated}
            )
            if record.status in {"pending", "proposed", "unconfirmed", "failed", "needs_review"}:
                self._put(tx, build, "pending.items", identifier, labels, {"count": 1})
        return rows[BATCH_SIZE - 1].key if len(rows) > BATCH_SIZE else None

    def _recall(
        self, tx: Transaction, build: dict[str, Any], after: tuple[int, str] | None
    ) -> tuple[int, str] | None:
        rows = tx.statistics.recall_rows(
            build["tenant_id"], build["from_us"], build["to_us"], after=after
        )
        for row in rows[:BATCH_SIZE]:
            if row["statistics_json"] is None:
                # No retroactive scope or timing inference from response_json.
                continue
            observed = json.loads(row["statistics_json"])
            identifier = "request:" + row["id"]
            labels = {
                key: observed[key]
                for key in (
                    "scope",
                    "privacy_labels",
                    "resources",
                    "required_subjects",
                    "required_custom_labels",
                )
            }
            values = {
                "recall.requests": {"count": 1},
                "recall.budget_truncated": {
                    "numerator": int(observed["budget_truncated"]),
                    "denominator": 1,
                },
                "recall.host_selected": {"count": row["host_selected"]},
                "recall.model_visible": {"count": row["model_visible"]},
            }
            if row["duration_us"] is not None:
                for quantile in (50, 95, 99):
                    values[f"recall.latency_p{quantile}"] = {
                        "histogram": histogram(row["duration_us"])
                    }
            for metric, value in values.items():
                self._put(tx, build, metric, identifier, labels, value, created=row["created_us"])
            for route in observed["routes"]:
                self._put(
                    tx,
                    build,
                    "recall.candidates",
                    identifier + ":" + route["route"],
                    {**labels, "route": route["route"]},
                    {"count": route["candidate_count"]},
                    created=row["created_us"],
                )
            for degradation in observed["degraded"]:
                self._put(
                    tx,
                    build,
                    "recall.degraded",
                    identifier + ":" + degradation["route"],
                    {**labels, **degradation},
                    {"count": 1},
                    created=row["created_us"],
                )
        return (
            (rows[BATCH_SIZE - 1]["created_us"], rows[BATCH_SIZE - 1]["id"])
            if len(rows) > BATCH_SIZE
            else None
        )

    def _audit(
        self, tx: Transaction, build: dict[str, Any], after: tuple[int, str] | None
    ) -> tuple[int, str] | None:
        from iris_memory_core.application.console.resources import TYPE_TO_COLLECTION

        rows = tx.statistics.audit_rows(
            build["tenant_id"], build["from_us"], build["to_us"], after=after
        )
        for row in rows[:BATCH_SIZE]:
            labels: dict[str, Any] = {
                "scope": {"tenant_id": build["tenant_id"]},
                "privacy_labels": [],
                "resources": [],
                "action": row["action"],
            }
            if row["resource_type"] in TYPE_TO_COLLECTION:
                record = tx.console_reads.get(
                    TYPE_TO_COLLECTION[row["resource_type"]], build["tenant_id"], row["resource_id"]
                )
                if record is None:
                    continue
                labels.update(_labels(record))
            elif row["resource_type"] == "console_operator_key":
                labels["operator_key_id"] = row["resource_id"]
            elif row["resource_type"] == "console_operation":
                operation = tx.console_operations.get(build["tenant_id"], row["resource_id"])
                if operation is None:
                    continue
                labels["operator_key_id"] = operation.key_id
            else:
                # Legacy audit records without an authorization envelope are not
                # retrospectively treated as tenant-global visible events.
                labels["unresolved_scope"] = True
            self._put(
                tx,
                build,
                "security.audit",
                row["id"],
                labels,
                {"count": 1},
                created=row["created_us"],
            )
        return (
            (rows[BATCH_SIZE - 1]["created_us"], rows[BATCH_SIZE - 1]["id"])
            if len(rows) > BATCH_SIZE
            else None
        )

    def _aggregate(
        self, tx: Transaction, build: dict[str, Any], after: tuple[int, str] | None
    ) -> tuple[int, str] | None:
        rows = tx.statistics.aggregate_hours(build["tenant_id"], build["id"], after=after)
        for row in rows[:BATCH_SIZE]:
            for granularity in ("day", "week"):
                # Every source atom is one canonical event: preserving its identity
                # makes day/week merging idempotent and permits current reauthorization.
                tx.statistics.put(
                    build_id=build["id"],
                    tenant_id=build["tenant_id"],
                    bucket=max(0, bucket_start(row["bucket_start_us"], granularity)),
                    granularity=granularity,
                    metric=row["metric"],
                    atom_id=row["atom_id"],
                    labels=json.loads(row["labels_json"]),
                    value=json.loads(row["value_json"]),
                )
        return (
            (rows[BATCH_SIZE - 1]["bucket_start_us"], rows[BATCH_SIZE - 1]["cursor_id"])
            if len(rows) > BATCH_SIZE
            else None
        )

    def step(self, tx: Transaction, tenant_id: str, identifier: str, now: int) -> tuple[int, bool]:
        build = tx.statistics.build(tenant_id, identifier)
        if build is None or build["state"] != "building":
            raise ConflictError("statistics build is no longer active")
        stage = build["stage"]
        if stage >= len(STAGES):
            raise ConflictError("statistics build stage is invalid")
        after = (
            (build["cursor_created_us"], build["cursor_id"])
            if build["cursor_id"] is not None
            else None
        )
        if STAGES[stage] == "recall":
            cursor = self._recall(tx, build, after)
        elif STAGES[stage] == "audit":
            cursor = self._audit(tx, build, after)
        elif STAGES[stage] == "aggregate":
            cursor = self._aggregate(tx, build, after)
        else:
            cursor = self._memory(tx, build, STAGES[stage], after, now)
        next_stage = stage + 1 if cursor is None else stage
        complete = next_stage == len(STAGES)
        tx.statistics.advance(
            tenant_id, identifier, stage=next_stage, after=cursor, now=now, complete=complete
        )
        return next_stage, complete
