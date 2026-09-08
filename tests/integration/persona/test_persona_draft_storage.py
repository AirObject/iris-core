"""Independent draft revisions, irreversible closure and publication consistency."""

from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.application.persona import PERSONA_MANAGE_CAPABILITY, PersonaService
from iris_memory_core.application.provisioning import ProvisioningService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    InvalidTransitionError,
    NotFoundError,
    RevisionMismatchError,
)
from iris_memory_core.storage.migrations import MigrationRunner
from iris_memory_core.storage.persona_draft import PersonaDraftRepository
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store
from tests.migration_support import migrate_through


@pytest.fixture
def draft_world(tmp_path: Path) -> dict[str, Any]:
    database = tmp_path / "canonical.sqlite3"
    MigrationRunner(database).migrate()
    store = Store(
        SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)),
    )
    provisioning = ProvisioningService(store)
    provisioning.create_tenant("draft-tenant")
    access = AccessContext(
        tenant_id="draft-tenant",
        app_instance_id="draft-test",
        admin=True,
        capabilities=frozenset({PERSONA_MANAGE_CAPABILITY}),
    )
    agent = provisioning.create_agent(access, "Draft seed")
    return {"store": store, "agent": agent.id, "access": access}


def repository(world: Any, tx: Any) -> PersonaDraftRepository:
    return PersonaDraftRepository(tx.raw(), world["store"].clock, world["store"].ids)


def arguments(world: Any) -> dict[str, Any]:
    return dict(
        tenant_id="draft-tenant",
        agent_id=world["agent"],
        base_revision=1,
        policy_revision=1,
        fields={"core": {"name": "Iris"}, "traits": {"style": "warm"}, "narrative": {}},
        source_refs=[],
        actor="console:draft-test",
    )


def test_drafts_do_not_consume_publication_revisions_and_discard_scrubs_body(
    draft_world: Any,
) -> None:
    world = draft_world
    with world["store"].write() as tx:
        repo = repository(world, tx)
        drafts = [repo.create(**arguments(world)) for _ in range(3)]
        assert tx.personas.current(world["agent"]).revision == 1
        updated = repo.update(**arguments(world), draft_id=drafts[0].id, expected_revision=1)
        assert updated.revision == 2
        with pytest.raises(RevisionMismatchError):
            repo.discard(
                tenant_id="draft-tenant",
                agent_id=world["agent"],
                draft_id=updated.id,
                expected_revision=1,
                actor="console:draft-test",
            )
        discarded = repo.discard(
            tenant_id="draft-tenant",
            agent_id=world["agent"],
            draft_id=updated.id,
            expected_revision=2,
            actor="console:draft-test",
        )
        assert discarded.revision == 3 and discarded.status == "discarded"
        assert discarded.fields_json is None and discarded.source_refs_json is None
        assert (
            tx.raw()
            .execute("SELECT revision FROM persona_draft_discards WHERE draft_id=?", (updated.id,))
            .fetchone()[0]
            == 3
        )
        with pytest.raises(InvalidTransitionError):
            repo.update(**arguments(world), draft_id=updated.id, expected_revision=3)
        with pytest.raises(NotFoundError):
            repo.get("another-tenant", world["agent"], drafts[1].id)
        assert tx.personas.current(world["agent"]).revision == 1


def test_draft_only_closes_as_published_after_matching_real_publication(draft_world: Any) -> None:
    world = draft_world
    with world["store"].write() as tx:
        draft = repository(world, tx).create(**arguments(world))
        with pytest.raises(InvalidTransitionError):
            repository(world, tx).mark_published(
                tenant_id="draft-tenant",
                agent_id=world["agent"],
                draft_id=draft.id,
                expected_revision=1,
                published_revision_id=tx.personas.current(world["agent"]).id,
                actor="console:draft-test",
            )
    persona = PersonaService(world["store"], world["store"].clock).publish_revision(
        world["access"],
        world["agent"],
        expected_revision=1,
        core={"name": "Iris"},
        traits={"style": "warm"},
        narrative={},
        reason="operator_request",
    )
    with world["store"].write() as tx:
        published = repository(world, tx).mark_published(
            tenant_id="draft-tenant",
            agent_id=world["agent"],
            draft_id=draft.id,
            expected_revision=1,
            published_revision_id=persona.id,
            actor="console:draft-test",
        )
        assert published.status == "published" and published.revision == 2
        assert published.published_revision_id == tx.personas.current(world["agent"]).id
        with pytest.raises(InvalidTransitionError):
            repository(world, tx).discard(
                tenant_id="draft-tenant",
                agent_id=world["agent"],
                draft_id=draft.id,
                expected_revision=2,
                actor="console:draft-test",
            )
        assert tx.raw().execute("SELECT COUNT(*) FROM persona_draft_discards").fetchone()[0] == 0


def test_upgrade_from_schema22_adds_empty_draft_tables(tmp_path: Path) -> None:
    database = tmp_path / "prior.sqlite3"
    migrate_through(database, 22)
    from iris_memory_core.storage.backup import BackupService

    old_store = Store(
        SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)),
        verify_schema_window=False,
    )
    backup = BackupService(old_store)
    snapshot = tmp_path / "before-statistics"
    backup.create_backup(snapshot)
    assert backup.verify_backup(snapshot).ok
    runner = MigrationRunner(database)
    assert [item.version for item in runner.migrate(allow_offline=True, backup_performed=True)] == [
        23,
        24,
    ]
    assert runner.migrate() == ()


def test_late_discard_journal_failure_rolls_back_scrub(draft_world: Any) -> None:
    import sqlite3

    world = draft_world
    with world["store"].write() as tx:
        draft = repository(world, tx).create(**arguments(world))
        tx.raw().execute(
            "CREATE TRIGGER fail_draft_journal BEFORE INSERT ON persona_draft_discards "
            "BEGIN SELECT RAISE(ABORT,'journal unavailable'); END"
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="journal unavailable"),
        world["store"].write() as tx,
    ):
        repository(world, tx).discard(
            tenant_id="draft-tenant",
            agent_id=world["agent"],
            draft_id=draft.id,
            expected_revision=1,
            actor="console:draft-test",
        )
    with world["store"].read() as tx:
        assert repository(world, tx).get("draft-tenant", world["agent"], draft.id) == draft


def test_concurrent_draft_updates_have_exactly_one_revision_winner(draft_world: Any) -> None:
    from concurrent.futures import ThreadPoolExecutor

    world = draft_world
    with world["store"].write() as tx:
        draft = repository(world, tx).create(**arguments(world))

    def update(number: int) -> str:
        try:
            with world["store"].write() as tx:
                values = arguments(world)
                values["fields"]["traits"] = {"style": f"draft {number}"}
                repository(world, tx).update(**values, draft_id=draft.id, expected_revision=1)
            return "saved"
        except RevisionMismatchError:
            return "stale"

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(update, range(8)))
    assert outcomes.count("saved") == 1 and outcomes.count("stale") == 7
    with world["store"].read() as tx:
        assert repository(world, tx).get("draft-tenant", world["agent"], draft.id).revision == 2
        assert tx.personas.current(world["agent"]).revision == 1


@pytest.mark.parametrize("predecessor", range(1, 24))
def test_all_historical_migration_prefixes_reach_schema24_without_reordering(
    tmp_path: Path, predecessor: int
) -> None:
    import sqlite3
    from contextlib import closing

    database = tmp_path / "historical.sqlite3"
    migrate_through(database, predecessor)
    with closing(sqlite3.connect(database)) as old:
        checksums = old.execute(
            "SELECT version,checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        # Older prefixes cross the existing offline migration and need an actual backup.
        with closing(sqlite3.connect(tmp_path / "before.sqlite3")) as backup:
            old.backup(backup)
            assert backup.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    applied = MigrationRunner(database).migrate(allow_offline=True, backup_performed=True)
    assert [item.version for item in applied] == list(range(predecessor + 1, 25))
    with closing(sqlite3.connect(database)) as upgraded:
        assert (
            upgraded.execute(
                "SELECT version,checksum FROM schema_migrations WHERE version<=? ORDER BY version",
                (predecessor,),
            ).fetchall()
            == checksums
        )
        assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []
        assert upgraded.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert upgraded.execute("SELECT COUNT(*) FROM persona_drafts").fetchone()[0] == 0
    assert MigrationRunner(database).migrate() == ()


def test_restored_tombstone_identity_cannot_be_reused_even_without_draft_row(
    draft_world: Any,
) -> None:
    from types import SimpleNamespace

    world = draft_world
    with world["store"].write() as tx:
        tx.record_tombstone(
            tenant_id="draft-tenant",
            resource_type="persona_draft",
            resource_id="retained-deleted-id",
            deleted_by="restore",
            reason_code="operator_request",
        )
        repo = PersonaDraftRepository(
            tx.raw(), world["store"].clock, SimpleNamespace(new=lambda: "retained-deleted-id")
        )
        with pytest.raises(InvalidTransitionError, match="cannot be reused"):
            repo.create(**arguments(world))
        assert tx.raw().execute("SELECT COUNT(*) FROM persona_drafts").fetchone()[0] == 0
