"""Release resource failures and trusted offline initialization boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from iris_memory_core import _resources
from iris_memory_core.application.security import CredentialService
from iris_memory_core.cli import main
from iris_memory_core.domain.errors import ConflictError, NotFoundError
from iris_memory_core.indexing.fts import FtsDegradedError, FtsProjectionService
from iris_memory_core.runtime import ServiceConfig, open_store
from iris_memory_core.storage.migrations import MigrationNameError, discover_migrations


def test_missing_and_empty_migrations_fail_closed(tmp_path: Path) -> None:
    for path in (tmp_path, tmp_path / "missing"):
        with pytest.raises(MigrationNameError, match="missing or empty"):
            discover_migrations(path)


def test_missing_installed_resource_cannot_fall_back_to_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "migrations").mkdir()
    monkeypatch.setattr(_resources, "files", lambda _: tmp_path / "installed")
    monkeypatch.setattr(_resources, "__file__", str(tmp_path / "site-packages/core/_resources.py"))
    _resources.runtime_resource.cache_clear()
    try:
        with pytest.raises(_resources.RuntimeResourceError):
            _resources.runtime_resource("migrations")
    finally:
        _resources.runtime_resource.cache_clear()


@pytest.mark.parametrize("with_actor", [False, True])
@pytest.mark.parametrize("with_search", [False, True])
def test_offline_init_issues_scoped_credential_and_refuses_overwrite(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    with_actor: bool,
    with_search: bool,
) -> None:
    database = tmp_path / "data/core.sqlite3"
    output = tmp_path / "client.json"
    args = [
        "init",
        "--database",
        str(database),
        "--tenant",
        "release-tenant",
        "--agent-name",
        "Release agent",
        "--app-instance",
        "release-client",
        "--credential-file",
        str(output),
        "--allow-local-sqlite",
    ]
    if with_actor:
        args += ["--actor-provider", "release-chat", "--actor-subject", "verified-viewer"]
    if with_search:
        args += ["--initialize-search"]
    assert main(args) == 0
    material = json.loads(output.read_text())
    printed = capsys.readouterr().out
    assert material["token"] not in printed
    if with_actor:
        assert material["actor_external_identity_id"]
        assert (
            json.loads(printed)["actor_external_identity_id"]
            == material["actor_external_identity_id"]
        )
    else:
        assert "actor_external_identity_id" not in material
    assert output.stat().st_mode & 0o777 == 0o600
    store = open_store(ServiceConfig(database=database, allow_local_sqlite=True))
    with store.read() as tx:
        pointer = tx.fts.pointer("release-tenant")
        if with_search:
            assert pointer is not None
            assert pointer.generation_id == material["search_generation_id"]
            assert json.loads(printed)["search_generation_id"] == pointer.generation_id
        else:
            assert pointer is None
            assert "search_generation_id" not in material
    access = CredentialService(store, store.clock).authenticate(material["token"])
    assert "events.sse.v1" in access.capabilities
    assert "events.checkpoint.v1" in access.capabilities
    assert not access.admin
    assert access.agent_ids == frozenset({material["agent_id"]})
    assert access.allowed_space_ids == frozenset({material["space_id"]})
    assert access.data_purposes == frozenset({"reply"})
    assert not any(name.startswith("admin.") for name in access.capabilities)
    before = output.read_bytes()
    assert main(args) == 1
    assert output.read_bytes() == before
    assert material["token"] not in str(capsys.readouterr())


def test_offline_init_refuses_symlink(tmp_path: Path) -> None:
    target = tmp_path / "protected"
    target.write_text("unchanged")
    link = tmp_path / "client.json"
    link.symlink_to(target)
    assert (
        main(
            [
                "init",
                "--database",
                str(tmp_path / "data/core.sqlite3"),
                "--tenant",
                "release",
                "--agent-name",
                "Agent",
                "--app-instance",
                "client",
                "--credential-file",
                str(link),
                "--allow-local-sqlite",
            ]
        )
        == 1
    )
    assert target.read_text() == "unchanged"


@pytest.mark.parametrize("failure", ["verification", "unavailable"])
def test_search_initialization_failure_rolls_back_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    database = tmp_path / "data/core.sqlite3"
    output = tmp_path / "client.json"
    args = [
        "init",
        "--database",
        str(database),
        "--tenant",
        "search-tenant",
        "--agent-name",
        "Search agent",
        "--app-instance",
        "search-client",
        "--credential-file",
        str(output),
        "--allow-local-sqlite",
        "--initialize-search",
    ]

    def fail(*_args: object) -> None:
        if failure == "verification":
            raise ConflictError("injected generation verification failure")
        raise FtsDegradedError("fts_unavailable", retryable=False)

    method = "_verify_generation" if failure == "verification" else "rebuild_in_tx"
    with monkeypatch.context() as patch:
        patch.setattr(FtsProjectionService, method, fail)
        assert main(args) == 1
    assert not output.exists()
    store = open_store(ServiceConfig(database=database, allow_local_sqlite=True))
    with store.read() as tx:
        with pytest.raises(NotFoundError):
            tx.get_tenant("search-tenant")
        assert tx.fts.pointer("search-tenant") is None
    assert main(args) == 0
    assert json.loads(output.read_text())["search_generation_id"]
