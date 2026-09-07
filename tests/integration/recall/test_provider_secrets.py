"""Provider secrets exercise real files, permission checks, and authenticated encryption."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from dataclasses import replace
from pathlib import Path

import pytest

from iris_memory_core.domain.errors import ConflictError
from iris_memory_core.domain.provider_configs import ProviderSecret
from iris_memory_core.providers.secrets import ProviderSecrets, read_private_file


@pytest.fixture
def master(tmp_path: Path) -> Path:
    path = tmp_path / "provider-master.key"
    path.write_bytes(os.urandom(32))
    path.chmod(0o600)
    return path


def assert_unavailable(error: pytest.ExceptionInfo[ConflictError]) -> None:
    assert error.value.details == {"kind": "secret_unavailable"}
    assert str(error.value) == "conflict: provider secret is unavailable"


def test_secret_ref_is_exactly_tenant_scoped_and_never_serializes_value() -> None:
    value = "test-provider-token-ABCD"
    environment = {"FIRST": value, "SECOND": "test-other-token-EFGH"}
    resolver = ProviderSecrets(
        allowed_references={"first": frozenset({"env:FIRST"}), "second": frozenset({"env:SECOND"})},
        environment=environment,
    )
    secret = resolver.reference("first", "env:FIRST")
    assert resolver.resolve("first", "config", 1, secret) == value
    assert resolver.describe("first", "config", 1, secret) == {
        "secret_mode": "secret_ref",
        "secret_hint": "ABCD",
        "secret_digest_prefix": hashlib.sha256(value.encode()).hexdigest()[:8],
        "resolved": True,
    }
    assert value not in repr(secret)
    assert "env:FIRST" not in json.dumps(resolver.describe("first", "config", 1, secret))
    with pytest.raises(ConflictError) as error:
        resolver.resolve("second", "config", 1, secret)
    assert_unavailable(error)
    with pytest.raises(ConflictError):
        resolver.reference("first", "env:SECOND")


def test_references_cannot_share_tenant_ownership() -> None:
    with pytest.raises(ConflictError):
        ProviderSecrets(
            allowed_references={"a": frozenset({"env:SAME"}), "b": frozenset({"env:SAME"})}
        )


@pytest.mark.parametrize(
    "reference",
    [
        "env:",
        "env:BAD-NAME",
        "env:VALUE\n",
        "file:relative",
        "file:/tmp/../secret",
        "https://host/secret",
        "literal-secret",
    ],
)
def test_reference_syntax_cannot_select_arbitrary_sources(reference: str) -> None:
    with pytest.raises(ConflictError) as error:
        ProviderSecrets(allowed_references={"tenant": frozenset({reference})})
    assert_unavailable(error)


def test_missing_reference_is_unresolved_then_resolves_live() -> None:
    environment: dict[str, str] = {}
    resolver = ProviderSecrets(
        allowed_references={"t": frozenset({"env:KEY"})}, environment=environment
    )
    secret = resolver.reference("t", "env:KEY")
    assert resolver.describe("t", "c", 1, secret)["resolved"] is False
    environment["KEY"] = "new-test-provider-key"
    assert resolver.describe("t", "c", 1, secret)["resolved"] is True
    first = resolver.fingerprint("t", "c", 1, secret)
    environment["KEY"] = "rotated-test-provider-key"
    assert resolver.fingerprint("t", "c", 1, secret) != first


@pytest.mark.parametrize(
    "value",
    [
        "",
        "short",
        "x" * 4097,
        "test-secret\r\nInjected: value",
        "test secret with spaces",
        "non-ascii-秘密-value",
    ],
)
def test_secret_plaintext_rejects_empty_oversized_or_unsafe_header_values(
    value: str, master: Path
) -> None:
    with pytest.raises(ConflictError) as error:
        ProviderSecrets(master_key_file=master).seal("t", "c", 1, value)
    assert_unavailable(error)


def test_private_file_reference_accepts_only_registered_file(tmp_path: Path, master: Path) -> None:
    key = tmp_path / "endpoint.key"
    key.write_bytes(b"fixture-provider-key\n")
    key.chmod(0o600)
    reference = "file:" + str(key)
    resolver = ProviderSecrets(
        allowed_references={"t": frozenset({reference})}, master_key_file=master
    )
    record = resolver.reference("t", reference)
    assert resolver.resolve("t", "c", 1, record) == "fixture-provider-key"
    assert resolver.describe("t", "c", 1, record)["resolved"] is True
    key.chmod(0o640)
    assert resolver.describe("t", "c", 1, record)["resolved"] is False


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o700])
def test_master_rejects_permissions_wider_than_0600(master: Path, mode: int) -> None:
    master.chmod(mode)
    with pytest.raises(ConflictError) as error:
        ProviderSecrets(master_key_file=master)
    assert_unavailable(error)


@pytest.mark.parametrize("size", [0, 16, 31, 33, 100000])
def test_master_is_exactly_32_raw_bytes(master: Path, size: int) -> None:
    master.write_bytes(b"x" * size)
    with pytest.raises(ConflictError):
        ProviderSecrets(master_key_file=master)


def test_file_rejects_final_and_parent_symlinks_and_hardlinks(tmp_path: Path, master: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(master)
    parent = tmp_path / "parent"
    parent.symlink_to(tmp_path, target_is_directory=True)
    for path in (link, parent / master.name):
        with pytest.raises(ConflictError):
            read_private_file(path, maximum=32)
    hardlink = tmp_path / "hardlink"
    os.link(master, hardlink)
    with pytest.raises(ConflictError):
        read_private_file(hardlink, maximum=32)


def test_fifo_rejection_does_not_block_open(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, mode=0o600)
    started = time.monotonic()
    with pytest.raises(ConflictError):
        read_private_file(fifo, maximum=32)
    assert time.monotonic() - started < 0.5


def test_provider_and_console_master_files_cannot_be_secret_references(
    tmp_path: Path, master: Path
) -> None:
    console = tmp_path / "console-auth.key"
    console.write_bytes(b"c" * 32)
    console.chmod(0o600)
    refs = frozenset({"file:" + str(master), "file:" + str(console)})
    resolver = ProviderSecrets(
        allowed_references={"t": refs}, master_key_file=master, reserved_files=frozenset({console})
    )
    for ref in refs:
        secret = resolver.reference("t", ref)
        assert resolver.describe("t", "c", 1, secret)["resolved"] is False
        with pytest.raises(ConflictError):
            resolver.resolve("t", "c", 1, secret)


def test_sealed_requires_master_and_uses_random_nonce(master: Path) -> None:
    value = "fixture-sealed-secret-value"
    with pytest.raises(ConflictError) as error:
        ProviderSecrets().seal("t", "c", 1, value)
    assert_unavailable(error)
    resolver = ProviderSecrets(master_key_file=master)
    first = resolver.seal("t", "c", 1, value)
    second = resolver.seal("t", "c", 1, value)
    assert first.ciphertext != second.ciphertext
    assert resolver.resolve("t", "c", 1, first) == value
    assert value not in repr(first)
    assert first.ciphertext is not None
    assert first.ciphertext not in repr(first)
    assert value not in json.dumps(resolver.describe("t", "c", 1, first))


@pytest.mark.parametrize(
    ("tenant", "config", "revision"), [("other", "c", 1), ("t", "other", 1), ("t", "c", 2)]
)
def test_sealed_cannot_be_moved_between_tenant_config_or_revision(
    master: Path, tenant: str, config: str, revision: int
) -> None:
    resolver = ProviderSecrets(master_key_file=master)
    secret = resolver.seal("t", "c", 1, "fixture-sealed-secret")
    with pytest.raises(ConflictError) as error:
        resolver.resolve(tenant, config, revision, secret)
    assert_unavailable(error)


def test_aad_encoding_is_unambiguous_when_ids_contain_separators(master: Path) -> None:
    resolver = ProviderSecrets(master_key_file=master)
    secret = resolver.seal("a|b", "c", 1, "fixture-sealed-secret")
    with pytest.raises(ConflictError):
        resolver.resolve("a", "b|c", 1, secret)


@pytest.mark.parametrize("damage", ["digest", "ciphertext", "missing", "oversized"])
def test_ciphertext_and_binding_tampering_fail_uniformly(master: Path, damage: str) -> None:
    resolver = ProviderSecrets(master_key_file=master)
    secret = resolver.seal("t", "c", 1, "fixture-sealed-secret")
    if damage == "digest":
        secret = replace(secret, digest_prefix="0" * 8)
    elif damage == "ciphertext":
        secret = replace(secret, ciphertext="invalid==")
    elif damage == "missing":
        secret = replace(secret, ciphertext=None)
    else:
        secret = replace(secret, ciphertext="A" * 10000)
    with pytest.raises(ConflictError) as error:
        resolver.resolve("t", "c", 1, secret)
    assert_unavailable(error)


def test_offline_reseal_preserves_identity_and_rejects_old_master(
    tmp_path: Path, master: Path
) -> None:
    replacement = tmp_path / "replacement.key"
    replacement.write_bytes(os.urandom(32))
    replacement.chmod(0o600)
    old = ProviderSecrets(master_key_file=master)
    new = ProviderSecrets(master_key_file=replacement)
    value = "fixture-sealed-rotation"
    secret = old.seal("t", "c", 4, value)
    rotated = old.reseal(new, "t", "c", 4, secret)
    assert rotated.digest_prefix == secret.digest_prefix
    assert rotated.hint == secret.hint
    assert new.resolve("t", "c", 4, rotated) == value
    with pytest.raises(ConflictError):
        old.resolve("t", "c", 4, rotated)
    with pytest.raises(ConflictError):
        new.resolve("t", "c", 4, secret)
    assert old.reseal(
        new, "t", "c", 1, ProviderSecret("secret_ref", reference="env:NAME")
    ) == ProviderSecret("secret_ref", reference="env:NAME")


@pytest.mark.parametrize("damage", ["special-permission", "foreign-owner"])
def test_descriptor_metadata_rejects_special_modes_and_foreign_owner(
    master: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    actual = os.fstat

    def unsafe(descriptor: int) -> os.stat_result:
        metadata = list(actual(descriptor))
        if damage == "special-permission":
            metadata[stat.ST_MODE] = int(metadata[stat.ST_MODE]) | stat.S_ISUID
        else:
            metadata[stat.ST_UID] = os.getuid() + 1
        return os.stat_result(metadata)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fstat", unsafe)
        with pytest.raises(ConflictError):
            read_private_file(master, maximum=32)


def test_read_only_master_permission_is_accepted(master: Path) -> None:
    master.chmod(0o400)
    resolver = ProviderSecrets(master_key_file=master)
    secret = resolver.seal("t", "c", 1, "fixture-sealed-secret")
    assert resolver.resolve("t", "c", 1, secret) == "fixture-sealed-secret"
