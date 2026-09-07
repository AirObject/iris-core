"""Online backup, isolated restore, smoke read/write and tamper rejection (§21)."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from iris_memory_core.application.identity import IdentityService
from iris_memory_core.application.provisioning import ProvisioningService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.identity import EntityKind
from iris_memory_core.storage.backup import BackupService
from iris_memory_core.storage.uow import Store
from tests.conftest import access_for

ROUNDS = 3


def seed_world(
    store: Store,
    provisioning: ProvisioningService,
    identities: IdentityService,
    admin_access: AccessContext,
) -> None:
    agent = provisioning.create_agent(admin_access, f"Iris-{store.clock.now_us()}")
    group = provisioning.create_space_group(admin_access, "community", reason="ops")
    scoped = access_for(
        admin_access.tenant_id,
        agent_ids=frozenset({agent.id}),
        space_group_ids=frozenset({group.id}),
        admin=True,
    )
    provisioning.create_space(
        scoped, "chat_group", agent_id=agent.id, space_group_id=group.id, reason="ops: bind"
    )
    entity = identities.create_entity(admin_access, EntityKind.PERSON, display_name="u")
    identities.register_external_identity(
        admin_access, "qq-onebot11", "bot", "42", entity_id=entity.id
    )


def test_backup_restore_smoke_repeats_three_times(
    store: Store,
    provisioning: ProvisioningService,
    identities: IdentityService,
    admin_access: AccessContext,
    tmp_path: Path,
) -> None:
    seed_world(store, provisioning, identities, admin_access)
    backups = BackupService(store)

    for round_index in range(ROUNDS):
        backup_dir = tmp_path / f"backup-{round_index}"
        report = backups.create_backup(backup_dir)
        assert report.schema_version >= 2
        assert report.agent_watermarks  # manifest captured live watermarks
        assert report.tombstone_watermark >= 0

        check = backups.verify_backup(backup_dir)
        assert check.ok, check.problems

        target = tmp_path / f"restored-{round_index}"
        restore = backups.restore_backup(backup_dir, target)
        assert restore.check.ok, restore.check.problems
        assert (target / "canonical.sqlite3").is_file()

        smoke = backups.smoke_restored(target / "canonical.sqlite3")
        assert smoke.read_watermarks >= 1
        assert smoke.wrote_probe_event is True

        # Data survived the round trip.
        measurement = backups.measure_recovery(backup_dir, tmp_path / f"cycle-{round_index}")
        assert measurement.restore_ms >= 0 and measurement.smoke_ms >= 0
        assert "RPO" in measurement.rpo_note


def test_tampered_backup_is_rejected_before_restore(
    store: Store,
    provisioning: ProvisioningService,
    identities: IdentityService,
    admin_access: AccessContext,
    tmp_path: Path,
) -> None:
    seed_world(store, provisioning, identities, admin_access)
    backups = BackupService(store)
    backup_dir = tmp_path / "backup"
    backups.create_backup(backup_dir)

    canonical = backup_dir / "canonical.sqlite3"
    canonical.write_bytes(canonical.read_bytes() + b"\x00tampered")
    check = backups.verify_backup(backup_dir)
    assert not check.ok
    restore = backups.restore_backup(backup_dir, tmp_path / "target")
    assert not restore.check.ok
    assert not (tmp_path / "target" / "canonical.sqlite3").is_file()


def test_checksum_tampering_and_coverage_gaps_are_detected(
    store: Store,
    provisioning: ProvisioningService,
    identities: IdentityService,
    admin_access: AccessContext,
    tmp_path: Path,
) -> None:
    seed_world(store, provisioning, identities, admin_access)
    backups = BackupService(store)
    backup_dir = tmp_path / "backup"
    backups.create_backup(backup_dir)

    # Fingerprint tampering is caught (it is covered by checksums.txt).
    original = (backup_dir / "config-fingerprint.json").read_text(encoding="utf-8")
    (backup_dir / "config-fingerprint.json").write_text(
        original.replace('"config_version": 1', '"config_version": 2'), encoding="utf-8"
    )
    check = backups.verify_backup(backup_dir)
    assert not check.ok and any("config-fingerprint" in p for p in check.problems)

    # Removing coverage (a checksums.txt without the manifest) is caught.
    backups.create_backup(tmp_path / "backup2")
    (tmp_path / "backup2" / "checksums.txt").write_text(
        "0000000000000000000000000000000000000000000000000000000000000000  canonical.sqlite3\n",
        encoding="utf-8",
    )
    check2 = backups.verify_backup(tmp_path / "backup2")
    assert not check2.ok and any("not covered" in p for p in check2.problems)


def test_tampered_manifest_watermarks_are_reconciled_against_snapshot(
    store: Store,
    provisioning: ProvisioningService,
    identities: IdentityService,
    admin_access: AccessContext,
    tmp_path: Path,
) -> None:
    seed_world(store, provisioning, identities, admin_access)
    backups = BackupService(store)
    backup_dir = tmp_path / "backup"
    backups.create_backup(backup_dir)

    from iris_memory_core.storage.backup import (
        CANONICAL_NAME,
        CHECKSUMS_NAME,
        FINGERPRINT_NAME,
        MANIFEST_NAME,
        _sha256_file,
    )

    manifest_path = backup_dir / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    forged = {key: value + 5 for key, value in manifest["agent_watermarks"].items()}
    manifest["agent_watermarks"] = forged
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = [
        f"{_sha256_file(backup_dir / name)}  {name}"
        for name in (CANONICAL_NAME, MANIFEST_NAME, FINGERPRINT_NAME)
    ]
    (backup_dir / CHECKSUMS_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")

    check = backups.verify_backup(backup_dir)
    assert not check.ok and any("watermarks" in problem for problem in check.problems)


def test_backup_catalog_and_audit_are_recorded(
    store: Store,
    provisioning: ProvisioningService,
    identities: IdentityService,
    admin_access: AccessContext,
    tmp_path: Path,
) -> None:
    seed_world(store, provisioning, identities, admin_access)
    report = BackupService(store).create_backup(tmp_path / "catalogued")
    with store.read() as tx:
        rows = (
            tx.raw()
            .execute("SELECT status FROM backup_catalog WHERE id = ?", (report.backup_id,))
            .fetchall()
        )
        audits = (
            tx.raw()
            .execute("SELECT COUNT(*) FROM audit_events WHERE action = 'backup.created'")
            .fetchone()
        )
    assert [row[0] for row in rows] == ["valid"]
    assert int(audits[0]) >= 1


def test_restore_rejects_database_with_broken_invariants(
    store: Store, provisioning: ProvisioningService, admin_access: AccessContext, tmp_path: Path
) -> None:
    """A canonical file that passes checksums but violates pointer invariants fails restore."""
    provisioning.create_agent(admin_access, "Iris")
    backups = BackupService(store)
    backup_dir = tmp_path / "backup"
    report = backups.create_backup(backup_dir)
    # Simulate a pointer-invariant break that checksums cannot see: point the
    # agent at a persona revision that does not exist.
    import sqlite3

    raw = sqlite3.connect(backup_dir / "canonical.sqlite3")
    try:
        raw.execute("UPDATE agents SET persona_current_revision_id = 'missing'")
        raw.commit()
        raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        raw.close()
    # Recompute every checksum so only the invariant check can catch it.
    from iris_memory_core.storage.backup import (
        CANONICAL_NAME,
        CHECKSUMS_NAME,
        FINGERPRINT_NAME,
        MANIFEST_NAME,
        _sha256_file,
    )

    manifest_path = backup_dir / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][CANONICAL_NAME] = _sha256_file(backup_dir / CANONICAL_NAME)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = [
        f"{_sha256_file(backup_dir / name)}  {name}"
        for name in (CANONICAL_NAME, MANIFEST_NAME, FINGERPRINT_NAME)
    ]
    (backup_dir / CHECKSUMS_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
    del report
    check = backups.verify_backup(backup_dir)
    assert check.ok  # hashes agree...
    restore = backups.restore_backup(backup_dir, tmp_path / "target")
    assert not restore.check.ok  # ...but the invariant check refuses the switch


def _phase0_database(tmp_path: Path) -> Path:
    """A genuine Phase 0 database: only the published 0001 is applied."""
    import shutil

    from iris_memory_core.storage.migrations import MigrationRunner, default_migrations_path

    legacy = tmp_path / "legacy-migrations"
    legacy.mkdir()
    source = default_migrations_path() / "0001_phase0_metadata.sql"
    shutil.copy(source, legacy / source.name)
    database = tmp_path / "phase0.sqlite3"
    MigrationRunner(database, legacy).migrate()
    return database


def test_phase0_pre_migration_backup_verifies_and_restores(tmp_path: Path) -> None:
    """verify_backup used to crash on Phase 0 snapshots (no resource_tombstones)."""
    from iris_memory_core.storage.backup import (
        create_standalone_backup,
        restore_backup,
        verify_backup,
    )

    database = _phase0_database(tmp_path)
    backup_dir = tmp_path / "backup"
    create_standalone_backup(database, backup_dir)
    check = verify_backup(backup_dir)
    assert check.ok, check.problems
    report = restore_backup(backup_dir, tmp_path / "target")
    assert report.check.ok, report.check.problems
    assert (tmp_path / "target" / "canonical.sqlite3").is_file()


def _recompute_checksums(backup_dir: Path) -> None:
    """Rebuild manifest.json + checksums.txt as an attacker would."""
    import hashlib

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    names = ["canonical.sqlite3", "manifest.json", "config-fingerprint.json", "authenticity.tag"]
    names = [n for n in names if (backup_dir / n).is_file()]
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"]["canonical.sqlite3"] = digest(backup_dir / "canonical.sqlite3")
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines = [f"{digest(backup_dir / name)}  {name}" for name in names]
    (backup_dir / "checksums.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_signed_backup_detects_recomputed_checksum_tampering(
    store: Store,
    provisioning: ProvisioningService,
    identities: IdentityService,
    admin_access: AccessContext,
    tmp_path: Path,
) -> None:
    """The reviewer's adversarial repro: rewrite the snapshot AND recompute
    manifest/checksums. Only the external-key HMAC catches it."""
    import sqlite3

    seed_world(store, provisioning, identities, admin_access)
    key = b"unit-test-backup-key-0123456789ab"
    backups = BackupService(store)
    backup_dir = tmp_path / "backup"
    backups.create_backup(backup_dir, signing_key=key)
    assert backups.verify_backup(backup_dir, signing_key=key).ok

    connection = sqlite3.connect(backup_dir / "canonical.sqlite3")
    try:
        connection.execute("UPDATE agents SET display_name = 'attacker-controlled'")
        connection.commit()
    finally:
        connection.close()
    _recompute_checksums(backup_dir)

    # Without the key: recomputed checksums match — corruption detection only.
    plain = backups.verify_backup(backup_dir)
    assert plain.ok
    # With the key: the payload digests changed, the tag cannot be recomputed.
    signed = backups.verify_backup(backup_dir, signing_key=key)
    assert not signed.ok
    assert any("authenticity" in problem for problem in signed.problems)
    report = backups.restore_backup(backup_dir, tmp_path / "target", signing_key=key)
    assert not report.check.ok
    # A different (wrong) key is rejected too.
    wrong = backups.verify_backup(backup_dir, signing_key=b"wrong-key-0123456789abcdef")
    assert not wrong.ok


def test_keyed_verification_requires_an_authenticity_tag(
    store: Store,
    provisioning: ProvisioningService,
    identities: IdentityService,
    admin_access: AccessContext,
    tmp_path: Path,
) -> None:
    seed_world(store, provisioning, identities, admin_access)
    backups = BackupService(store)
    backup_dir = tmp_path / "backup"
    backups.create_backup(backup_dir)  # unsigned
    check = backups.verify_backup(backup_dir, signing_key=b"unit-test-backup-key-0123456789ab")
    assert not check.ok
    assert any("no authenticity tag" in problem for problem in check.problems)


def _marker_of(database: Path) -> str:
    import sqlite3

    connection = sqlite3.connect(database)
    try:
        row = connection.execute("SELECT display_name FROM agents LIMIT 1").fetchone()
        return str(row[0]) if row is not None else ""
    finally:
        connection.close()


def test_interrupted_restore_switch_is_recovered_deterministically(
    store: Store,
    provisioning: ProvisioningService,
    identities: IdentityService,
    admin_access: AccessContext,
    tmp_path: Path,
) -> None:
    """Kill-9 between the two renames must never leave the service path without
    a database; recover_pending_switch finishes or rolls back exactly."""
    import sqlite3

    seed_world(store, provisioning, identities, admin_access)

    from iris_memory_core.storage.backup import (
        JOURNAL_SUFFIX,
        _write_file_durable,
        recover_pending_switch,
    )

    def make_backup(destination: Path, marker: str) -> Path:
        backups = BackupService(store)
        backups.create_backup(destination)
        connection = sqlite3.connect(destination / "canonical.sqlite3")
        try:
            connection.execute("UPDATE agents SET display_name = ?", (marker,))
            connection.commit()
        finally:
            connection.close()
        _recompute_checksums(destination)
        return destination

    def make_live(target: Path, marker: str) -> None:
        target.mkdir(parents=True)
        connection = sqlite3.connect(target / "canonical.sqlite3")
        try:
            connection.execute("CREATE TABLE agents (display_name TEXT)")
            connection.execute("INSERT INTO agents VALUES (?)", (marker,))
            connection.commit()
        finally:
            connection.close()

    parent = tmp_path / "srv"
    parent.mkdir()
    target = parent / "data"

    # (a) crash between rename1 and rename2: old data aside, staged restore ready.
    backup = make_backup(tmp_path / "b1", "restored-marker")
    make_live(target, "live-marker")
    staging = parent / "data.restoring"
    staging.mkdir()
    (staging / "canonical.sqlite3").write_bytes((backup / "canonical.sqlite3").read_bytes())
    aside = parent / "data.previous-1"
    target.rename(aside)
    _write_file_durable(
        parent / f"data{JOURNAL_SUFFIX}", json.dumps({"staging": staging.name, "aside": aside.name})
    )
    assert recover_pending_switch(target) == "completed"
    assert _marker_of(target / "canonical.sqlite3") == "restored-marker"
    assert not aside.exists() and not staging.exists()
    assert not (parent / f"data{JOURNAL_SUFFIX}").exists()

    # (b) crash after the journal write, before rename1: target intact.
    make_backup(tmp_path / "b2", "never-applied")
    staging = parent / "data.restoring"
    staging.mkdir()
    (staging / "canonical.sqlite3").write_bytes((backup / "canonical.sqlite3").read_bytes())
    _write_file_durable(
        parent / f"data{JOURNAL_SUFFIX}", json.dumps({"staging": staging.name, "aside": None})
    )
    assert recover_pending_switch(target) == "aborted"
    assert _marker_of(target / "canonical.sqlite3") == "restored-marker"  # untouched
    assert not staging.exists()

    # (c) crash between rename2 and journal removal: switch already complete.
    aside = parent / "data.previous-2"
    (parent / "data.previous-2").mkdir()
    _write_file_durable(
        parent / f"data{JOURNAL_SUFFIX}",
        json.dumps({"staging": "data.restoring", "aside": aside.name}),
    )
    assert recover_pending_switch(target) == "completed"
    assert not aside.exists()

    # (d) crash between the renames with the staged copy gone: roll back to aside.
    aside = parent / "data.previous-3"
    target.rename(aside)
    _write_file_durable(
        parent / f"data{JOURNAL_SUFFIX}",
        json.dumps({"staging": "data.restoring", "aside": aside.name}),
    )
    assert recover_pending_switch(target) == "rolled_back"
    assert _marker_of(target / "canonical.sqlite3") == "restored-marker"
    assert target.exists() and not aside.exists()

    # No journal, nothing pending.
    assert recover_pending_switch(target) == "none"


def test_restore_cleans_pending_state_before_switching(
    store: Store,
    provisioning: ProvisioningService,
    identities: IdentityService,
    admin_access: AccessContext,
    tmp_path: Path,
) -> None:
    """A leftover pending switch is resolved before a fresh restore starts."""
    from iris_memory_core.storage.backup import JOURNAL_SUFFIX, _write_file_durable

    seed_world(store, provisioning, identities, admin_access)
    backups = BackupService(store)
    backup_dir = tmp_path / "backup"
    backups.create_backup(backup_dir)
    parent = tmp_path / "srv"
    parent.mkdir()
    target = parent / "data"
    target.mkdir()
    (target / "canonical.sqlite3").write_bytes(b"")
    _write_file_durable(
        parent / f"data{JOURNAL_SUFFIX}", json.dumps({"staging": "data.restoring", "aside": None})
    )
    report = backups.restore_backup(backup_dir, target)
    assert report.check.ok, report.check.problems
    assert not (parent / f"data{JOURNAL_SUFFIX}").exists()
    # The empty previous target was replaced by the verified backup payload,
    # forward-migrated to the current schema with the FTS projection marked
    # pending rebuild (ADR-0014 §9) — never a byte-identical projection.
    import sqlite3 as _sqlite3

    connection = _sqlite3.connect(target / "canonical.sqlite3")
    try:
        schema = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
        fts_state = connection.execute(
            "SELECT state FROM fts_projection_state WHERE id = 1"
        ).fetchone()[0]
    finally:
        connection.close()
    assert schema == 21
    assert fts_state == "pending_rebuild"


def test_persona_pointer_invariants_require_published_same_agent_revision(
    store: Store, provisioning: ProvisioningService, admin_access: AccessContext, tmp_path: Path
) -> None:
    import shutil
    import sqlite3

    from iris_memory_core.storage.backup import verify_database_invariants

    provisioning.create_agent(admin_access, "Iris")

    def crafted(mutate: Callable[[sqlite3.Connection], None]) -> Path:
        path = tmp_path / f"crafted-{store.clock.now_us()}.sqlite3"
        shutil.copy(store.runtime.database, path)
        connection = sqlite3.connect(path)
        try:
            mutate(connection)
            connection.commit()
        finally:
            connection.close()
        return path

    def set_pointer_status(connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE persona_revisions SET status = 'draft' "
            "WHERE id = (SELECT persona_current_revision_id FROM agents LIMIT 1)"
        )

    def point_at_other_agent(connection: sqlite3.Connection) -> None:
        other = connection.execute(
            "INSERT INTO persona_revisions (id, tenant_id, agent_id, revision, core, traits, "
            "narrative, content_hash, status, source, created_us) VALUES "
            "('draft-x', 'tenant-zz', 'agent-zz', 9, '', '', '', 'h', 'draft', 'manual', 0)"
        )
        del other
        connection.execute(
            "UPDATE agents SET persona_current_revision_id = 'draft-x' "
            "WHERE id = (SELECT id FROM agents LIMIT 1)"
        )

    problems = verify_database_invariants(crafted(set_pointer_status))
    assert any("published" in problem for problem in problems)

    problems = verify_database_invariants(crafted(point_at_other_agent))
    assert any("persona current pointer" in problem for problem in problems)

    assert verify_database_invariants(store.runtime.database) == ()


def test_restore_verifies_the_bytes_it_switches(
    store: Store,
    provisioning: ProvisioningService,
    identities: IdentityService,
    admin_access: AccessContext,
    tmp_path: Path,
) -> None:
    """The reviewer's TOCTOU repro: mutate the backup after verification began.

    Verification runs on the staging copy — the exact bytes that switch in —
    so a mutation of the source directory mid-restore can never inject
    content: the restore either refuses (staged bytes fail) or restores the
    pre-mutation bytes. Here the attack lands in the source dir after the
    copy; the switched-in database still carries the original marker.
    """
    import sqlite3
    from unittest import mock

    from iris_memory_core.storage import backup as backup_module

    seed_world(store, provisioning, identities, admin_access)
    key = b"unit-test-backup-key-0123456789ab"
    backups = BackupService(store)
    backup_dir = tmp_path / "backup"
    backups.create_backup(backup_dir, signing_key=key)

    real_verify = backup_module.verify_backup

    def attacking_verify(directory: Path, **_kwargs: object) -> object:
        # The attacker rewrites the SOURCE directory while the restore is
        # between its copy and verification of the staging copy.
        if directory.name.endswith(".restoring"):
            connection = sqlite3.connect(backup_dir / "canonical.sqlite3")
            try:
                connection.execute("UPDATE agents SET display_name = 'attacker-controlled'")
                connection.commit()
            finally:
                connection.close()
        return real_verify(directory, signing_key=key)

    target = tmp_path / "srv" / "data"
    with mock.patch.object(backup_module, "verify_backup", side_effect=attacking_verify):
        report = backups.restore_backup(backup_dir, target, signing_key=key)
    assert report.check.ok, report.check.problems
    connection = sqlite3.connect(target / "canonical.sqlite3")
    try:
        names = {row[0] for row in connection.execute("SELECT display_name FROM agents")}
    finally:
        connection.close()
    assert "attacker-controlled" not in names  # the attack never reached the target

    # And a tampered STAGING copy (mutation after copy, before verify) refuses:
    def staging_tampering_verify(directory: Path, **_kwargs: object) -> object:
        if directory.name.endswith(".restoring"):
            (directory / "checksums.txt").write_text("corrupted\n", encoding="utf-8")
        return real_verify(directory, signing_key=key)

    with mock.patch.object(backup_module, "verify_backup", side_effect=staging_tampering_verify):
        report = backups.restore_backup(backup_dir, tmp_path / "srv" / "data2", signing_key=key)
    assert not report.check.ok

    # Mutation by the invariant-inspection step itself is caught by the final
    # full verification immediately before the journal/switch.
    late_backup = tmp_path / "late-backup"
    backups.create_backup(late_backup, signing_key=key)
    real_invariants = backup_module.verify_database_invariants

    def mutating_invariants(database: Path) -> tuple[str, ...]:
        problems = real_invariants(database)
        connection = sqlite3.connect(database)
        try:
            connection.execute("UPDATE agents SET display_name = 'late-attacker'")
            connection.commit()
        finally:
            connection.close()
        return problems

    with mock.patch.object(
        backup_module, "verify_database_invariants", side_effect=mutating_invariants
    ):
        report = backups.restore_backup(late_backup, tmp_path / "srv" / "data3", signing_key=key)
    assert not report.check.ok
    assert not (tmp_path / "srv" / "data3").exists()


def test_verify_backup_returns_check_for_corrupt_content(
    store: Store,
    provisioning: ProvisioningService,
    identities: IdentityService,
    admin_access: AccessContext,
    tmp_path: Path,
) -> None:
    """Corrupt manifests / non-database canonical fail the check, never raise."""
    seed_world(store, provisioning, identities, admin_access)
    backups = BackupService(store)
    backup_dir = tmp_path / "backup"
    backups.create_backup(backup_dir)

    broken = tmp_path / "broken-json"
    shutil.copytree(backup_dir, broken)
    (broken / "manifest.json").write_text("{not json", encoding="utf-8")
    check = backups.verify_backup(broken)
    assert not check.ok
    assert any("backup unreadable" in problem for problem in check.problems)

    garbage = tmp_path / "garbage-db"
    shutil.copytree(backup_dir, garbage)
    (garbage / "canonical.sqlite3").write_bytes(b"this is not a database" * 100)
    check = backups.verify_backup(garbage)
    assert not check.ok  # reconciling a non-database canonical fails, not raises

    original_manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    malformed_shapes: tuple[object, ...] = (
        [],
        {**original_manifest, "files": None},
        {**original_manifest, "schema_version": None},
        {**original_manifest, "agent_watermarks": []},
    )
    for index, payload in enumerate(malformed_shapes):
        malformed = tmp_path / f"malformed-shape-{index}"
        shutil.copytree(backup_dir, malformed)
        (malformed / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
        check = backups.verify_backup(malformed)
        assert not check.ok
        assert any("backup unreadable" in problem for problem in check.problems)


@pytest.mark.parametrize("unexpected_name", ["canonical.sqlite3-wal", "untrusted-extra.txt"])
def test_backup_file_set_is_exact_and_sidecars_are_never_restored(
    unexpected_name: str,
    store: Store,
    provisioning: ProvisioningService,
    identities: IdentityService,
    admin_access: AccessContext,
    tmp_path: Path,
) -> None:
    """Files outside the authenticated format, especially SQLite sidecars,
    are rejected before copy and can never influence the restored database."""
    seed_world(store, provisioning, identities, admin_access)
    backups = BackupService(store)
    backup_dir = tmp_path / "backup"
    backups.create_backup(backup_dir, signing_key=b"unit-test-backup-key-0123456789ab")
    (backup_dir / unexpected_name).write_bytes(b"attacker-controlled")

    check = backups.verify_backup(backup_dir, signing_key=b"unit-test-backup-key-0123456789ab")
    assert not check.ok
    assert any("unexpected backup entry" in problem for problem in check.problems)
    target = tmp_path / "target"
    report = backups.restore_backup(
        backup_dir, target, signing_key=b"unit-test-backup-key-0123456789ab"
    )
    assert not report.check.ok
    assert not target.exists()


def test_restore_missing_source_returns_failure_and_cleans_staging(tmp_path: Path) -> None:
    from iris_memory_core.storage.backup import restore_backup

    target = tmp_path / "srv" / "data"
    report = restore_backup(tmp_path / "missing-backup", target)
    assert not report.check.ok
    assert any("missing" in problem for problem in report.check.problems)
    assert not target.exists()
    assert not (target.parent / "data.restoring").exists()


def test_durable_file_write_does_not_follow_predictable_temp_symlink(tmp_path: Path) -> None:
    from iris_memory_core.storage.backup import _write_file_durable

    destination = tmp_path / "data.restore-journal"
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    old_predictable_temp = destination.with_suffix(destination.suffix + ".tmp")
    old_predictable_temp.symlink_to(victim)

    _write_file_durable(destination, "complete journal\n")

    assert victim.read_text(encoding="utf-8") == "keep"
    assert destination.read_text(encoding="utf-8") == "complete journal\n"
    assert old_predictable_temp.is_symlink()


def test_recovery_refuses_untrusted_journal_paths(tmp_path: Path) -> None:
    """Journal entries naming paths outside the module's naming scheme are
    rejected without touching anything (no rmtree primitive for attackers)."""
    from iris_memory_core.storage.backup import JOURNAL_SUFFIX, recover_pending_switch

    parent = tmp_path / "srv"
    parent.mkdir()
    target = parent / "data"
    target.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("evidence", encoding="utf-8")

    journal = parent / f"data{JOURNAL_SUFFIX}"
    for payload in (
        '{"staging": "../victim", "aside": null}',
        '{"staging": "data.restoring", "aside": "../victim"}',
        '{"staging": "some-other-dir", "aside": null}',
        "[]",
    ):
        journal.write_text(payload, encoding="utf-8")
        assert recover_pending_switch(target) == "invalid_journal"
        assert victim.exists() and (victim / "keep.txt").is_file()
        assert journal.is_file()  # left for operator inspection
    journal.unlink()
    assert recover_pending_switch(target) == "none"


def test_corrupt_journal_is_reported_not_raised(tmp_path: Path) -> None:
    """A crash mid-journal-write can no longer produce this (atomic rename),
    but a truncated/garbage journal from any source must report, not raise."""
    from iris_memory_core.storage.backup import JOURNAL_SUFFIX, recover_pending_switch

    parent = tmp_path / "srv"
    parent.mkdir()
    target = parent / "data"
    journal = parent / f"data{JOURNAL_SUFFIX}"
    journal.write_bytes(b'{"staging": "data.res')
    assert recover_pending_switch(target) == "invalid_journal"
    journal.unlink()
    journal.mkdir()
    assert recover_pending_switch(target) == "invalid_journal"


def test_recovery_retains_journal_when_cleanup_fails(tmp_path: Path) -> None:
    from unittest import mock

    from iris_memory_core.storage import backup as backup_module

    parent = tmp_path / "srv"
    target = parent / "data"
    staging = parent / "data.restoring"
    target.mkdir(parents=True)
    staging.mkdir()
    journal = parent / f"data{backup_module.JOURNAL_SUFFIX}"
    backup_module._write_file_durable(journal, json.dumps({"staging": staging.name, "aside": None}))

    with mock.patch.object(
        backup_module.shutil,  # type: ignore[attr-defined]
        "rmtree",
        side_effect=OSError("busy"),
    ):
        assert backup_module.recover_pending_switch(target) == "cleanup_failed"

    assert staging.is_dir()
    assert journal.is_file()


def test_restore_switch_lock_serializes_concurrent_operations(tmp_path: Path) -> None:
    from iris_memory_core.domain.errors import ConflictError
    from iris_memory_core.storage.backup import _restore_lock

    target = tmp_path / "srv" / "data"
    target.mkdir(parents=True)
    with (
        _restore_lock(target, timeout_s=0.2),
        pytest.raises(ConflictError),
        _restore_lock(target, timeout_s=0.2),
    ):
        pass
    # Releasing re-enables acquisition.
    with _restore_lock(target, timeout_s=0.2):
        pass


def test_backup_publication_is_atomic_and_cleans_up_on_failure(
    store: Store,
    provisioning: ProvisioningService,
    identities: IdentityService,
    admin_access: AccessContext,
    tmp_path: Path,
) -> None:
    from unittest import mock

    from iris_memory_core.storage import backup as backup_module

    seed_world(store, provisioning, identities, admin_access)
    backups = BackupService(store)
    destination = tmp_path / "backups" / "b1"
    backups.create_backup(destination)
    assert (destination / "manifest.json").is_file()
    leftovers = [p.name for p in destination.parent.iterdir() if ".staging-" in p.name]
    assert leftovers == []  # nothing half-published remains

    failing = tmp_path / "backups" / "b2"
    with (
        mock.patch.object(
            backup_module, "_write_backup_files_into", side_effect=OSError("disk full")
        ),
        pytest.raises(OSError, match="disk full"),
    ):
        backups.create_backup(failing)
    assert not failing.exists()
    leftovers = [p.name for p in failing.parent.iterdir() if ".staging-" in p.name]
    assert leftovers == []
