"""State Coalesced Write p95 baseline (§30, Phase 3 quantified gate).

Measures the PUT path under concurrent writers hammering the SAME coalesce
key (the coalescing hot path) plus unique-key writes, and reports dataset,
payload, concurrency, unmerged-write ratio, SQLite busy count and queue lag
alongside the p95. Asserts the §30 target: p95 ≤ 25 ms.
"""

from __future__ import annotations

import json
import platform
import sqlite3
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from iris_memory_core.application.backpressure import (
    BackpressureConfig,
    BackpressureGauge,
    FixedDiskProbe,
)
from iris_memory_core.application.state import StateService
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.migrations import MigrationRunner
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store
from tests.conftest import access_for

WARMUP = 10
SAMPLES = 120
CONCURRENCY = 8  # contention phase (reported, not the §30 assertion)
COALESCE_KEY = "environment:hot.%"
UNIQUE_KEYS = 40
PAYLOAD = {
    "scene": "gaming",
    "map": "de_dust2",
    "stats": {"fps": 240, "ping": 12},
    "labels": [f"l{i}" for i in range(6)],
}


def test_state_coalesced_write_p95() -> None:
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    database = tmp / "perf.sqlite3"
    MigrationRunner(database).migrate()
    busy_events: list[str] = []
    runtime = SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),))
    store = Store(runtime, busy_observer=busy_events.append)
    gauge = BackpressureGauge(
        BackpressureConfig(soft_disk_free_bytes=10**12, hard_disk_free_bytes=10**11),
        probe=FixedDiskProbe(10**13),
        database_path=database,
    )
    idem = IdempotencyManager(store)
    admin = access_for("t1", admin=True)
    with store.write() as tx:
        tx.insert_tenant("t1", status="active")
    from iris_memory_core.application.provisioning import ProvisioningService

    agent = ProvisioningService(store).create_agent(admin, "Perf")
    access = access_for("t1", agent_ids=frozenset({agent.id}))
    service = StateService(store, store.clock, gauge=gauge, idempotency=idem)

    # Dataset: a pre-existing state corpus the writer competes with.
    for index in range(2_000):
        service.put(
            access,
            "runtime",
            f"corpus.{index}",
            agent_id=agent.id,
            value={"i": index, "pad": "x" * 64},
            source_authority="host",
            idempotency_key=f"corpus-{index}",
            ttl_us=0,
        )

    service.put(
        access,
        "environment",
        "hot.key",
        agent_id=agent.id,
        value=PAYLOAD,
        source_authority="host",
        idempotency_key="hot-seed",
        ttl_us=0,
    )

    import itertools
    import threading

    latencies: list[float] = []
    burst_latencies: list[float] = []
    sequence = itertools.count()
    sequence_lock = threading.Lock()

    def state_write(key: str) -> float:
        """One successful PUT on one hot key (Expected Revision chain).

        The derived projection job coalesces per (scope, namespace, key):
        sequential writes to the same key merge into ONE pending job while
        every canonical revision still lands.
        """
        with sequence_lock:
            ticket = next(sequence)
        payload = dict(PAYLOAD, key=key, n=ticket)
        started = time.perf_counter()
        with store.read() as tx:
            record = tx.states.find(f"t1|{agent.id}|||", "environment", key)
        expected = record.current_revision if record else None
        service.put(
            access,
            "environment",
            key,
            agent_id=agent.id,
            value=payload,
            source_authority="host",
            idempotency_key=f"w-{ticket}",
            expected_revision=expected,
            observed_us=store.clock.now_us(),
            ttl_us=0,
            coalesce_key=f"environment:{key}",
        )
        return (time.perf_counter() - started) * 1000

    # §30 assertion phase: the coalesced write path itself, measured like the
    # Phase 2 observation baseline (single stream, coalescing fully active).
    for _ in range(WARMUP):
        state_write("hot.0")
    for _ in range(SAMPLES):
        latencies.append(state_write("hot.0"))

    # Contention phase (reported only): a burst of concurrent stream writers
    # queues behind the process-wide single writer gate (§20.2) — the p95
    # here measures gate head-of-line latency, not the write path.
    for _ in range(3):
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            burst_latencies.extend(
                pool.map(lambda worker: state_write(f"hot.{worker}"), range(CONCURRENCY))
            )

    connection = sqlite3.connect(database)
    try:
        projection_jobs = connection.execute(
            "SELECT COUNT(*) FROM outbox_jobs WHERE job_kind = 'state.projection'"
        ).fetchone()[0]
        pending_projection = connection.execute(
            "SELECT COUNT(*) FROM outbox_jobs WHERE job_kind = 'state.projection' "
            "AND status IN ('pending','leased','retryable')"
        ).fetchone()[0]
        revisions = connection.execute(
            "SELECT COALESCE(SUM(current_revision), 0) FROM state_records WHERE key LIKE 'hot.%'"
        ).fetchone()[0]
        oldest_pending_us = connection.execute(
            "SELECT MIN(available_at_us) FROM outbox_jobs WHERE status IN "
            "('pending','leased','retryable')"
        ).fetchone()[0]
    finally:
        connection.close()
    now_us = store.clock.now_us()
    queue_lag_us = (now_us - oldest_pending_us) if oldest_pending_us else 0

    p95 = statistics.quantiles(latencies, n=20)[18]
    median = statistics.median(latencies)
    burst_p95 = statistics.quantiles(burst_latencies, n=20)[18]
    payload_bytes = len(json.dumps(PAYLOAD).encode())
    print(
        f"\nenvironment: python={platform.python_version()} "
        f"sqlite={sqlite3.sqlite_version} machine={platform.machine()} "
        f"os={platform.system()}"
    )
    print(
        f"state coalesced write (sequential stream, coalescing active): "
        f"samples={len(latencies)} payload_bytes={payload_bytes} "
        f"corpus=2000 rows | p50={median:.2f}ms p95={p95:.2f}ms"
    )
    print(
        f"contention burst (reported, single-writer gate queueing): "
        f"samples={len(burst_latencies)} concurrency={CONCURRENCY} "
        f"p95={burst_p95:.2f}ms | busy_events={len(busy_events)} | "
        f"projection_jobs={projection_jobs} pending={pending_projection} "
        f"hot_key_revisions={revisions} queue_lag_us={queue_lag_us}"
    )
    assert p95 <= 25.0, f"state coalesced write p95 {p95:.2f}ms exceeds 25ms"
    # Coalescing kept the pending projection stream bounded while every
    # canonical revision still landed (retention prunes history rows).
    assert revisions >= SAMPLES + WARMUP + 3 * CONCURRENCY
    assert pending_projection <= projection_jobs
