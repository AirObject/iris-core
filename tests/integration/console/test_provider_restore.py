"""Real backups invalidate provider authority and recover through a fresh build."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.application.console.provider_activation_work import ProviderActivations
from iris_memory_core.application.console.provider_probes import ProviderProbes
from iris_memory_core.application.console.security import OperatorSecurity
from iris_memory_core.application.outbox import OutboxService
from iris_memory_core.domain.errors import ConflictError
from iris_memory_core.indexing.provider_generations import ProviderGenerationRuntime
from iris_memory_core.storage.backup import (
    BackupService,
    restore_backup,
    verify_database_invariants,
)
from iris_memory_core.storage.console_operation_restore import reset_operations_for_restore
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.runtime import SQLiteRuntime
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.console.test_provider_activations import (
    accept_activation,
    claimed,
    first_generation,
    next_config,
    old_query,
)
from tests.integration.console.test_provider_activations import activation as activation_fixture
from tests.integration.console.test_provider_activations import auth as auth_fixture
from tests.integration.console.test_provider_activations import gateway as gateway_fixture
from tests.integration.console.test_provider_activations import probe as probe_fixture
from tests.integration.console.test_provider_activations import world as world_fixture
from tests.integration.console.test_provider_probes import accept as accept_probe
from tests.integration.console.test_provider_probes import claim as claim_probe
from tests.integration.console.test_provider_rollbacks import pair, rollback

auth, world, gateway, probe, activation = (
    auth_fixture,
    world_fixture,
    gateway_fixture,
    probe_fixture,
    activation_fixture,
)


@pytest.fixture
def mutable_clock() -> MutableClock:
    # Real offline restoration stamps the actual wall clock; issue credentials
    # in that same era and advance the controlled clock after the switch.
    return MutableClock(time.time_ns() // 1000)


def reopened(world: dict[str, Any], database: Path) -> dict[str, Any]:
    world["store"].clock.set(time.time_ns() // 1000)
    store = Store(
        SQLiteRuntime(database, allowed_versions=(sqlite3.sqlite_version_info,)),
        clock=world["store"].clock,
    )
    original = world["security"]
    security = OperatorSecurity(
        store,
        store.clock,
        store.ids,
        permissions=original.permissions,
        fingerprint=original.fingerprint,
        seal=original.seal,
        open_sealed=original.open_sealed,
        idempotency=IdempotencyManager(store),
    )
    root = database.parent / "vector"
    generations = ProviderGenerationRuntime(store, store.clock, world["factory"], root)
    return {
        **world,
        "store": store,
        "security": security,
        "root": root,
        "generations": generations,
        "outbox": OutboxService(store, store.clock),
        "service": ProviderProbes(security, world["factory"]),
        "activation": ProviderActivations(security, world["factory"], generations),
    }


@pytest.mark.parametrize("intent", ["probe", "activate", "rollback"])
@pytest.mark.parametrize("stage", ["queued", "leased", "prepared"])
def test_actual_restore_blocks_old_intent_and_requires_fresh_probe_and_build(
    activation: dict[str, Any], tmp_path: Path, intent: str, stage: str
) -> None:
    if intent == "rollback":
        previous, _ = pair(activation)
        operation = rollback(activation)
    else:
        previous = first_generation(activation)
        next_config(activation)
        operation = (
            accept_probe(activation, key="pending-probe")
            if intent == "probe"
            else accept_activation(activation, "pending-activation")
        )
    job = (
        (claim_probe(activation) if intent == "probe" else claimed(activation))
        if stage != "queued"
        else None
    )
    original_handler = (
        activation["service"].work if intent == "probe" else activation["activation"].work
    )
    prepared_commit = original_handler(job) if stage == "prepared" else None
    with activation["store"].read() as tx:
        history = [
            tuple(row)
            for row in tx.raw().execute(
                "SELECT * FROM provider_config_revisions ORDER BY config_id"
            )
        ]
        probes = [
            tuple(row) for row in tx.raw().execute("SELECT * FROM provider_probes ORDER BY id")
        ]
        id_map = [
            tuple(row)
            for row in tx.raw().execute("SELECT * FROM vector_id_map ORDER BY surrogate_id")
        ]
        budget = [tuple(row) for row in tx.raw().execute("SELECT * FROM provider_probe_budgets")]
    destination = tmp_path / "backup"
    BackupService(activation["store"]).create_backup(destination)
    result = restore_backup(destination, tmp_path / "restored")
    assert result.check.ok, result.check.problems
    restored = reopened(activation, tmp_path / "restored" / "canonical.sqlite3")
    with restored["store"].read() as tx:
        assert tx.vector.pointer(restored["tenant"]) is None
        assert tx.vector.projection_state() == "pending_rebuild"
        assert tx.providers.serving(restored["tenant"]) is None
        assert tx.providers.get(restored["tenant"], previous.config_id).status == "retired"
        assert list(tx.raw().execute("SELECT * FROM provider_generation_bindings")) == []
        assert [
            tuple(row)
            for row in tx.raw().execute(
                "SELECT * FROM provider_config_revisions ORDER BY config_id"
            )
        ] == history
        assert [
            tuple(row) for row in tx.raw().execute("SELECT * FROM provider_probes ORDER BY id")
        ] == probes
        assert [
            tuple(row)
            for row in tx.raw().execute("SELECT * FROM vector_id_map ORDER BY surrogate_id")
        ] == id_map
        assert [
            tuple(row) for row in tx.raw().execute("SELECT * FROM provider_probe_budgets")
        ] == budget
        assert (
            tx.raw()
            .execute(
                "SELECT COUNT(*) FROM provider_configs WHERE latest_probe_id IS NOT NULL "
                "OR current_operation_id IS NOT NULL OR status='active'"
            )
            .fetchone()[0]
            == 0
        )
        blocked = tx.console_operations.get(restored["tenant"], operation.id)
        assert blocked.status == "blocked" and blocked.blocked_reason == "restore_requires_review"
        assert blocked.current_job_id is None
    # Reconciliation is idempotent and an old queued or leased worker may not
    # call the provider, refresh a probe, or install any generation.
    reset_operations_for_restore(restored["store"].runtime.database)
    before = len(restored["gateway"]["requests"])
    if job is None:
        job = claim_probe(restored) if intent == "probe" else claimed(restored)
    handler = restored["service"].work if intent == "probe" else restored["activation"].work
    if prepared_commit is not None:
        assert (
            restored["outbox"].execute(job, lambda _: prepared_commit, owner="first") == "completed"
        )
    else:
        assert restored["outbox"].execute(job, handler, owner="first") == "completed"
    assert len(restored["gateway"]["requests"]) == before
    with restored["store"].read() as tx:
        assert tx.console_operations.get(restored["tenant"], operation.id) == blocked
        assert tx.vector.pointer(restored["tenant"]) is None
    restored["config_id"] = previous.config_id
    with pytest.raises(ConflictError):
        rollback(restored, "premature-recovery")
    restored["principal"] = restored["security"].reauth(restored["principal"], restored["token"])
    accept_probe(restored, key="fresh-recovery-probe")
    assert (
        restored["outbox"].execute(claim_probe(restored), restored["service"].work, owner="first")
        == "completed"
    )
    rollback(restored, "fresh-recovery-build")
    assert (
        restored["outbox"].execute(claimed(restored), restored["activation"].work, owner="first")
        == "completed"
    )
    with restored["store"].read() as tx:
        serving = tx.providers.serving(restored["tenant"])
        assert serving.generation_id != previous.generation_id
    assert old_query(restored, serving)
    with sqlite3.connect(restored["store"].runtime.database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert verify_database_invariants(restored["store"].runtime.database) == ()


@pytest.mark.parametrize("hot", [False, True])
def test_repository_reset_removes_serving_before_fk_generations(
    activation: dict[str, Any], hot: bool
) -> None:
    old, _ = pair(activation, hot=hot)
    with activation["store"].write() as tx:
        tx.vector.reset_projection()
        assert tx.providers.serving(activation["tenant"]) is None
        assert tx.providers.get(activation["tenant"], old.config_id).latest_probe_id is None
        assert tx.raw().execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    "corruption",
    ["pointer-space", "missing-origin", "config-status", "probe-owner", "missing-serving"],
)
def test_restore_invariants_reject_logically_inconsistent_provider_snapshot(
    activation: dict[str, Any], tmp_path: Path, corruption: str
) -> None:
    old, current = pair(activation, hot=True)
    snapshot = tmp_path / "inconsistent.sqlite3"
    with (
        sqlite3.connect(activation["store"].runtime.database) as source,
        sqlite3.connect(snapshot) as target,
    ):
        source.backup(target)
        assert verify_database_invariants(snapshot) == ()
        if corruption == "pointer-space":
            target.execute("UPDATE vector_current SET model='different-query-model'")
        elif corruption == "missing-origin":
            target.execute("DELETE FROM provider_generation_bindings")
        elif corruption == "missing-serving":
            target.execute("DELETE FROM provider_serving")
        elif corruption == "config-status":
            target.execute(
                "UPDATE provider_configs SET status='retired' WHERE id=?", (current.config_id,)
            )
        else:
            target.execute(
                "UPDATE provider_configs SET latest_probe_id=(SELECT latest_probe_id "
                "FROM provider_configs WHERE id=?) WHERE id=?",
                (old.config_id, current.config_id),
            )
        target.commit()
        target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert any("provider" in problem for problem in verify_database_invariants(snapshot))
