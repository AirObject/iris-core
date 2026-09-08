"""Fixed-table, bounded Console statistics adapter; only projection writes are mutable."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from iris_memory_core.domain.console import OperatorGrant
from iris_memory_core.domain.errors import ConflictError, NotReadyError
from iris_memory_core.domain.statistics import DIMENSIONS


class StatisticsRepository:
    def __init__(
        self, connection: sqlite3.Connection, roots: dict[str, Path] | None = None
    ) -> None:
        self.connection = connection
        self.roots = roots or {}

    @contextmanager
    def budget(self, milliseconds: int = 250) -> Iterator[None]:
        """A whole submetric is bounded, so each individual statement is too."""
        if not 1 <= milliseconds <= 250:
            raise ValueError("statistics statement budget must be 1..250ms")
        deadline = time.monotonic() + milliseconds / 1000
        previous = bool(self.connection.execute("PRAGMA query_only").fetchone()[0])
        self.connection.execute("PRAGMA query_only=ON")
        self.connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            yield
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
                raise NotReadyError("statistics statement budget exceeded") from None
            raise
        finally:
            self.connection.set_progress_handler(None, 0)
            if not previous:
                self.connection.execute("PRAGMA query_only=OFF")

    def coverage_from_us(self) -> int:
        return int(
            self.connection.execute(
                "SELECT coverage_from_us FROM console_stat_coverage WHERE singleton=1"
            ).fetchone()[0]
        )

    def create_build(
        self,
        identifier: str,
        tenant_id: str,
        lower: int,
        upper: int,
        now: int,
        *,
        operation_id: str | None = None,
    ) -> None:
        active = self.connection.execute(
            "SELECT build_id,expires_us FROM console_stat_leases WHERE tenant_id=?", (tenant_id,)
        ).fetchone()
        if active is not None and active["expires_us"] > now:
            raise ConflictError("statistics rebuild is already active")
        self.connection.execute(
            (
                "INSERT INTO console_stat_builds(id,tenant_id,from_us,to_us,"
                "started_us) VALUES(?,?,?,?,?)"
            ),
            (identifier, tenant_id, lower, upper, now),
        )
        self.connection.execute(
            "INSERT INTO console_stat_leases(tenant_id,build_id,operation_id,epoch,expires_us) "
            "VALUES(?,?,?,1,?) ON CONFLICT(tenant_id) DO UPDATE SET build_id=excluded.build_id,"
            "operation_id=excluded.operation_id,epoch=console_stat_leases.epoch+1,"
            "expires_us=excluded.expires_us",
            (tenant_id, identifier, operation_id, now + 300_000_000),
        )

    def build(self, tenant_id: str, identifier: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM console_stat_builds WHERE tenant_id=? AND id=?", (tenant_id, identifier)
        ).fetchone()
        return dict(row) if row else None

    def current_build(
        self, tenant_id: str, *, bucket_us: int | None = None, end_us: int | None = None
    ) -> dict[str, Any] | None:
        values: list[Any] = [tenant_id]
        covered = ""
        if bucket_us is not None:
            covered = " AND from_us<=? AND to_us>=?"
            values.extend([bucket_us, end_us if end_us is not None else bucket_us + 1])
        row = self.connection.execute(
            "SELECT * FROM console_stat_builds WHERE tenant_id=? AND state='complete'"
            + covered
            + " ORDER BY completed_us DESC,id DESC LIMIT 1",
            values,
        ).fetchone()
        return dict(row) if row else None

    def advance(
        self,
        tenant_id: str,
        identifier: str,
        *,
        stage: int,
        after: tuple[int, str] | None,
        now: int,
        complete: bool = False,
    ) -> None:
        owner = self.connection.execute(
            "SELECT 1 FROM console_stat_leases WHERE tenant_id=? AND build_id=? AND expires_us>?",
            (tenant_id, identifier, now),
        ).fetchone()
        if owner is None:
            raise ConflictError("statistics tenant lease changed")
        self.connection.execute(
            "UPDATE console_stat_builds SET stage=?,cursor_created_us=?,cursor_id=?,state=?,"
            "completed_us=? WHERE tenant_id=? AND id=? AND state='building'",
            (
                stage,
                after[0] if after else None,
                after[1] if after else None,
                "complete" if complete else "building",
                now if complete else None,
                tenant_id,
                identifier,
            ),
        )
        if complete:
            self.connection.execute(
                "DELETE FROM console_stat_leases WHERE tenant_id=? AND build_id=?",
                (tenant_id, identifier),
            )
        else:
            self.connection.execute(
                "UPDATE console_stat_leases SET expires_us=? WHERE tenant_id=? AND build_id=?",
                (now + 300_000_000, tenant_id, identifier),
            )

    def cancel(self, tenant_id: str, identifier: str) -> None:
        self.connection.execute(
            "UPDATE console_stat_builds SET state='cancelled' "
            "WHERE tenant_id=? AND id=? AND state='building'",
            (tenant_id, identifier),
        )
        self.connection.execute(
            "DELETE FROM console_stat_leases WHERE tenant_id=? AND build_id=?",
            (tenant_id, identifier),
        )

    def put(
        self,
        *,
        build_id: str,
        tenant_id: str,
        bucket: int,
        granularity: str,
        metric: str,
        atom_id: str,
        labels: dict[str, Any],
        value: dict[str, Any],
    ) -> None:
        scope = labels["scope"]
        self.connection.execute(
            "INSERT INTO console_stat_rollups(build_id,tenant_id,bucket_start_us,"
            "granularity,metric,"
            "atom_id,agent_id,space_group_id,space_id,session_id,labels_json,value_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(build_id,granularity,metric,"
            "bucket_start_us,atom_id) "
            "DO UPDATE SET agent_id=excluded.agent_id,space_group_id=excluded.space_group_id,"
            "space_id=excluded.space_id,session_id=excluded.session_id,labels_json=excluded.labels_json,"
            "value_json=excluded.value_json",
            (
                build_id,
                tenant_id,
                bucket,
                granularity,
                metric,
                atom_id,
                *(scope.get(key) for key in DIMENSIONS),
                json.dumps(labels, sort_keys=True, separators=(",", ":")),
                json.dumps(value, sort_keys=True, separators=(",", ":")),
            ),
        )

    def aggregate_hours(
        self, tenant_id: str, identifier: str, *, after: tuple[int, str] | None, limit: int = 201
    ) -> tuple[dict[str, Any], ...]:
        if not 1 <= limit <= 201:
            raise ValueError("rollup batch too large")
        values: list[Any]
        clauses, values = (
            ["tenant_id=?", "build_id=?", "granularity='hour'"],
            [tenant_id, identifier],
        )
        if after:
            clauses.append("(bucket_start_us,metric||':'||atom_id)>(?,?)")
            values.extend(after)
        values.append(limit)
        rows = self.connection.execute(
            "SELECT *,metric||':'||atom_id AS cursor_id FROM console_stat_rollups "
            f"WHERE {' AND '.join(clauses)} ORDER BY bucket_start_us,cursor_id LIMIT ?",
            values,
        )
        return tuple(dict(row) for row in rows)

    def atoms(
        self,
        tenant_id: str,
        build_id: str,
        metric: str,
        granularity: str,
        grant: OperatorGrant,
        *,
        bucket: int | None = None,
        limit: int = 4001,
    ) -> tuple[dict[str, Any], ...]:
        if not 1 <= limit <= 4001:
            raise ValueError("statistics read exceeds its row bound")
        values: list[Any]
        clauses, values = (
            ["tenant_id=?", "build_id=?", "metric=?", "granularity=?"],
            [tenant_id, build_id, metric, granularity],
        )
        if bucket is not None:
            clauses.append("bucket_start_us=?")
            values.append(bucket)
        for dimension in DIMENSIONS:
            selector = getattr(grant, dimension[:-3] + "_selector")
            if selector.mode == "ids":
                if not selector.ids:
                    return ()
                clauses.append(
                    f"({dimension} IS NULL OR {dimension} IN "
                    f"({','.join('?' for _ in selector.ids)}))"
                )
                values.extend(sorted(selector.ids))
        values.append(limit)
        rows = self.connection.execute(
            "SELECT atom_id,labels_json,value_json,bucket_start_us FROM console_stat_rollups "
            f"WHERE {' AND '.join(clauses)} ORDER BY bucket_start_us,atom_id LIMIT ?",
            values,
        )
        return tuple(dict(row) for row in rows)

    def recall_rows(
        self,
        tenant_id: str,
        lower: int,
        upper: int,
        *,
        after: tuple[int, str] | None,
        limit: int = 201,
    ) -> tuple[dict[str, Any], ...]:
        if not 1 <= limit <= 201:
            raise ValueError("recall observation batch too large")
        clause, values = "", [tenant_id, lower, upper]
        if after:
            clause = " AND (r.created_us,r.id)>(?,?)"
            values.extend(after)
        values.append(limit)
        # No response_json or raw candidate list is selected.
        rows = self.connection.execute(
            "SELECT r.id,r.created_us,r.duration_us,r.statistics_json,"
            "r.retrieved_count,r.returned_count,"
            "(SELECT COALESCE(SUM(json_array_length(u.host_selected_ids)),0) "
            "FROM recall_usage_reports u "
            "WHERE u.tenant_id=r.tenant_id AND u.request_id=r.id) AS host_selected,"
            "(SELECT COALESCE(SUM(json_array_length(u.model_visible_ids)),0) "
            "FROM recall_usage_reports u "
            "WHERE u.tenant_id=r.tenant_id AND u.request_id=r.id) AS model_visible "
            "FROM recall_requests r WHERE r.tenant_id=? AND r.created_us>=? AND r.created_us<?"
            + clause
            + " ORDER BY r.created_us,r.id LIMIT ?",
            values,
        )
        return tuple(dict(row) for row in rows)

    def audit_rows(
        self,
        tenant_id: str,
        lower: int,
        upper: int,
        *,
        after: tuple[int, str] | None,
        limit: int = 201,
    ) -> tuple[dict[str, Any], ...]:
        if not 1 <= limit <= 201:
            raise ValueError("audit observation batch too large")
        clause = ""
        values: list[Any] = [tenant_id, lower, upper]
        if after:
            clause = " AND (created_us,id)>(?,?)"
            values.extend(after)
        values.append(limit)
        rows = self.connection.execute(
            "SELECT id,resource_type,resource_id,action,created_us "
            "FROM audit_events WHERE tenant_id=? AND created_us>=? AND created_us<?"
            + clause
            + " ORDER BY created_us,id LIMIT ?",
            values,
        )
        return tuple(dict(row) for row in rows)

    def live(self, tenant_id: str, source: str, *, limit: int = 1001) -> tuple[dict[str, Any], ...]:
        if not 1 <= limit <= 1001:
            raise ValueError("live statistics exceeds its row bound")
        queries = {
            "jobs": (
                "SELECT id,agent_id,job_kind,aggregate_type,aggregate_id,status,"
                "created_us,available_at_us,lease_expires_us,last_error_code,"
                "last_heartbeat_us FROM "
                "outbox_jobs WHERE tenant_id=? ORDER BY created_us DESC,id LIMIT ?"
            ),
            "login": (
                "SELECT a.id,a.action,a.resource_id,k.grants_json FROM audit_events a "
                "JOIN console_operator_keys k ON k.tenant_id=a.tenant_id AND k.id=a.resource_id "
                "WHERE a.tenant_id=? AND a.resource_type='console_operator_key' "
                "AND a.action IN ('console.login.failed','console.login.locked') "
                "ORDER BY a.created_us DESC,a.id LIMIT ?"
            ),
            "schedules": (
                "SELECT id,agent_id,job_kind,next_tick_at_us,last_tick_at_us,enabled "
                "FROM schedules WHERE tenant_id=? ORDER BY next_tick_at_us,id LIMIT ?"
            ),
            "keys": (
                "SELECT id,status,template,grants_json,expires_us FROM "
                "console_operator_keys WHERE tenant_id=? ORDER BY created_us DESC,id "
                "LIMIT ?"
            ),
            "audit": (
                "SELECT id,action,resource_type,resource_id,created_us FROM "
                "audit_events WHERE tenant_id=? ORDER BY created_us DESC,id LIMIT ?"
            ),
            "holds": (
                "SELECT id,agent_id,space_id,session_id,subject_entity_id,released_us "
                "FROM legal_holds WHERE tenant_id=? ORDER BY created_us DESC,id LIMIT ?"
            ),
            "retention": (
                "SELECT id,resource_type,privacy_label,enabled,threshold_days,action "
                "FROM retention_policies WHERE tenant_id=? ORDER BY created_us DESC,id "
                "LIMIT ?"
            ),
        }
        if source not in queries:
            raise ValueError("unknown live statistics source")
        return tuple(
            dict(row) for row in self.connection.execute(queries[source], (tenant_id, limit))
        )

    def active_sessions(self, key_ids: tuple[str, ...], now: int) -> int:
        if not key_ids or len(key_ids) > 1000:
            return 0
        rows = self.connection.execute(
            "SELECT id FROM console_sessions WHERE key_id IN ("
            + ",".join("?" for _ in key_ids)
            + ") AND revoked_us IS NULL "
            "AND expires_us>? AND idle_expires_us>? LIMIT 1001",
            (*key_ids, now, now),
        ).fetchall()
        if len(rows) > 1000:
            raise NotReadyError("session statistics row bound exceeded")
        return len(rows)

    def projection(
        self, tenant_id: str, kind: str
    ) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], ...]]:
        if kind not in {"fts", "vector", "profile", "graph", "recent_context"}:
            raise ValueError("unknown projection")
        if kind == "recent_context":
            rows = self.connection.execute(
                "SELECT g.id,g.agent_id,g.space_group_id,g.space_id,g.session_id,"
                "g.source_watermark,g.hot_observation_refs,g.created_us,g.expires_us "
                "FROM recent_context_current c JOIN recent_context_generations g "
                "ON g.id=c.current_generation_id AND g.tenant_id=c.tenant_id "
                "WHERE c.tenant_id=? ORDER BY g.id LIMIT 1001",
                (tenant_id,),
            )
            return {"state": "ready"}, tuple(dict(row) for row in rows)
        columns = "g.id,g.source_watermark,g.tombstone_watermark,g.created_us,c.switched_us"
        if kind != "fts":
            columns += ",g.agent_watermarks_json"
        row = self.connection.execute(
            f"SELECT {columns} FROM {kind}_current c JOIN {kind}_generations g "
            "ON g.id=c.generation_id AND g.tenant_id=c.tenant_id WHERE c.tenant_id=?",
            (tenant_id,),
        ).fetchone()
        if row is None:
            return None, ()
        metadata = dict(row)
        state = self.connection.execute(
            f"SELECT state FROM {kind}_projection_state WHERE id=1"
        ).fetchone()
        metadata["state"] = state[0] if state else "never_built"
        query = {
            "fts": (
                "SELECT resource_type,resource_id FROM fts_documents WHERE tenant_id=? "
                "AND generation_id=? AND doc_status='active' ORDER BY id LIMIT 1001"
            ),
            "vector": (
                "SELECT resource_type,resource_id FROM vector_id_map WHERE tenant_id=? "
                "AND status='active' ORDER BY surrogate_id LIMIT 1001"
            ),
            "profile": (
                "SELECT agent_id,space_group_id,space_id,session_id,"
                "privacy_labels_json,source_refs_json FROM profile_fields WHERE "
                "tenant_id=? AND generation_id=? ORDER BY subject_kind,subject_id,"
                "section,field,group_key LIMIT 1001"
            ),
            "graph": (
                "SELECT resource_type,resource_id FROM graph_edges WHERE tenant_id=? "
                "AND generation_id=? AND status IN ('active','disputed') ORDER BY "
                "edge_id LIMIT 1001"
            ),
        }[kind]
        values = (tenant_id,) if kind == "vector" else (tenant_id, row["id"])
        return metadata, tuple(dict(item) for item in self.connection.execute(query, values))

    def directory_size(self, metric: str) -> int | None:
        root = self.roots.get(metric)
        if root is None:
            return None
        deadline = time.monotonic() + 0.250
        pending = [root]
        count = total = 0
        while pending:
            directory = pending.pop()
            if directory.is_symlink():
                raise NotReadyError("statistics directory is not a regular managed root")
            if not directory.exists():
                continue
            with os.scandir(directory) as entries:
                for entry in entries:
                    count += 1
                    if count > 4000 or time.monotonic() >= deadline:
                        raise NotReadyError("statistics directory budget exceeded")
                    metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(metadata.st_mode):
                        total += metadata.st_size
        return total

    def file_sizes(self) -> dict[str, int]:
        database = self.connection.execute("PRAGMA database_list").fetchone()[2]
        path = Path(database)
        wal = Path(str(path) + "-wal")
        return {
            "storage.database_bytes": path.stat().st_size if path.is_file() else 0,
            "storage.wal_bytes": wal.stat().st_size if wal.is_file() else 0,
            "storage.free_bytes": int(
                self.connection.execute("PRAGMA freelist_count").fetchone()[0]
            )
            * int(self.connection.execute("PRAGMA page_size").fetchone()[0]),
        }
