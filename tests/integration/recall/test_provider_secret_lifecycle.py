"""Real sealed history, startup refusal, atomic rotation and recoverable CLI backup."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.cli import main
from iris_memory_core.domain.errors import ConflictError
from iris_memory_core.domain.provider_configs import (
    EmbeddingDefinition,
    ProviderConfig,
    ProviderConfigRevision,
)
from iris_memory_core.domain.vector import VectorSpaceConfig
from iris_memory_core.providers.secret_lifecycle import (
    deployment_secrets,
    rotate_sealed,
    validate_sealed_startup,
)
from iris_memory_core.providers.secrets import ProviderSecrets
from iris_memory_core.recall_runtime import RecallAssemblyConfig, assemble_recall
from iris_memory_core.runtime import ServiceConfig, load_config, open_store
from iris_memory_core.storage.backup import restore_backup, verify_backup
from tests.integration.migrations.test_provider_configuration_migration import world as _world

world = _world


def key_file(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(os.urandom(32))
    path.chmod(0o600)
    return path


def populate(world: dict[str, Any], secrets: ProviderSecrets, count: int = 3) -> None:
    store, tenant, key = world["store"], world["tenant"], world["key"]
    now = store.clock.now_us()
    with store.write() as tx:
        for number in range(count):
            identifier = f"config-{number:04d}"
            config = ProviderConfig(identifier, tenant, "draft", 1, 1, now, now, key.id)
            revision = ProviderConfigRevision(
                tenant,
                identifier,
                1,
                EmbeddingDefinition(
                    "openai-compatible",
                    "https://provider.example/v1/embeddings",
                    VectorSpaceConfig(model="fixture", dimension=2),
                ),
                secrets.seal(tenant, identifier, 1, f"private-provider-token-{number}"),
                now,
                key.id,
            )
            tx.providers.insert(config, revision)


def envelopes(world: dict[str, Any]) -> dict[tuple[str, str, int], tuple[str, str | None]]:
    with world["store"].read() as tx:
        return {
            (r.tenant_id, r.config_id, r.content_revision): (r.content_hash(), r.secret.ciphertext)
            for r in tx.providers.sealed_revisions()
            if r.secret is not None
        }


def test_startup_rejects_missing_wrong_and_same_console_master(
    world: dict[str, Any], tmp_path: Path
) -> None:
    store = world["store"]
    old, wrong = key_file(tmp_path, "old.key"), key_file(tmp_path, "wrong.key")
    populate(world, ProviderSecrets(master_key_file=old))
    config = ServiceConfig(database=store.runtime.database, migrate=False, allow_local_sqlite=True)
    for path in (None, wrong):
        with pytest.raises(ConflictError, match="provider secret is unavailable"):
            open_store(replace(config, secret_key_file=path))
        with pytest.raises(ConflictError):
            assemble_recall(store, store.clock, RecallAssemblyConfig(secret_key_file=path))
    assert (
        open_store(replace(config, secret_key_file=old)).runtime.database == store.runtime.database
    )
    assemble_recall(store, store.clock, RecallAssemblyConfig(secret_key_file=old))
    console_key = store.runtime.database.parent / "console-auth.key"
    with pytest.raises(ConflictError):
        deployment_secrets(store.runtime.database, console_key)
    copied = tmp_path / "copy.key"
    copied.write_bytes(console_key.read_bytes())
    copied.chmod(0o600)
    with pytest.raises(ConflictError):
        deployment_secrets(store.runtime.database, copied)


def test_startup_refuses_before_server_listener(
    world: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old = key_file(tmp_path, "old.key")
    populate(world, ProviderSecrets(master_key_file=old))
    monkeypatch.delenv("IRIS_MEMORY_SECRET_KEY_FILE", raising=False)
    monkeypatch.setattr("uvicorn.Server.run", lambda _: pytest.fail("listener must not start"))
    assert (
        main(
            [
                "serve",
                "--database",
                str(world["store"].runtime.database),
                "--allow-local-sqlite",
                "--no-migrate",
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert "provider secret is unavailable" in output.err
    assert "private-provider-token" not in output.err + output.out


@pytest.mark.parametrize("count", [1, 105])
def test_atomic_rotation_crosses_cursor_batch_and_retains_content(
    world: dict[str, Any], tmp_path: Path, count: int
) -> None:
    source = ProviderSecrets(master_key_file=key_file(tmp_path, "old.key"))
    target = ProviderSecrets(master_key_file=key_file(tmp_path, "new.key"))
    populate(world, source, count)
    before = envelopes(world)
    assert rotate_sealed(world["store"], source, target) == count
    after = envelopes(world)
    assert before.keys() == after.keys()
    assert all(
        before[key][0] == after[key][0] and before[key][1] != after[key][1] for key in before
    )
    assert validate_sealed_startup(world["store"], target) == count
    with pytest.raises(ConflictError):
        validate_sealed_startup(world["store"], source)
    with world["store"].read() as tx:
        events = [
            e
            for e in tx.list_audit_events(world["tenant"], limit=200)
            if e.action == "provider.secrets.rotated"
        ]
    assert len(events) == count
    assert "private-provider-token" not in repr(events)


def test_late_corruption_rolls_back_already_resealed_rows_and_audit(
    world: dict[str, Any], tmp_path: Path
) -> None:
    source = ProviderSecrets(master_key_file=key_file(tmp_path, "old.key"))
    target = ProviderSecrets(master_key_file=key_file(tmp_path, "new.key"))
    populate(world, source)
    with world["store"].write() as tx:
        tx.raw().execute(
            "UPDATE provider_sealed_secrets SET ciphertext=? WHERE config_id='config-0002'",
            ("X" * 64,),
        )
    before = envelopes(world)
    with pytest.raises(ConflictError):
        rotate_sealed(world["store"], source, target)
    assert envelopes(world) == before
    with world["store"].read() as tx:
        assert not any(
            e.action == "provider.secrets.rotated" for e in tx.list_audit_events(world["tenant"])
        )


def test_cli_verifies_backup_rotates_and_old_key_recovers(
    world: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old, new = key_file(tmp_path, "old.key"), key_file(tmp_path, "new.key")
    populate(world, ProviderSecrets(master_key_file=old))
    before = envelopes(world)
    backup = tmp_path / "before-rotation"
    args = [
        "provider",
        "rotate-master-key",
        "--database",
        str(world["store"].runtime.database),
        "--old-key-file",
        str(old),
        "--new-key-file",
        str(new),
        "--with-backup",
        str(backup),
        "--allow-local-sqlite",
    ]
    assert main(args) == 1  # No acknowledgement means no backup or mutation.
    assert not backup.exists() and envelopes(world) == before
    assert main([*args, "--allow-offline"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["sealed_revisions"] == 3
    assert "private-provider-token" not in output.out + output.err
    assert verify_backup(backup).ok
    assert validate_sealed_startup(world["store"], ProviderSecrets(master_key_file=new)) == 3
    recovered = tmp_path / "recovered"
    assert restore_backup(backup, recovered).check.ok
    store = open_store(
        ServiceConfig(
            database=recovered / "canonical.sqlite3",
            migrate=False,
            allow_local_sqlite=True,
            secret_key_file=old,
        )
    )
    assert validate_sealed_startup(store, ProviderSecrets(master_key_file=old)) == 3


def test_service_config_resolves_master_path_without_logging_it(tmp_path: Path) -> None:
    key = key_file(tmp_path, "master.key")
    config = load_config(
        environ={
            "IRIS_MEMORY_DATABASE": str(tmp_path / "data" / "canonical.sqlite3"),
            "IRIS_MEMORY_SECRET_KEY_FILE": str(key),
        }
    )
    assert config.secret_key_file == key and config.recall_config().secret_key_file == key
