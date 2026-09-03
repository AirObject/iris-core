"""Phase 8 concurrency, restore and health tests (ADR-0016 §7/§9/§10):

- Concurrent graph/profile reads x rebuild/swap rounds (no torn reads, all
  hits rehydratable, determinism after quiescence).
- Restore marks both projections pending_rebuild; untrusted projections
  never serve as restored state; rebuild restores service.
- Readiness reports both projection states; optional deployments degrade
  (not not-ready); required deployments go not_ready.
- Metrics fire from production paths (generation gauge after publish, lag
  at every trusted read).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from iris_memory_core.application.backpressure import (
    BackpressureConfig,
    BackpressureGauge,
    FixedDiskProbe,
)
from iris_memory_core.application.health import HealthService
from iris_memory_core.domain.graph import GraphDegradedError
from iris_memory_core.domain.profile import ProfileDegradedError
from iris_memory_core.domain.recall import ROUTE_GRAPH
from iris_memory_core.indexing.graph import GraphProjectionService
from iris_memory_core.indexing.profile import ProfileProjectionService
from iris_memory_core.observability.metrics import Metrics
from iris_memory_core.storage.backup import BackupService, restore_backup
from iris_memory_core.storage.runtime import SQLiteRuntime
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock, local_allowed_versions
from tests.integration.phase8_helpers import TENANT, Phase8World

CONCURRENCY_ROUNDS = 10
READER_THREADS = 12


@pytest.fixture
def world(clocked_store: Store, mutable_clock: MutableClock) -> Phase8World:
    return Phase8World(clocked_store, mutable_clock)


def _gauge(database: Any) -> BackpressureGauge:
    return BackpressureGauge(
        BackpressureConfig(
            soft_disk_free_bytes=10**12,
            hard_disk_free_bytes=10**11,
            retry_base_delay_us=1_000,
            retry_max_delay_us=10_000,
        ),
        probe=FixedDiskProbe(10**13),
        database_path=database,
    )


class TestConcurrentRebuildSwap:
    def test_reads_race_rebuilds_without_torn_results(self, world: Phase8World) -> None:
        bob = world.entity("Conc-Bob")
        carol = world.entity("Conc-Carol")
        dave = world.entity("Conc-Dave")
        world.bind_identity(bob, "conc-bob")
        world.relate("cr1", bob, carol, relation_type="knows")
        world.remember(
            "trust",
            "Bob trusts Carol",
            bob,
            category="relationship",
            value={"target_entity_id": carol},
        )
        world.rebuild()
        stop = threading.Event()
        errors: list[BaseException] = []
        observations: list[int] = []

        def reader() -> None:
            while not stop.is_set():
                try:
                    result = world.recall_as_speaker("conc-bob")
                    graph_ok = ROUTE_GRAPH in result.completed_routes
                    degraded = [d for d in result.degraded_routes if d.route == ROUTE_GRAPH]
                    # The route either completes or degrades with a stable
                    # reason — never garbage, never an exception.
                    assert graph_ok or degraded, result.degraded_routes
                    observations.append(len(result.candidates))
                except BaseException as error:
                    errors.append(error)
                    return

        with ThreadPoolExecutor(max_workers=READER_THREADS) as pool:
            futures = [pool.submit(reader) for _ in range(READER_THREADS)]
            for round_index in range(CONCURRENCY_ROUNDS):
                world.relate(
                    f"conc-r{round_index}", carol, dave, relation_type=f"knows{round_index}"
                )
                world.graph.rebuild(TENANT)
                world.profile.rebuild(TENANT)
            stop.set()
            for future in futures:
                future.result(timeout=60)
        assert not errors, errors
        assert observations

    def test_after_quiescence_state_is_deterministic(self, world: Phase8World) -> None:
        bob = world.entity("Q-Bob")
        carol = world.entity("Q-Carol")
        world.relate("qr1", bob, carol)
        world.rebuild()
        first = world.graph.rebuild(TENANT).content_checksum
        second = world.graph.rebuild(TENANT).content_checksum
        assert first == second


class TestRestore:
    def _backup_and_restore(self, world: Phase8World, tmp_path: Any) -> Any:
        backup_dir = tmp_path / "backup"
        BackupService(world.store).create_backup(backup_dir)
        target = tmp_path / "restored"
        report = restore_backup(backup_dir, target)
        assert report.check.ok, report.check.problems
        return target

    def test_restore_marks_projections_pending_rebuild(
        self, world: Phase8World, tmp_path: Any
    ) -> None:
        bob = world.entity("RB-Bob")
        carol = world.entity("RB-Carol")
        world.relate("rbr1", bob, carol)
        world.rebuild()
        target = self._backup_and_restore(world, tmp_path)
        import sqlite3

        connection = sqlite3.connect(target / "canonical.sqlite3")
        try:
            profile_state = connection.execute(
                "SELECT state FROM profile_projection_state WHERE id = 1"
            ).fetchone()[0]
            graph_state = connection.execute(
                "SELECT state FROM graph_projection_state WHERE id = 1"
            ).fetchone()[0]
            counts = connection.execute(
                "SELECT (SELECT COUNT(*) FROM graph_generations) + "
                "(SELECT COUNT(*) FROM graph_edges) + "
                "(SELECT COUNT(*) FROM profile_generations) + "
                "(SELECT COUNT(*) FROM profile_fields)"
            ).fetchone()[0]
        finally:
            connection.close()
        assert profile_state == "pending_rebuild"
        assert graph_state == "pending_rebuild"
        assert counts == 0
        # The restored database serves reads again (fresh store).
        runtime = SQLiteRuntime(
            target / "canonical.sqlite3", allowed_versions=local_allowed_versions()
        )
        store = Store(runtime, clock=world.clock)
        graph = GraphProjectionService(store, world.clock)
        profile = ProfileProjectionService(store, world.clock)
        with store.read() as tx:
            with pytest.raises(GraphDegradedError) as graph_error:
                graph.trusted_generation_in_tx(tx, tenant_id=TENANT, agent_id=world.agent)
            with pytest.raises(ProfileDegradedError) as profile_error:
                profile.trusted_generation_in_tx(tx, tenant_id=TENANT, agent_id=world.agent)
        assert graph_error.value.reason_code == "graph_rebuild_pending"
        assert profile_error.value.reason_code == "profile_rebuild_pending"
        # Rebuild restores both projections from the canonical rows.
        graph.rebuild(TENANT)
        profile.rebuild(TENANT)
        with store.read() as tx:
            pointer, _generation = graph.trusted_generation_in_tx(
                tx, tenant_id=TENANT, agent_id=world.agent
            )
            edges = tx.graph.all_edges(TENANT, pointer.generation_id)
        assert any(edge.edge_kind == "relation" for edge in edges)

    def test_restored_projection_cannot_smuggle_content(
        self, world: Phase8World, tmp_path: Any
    ) -> None:
        """The restore reset makes every surviving projection row untrusted:
        a hand-smuggled, structurally self-consistent generation whose edges
        reference resources that do not exist canonically fails CLOSED at
        verification (dangling source coverage), and its edges can never
        yield candidates (candidate construction re-reads the canonical
        row)."""
        bob = world.entity("Smuggle-Bob")
        carol = world.entity("Smuggle-Carol")
        world.relate("sr1", bob, carol)
        world.rebuild()
        target = self._backup_and_restore(world, tmp_path)
        import json as _json
        import sqlite3

        connection = sqlite3.connect(target / "canonical.sqlite3")
        try:
            agent_id = connection.execute("SELECT id FROM agents LIMIT 1").fetchone()[0]
            connection.executescript(
                f"""
                INSERT INTO graph_generations (id, tenant_id, builder_version,
                    source_watermark, tombstone_watermark, node_count, edge_count,
                    content_checksum, agent_watermarks_json, status, created_us,
                    verified_us)
                VALUES ('smuggled', 't1', 1, 0, 0, 2, 1, 'c', '{_json.dumps({agent_id: 0})}',
                    'verified', 1, 1);
                INSERT INTO graph_current (tenant_id, generation_id, switch_epoch,
                    builder_version, source_watermark, tombstone_watermark, switched_us)
                VALUES ('t1', 'smuggled', 1, 1, 0, 0, 1);
                INSERT INTO graph_nodes (tenant_id, generation_id, node_id,
                    node_kind, node_status)
                VALUES ('t1', 'smuggled', '{bob}', 'entity', 'canonical'),
                       ('t1', 'smuggled', '{carol}', 'entity', 'canonical');
                INSERT INTO graph_edges (tenant_id, generation_id, edge_id, edge_kind,
                    edge_type, source_node_id, source_node_kind, target_node_id,
                    target_node_kind, resource_type, resource_id, resource_revision,
                    agent_id, privacy_labels_json, status, confidence, importance,
                    content_hash, created_us)
                VALUES ('t1', 'smuggled', 'smuggled-edge', 'relation', 'knows',
                    '{bob}', 'entity', '{carol}', 'entity', 'relation',
                    'nonexistent-relation', 1, '{agent_id}', '[]', 'active',
                    1.0, 1.0, 'h', 1);
                UPDATE graph_projection_state SET state = 'ready' WHERE id = 1;
                """
            )
            connection.commit()
        finally:
            connection.close()
        runtime = SQLiteRuntime(
            target / "canonical.sqlite3", allowed_versions=local_allowed_versions()
        )
        store = Store(runtime, clock=world.clock)
        graph = GraphProjectionService(store, world.clock)
        # The read gate accepts the self-consistent structure, but the
        # smuggled edge references no canonical relation: verification fails
        # closed and the projection is marked pending_rebuild — the verdict
        # RETURNS (never raises into a worker rollback) and persists.
        with store.write() as tx:
            assert graph.verify_in_tx(tx, TENANT) is False
        with store.read() as tx:
            assert tx.graph.projection_state() == "pending_rebuild"
            with pytest.raises(GraphDegradedError):
                graph.trusted_generation_in_tx(tx, tenant_id=TENANT, agent_id=world.agent)


class TestHealthAndMetrics:
    def test_readiness_reports_projection_states(self, world: Phase8World) -> None:
        gauge = _gauge(world.store.runtime.database)
        health = HealthService(world.store, world.clock, gauge=gauge)
        report = health.readiness()
        # Fresh install: never_built never degrades.
        assert report.checks["profile_projection_state"] == "never_built"
        assert report.checks["graph_projection_state"] == "never_built"
        assert "profile_projection_rebuild_pending" not in report.reasons
        world.rebuild()
        report = health.readiness()
        assert report.checks["profile_projection_state"] == "ready"
        assert report.checks["graph_projection_state"] == "ready"
        with world.store.write() as tx:
            tx.graph.set_projection_state("pending_rebuild")
            tx.profile.set_projection_state("pending_rebuild")
        report = health.readiness()
        assert report.status == "degraded"
        assert "graph_projection_rebuild_pending" in report.reasons
        assert "profile_projection_rebuild_pending" in report.reasons

    def test_required_deployments_go_not_ready(self, world: Phase8World) -> None:
        gauge = _gauge(world.store.runtime.database)
        health = HealthService(
            world.store, world.clock, gauge=gauge, graph_required=True, profile_required=True
        )
        with world.store.write() as tx:
            tx.graph.set_projection_state("pending_rebuild")
        report = health.readiness()
        assert report.status == "not_ready"
        assert "graph_projection_required" in report.reasons

    def test_metrics_fire_from_production_paths(self, world: Phase8World) -> None:
        metrics = Metrics()
        graph = GraphProjectionService(world.store, world.clock, metrics=metrics)
        profile = ProfileProjectionService(world.store, world.clock, metrics=metrics)
        world.graph = graph
        world.profile = profile
        bob = world.entity("Metrics-Bob")
        carol = world.entity("Metrics-Carol")
        world.relate("mr1", bob, carol)
        graph.rebuild(TENANT)
        profile.rebuild(TENANT)
        with world.store.read() as tx:
            graph.trusted_generation_in_tx(tx, tenant_id=TENANT, agent_id=world.agent)
            profile.trusted_generation_in_tx(tx, tenant_id=TENANT, agent_id=world.agent)
        snapshot = metrics.snapshot()
        generation_gauges = {
            (g["name"], tuple(sorted(g["labels"].items())))
            for g in snapshot["gauges"]
            if g["name"] == "iris_index_generation"
        }
        assert ("iris_index_generation", (("index_kind", "graph"),)) in generation_gauges
        assert ("iris_index_generation", (("index_kind", "profile"),)) in generation_gauges
        lag_gauges = {
            (g["name"], tuple(sorted(g["labels"].items())))
            for g in snapshot["gauges"]
            if g["name"] == "iris_index_lag_revisions"
        }
        assert ("iris_index_lag_revisions", (("index_kind", "graph"),)) in lag_gauges
        assert ("iris_index_lag_revisions", (("index_kind", "profile"),)) in lag_gauges
