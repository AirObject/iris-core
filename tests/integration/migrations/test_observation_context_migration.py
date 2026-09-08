"""Schema 24 journal identity and retries survive the additive context migration."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from iris_memory_core.application.observation import ObservationService
from iris_memory_core.storage.migrations import MigrationRunner, default_migrations_path
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store
from tests.integration.cognition.test_reflection_pipeline import _world


def test_schema24_raw_identity_and_fingerprint_survive(tmp_path: Path) -> None:
    old = tmp_path / "schema24"
    old.mkdir()
    for path in default_migrations_path().glob("*.sql"):
        if int(path.name[:4]) <= 24:
            shutil.copy2(path, old / path.name)
    database = tmp_path / "canonical.sqlite3"
    MigrationRunner(database, old).migrate()
    store = Store(
        SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)),
        verify_schema_window=False,
    )
    tenant, agent, space, access = _world(store)
    record = {
        "agent_id": agent,
        "space_id": space,
        "role": "user",
        "kind": "message.text",
        "idempotency_key": "old-idem",
        "content": "old raw evidence",
        "occurred_us": 1,
        "committed_us": 1,
        "source_event_id": "platform-old",
    }
    service = ObservationService(store)
    fingerprint = service._draft(access, record).fingerprint()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO observations (id,tenant_id,agent_id,space_id,app_instance_id,"
            "source_event_id,idempotency_key,record_fingerprint,role,kind,content,effect_state,"
            "occurred_us,committed_us,created_us) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "old",
                tenant,
                agent,
                space,
                access.app_instance_id,
                "platform-old",
                "old-idem",
                fingerprint,
                "user",
                "message.text",
                "old raw evidence",
                "committed",
                1,
                1,
                1,
            ),
        )
    with store.write() as tx:
        tx.advance_watermark(tenant, agent, [("observation", "old", 1)])
    assert [m.version for m in MigrationRunner(database).migrate()] == [25]
    store = Store(store.runtime)
    with store.read() as tx:
        restored = tx.observations.get("old")
        assert restored.context_kind == "interaction"
        assert restored.source_thread_id is None
        assert restored.content == "old raw evidence"
        assert tx.reflection.observation_fingerprint("old") == fingerprint
    retried = ObservationService(store).observe_batch(access, [record])
    assert retried.duplicate_observation_ids == ("old",)
    # Same platform event on a different ingestion path still does not duplicate evidence.
    retried = ObservationService(store).observe_batch(
        access, [{**record, "idempotency_key": "new-key", "context_kind": "background"}]
    )
    assert retried.duplicate_observation_ids == ("old",)
