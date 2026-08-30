"""Observation latency gates (§30, Phase 2 quantitative baseline).

Single-observation p95 <= 30 ms and 100-record batch p95 <= 150 ms measured
on the local store with synchronous=FULL. The environment (hardware, SQLite
version, concurrency, payload, database state) is captured for the report.
"""

from __future__ import annotations

import platform
import sqlite3
import statistics
import sys
from pathlib import Path

import pytest

from iris_memory_core.application.observation import ObservationService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.storage.runtime import sqlite_runtime_version
from iris_memory_core.storage.uow import Store

SINGLE_P95_BUDGET_MS = 30.0
BATCH100_P95_BUDGET_MS = 150.0
WARMUP = 10
SAMPLES = 60


def _p95(samples_ms: list[float]) -> float:
    ordered = sorted(samples_ms)
    index = min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))
    return ordered[index]


def _record(agent_id: str, key: str, cursor: int) -> dict[str, object]:
    return {
        "agent_id": agent_id,
        "role": "user",
        "kind": "message.text",
        "idempotency_key": key,
        "occurred_us": 1_700_000_000_000_000 + cursor,
        "committed_us": 1_700_000_000_000_001 + cursor,
        "content": "x" * 512,
        "source_stream": "bench",
        "source_cursor": str(cursor),
    }


@pytest.fixture
def bench_store(database: Path) -> Store:
    from iris_memory_core.storage.migrations import MigrationRunner
    from iris_memory_core.storage.runtime import SQLiteRuntime
    from tests.conftest import local_allowed_versions

    MigrationRunner(database).migrate()
    runtime = SQLiteRuntime(database, allowed_versions=local_allowed_versions())
    return Store(runtime)


@pytest.fixture
def bench_agent(bench_store: Store) -> tuple[Store, AccessContext, str]:
    from iris_memory_core.application.provisioning import ProvisioningService

    with bench_store.write() as tx:
        tx.insert_tenant("bench", status="active")
    admin = AccessContext("bench", app_instance_id="bootstrap", admin=True)
    agent = ProvisioningService(bench_store).create_agent(admin, "Bench Agent")
    access = AccessContext(
        "bench", app_instance_id="app-1", admin=True, agent_ids=frozenset({agent.id})
    )
    return bench_store, access, agent.id


def test_single_observation_p95_under_30ms(
    bench_agent: tuple[Store, AccessContext, str],
) -> None:
    store, access, agent_id = bench_agent
    service = ObservationService(store)
    cursor = 1
    for _ in range(WARMUP):
        service.observe_batch(access, [_record(agent_id, f"warm-{cursor}", cursor)])
        cursor += 1
    samples: list[float] = []
    import time

    for _ in range(SAMPLES):
        started = time.perf_counter()
        outcome = service.observe_batch(access, [_record(agent_id, f"s-{cursor}", cursor)])
        samples.append((time.perf_counter() - started) * 1000)
        cursor += 1
        assert len(outcome.accepted_observation_ids) == 1
    p95 = _p95(samples)
    environment = _environment(store)
    print(f"\nsingle-observation p95={p95:.2f}ms median={statistics.median(samples):.2f}ms")
    print(f"environment: {environment}")
    assert p95 <= SINGLE_P95_BUDGET_MS


def test_batch100_p95_under_150ms(
    bench_agent: tuple[Store, AccessContext, str],
) -> None:
    store, access, agent_id = bench_agent
    service = ObservationService(store)
    cursor = 1
    for _ in range(WARMUP):
        service.observe_batch(
            access, [_record(agent_id, f"warm-{cursor + i}", cursor + i) for i in range(100)]
        )
        cursor += 100
    samples: list[float] = []
    import time

    for _ in range(SAMPLES):
        records = [_record(agent_id, f"b-{cursor + i}", cursor + i) for i in range(100)]
        started = time.perf_counter()
        outcome = service.observe_batch(access, records)
        samples.append((time.perf_counter() - started) * 1000)
        cursor += 100
        assert len(outcome.accepted_observation_ids) == 100
    p95 = _p95(samples)
    print(f"\nbatch-100 p95={p95:.2f}ms median={statistics.median(samples):.2f}ms")
    print(f"environment: {_batch_state(store)}")
    assert p95 <= BATCH100_P95_BUDGET_MS


def _environment(store: Store) -> str:
    return (
        f"python={sys.version.split()[0]}; sqlite={sqlite3.sqlite_version}; "
        f"platform={platform.machine()}/{platform.system()}; "
        f"concurrency=1; payload=512B content per record"
    )


def _batch_state(store: Store) -> str:
    with store.read() as tx:
        observations = tx.raw().execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        jobs = tx.raw().execute("SELECT COUNT(*) FROM outbox_jobs").fetchone()[0]
    return (
        f"sqlite={sqlite_runtime_version()}; observations={observations}; "
        f"outbox_jobs={jobs} (unsettled and completed)"
    )
