"""Online backup, verified isolated restore and RPO/RTO measurement (§21).

Backups use the SQLite Online Backup API — never a raw copy of the live
files. A backup directory contains exactly ``canonical.sqlite3``,
``manifest.json``, ``config-fingerprint.json`` and ``checksums.txt``, plus an
optional ``authenticity.tag``; restore rejects every other entry and verifies
all hashes plus integrity, foreign-key, schema-window and current-pointer
invariants in an isolated directory before switching.

Integrity vs authenticity: ``checksums.txt`` detects accidental corruption
only — an attacker who can rewrite the snapshot can recompute every hash in
the directory. Authenticity against that threat requires a trust root outside
the backup: pass ``signing_key`` (an operator-held secret, e.g. via the CLI's
``--backup-key-file``) and the backup carries ``authenticity.tag``, an
HMAC-SHA256 over the payload digests that verification recomputes.

The restore switch is crash-recoverable: a journal file records the staged
and aside directory names before the first rename, is removed only after the
switch completes, and ``recover_pending_switch`` deterministically finishes
or rolls back an interrupted switch at startup.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from iris_memory_core.domain.errors import ConflictError
from iris_memory_core.domain.memory import ArtifactLocatorError, normalize_local_locator
from iris_memory_core.domain.retention import ForgetRequest
from iris_memory_core.storage.migrations import (
    MigrationError,
    normalize_prerelease_phase5_schema,
)
from iris_memory_core.storage.runtime import SQLiteRuntime, current_schema_version
from iris_memory_core.storage.uow import Store

CANONICAL_NAME = "canonical.sqlite3"
MANIFEST_NAME = "manifest.json"
CHECKSUMS_NAME = "checksums.txt"
FINGERPRINT_NAME = "config-fingerprint.json"
AUTH_NAME = "authenticity.tag"
JOURNAL_SUFFIX = ".restore-journal"
_PAYLOAD_NAMES = (CANONICAL_NAME, MANIFEST_NAME, FINGERPRINT_NAME)
_REQUIRED_BACKUP_NAMES = frozenset((*_PAYLOAD_NAMES, CHECKSUMS_NAME))
_ALLOWED_BACKUP_NAMES = frozenset((*_REQUIRED_BACKUP_NAMES, AUTH_NAME))
#: Phase 5: local artifact blobs ride inside ``artifacts/<shard>/<uuid>``
#: (server-derived locators only). Their integrity anchors to the manifest's
#: artifact digest map, which itself sits inside the checksummed manifest.
ARTIFACTS_DIR_NAME = "artifacts"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"not a regular backup file: {path.name}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    finally:
        if fd >= 0:
            os.close(fd)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    """Durably record directory entries (renames, journal files) on disk."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _checkpoint_database_for_switch(database: Path) -> None:
    """Fold trusted staging writes into the main DB before immutable checks.

    ``SQLiteRuntime`` uses WAL.  An ``immutable=1`` verification connection
    intentionally ignores WAL sidecars, so ledger replay must be checkpointed
    before the invariant pass or that pass would inspect the pre-replay image.
    DELETE mode also guarantees the directory switch has one canonical SQLite
    file rather than a crash-sensitive main/WAL pair.
    """
    connection = sqlite3.connect(database, isolation_level=None)
    try:
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and int(checkpoint[0]) != 0:
            raise OSError(f"staging WAL checkpoint stayed busy: {tuple(checkpoint)}")
        mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()
        if mode is None or str(mode[0]).lower() != "delete":
            raise OSError(f"staging database did not enter DELETE journal mode: {mode}")
    finally:
        connection.close()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{database}{suffix}")
        if sidecar.exists():
            raise OSError(f"staging SQLite sidecar survived checkpoint: {sidecar.name}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(database, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(database.parent)


def _write_file_durable(path: Path, content: str) -> None:
    """Publish ``content`` atomically: fsync a temp file, rename into place.

    A crash mid-write can never leave a truncated file at ``path`` — readers
    see either the previous content or the complete new content.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _copy_artifact_blobs(
    artifacts_manifest: dict[str, dict[str, Any]], artifact_root: Path, destination: Path
) -> None:
    """Copy the manifest's local blobs into ``artifacts/<locator>`` (regular
    files only, no symlink following, hash-verified after the copy)."""
    for artifact_id, meta in sorted(artifacts_manifest.items()):
        source = artifact_root / str(meta["locator"])
        if source.is_symlink() or not source.is_file():
            raise OSError(f"artifact blob missing for backup: {artifact_id}")
        target = destination / ARTIFACTS_DIR_NAME / str(meta["locator"])
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_regular_file(source, target)
        if _sha256_file(target) != meta["content_hash"]:
            raise OSError(f"artifact blob hash mismatch while backing up: {artifact_id}")


def _inspect_backup_directory(directory: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return a safe, exact backup file set and any shape violations.

    Restore never copies unknown entries. In particular SQLite sidecars are
    not authenticated payloads and must not accompany ``canonical.sqlite3``:
    a crafted WAL/journal could otherwise change what SQLite reads while the
    signed canonical file itself still hashes correctly.
    """
    if directory.is_symlink() or not directory.is_dir():
        return (), ("backup directory is missing or is not a real directory",)
    problems: list[str] = []
    names: list[str] = []
    for entry in directory.iterdir():
        names.append(entry.name)
        try:
            mode = entry.lstat().st_mode
        except OSError as error:
            problems.append(f"backup entry {entry.name} is unreadable: {error}")
            continue
        if entry.name == ARTIFACTS_DIR_NAME:
            # The artifact blob tree is validated recursively below.
            continue
        if entry.is_symlink() or not stat.S_ISREG(mode):
            problems.append(f"backup entry {entry.name} is not a regular file")
        if entry.name not in _ALLOWED_BACKUP_NAMES:
            problems.append(f"unexpected backup entry {entry.name}")
    artifacts_dir = directory / ARTIFACTS_DIR_NAME
    if artifacts_dir.exists():
        if artifacts_dir.is_symlink() or not artifacts_dir.is_dir():
            problems.append("artifacts entry is not a real directory")
        else:
            for entry in sorted(artifacts_dir.rglob("*")):
                if entry.is_symlink():
                    problems.append(f"artifact entry {entry.relative_to(directory)} is a symlink")
                elif entry.exists() and not entry.is_file() and not entry.is_dir():
                    problems.append(
                        f"artifact entry {entry.relative_to(directory)} is not a regular file"
                    )
    actual = set(names)
    for missing in sorted(_REQUIRED_BACKUP_NAMES - actual):
        problems.append(f"missing {missing}")
    return tuple(sorted(actual & _ALLOWED_BACKUP_NAMES)), tuple(problems)


def _copy_regular_file(source: Path, destination: Path) -> None:
    """Copy one regular file without following either source or target links."""
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    target_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, source_flags)
    target_fd = -1
    try:
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise OSError(f"not a regular backup file: {source.name}")
        target_fd = os.open(destination, target_flags, 0o600)
        with os.fdopen(source_fd, "rb") as source_handle:
            source_fd = -1
            with os.fdopen(target_fd, "wb") as target_handle:
                target_fd = -1
                shutil.copyfileobj(source_handle, target_handle)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            _fsync_dir(destination.parent)
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if target_fd >= 0:
            os.close(target_fd)


@contextmanager
def _restore_lock(target_dir: Path, *, timeout_s: float = 10.0) -> Iterator[None]:
    """Serialize restore/recovery per target directory across processes.

    ``flock`` on a sidecar lock file; held for the whole verify-copy-switch
    (or recovery) sequence so concurrent restores cannot clean up each other's
    staging directories or race the journal.
    """
    lock_path = target_dir.parent / f"{target_dir.name}.restore-lock"
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as busy:
                if time.monotonic() >= deadline:
                    raise ConflictError(
                        "another restore or recovery holds the switch lock",
                        details={"target": str(target_dir)},
                    ) from busy
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)  # releases the flock


@dataclass(frozen=True, slots=True)
class BackupReport:
    backup_id: str
    directory: Path
    schema_version: int
    sqlite_runtime: str
    agent_watermarks: dict[str, int]
    tombstone_watermark: int
    duration_ms: int


@dataclass(frozen=True, slots=True)
class RestoreCheck:
    ok: bool
    problems: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RestoreReport:
    check: RestoreCheck
    target: Path
    duration_ms: int


@dataclass(frozen=True, slots=True)
class SmokeReport:
    read_watermarks: int
    wrote_probe_event: bool
    duration_ms: int


@dataclass(frozen=True, slots=True)
class RecoveryMeasurement:
    """RPO is the last committed transaction captured by the online backup;
    RTO is the measured restore + smoke duration for the declared dataset."""

    backup_ms: int
    restore_ms: int
    smoke_ms: int
    rpo_note: str


def _validated_manifest(value: object) -> dict[str, Any]:
    """Validate the manifest shape before any typed access or reconciliation."""
    if not isinstance(value, dict):
        raise ValueError("manifest must be a JSON object")
    files = value.get("files")
    if not isinstance(files, dict) or set(files) != {CANONICAL_NAME}:
        raise ValueError("manifest files must contain exactly canonical.sqlite3")
    canonical_digest = files.get(CANONICAL_NAME)
    if not isinstance(canonical_digest, str) or len(canonical_digest) != 64:
        raise ValueError("manifest canonical checksum must be SHA-256")
    try:
        int(canonical_digest, 16)
    except ValueError as error:
        raise ValueError("manifest canonical checksum must be hexadecimal") from error
    schema_version = value.get("schema_version")
    tombstone_watermark = value.get("tombstone_watermark")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or not isinstance(tombstone_watermark, int)
        or isinstance(tombstone_watermark, bool)
    ):
        raise ValueError("manifest schema and tombstone versions must be integers")
    watermarks = value.get("agent_watermarks")
    if not isinstance(watermarks, dict) or any(
        not isinstance(key, str) or not isinstance(item, int) or isinstance(item, bool)
        for key, item in watermarks.items()
    ):
        raise ValueError("manifest agent_watermarks must map strings to integers")
    return value


def _reconcile_manifest(canonical: Path, manifest: dict[str, Any]) -> tuple[str, ...]:
    """Compare manifest claims against the shipped snapshot's actual content.

    Pre-migration backups may ship older schemas; reconciliation reads only
    the tables the snapshot actually has and compares against empty state
    otherwise (the writer records empty maps for missing tables).
    """
    problems: list[str] = []
    connection = sqlite3.connect(f"file:{canonical}?mode=ro&immutable=1", uri=True)
    try:
        schema_row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()
        actual_schema = int(schema_row[0]) if schema_row is not None else 0
        if int(manifest.get("schema_version", -1)) != actual_schema:
            problems.append("manifest schema_version does not match the snapshot")

        def _has(table: str) -> bool:
            return (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
                ).fetchone()
                is not None
            )

        actual_watermarks = (
            {
                f"{row[0]}:{row[1]}": int(row[2])
                for row in connection.execute(
                    "SELECT tenant_id, agent_id, current_seq FROM agent_watermarks"
                )
            }
            if _has("agent_watermarks")
            else {}
        )
        raw_watermarks = manifest.get("agent_watermarks", {})
        manifest_watermarks = (
            {str(key): int(value) for key, value in raw_watermarks.items()}
            if isinstance(raw_watermarks, dict)
            else {}
        )
        if manifest_watermarks != actual_watermarks:
            problems.append("manifest agent watermarks do not match the snapshot")
        actual_tombstones = 0
        if _has("resource_tombstones"):
            tombstone_row = connection.execute(
                "SELECT COALESCE(MAX(tombstone_seq), 0) FROM resource_tombstones"
            ).fetchone()
            actual_tombstones = int(tombstone_row[0]) if tombstone_row is not None else 0
        if int(manifest.get("tombstone_watermark", -1)) != actual_tombstones:
            problems.append("manifest tombstone watermark does not match the snapshot")
        # Phase 5: artifact manifest and deletion-ledger watermark.
        if _has("artifacts"):
            actual_artifacts = {
                str(row[0]): {
                    "locator": str(row[1]),
                    "content_hash": str(row[2]),
                    "size_bytes": int(row[3]),
                }
                for row in connection.execute(
                    "SELECT id, locator, content_hash, size_bytes FROM artifacts "
                    "WHERE storage_kind = 'local_blob' AND status != 'tombstoned'"
                )
            }
        else:
            actual_artifacts = {}
        if manifest.get("artifacts", {}) != actual_artifacts:
            problems.append("manifest artifact inventory does not match the snapshot")
        if _has("forget_requests"):
            actual_ledger = _ledger_manifest_from_rows(_ledger_rows(connection))
        else:
            actual_ledger = {}
        manifest_ledger = manifest.get("forget_ledger_by_tenant", {})
        if _is_legacy_ledger_manifest(manifest_ledger):
            # Legacy int-format manifests recorded only the per-tenant
            # watermark; verify what they claim (the watermark), not the full
            # identity list the snapshot rows can now express. Restore then
            # replays the whole supplied ledger — idempotent, never misses.
            for tenant_id, watermark in manifest_ledger.items():
                actual_watermark = int(actual_ledger.get(str(tenant_id), {}).get("watermark_us", 0))
                if int(watermark) != actual_watermark:
                    problems.append(
                        "manifest deletion-ledger watermark does not match the snapshot"
                    )
                    break
            unknown = set(actual_ledger) - {str(key) for key in manifest_ledger}
            if unknown:
                problems.append("manifest deletion-ledger inventory does not match the snapshot")
        elif isinstance(manifest_ledger, dict):
            # Identity-carrying manifests (including the coarser shapes older
            # pre-release builds wrote): every claimed request must match a
            # snapshot identity on every field the claim names, tenant counts
            # must agree, and the snapshot must not carry unknown tenants.
            ledger_ok = True
            for tenant_id, entry in manifest_ledger.items():
                actual_entry = actual_ledger.get(str(tenant_id))
                if (
                    not isinstance(entry, dict)
                    or actual_entry is None
                    or int(entry.get("watermark_us", -1)) != int(actual_entry["watermark_us"])
                    or not isinstance(entry.get("requests"), list)
                    or len(entry["requests"]) != len(actual_entry["requests"])
                    or not all(
                        _ledger_claim_matches(item, actual_entry["requests"])
                        for item in entry["requests"]
                    )
                ):
                    ledger_ok = False
                    break
            if set(actual_ledger) - {str(key) for key in manifest_ledger}:
                ledger_ok = False
            if not ledger_ok:
                problems.append("manifest deletion-ledger inventory does not match the snapshot")
        else:
            problems.append("manifest deletion-ledger inventory does not match the snapshot")
    finally:
        connection.close()
    # Blob bytes are verified against the manifest digests (the manifest is
    # itself covered by checksums.txt / the authenticity tag).  The file-set
    # comparison is bidirectional: a missing artifacts/ directory must not
    # skip verification, and unlisted bytes must not ride into the live root.
    artifact_inventory = manifest.get("artifacts") or {}
    artifacts_dir = canonical.parent / ARTIFACTS_DIR_NAME
    expected_files: set[str] = set()
    if isinstance(artifact_inventory, dict):
        for artifact_id, meta in artifact_inventory.items():
            locator = str(meta.get("locator", "")) if isinstance(meta, dict) else ""
            try:
                normalize_local_locator(locator)
            except ArtifactLocatorError:
                problems.append(f"artifact manifest entry {artifact_id} has an unsafe locator")
                continue
            expected_files.add(locator)
            blob = artifacts_dir / locator
            if not blob.is_file():
                problems.append(f"artifact blob missing from backup: {artifact_id}")
                continue
            if _sha256_file(blob) != meta.get("content_hash"):
                problems.append(f"artifact blob checksum mismatch: {artifact_id}")
            if blob.stat().st_size != meta.get("size_bytes"):
                problems.append(f"artifact blob size mismatch: {artifact_id}")
    actual_files = (
        {
            str(path.relative_to(artifacts_dir))
            for path in artifacts_dir.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        if artifacts_dir.is_dir()
        else set()
    )
    for unexpected in sorted(actual_files - expected_files):
        problems.append(f"unexpected artifact blob in backup: {unexpected}")
    return tuple(problems)


def _ledger_rows(connection: sqlite3.Connection) -> list[tuple[Any, ...]]:
    """Read forget_requests identities with the columns the snapshot HAS.

    The identity columns (app_instance_id / idempotency_key / erase_content)
    were added while migration 0006 was still unreleased: snapshots written by
    those builds carry the legacy column set, and a hard SELECT on the new
    columns would fail the whole verify/restore for a structurally valid old
    backup. Missing columns read as the '' / 0 those builds would have
    recorded. Row shape: (tenant_id, selector_key, created_us, app_instance_id,
    idempotency_key, reason_code, erase_content).
    """
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(forget_requests)")}
    if not columns:
        return []
    select = (
        "SELECT tenant_id, selector_key, created_us, "
        + ("app_instance_id" if "app_instance_id" in columns else "''")
        + ", "
        + ("idempotency_key" if "idempotency_key" in columns else "''")
        + ", "
        + "reason_code, "
        + ("erase_content" if "erase_content" in columns else "0")
        + " FROM forget_requests"
    )
    return [tuple(row) for row in connection.execute(select)]


def _ledger_manifest_from_rows(rows: Sequence[tuple[Any, ...]]) -> dict[str, dict[str, Any]]:
    """Deletion-ledger inventory the manifest carries per tenant.

    The FULL set of request identities (app_instance_id, selector_key,
    created_us, idempotency_key, reason_code, erase_content) — not just a
    MAX(created_us) watermark — so a restore can replay by exact set
    difference: two forgets landing in the same microsecond around a backup
    are distinguished by identity, never by an ambiguous timestamp boundary,
    and two requests sharing selector+instant under different app instances,
    keys or modes stay two requests (§21.2, ADR-0013 §7/§10). Identities
    carry hashes/us only — no content.
    """
    by_tenant: dict[str, dict[str, Any]] = {}
    for row in rows:
        tenant_id = str(row[0])
        selector_key = str(row[1])
        created_us = int(row[2])
        entry = by_tenant.setdefault(tenant_id, {"watermark_us": 0, "requests": []})
        entry["watermark_us"] = max(int(entry["watermark_us"]), created_us)
        entry["requests"].append(
            {
                "app_instance_id": str(row[3]),
                "selector_key": selector_key,
                "created_us": created_us,
                "idempotency_key": str(row[4]),
                "reason_code": str(row[5]),
                "erase_content": bool(row[6]),
            }
        )
    for entry in by_tenant.values():
        entry["requests"].sort(
            key=lambda item: (
                item["created_us"],
                item["app_instance_id"],
                item["selector_key"],
                item["idempotency_key"],
                item["reason_code"],
            )
        )
    return by_tenant


def _is_legacy_ledger_manifest(value: Any) -> bool:
    """Legacy manifests carried only a per-tenant MAX(created_us) int."""
    return (
        isinstance(value, dict)
        and bool(value)
        and all(isinstance(item, int) and not isinstance(item, bool) for item in value.values())
    )


#: Request-entry fields a full-shape (identity-carrying) manifest entry has.
#: Coarser pre-release shapes named only a subset; those manifests cannot
#: feed exact set-difference replay and fall back to whole-ledger replay.
_LEDGER_IDENTITY_FIELDS = (
    "app_instance_id",
    "selector_key",
    "created_us",
    "idempotency_key",
    "reason_code",
    "erase_content",
)


def _ledger_claim_matches(claimed: Any, actual_requests: list[dict[str, Any]]) -> bool:
    """One manifest request entry against the snapshot's actual identities.

    Older builds' manifests claim only the identity fields they knew (the
    earliest carried just selector_key + created_us): a claim is verified on
    exactly the fields it names — the snapshot's extra identity fields are
    finer-grained knowledge, not a mismatch. A full-shape manifest entry must
    of course match on every field it carries."""
    if not isinstance(claimed, dict):
        return False
    for actual in actual_requests:
        agrees = True
        for key, value in claimed.items():
            if key not in actual or actual[key] != value:
                agrees = False
                break
        if agrees:
            return True
    return False


def write_backup_files(
    database: Path,
    destination: Path,
    backup_id: str,
    *,
    signing_key: bytes | None = None,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    """Snapshot ``database`` into ``destination`` and write manifest + checksums.

    Manifest metadata is read from the snapshot itself so a commit during the
    backup can never make the manifest describe state the file lacks. Used by
    both the scheduled backup service and the pre-migration CLI backup. With
    ``signing_key`` an HMAC tag over the payload digests is added — the only
    mechanism here that withstands an attacker who rewrites files and
    recomputes the in-directory checksums.

    The complete file set is built in a staging directory with per-file fsync,
    then published by a single atomic rename: a crash can never leave the
    catalog pointing at a half-written backup directory.
    """
    if destination.exists():
        raise FileExistsError(f"backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f"{destination.name}.staging-{backup_id}"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        manifest = _write_backup_files_into(
            database, staging, backup_id, signing_key, artifact_root=artifact_root
        )
        _, shape_problems = _inspect_backup_directory(staging)
        if shape_problems:
            raise OSError("generated backup has invalid file set: " + "; ".join(shape_problems))
        os.replace(staging, destination)
        _fsync_dir(destination.parent)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def _write_backup_files_into(
    database: Path,
    destination: Path,
    backup_id: str,
    signing_key: bytes | None,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    destination.mkdir(parents=True)
    target_path = destination / CANONICAL_NAME
    source = sqlite3.connect(database)
    try:
        target = sqlite3.connect(target_path)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    with target_path.open("rb") as handle:
        os.fsync(handle.fileno())
    _fsync_dir(destination)
    # The Online Backup API has materialized a complete main database file.
    # Open it immutable so its WAL-mode header cannot cause SQLite to create
    # unauthenticated -wal/-shm sidecars inside the backup set.
    snapshot = sqlite3.connect(f"file:{target_path}?mode=ro&immutable=1", uri=True)
    try:
        schema_version = current_schema_version(snapshot)

        # Pre-migration backups may snapshot older schemas; read watermark and
        # tombstone state only when the tables exist.
        def _has(table: str) -> bool:
            return (
                snapshot.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()
                is not None
            )

        if _has("agent_watermarks"):
            agent_watermarks = {
                f"{row[0]}:{row[1]}": int(row[2])
                for row in snapshot.execute(
                    "SELECT tenant_id, agent_id, current_seq FROM agent_watermarks"
                )
            }
        else:
            agent_watermarks = {}
        if _has("resource_tombstones"):
            tombstone_row = snapshot.execute(
                "SELECT COALESCE(MAX(tombstone_seq), 0) FROM resource_tombstones"
            ).fetchone()
        else:
            tombstone_row = (0,)
        # Phase 5: artifact manifest (local blobs only) and the deletion
        # ledger watermark — the boundary a restore replays from (§21.2).
        artifacts_manifest: dict[str, dict[str, Any]] = {}
        if _has("artifacts"):
            for art_id, locator, content_hash, size_bytes in snapshot.execute(
                "SELECT id, locator, content_hash, size_bytes FROM artifacts "
                "WHERE storage_kind = 'local_blob' AND status != 'tombstoned'"
            ):
                artifacts_manifest[str(art_id)] = {
                    "locator": str(locator),
                    "content_hash": str(content_hash),
                    "size_bytes": int(size_bytes),
                }
        # Missing table → no columns → no rows; the helper is its own guard.
        ledger_by_tenant = _ledger_manifest_from_rows(_ledger_rows(snapshot))
    finally:
        snapshot.close()
    if artifact_root is not None and artifacts_manifest:
        _copy_artifact_blobs(artifacts_manifest, artifact_root, destination)
    manifest: dict[str, Any] = {
        "backup_id": backup_id,
        "schema_version": schema_version,
        "sqlite_runtime": sqlite_runtime_version_string(),
        "agent_watermarks": agent_watermarks,
        "tombstone_watermark": int(tombstone_row[0]) if tombstone_row is not None else 0,
        "artifacts": artifacts_manifest,
        "forget_ledger_by_tenant": ledger_by_tenant,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": {CANONICAL_NAME: _sha256_file(target_path)},
    }
    _write_file_durable(
        destination / FINGERPRINT_NAME,
        json.dumps(
            {"config_version": 1, "secrets_included": False},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    _write_file_durable(
        destination / MANIFEST_NAME,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    checksum_names = list(_PAYLOAD_NAMES)
    if signing_key is not None:
        _write_file_durable(destination / AUTH_NAME, _auth_tag(destination, signing_key))
        checksum_names.append(AUTH_NAME)
    checksum_lines = [f"{_sha256_file(destination / name)}  {name}" for name in checksum_names]
    _write_file_durable(destination / CHECKSUMS_NAME, "\n".join(checksum_lines) + "\n")
    _fsync_dir(destination)
    return manifest


def _auth_tag(directory: Path, signing_key: bytes) -> str:
    """HMAC-SHA256 over the actual payload file digests (not checksums.txt,
    which an attacker can rewrite): the key is the external trust root."""
    payload = "".join(_sha256_file(directory / name) for name in _PAYLOAD_NAMES)
    return hmac.new(signing_key, payload.encode("ascii"), hashlib.sha256).hexdigest()


def create_standalone_backup(
    database: Path, destination: Path, *, signing_key: bytes | None = None
) -> dict[str, Any]:
    """Backup without a live Store (pre-migration CLI path)."""
    from iris_memory_core.application.ports import Uuid7Generator

    return write_backup_files(
        database, destination, str(Uuid7Generator().new()), signing_key=signing_key
    )


def sqlite_runtime_version_string() -> str:
    import sqlite3 as _sqlite3

    return ".".join(str(part) for part in _sqlite3.sqlite_version_info[:3])


def verify_backup(backup_dir: Path, *, signing_key: bytes | None = None) -> RestoreCheck:
    """Verify file coverage, checksums, authenticity and manifest reconciliation.

    Without ``signing_key`` this proves accidental-corruption detection only;
    with a key the stored HMAC tag is recomputed from the actual payload file
    digests, so rewritten files plus recomputed checksums no longer verify.
    Unreadable content (corrupt JSON, non-database canonical, I/O errors) is a
    failed check, never an exception.
    """
    problems: list[str] = []
    manifest_path = backup_dir / MANIFEST_NAME
    checksums_path = backup_dir / CHECKSUMS_NAME
    canonical = backup_dir / CANONICAL_NAME
    try:
        safe_names, shape_problems = _inspect_backup_directory(backup_dir)
        problems.extend(shape_problems)
        if problems:
            return RestoreCheck(False, tuple(problems))
        manifest = _validated_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
        # Every payload file must be covered by checksums.txt and match it.
        listed: set[str] = set()
        expected_names = set(_PAYLOAD_NAMES)
        if AUTH_NAME in safe_names:
            expected_names.add(AUTH_NAME)
        for line in checksums_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            expected, _, name = line.partition("  ")
            name = name.strip()
            if name not in expected_names:
                problems.append(f"unexpected checksums.txt entry {name!r}")
                continue
            if name in listed:
                problems.append(f"duplicate checksums.txt entry {name}")
                continue
            if len(expected) != 64:
                problems.append(f"invalid SHA-256 checksum for {name}")
                continue
            try:
                int(expected, 16)
            except ValueError:
                problems.append(f"invalid SHA-256 checksum for {name}")
                continue
            listed.add(name)
            if _sha256_file(backup_dir / name) != expected:
                problems.append(f"checksum mismatch for {name}")
        for required_name in sorted(expected_names):
            if required_name not in listed:
                problems.append(f"{required_name} is not covered by checksums.txt")
        declared = manifest["files"][CANONICAL_NAME]
        if declared != _sha256_file(canonical):
            problems.append("manifest checksum mismatch")
        if signing_key is not None:
            tag_path = backup_dir / AUTH_NAME
            if AUTH_NAME not in safe_names:
                problems.append("backup carries no authenticity tag but a signing key was given")
            elif not hmac.compare_digest(
                tag_path.read_text(encoding="utf-8").strip(), _auth_tag(backup_dir, signing_key)
            ):
                problems.append("backup authenticity tag mismatch (tampered or wrong key)")
        # The manifest must describe exactly the snapshot it ships with.
        problems.extend(_reconcile_manifest(canonical, manifest))
    except (
        AttributeError,
        json.JSONDecodeError,
        sqlite3.Error,
        OSError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
    ) as error:
        problems.append(f"backup unreadable: {error}")
    return RestoreCheck(not problems, tuple(problems))


def _journal_path(target_dir: Path) -> Path:
    return target_dir.parent / f"{target_dir.name}{JOURNAL_SUFFIX}"


def _staging_dir(target_dir: Path) -> Path:
    return target_dir.parent / f"{target_dir.name}.restoring"


def _trusted_journal_paths(target_dir: Path, state: object) -> tuple[Path, Path | None] | None:
    """Resolve journal entries, confining them to names this module writes.

    The journal is attacker-writable disk; ``{"staging": "../victim"}`` must
    never turn recovery into a delete primitive outside the target's parent.
    Only the exact staging name and ``.previous-*`` aside names this module
    produces are accepted, as plain basenames and never symlinks.
    """
    if not isinstance(state, dict):
        return None
    staging_name = state.get("staging")
    aside_name = state.get("aside")
    if not isinstance(staging_name, str) or staging_name != _staging_dir(target_dir).name:
        return None
    staging = _staging_dir(target_dir)
    if staging.is_symlink():
        return None
    aside: Path | None = None
    if aside_name is not None:
        if (
            not isinstance(aside_name, str)
            or Path(aside_name).name != aside_name
            or not aside_name.startswith(f"{target_dir.name}.previous-")
        ):
            return None
        aside = target_dir.parent / aside_name
        if aside.is_symlink():
            return None
    return staging, aside


def recover_pending_switch(target_dir: Path) -> str:
    """Finish or roll back a restore switch interrupted by a crash (serialized)."""
    with _restore_lock(target_dir):
        return _recover_pending_switch_locked(target_dir)


def _recover_pending_switch_locked(target_dir: Path) -> str:
    """Finish or roll back a restore switch interrupted by a crash.

    The journal is written (atomically) before the first rename and removed
    only after the switch completes, so its presence marks an interrupted
    switch exactly. Returns one of ``none | completed | rolled_back | aborted
    | invalid_journal | cleanup_failed``:

    - target present, staging gone — the switch had completed; clean up.
    - staging present, target missing — finish the second rename.
    - staging gone, target missing, aside present — roll back to the aside.
    - target and staging both present — the crash happened before the switch
      started; keep the current target and discard the staged copy.
    - ``invalid_journal`` — unreadable or untrusted journal content; nothing
      is touched and the journal is left for operator inspection.
    - ``cleanup_failed`` — a trusted state was identified but stale staging or
      aside content could not be removed; the journal is retained so recovery
      can be retried and no orphaned state is forgotten.
    """
    journal = _journal_path(target_dir)
    if journal.is_symlink():
        return "invalid_journal"
    if not journal.exists():
        return "none"
    if not journal.is_file():
        return "invalid_journal"
    try:
        state = json.loads(journal.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return "invalid_journal"
    resolved = _trusted_journal_paths(target_dir, state)
    if resolved is None:
        return "invalid_journal"
    staging, aside = resolved
    if target_dir.exists() and not staging.exists():
        outcome = "completed"
    elif staging.exists() and not target_dir.exists():
        os.replace(staging, target_dir)
        outcome = "completed"
    elif not staging.exists() and not target_dir.exists() and aside is not None and aside.exists():
        os.replace(aside, target_dir)
        outcome = "rolled_back"
    else:
        if staging.exists():
            try:
                shutil.rmtree(staging)
            except OSError:
                return "cleanup_failed"
            if staging.exists():
                return "cleanup_failed"
        outcome = "aborted"
    _fsync_dir(target_dir.parent)
    if outcome == "completed" and aside is not None and aside.exists():
        try:
            shutil.rmtree(aside)
        except OSError:
            return "cleanup_failed"
        if aside.exists():
            return "cleanup_failed"
        _fsync_dir(target_dir.parent)
    journal.unlink(missing_ok=True)
    _fsync_dir(target_dir.parent)
    return outcome


def _restore_failure(target_dir: Path, started: float, *problems: str) -> RestoreReport:
    return RestoreReport(
        RestoreCheck(False, tuple(problems)),
        target_dir,
        int((time.monotonic() - started) * 1000),
    )


def _reset_fts_projection_for_restore(database: Path) -> None:
    """Reset any FTS projection bytes the backup carried (ADR-0014 §9).

    The FTS index — documents, generations, pointers and the virtual table —
    is never restored as a source of truth: everything is dropped and the
    state marker flips to ``pending_rebuild``. Snapshots older than Schema 7
    have no FTS tables at all and are left untouched (the startup migration
    then creates them in ``never_built``, which equally forces a rebuild).
    """
    connection = sqlite3.connect(database, isolation_level=None)
    try:
        has_fts = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fts_documents'"
            ).fetchone()
            is not None
        )
        if not has_fts:
            return
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP TABLE IF EXISTS fts_index")
        connection.execute("DELETE FROM fts_documents")
        connection.execute("DELETE FROM fts_current")
        connection.execute("DELETE FROM fts_generations")
        connection.execute(
            "INSERT INTO fts_projection_state (id, state, marked_us) VALUES (1, "
            "'pending_rebuild', CAST(strftime('%s', 'now') AS INTEGER) * 1000000) "
            "ON CONFLICT(id) DO UPDATE SET state = 'pending_rebuild', "
            "marked_us = CAST(strftime('%s', 'now') AS INTEGER) * 1000000"
        )
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _reset_vector_projection_for_restore(database: Path) -> None:
    """Reset any vector projection metadata the backup carried (ADR-0015 §10).

    The FAISS generation files never travel inside a backup, so generation
    rows, the pointer and the delta ledger must not survive a restore — a
    pointer to missing files would poison the first load. The ID MAP and the
    surrogate counter SURVIVE: surrogate assignment stays stable across
    restores (server-assigned, never reused). Snapshots older than Schema 8
    have no vector tables and are left untouched (the startup migration
    creates them in ``never_built``).
    """
    connection = sqlite3.connect(database, isolation_level=None)
    try:
        has_vector = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vector_generations'"
            ).fetchone()
            is not None
        )
        if not has_vector:
            return
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM vector_delta_ledger")
        connection.execute("DELETE FROM vector_current")
        connection.execute("DELETE FROM vector_generations")
        connection.execute(
            "INSERT INTO vector_projection_state (id, state, marked_us, "
            "last_surrogate_id) VALUES (1, 'pending_rebuild', "
            "CAST(strftime('%s', 'now') AS INTEGER) * 1000000, "
            "COALESCE((SELECT last_surrogate_id FROM vector_projection_state WHERE id = 1), 0)) "
            "ON CONFLICT(id) DO UPDATE SET state = 'pending_rebuild', "
            "marked_us = CAST(strftime('%s', 'now') AS INTEGER) * 1000000"
        )
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _discard_staging(staging: Path) -> str | None:
    """Best-effort cleanup whose failure is visible to the caller."""
    if not staging.exists():
        return None
    try:
        shutil.rmtree(staging)
    except OSError as error:
        return f"failed to clean restore staging directory: {error}"
    if staging.exists():
        return "failed to clean restore staging directory"
    return None


def restore_backup(
    backup_dir: Path,
    target_dir: Path,
    *,
    signing_key: bytes | None = None,
    artifact_root: Path | None = None,
    prepare_staging: Callable[[Path], None] | None = None,
) -> RestoreReport:
    """Copy into isolation, verify THE COPIED BYTES, then switch recoverably.

    Verification runs against the staging copy — the exact bytes that will be
    switched in — so mutating the backup directory after a previous verify
    (TOCTOU) can never restore unauthenticated content. The switch is two
    renames around an atomically written journal; a crash at any point leaves
    a state ``recover_pending_switch`` resolves deterministically at startup.
    The whole sequence is serialized per target directory.
    """
    started = time.monotonic()
    expected_artifact_root = target_dir / ARTIFACTS_DIR_NAME
    if artifact_root is not None and artifact_root.absolute() != expected_artifact_root.absolute():
        return _restore_failure(
            target_dir,
            started,
            "artifact root must be inside the restored target directory",
        )
    with _restore_lock(target_dir):
        pending = _recover_pending_switch_locked(target_dir)
        if pending == "invalid_journal":
            return _restore_failure(
                target_dir, started, "restore journal is unreadable or untrusted"
            )
        if pending == "cleanup_failed":
            return _restore_failure(
                target_dir, started, "pending restore state could not be cleaned; journal retained"
            )
        staging = _staging_dir(target_dir)
        try:
            if staging.is_symlink() or (staging.exists() and not staging.is_dir()):
                return _restore_failure(
                    target_dir, started, "restore staging path is not a real directory"
                )
            cleanup_problem = _discard_staging(staging)
            if cleanup_problem is not None:
                return _restore_failure(target_dir, started, cleanup_problem)
            safe_names, shape_problems = _inspect_backup_directory(backup_dir)
            if shape_problems:
                return _restore_failure(target_dir, started, *shape_problems)
            # A private, module-owned staging directory is the trust boundary:
            # untrusted source bytes are copied once and all later checks read
            # only this exact file set. Unknown files (especially SQLite WAL/
            # journal sidecars) are rejected instead of copied.
            staging.mkdir(parents=True, mode=0o700)
            staging.chmod(0o700)
            for name in safe_names:
                _copy_regular_file(backup_dir / name, staging / name)
            source_artifacts = backup_dir / ARTIFACTS_DIR_NAME
            if source_artifacts.is_dir() and not source_artifacts.is_symlink():
                # Blob bytes land in staging too; verification hashes them
                # against the manifest before anything switches in.
                for blob in sorted(source_artifacts.rglob("*")):
                    if blob.is_file() and not blob.is_symlink():
                        relative = blob.relative_to(source_artifacts)
                        target_blob = staging / ARTIFACTS_DIR_NAME / relative
                        target_blob.parent.mkdir(parents=True, exist_ok=True)
                        _copy_regular_file(blob, target_blob)
            _fsync_dir(staging)
        except (OSError, ValueError) as error:
            cleanup_problem = _discard_staging(staging)
            problems = [f"backup unreadable: {error}"]
            if cleanup_problem is not None:
                problems.append(cleanup_problem)
            return _restore_failure(target_dir, started, *problems)
        # Verify the staged bytes: checksums, authenticity tag, manifest
        # reconciliation — everything — against what will actually switch in.
        check = verify_backup(staging, signing_key=signing_key)
        if not check.ok:
            cleanup_problem = _discard_staging(staging)
            problems = list(check.problems)
            if cleanup_problem is not None:
                problems.append(cleanup_problem)
            return _restore_failure(target_dir, started, *problems)
        try:
            invariant_problems = verify_database_invariants(staging / CANONICAL_NAME)
        except (sqlite3.Error, OSError, TypeError, ValueError) as error:
            invariant_problems = (f"backup unreadable: {error}",)
        if invariant_problems:
            cleanup_problem = _discard_staging(staging)
            problems = list(invariant_problems)
            if cleanup_problem is not None:
                problems.append(cleanup_problem)
            return _restore_failure(target_dir, started, *problems)
        # Re-read every authenticated byte after invariant inspection. This
        # closes mutation by inspection code itself; combined with the 0700
        # staging directory, no untrusted source writer can alter the set in
        # the verify-to-switch interval.
        final_check = verify_backup(staging, signing_key=signing_key)
        if not final_check.ok:
            cleanup_problem = _discard_staging(staging)
            problems = list(final_check.problems)
            if cleanup_problem is not None:
                problems.append(cleanup_problem)
            return _restore_failure(target_dir, started, *problems)
        # A handful of pre-release Phase 5 builds wrote structurally older
        # tables while already recording schema version 6.  The authenticated
        # source bytes have now passed both verification passes, so normalize
        # only the private staging copy before it can become the live target.
        # This also advances the recorded 0006 checksum to the exact current
        # shape; the ordinary migration runner can then start the restored DB.
        try:
            normalized = normalize_prerelease_phase5_schema(staging / CANONICAL_NAME)
            if normalized:
                _checkpoint_database_for_switch(staging / CANONICAL_NAME)
            normalized_problems = (
                verify_database_invariants(staging / CANONICAL_NAME) if normalized else ()
            )
        except (MigrationError, sqlite3.Error, OSError, TypeError, ValueError) as error:
            normalized_problems = (f"backup schema normalization failed: {error}",)
        if normalized_problems:
            cleanup_problem = _discard_staging(staging)
            problems = list(normalized_problems)
            if cleanup_problem is not None:
                problems.append(cleanup_problem)
            return _restore_failure(target_dir, started, *problems)
        # Trusted callers may apply deterministic, local recovery work (most
        # importantly deletion-ledger replay) to the isolated copy.  A failure
        # discards staging and leaves the previous target untouched; no
        # incompletely replayed database is ever made visible.
        if prepare_staging is not None:
            try:
                prepare_staging(staging)
                _checkpoint_database_for_switch(staging / CANONICAL_NAME)
                prepared_problems = verify_database_invariants(staging / CANONICAL_NAME)
            # ``prepare_staging`` is an internal extension point, but no
            # callback bug may bypass staging cleanup or partially expose a
            # restore.  Convert every ordinary exception into a failed report;
            # process-control exceptions still propagate.
            except Exception as error:
                prepared_problems = (f"backup staging preparation failed: {error}",)
            if prepared_problems:
                cleanup_problem = _discard_staging(staging)
                problems = list(prepared_problems)
                if cleanup_problem is not None:
                    problems.append(cleanup_problem)
                return _restore_failure(target_dir, started, *problems)
        # Phase 6 (ADR-0014 §9): reset any FTS projection bytes the backup
        # carried and mark the rebuild pending — the FTS index is never
        # restored as a source of truth. Snapshots older than Schema 7 carry
        # no FTS tables; the startup migration leaves them in ``never_built``,
        # which equally requires a rebuild before the FTS route can serve.
        try:
            _reset_fts_projection_for_restore(staging / CANONICAL_NAME)
            _reset_vector_projection_for_restore(staging / CANONICAL_NAME)
            _checkpoint_database_for_switch(staging / CANONICAL_NAME)
            phase6_problems = verify_database_invariants(staging / CANONICAL_NAME)
        except (sqlite3.Error, OSError, TypeError, ValueError) as error:
            phase6_problems = (f"restore projection reset failed: {error}",)
        if phase6_problems:
            cleanup_problem = _discard_staging(staging)
            problems = list(phase6_problems)
            if cleanup_problem is not None:
                problems.append(cleanup_problem)
            return _restore_failure(target_dir, started, *problems)
        aside = target_dir.parent / f"{target_dir.name}.previous-{int(time.time())}"
        had_previous = target_dir.exists()
        journal = _journal_path(target_dir)
        _write_file_durable(
            journal,
            json.dumps({"staging": staging.name, "aside": aside.name if had_previous else None})
            + "\n",
        )
        _fsync_dir(target_dir.parent)
        try:
            if had_previous:
                os.replace(target_dir, aside)
            try:
                os.replace(staging, target_dir)
            except OSError:
                if had_previous and not target_dir.exists():
                    os.replace(aside, target_dir)  # roll back to the previous state
                raise
            _fsync_dir(target_dir.parent)
            if had_previous and aside.exists():
                try:
                    shutil.rmtree(aside)
                except OSError as error:
                    # The new target is already durable. Keep the journal so
                    # recover_pending_switch can retry cleanup and does not
                    # silently forget the previous tree.
                    return _restore_failure(
                        target_dir,
                        started,
                        f"restore switched but previous target cleanup failed: {error}; "
                        "journal retained",
                    )
                _fsync_dir(target_dir.parent)
            journal.unlink(missing_ok=True)
            _fsync_dir(target_dir.parent)
        except BaseException:
            # The journal stays on disk: recover_pending_switch resolves the
            # interrupted switch deterministically at next startup.
            raise
        return RestoreReport(final_check, target_dir, int((time.monotonic() - started) * 1000))


def verify_database_invariants(database: Path) -> tuple[str, ...]:
    """Structural checks an isolated restore must pass before switching.

    Tolerates pre-migration snapshots: checks that need Phase 1 tables are
    skipped when the table does not exist, so a Phase 0 backup verifies (and
    restores) on its own schema. The schema window allows version 1 for such
    backups — the runtime Ready path still enforces the current window after
    the operator migrates the restored database.
    """
    problems: list[str] = []
    connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]) != "ok":
            problems.append(f"integrity_check failed: {integrity}")
        for row in connection.execute("PRAGMA foreign_key_check"):
            problems.append(f"foreign key violation: {tuple(row)}")
        schema_row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()
        schema_version = int(schema_row[0]) if schema_row is not None else 0
        from iris_memory_core.storage.runtime import SUPPORTED_SCHEMA_MAX

        if not 1 <= schema_version <= SUPPORTED_SCHEMA_MAX:
            problems.append(f"schema version {schema_version} outside supported window")

        def _has(table: str) -> bool:
            return (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
                ).fetchone()
                is not None
            )

        if _has("agents") and _has("persona_revisions"):
            dangling = connection.execute(
                "SELECT COUNT(*) FROM agents a WHERE a.persona_current_revision_id IS NULL "
                "OR a.persona_current_revision_id NOT IN (SELECT id FROM persona_revisions)"
            ).fetchone()
            if dangling is not None and int(dangling[0]) > 0:
                problems.append("agents with missing persona current pointer")
            # The pointer must reference that agent's own published revision in
            # the same tenant — not another agent's, and not a draft/retired one.
            misowned = connection.execute(
                "SELECT COUNT(*) FROM agents a JOIN persona_revisions p "
                "ON p.id = a.persona_current_revision_id "
                "WHERE p.agent_id <> a.id OR p.tenant_id <> a.tenant_id "
                "OR p.status <> 'published'"
            ).fetchone()
            if misowned is not None and int(misowned[0]) > 0:
                problems.append(
                    "persona current pointer must reference the agent's own published revision"
                )
        if _has("bindings") and _has("external_identities"):
            broken_bindings = connection.execute(
                "SELECT COUNT(*) FROM bindings b JOIN external_identities i "
                "ON i.id = b.external_identity_id WHERE b.tenant_id <> i.tenant_id"
            ).fetchone()
            if broken_bindings is not None and int(broken_bindings[0]) > 0:
                problems.append("bindings crossing tenants")
        if _has("resource_tombstones"):
            tombstone_dupes = connection.execute(
                "SELECT COUNT(*) FROM (SELECT tenant_id, resource_type, resource_id, COUNT(*) c "
                "FROM resource_tombstones GROUP BY 1, 2, 3 HAVING c > 1)"
            ).fetchone()
            if tombstone_dupes is not None and int(tombstone_dupes[0]) > 0:
                problems.append("duplicate tombstones")
        # Phase 2 spine invariants (§21 restore step 3): skipped for older
        # snapshots whose schema predates these tables.
        if _has("schedule_ticks") and _has("outbox_jobs"):
            orphan_ticks = connection.execute(
                "SELECT COUNT(*) FROM schedule_ticks t WHERE t.outbox_id IS NOT NULL "
                "AND t.outbox_id NOT IN (SELECT id FROM outbox_jobs)"
            ).fetchone()
            if orphan_ticks is not None and int(orphan_ticks[0]) > 0:
                problems.append("ticks referencing missing outbox jobs")
            unfulfilled_ticks = connection.execute(
                "SELECT COUNT(*) FROM schedule_ticks WHERE status = 'enqueued' "
                "AND outbox_id IS NULL"
            ).fetchone()
            if unfulfilled_ticks is not None and int(unfulfilled_ticks[0]) > 0:
                problems.append("enqueued ticks without an outbox job")
        if _has("outbox_jobs"):
            bad_completion = connection.execute(
                "SELECT COUNT(*) FROM outbox_jobs WHERE status = 'completed' "
                "AND completed_us IS NULL"
            ).fetchone()
            if bad_completion is not None and int(bad_completion[0]) > 0:
                problems.append("completed outbox jobs without completion time")
            fenced_residue = connection.execute(
                "SELECT COUNT(*) FROM outbox_jobs WHERE status = 'completed' "
                "AND (lease_owner IS NOT NULL OR lease_expires_us IS NOT NULL)"
            ).fetchone()
            if fenced_residue is not None and int(fenced_residue[0]) > 0:
                problems.append("completed outbox jobs still holding lease fields")
        if _has("surface_leases") and _has("surface_lease_state"):
            double_active = connection.execute(
                "SELECT COUNT(*) FROM (SELECT tenant_id, agent_id, COUNT(*) c "
                "FROM surface_leases WHERE status = 'active' GROUP BY 1, 2 HAVING c > 1)"
            ).fetchone()
            if double_active is not None and int(double_active[0]) > 0:
                problems.append("more than one active surface lease per agent")
            epoch_regression = connection.execute(
                "SELECT COUNT(*) FROM surface_lease_state s WHERE s.current_epoch < "
                "(SELECT COALESCE(MAX(l.lease_epoch), 0) FROM surface_leases l "
                "WHERE l.tenant_id = s.tenant_id AND l.agent_id = s.agent_id)"
            ).fetchone()
            if epoch_regression is not None and int(epoch_regression[0]) > 0:
                problems.append("surface lease epoch regressed below issued epochs")
        # Phase 3 invariants (§9, ADR-0004): skipped for older snapshots whose
        # schema predates these tables.
        if _has("state_records") and _has("state_record_revisions"):
            dangling_state = connection.execute(
                "SELECT COUNT(*) FROM state_records r WHERE r.current_revision_id = '' "
                "OR r.current_revision_id NOT IN (SELECT id FROM state_record_revisions)"
            ).fetchone()
            if dangling_state is not None and int(dangling_state[0]) > 0:
                problems.append("state current pointers without their revision row")
            state_pointer_mismatch = connection.execute(
                "SELECT COUNT(*) FROM state_records r JOIN state_record_revisions v "
                "ON v.id = r.current_revision_id WHERE v.revision <> r.current_revision "
                "OR v.record_id <> r.id"
            ).fetchone()
            if state_pointer_mismatch is not None and int(state_pointer_mismatch[0]) > 0:
                problems.append("state current pointer revision mismatch")
        if _has("focus_items") and _has("focus_item_revisions"):
            dangling_focus = connection.execute(
                "SELECT COUNT(*) FROM focus_items f WHERE f.current_revision_id = '' "
                "OR f.current_revision_id NOT IN (SELECT id FROM focus_item_revisions)"
            ).fetchone()
            if dangling_focus is not None and int(dangling_focus[0]) > 0:
                problems.append("focus current pointers without their revision row")
            focus_pointer_mismatch = connection.execute(
                "SELECT COUNT(*) FROM focus_items f JOIN focus_item_revisions v "
                "ON v.id = f.current_revision_id WHERE v.revision <> f.current_revision "
                "OR v.item_id <> f.id"
            ).fetchone()
            if focus_pointer_mismatch is not None and int(focus_pointer_mismatch[0]) > 0:
                problems.append("focus current pointer revision mismatch")
        if _has("recent_context_current") and _has("recent_context_generations"):
            dangling_generation = connection.execute(
                "SELECT COUNT(*) FROM recent_context_current c WHERE "
                "c.current_generation_id NOT IN (SELECT id FROM recent_context_generations)"
            ).fetchone()
            if dangling_generation is not None and int(dangling_generation[0]) > 0:
                problems.append("recent context pointers without their generation")
            unverified_pointer = connection.execute(
                "SELECT COUNT(*) FROM recent_context_current c JOIN "
                "recent_context_generations g ON g.id = c.current_generation_id "
                "WHERE g.status <> 'verified'"
            ).fetchone()
            if unverified_pointer is not None and int(unverified_pointer[0]) > 0:
                problems.append("recent context pointer references an unverified generation")
            misbound_pointer = connection.execute(
                "SELECT COUNT(*) FROM recent_context_current c JOIN "
                "recent_context_generations g ON g.id = c.current_generation_id WHERE "
                "c.target_key <> g.target_key OR c.tenant_id <> g.tenant_id "
                "OR c.agent_id <> g.agent_id OR c.space_id <> g.space_id "
                "OR COALESCE(c.session_id, '') <> COALESCE(g.session_id, '') "
                "OR COALESCE(c.space_group_id, '') <> COALESCE(g.space_group_id, '')"
            ).fetchone()
            if misbound_pointer is not None and int(misbound_pointer[0]) > 0:
                problems.append("recent context pointer target does not match its generation")
        if _has("recent_context_generations") and _has("observations"):
            import json as _json

            from iris_memory_core.domain.recent import (
                BuiltProjection,
                ObservationRef,
                SummarySegment,
                projection_invariants,
            )

            for row in connection.execute(
                "SELECT tenant_id, agent_id, space_id, session_id, builder_version, "
                "source_watermark, head_observation_id, tail_observation_id, "
                "hot_observation_refs, summary_segments, token_estimate, result_hash "
                "FROM recent_context_generations WHERE status = 'verified'"
            ).fetchall():
                try:
                    hot_raw = _json.loads(row[8])
                    segments_raw = _json.loads(row[9])
                    hot = tuple(
                        ObservationRef(
                            observation_id=item["observation_id"],
                            revision=int(item["revision"]),
                            occurred_us=int(item["occurred_us"]),
                            token_estimate=int(item["token_estimate"]),
                        )
                        for item in hot_raw
                    )
                    segments = tuple(
                        SummarySegment(
                            segment_id=segment["segment_id"],
                            source_refs=tuple(
                                ObservationRef(
                                    observation_id=ref["observation_id"],
                                    revision=int(ref["revision"]),
                                    occurred_us=int(ref["occurred_us"]),
                                    token_estimate=int(ref["token_estimate"]),
                                )
                                for ref in segment["source_refs"]
                            ),
                            token_estimate=int(segment["token_estimate"]),
                        )
                        for segment in segments_raw
                    )
                    projection = BuiltProjection(
                        builder_version=int(row[4]),
                        source_watermark=int(row[5]),
                        head_observation_id=row[6],
                        tail_observation_id=row[7],
                        hot_observation_refs=hot,
                        summary_segments=segments,
                        token_estimate=int(row[10]),
                        result_hash=str(row[11]),
                    )
                except (KeyError, TypeError, ValueError, _json.JSONDecodeError):
                    problems.append("verified recent generation has invalid projection encoding")
                    break
                if projection_invariants(projection):
                    problems.append("verified recent generation failed projection invariants")
                    break
                missing_or_mismatched = 0
                for ref in projection.referenced_refs():
                    found = connection.execute(
                        "SELECT COUNT(*) FROM observations WHERE id = ? AND tenant_id = ? "
                        "AND agent_id = ? AND space_id = ? "
                        "AND COALESCE(session_id, '') = COALESCE(?, '') "
                        "AND revision = ? AND occurred_us = ?",
                        (
                            ref.observation_id,
                            row[0],
                            row[1],
                            row[2],
                            row[3],
                            ref.revision,
                            ref.occurred_us,
                        ),
                    ).fetchone()
                    if found is None or int(found[0]) != 1:
                        missing_or_mismatched += 1
                if missing_or_mismatched:
                    problems.append(
                        "verified recent generation references missing or mismatched observations"
                    )
                    break
        # Phase 4 invariants (S10-S12, ADR-0004): every mutable aggregate's
        # current pointer must resolve to its own revision row; occurrences
        # must resolve to their trigger; occurrence-attached events must
        # resolve. Skipped for older snapshots whose schema predates them.
        for current_table, revision_table, join_key, label in (
            ("notes", "note_revisions", "note_id", "note"),
            ("tasks", "task_revisions", "task_id", "task"),
            ("task_steps", "task_step_revisions", "step_id", "task step"),
            (
                "task_dependencies",
                "task_dependency_revisions",
                "dependency_id",
                "task dependency",
            ),
            ("task_triggers", "task_trigger_revisions", "trigger_id", "trigger"),
            ("cognitive_events", "cognitive_event_revisions", "event_id", "cognitive event"),
            ("episodes", "episode_revisions", "episode_id", "episode"),
            ("claims", "claim_revisions", "claim_id", "claim"),
            ("relations", "relation_revisions", "relation_id", "relation"),
        ):
            if not (_has(current_table) and _has(revision_table)):
                continue
            dangling = connection.execute(
                f"SELECT COUNT(*) FROM {current_table} c WHERE c.current_revision_id = '' "
                f"OR c.current_revision_id NOT IN (SELECT id FROM {revision_table})"
            ).fetchone()
            if dangling is not None and int(dangling[0]) > 0:
                problems.append(f"{label} current pointers without their revision row")
            mismatch = connection.execute(
                f"SELECT COUNT(*) FROM {current_table} c JOIN {revision_table} v "
                f"ON v.id = c.current_revision_id WHERE v.revision <> c.current_revision "
                f"OR v.{join_key} <> c.id"
            ).fetchone()
            if mismatch is not None and int(mismatch[0]) > 0:
                problems.append(f"{label} current pointer revision mismatch")
        if _has("task_trigger_occurrences") and _has("task_triggers"):
            orphan_occurrence = connection.execute(
                "SELECT COUNT(*) FROM task_trigger_occurrences o WHERE o.trigger_id "
                "NOT IN (SELECT id FROM task_triggers)"
            ).fetchone()
            if orphan_occurrence is not None and int(orphan_occurrence[0]) > 0:
                problems.append("trigger occurrences without their trigger row")
            bad_event_ref = connection.execute(
                "SELECT COUNT(*) FROM task_trigger_occurrences o WHERE "
                "o.cognitive_event_id IS NOT NULL AND o.cognitive_event_id NOT IN "
                "(SELECT id FROM cognitive_events)"
            ).fetchone()
            if bad_event_ref is not None and int(bad_event_ref[0]) > 0:
                problems.append("trigger occurrences referencing a missing cognitive event")
        if _has("task_steps") and _has("tasks"):
            orphan_step = connection.execute(
                "SELECT COUNT(*) FROM task_steps s WHERE s.task_id NOT IN (SELECT id FROM tasks)"
            ).fetchone()
            if orphan_step is not None and int(orphan_step[0]) > 0:
                problems.append("task steps without their task row")
        if _has("cognitive_events"):
            acknowledged_without_ack = connection.execute(
                "SELECT COUNT(*) FROM cognitive_events WHERE status = 'acknowledged' "
                "AND ack_id IS NULL"
            ).fetchone()
            if acknowledged_without_ack is not None and int(acknowledged_without_ack[0]) > 0:
                problems.append("acknowledged cognitive events without an ack id")
            terminal_with_lease = connection.execute(
                "SELECT COUNT(*) FROM cognitive_events WHERE status IN "
                "('acknowledged', 'expired', 'cancelled') AND delivered_lease_id IS NOT NULL"
            ).fetchone()
            if terminal_with_lease is not None and int(terminal_with_lease[0]) > 0:
                problems.append("terminal cognitive events still holding a delivery lease")
        # Phase 5 invariants (ADR-0013 §9): active claims carry valid
        # evidence, evidence rows resolve to live non-tombstoned sources,
        # claim system-time stamps are monotone, and the forget ledger stays
        # anchored to the tombstone sequence it claims.
        if _has("claims") and _has("claim_evidence"):
            miscounted = connection.execute(
                "SELECT COUNT(*) FROM claims c WHERE c.status != 'tombstoned' AND "
                "c.evidence_count <> (SELECT COUNT(*) FROM claim_evidence e "
                "WHERE e.claim_id = c.id AND e.invalidated_us IS NULL)"
            ).fetchone()
            if miscounted is not None and int(miscounted[0]) > 0:
                problems.append("claim evidence_count does not match its valid evidence rows")
            for source_table, source_type in (
                ("observations", "observation"),
                ("artifacts", "artifact"),
                ("episodes", "episode"),
                ("notes", "note"),
                ("claims", "claim"),
            ):
                if not _has(source_table):
                    continue
                dead = connection.execute(
                    "SELECT COUNT(*) FROM claim_evidence e WHERE e.source_type = ? "
                    "AND e.invalidated_us IS NULL AND ("
                    "e.source_id NOT IN (SELECT id FROM " + source_table + ") OR EXISTS ("
                    "SELECT 1 FROM resource_tombstones rt WHERE rt.tenant_id = e.tenant_id "
                    "AND rt.resource_type = ? AND rt.resource_id = e.source_id))",
                    (source_type, source_type),
                ).fetchone()
                if dead is not None and int(dead[0]) > 0:
                    problems.append(
                        f"valid evidence rows citing a missing or tombstoned {source_type}"
                    )
            non_monotone = connection.execute(
                "SELECT COUNT(*) FROM claim_revisions WHERE superseded_at_us IS NOT NULL "
                "AND superseded_at_us <= recorded_at_us"
            ).fetchone()
            if non_monotone is not None and int(non_monotone[0]) > 0:
                problems.append("claim revision superseded_at_us is not after recorded_at_us")
            if _has("forget_requests"):
                unanchored = connection.execute(
                    "SELECT COUNT(*) FROM forget_requests WHERE tombstone_seq_hi > "
                    "(SELECT COALESCE(MAX(tombstone_seq), 0) FROM resource_tombstones)"
                ).fetchone()
                if unanchored is not None and int(unanchored[0]) > 0:
                    problems.append("forget ledger rows exceed the tombstone watermark")
        if _has("legal_holds"):
            invalid_hold = connection.execute(
                "SELECT COUNT(*) FROM legal_holds WHERE released_us IS NOT NULL "
                "AND released_us < created_us"
            ).fetchone()
            if invalid_hold is not None and int(invalid_hold[0]) > 0:
                problems.append("legal hold released before it was created")
        # Phase 6 invariants (ADR-0014 §9): the FTS projection pointer
        # resolves to a verified generation of the same tenant, documents
        # reference existing generations, and a ready state never hides a
        # missing pointer. Usage rows keep their request anchors (the FK
        # already enforces existence; here we keep tenant coherence).
        if _has("fts_current") and _has("fts_generations"):
            broken_pointer = connection.execute(
                "SELECT COUNT(*) FROM fts_current c WHERE c.generation_id NOT IN "
                "(SELECT id FROM fts_generations WHERE status = 'verified' "
                "AND tenant_id = c.tenant_id)"
            ).fetchone()
            if broken_pointer is not None and int(broken_pointer[0]) > 0:
                problems.append("fts current pointer does not resolve to a verified generation")
            orphan_docs = connection.execute(
                "SELECT COUNT(*) FROM fts_documents d WHERE d.generation_id NOT IN "
                "(SELECT id FROM fts_generations)"
            ).fetchone()
            if orphan_docs is not None and int(orphan_docs[0]) > 0:
                problems.append("fts documents referencing a missing generation")
        if _has("fts_projection_state"):
            stale_ready = connection.execute(
                "SELECT COUNT(*) FROM fts_projection_state WHERE state = 'ready' "
                "AND NOT EXISTS (SELECT 1 FROM fts_current)"
            ).fetchone()
            if stale_ready is not None and int(stale_ready[0]) > 0:
                problems.append("fts projection ready state without a current pointer")
        if _has("recall_usage_reports") and _has("recall_requests"):
            cross_request = connection.execute(
                "SELECT COUNT(*) FROM recall_usage_reports u WHERE u.request_id NOT IN "
                "(SELECT id FROM recall_requests WHERE tenant_id = u.tenant_id)"
            ).fetchone()
            if cross_request is not None and int(cross_request[0]) > 0:
                problems.append("usage reports anchored outside their tenant's requests")
        # Phase 7 invariants (ADR-0015 §10): the vector pointer resolves to a
        # verified same-tenant generation, delta rows keep tenant anchors,
        # id map surrogates are unique per tenant (the UNIQUE index enforces
        # it; this also survives legacy restores) and a ready state never
        # hides a missing pointer.
        if _has("vector_current") and _has("vector_generations"):
            broken_vector_pointer = connection.execute(
                "SELECT COUNT(*) FROM vector_current c WHERE c.generation_id NOT IN "
                "(SELECT id FROM vector_generations WHERE status = 'verified' "
                "AND tenant_id = c.tenant_id)"
            ).fetchone()
            if broken_vector_pointer is not None and int(broken_vector_pointer[0]) > 0:
                problems.append("vector current pointer does not resolve to a verified generation")
        if _has("vector_delta_ledger") and _has("agents"):
            orphan_delta = connection.execute(
                "SELECT COUNT(*) FROM vector_delta_ledger d WHERE d.agent_id NOT IN "
                "(SELECT id FROM agents WHERE tenant_id = d.tenant_id)"
            ).fetchone()
            if orphan_delta is not None and int(orphan_delta[0]) > 0:
                problems.append("vector delta rows anchored outside their tenant's agents")
        if _has("vector_projection_state"):
            vector_ready = connection.execute(
                "SELECT COUNT(*) FROM vector_projection_state WHERE state = 'ready' "
                "AND NOT EXISTS (SELECT 1 FROM vector_current)"
            ).fetchone()
            if vector_ready is not None and int(vector_ready[0]) > 0:
                problems.append("vector projection ready state without a current pointer")
    finally:
        connection.close()
    return tuple(problems)


if TYPE_CHECKING:
    from iris_memory_core.application.forget import ForgetService


class BackupService:
    def __init__(self, store: Store) -> None:
        self._store = store

    # -- backup ------------------------------------------------------------------

    def create_backup(self, destination: Path, *, signing_key: bytes | None = None) -> BackupReport:
        started = time.monotonic()
        backup_id = str(self._store.ids.new())
        manifest = write_backup_files(
            self._store.runtime.database,
            destination,
            backup_id,
            signing_key=signing_key,
            artifact_root=self._store.artifact_root,
        )
        schema_version = int(manifest["schema_version"])
        agent_watermarks = {str(k): int(v) for k, v in manifest["agent_watermarks"].items()}
        tombstone_watermark = int(manifest["tombstone_watermark"])
        checksum = manifest["files"][CANONICAL_NAME]
        del checksum
        duration_ms = int((time.monotonic() - started) * 1000)
        with self._store.write() as tx:
            tx.raw().execute(
                "INSERT INTO backup_catalog (id, manifest_path, schema_version, status, notes, "
                "created_us) VALUES (?, ?, ?, 'valid', ?, ?)",
                (
                    backup_id,
                    str(destination / MANIFEST_NAME),
                    schema_version,
                    f"duration_ms={duration_ms}",
                    self._store.clock.now_us(),
                ),
            )
            tx.audit(
                tenant_id="system",
                actor="backup",
                action="backup.created",
                resource_type="backup",
                resource_id=backup_id,
                reason_code="scheduled",
                details={"schema_version": schema_version},
            )
        return BackupReport(
            backup_id=backup_id,
            directory=destination,
            schema_version=schema_version,
            sqlite_runtime=str(manifest["sqlite_runtime"]),
            agent_watermarks=agent_watermarks,
            tombstone_watermark=tombstone_watermark,
            duration_ms=duration_ms,
        )

    # -- verification and restore ---------------------------------------------------

    def verify_backup(self, backup_dir: Path, *, signing_key: bytes | None = None) -> RestoreCheck:
        return verify_backup(backup_dir, signing_key=signing_key)

    def restore_backup(
        self,
        backup_dir: Path,
        target_dir: Path,
        *,
        signing_key: bytes | None = None,
        deletion_ledger: Sequence[ForgetRequest] = (),
        forget_service: ForgetService | None = None,
    ) -> RestoreReport:
        """Restore and (when a deletion ledger is supplied) replay it.

        An old backup cannot know about Forget requests committed after it
        was taken. The operator supplies the compliance deletion ledger
        exported from the live system (ForgetService.export_deletion_ledger)
        together with a ForgetService bound to the RESTORED store; rows newer
        than the backup's ledger watermark are re-executed before the store
        serves traffic (§21.2, ADR-0013 §10). Replay is idempotent.
        """
        if deletion_ledger:
            from iris_memory_core.application.forget import ForgetService

            if not isinstance(forget_service, ForgetService):
                raise ConflictError(
                    "deletion-ledger replay requires a ForgetService bound to the restored store"
                )

        def prepare(staging: Path) -> None:
            if not deletion_ledger:
                return
            assert forget_service is not None
            # Replay by exact IDENTITY difference on the manifest copy that was
            # authenticated into staging.  Reading the original backup path
            # here would reopen a verify-to-replay TOCTOU.
            known = self.backup_ledger_identities(staging)
            pending = tuple(
                request
                for request in deletion_ledger
                if isinstance(request, ForgetRequest)
                and (
                    request.app_instance_id,
                    request.selector_key,
                    request.created_us,
                    request.idempotency_key,
                    request.reason_code,
                    request.erase_content,
                )
                not in known.get(request.tenant_id, frozenset())
            )
            staging_store = Store(
                SQLiteRuntime(
                    staging / CANONICAL_NAME,
                    allowed_versions=(self._store.runtime.runtime_report.sqlite_version,),
                ),
                clock=self._store.clock,
                # The staging snapshot is authenticated legacy bytes; an
                # older-schema backup replays here and the STARTUP migration
                # (never restore, ADR-0014 §12-10) brings it forward once
                # the current binary opens the switched-in target.
                verify_schema_window=False,
            )
            forget_service.replay_deletion_ledger(staging_store, pending)

        return restore_backup(
            backup_dir,
            target_dir,
            signing_key=signing_key,
            artifact_root=target_dir / ARTIFACTS_DIR_NAME,
            prepare_staging=prepare if deletion_ledger else None,
        )

    def _manifest_ledger(self, backup_dir: Path) -> dict[str, Any]:
        manifest_path = backup_dir / MANIFEST_NAME
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        ledger = manifest.get("forget_ledger_by_tenant", {})
        return ledger if isinstance(ledger, dict) else {}

    def backup_ledger_watermark(self, backup_dir: Path) -> dict[str, int]:
        """The deletion-ledger watermark a backup carries (tenant -> max us)."""
        watermark: dict[str, int] = {}
        for tenant_id, entry in self._manifest_ledger(backup_dir).items():
            if isinstance(entry, int):
                # Legacy int-format manifests carry only the watermark.
                watermark[str(tenant_id)] = int(entry)
            elif isinstance(entry, dict) and isinstance(entry.get("watermark_us"), int):
                watermark[str(tenant_id)] = int(entry["watermark_us"])
        return watermark

    def backup_ledger_identities(
        self, backup_dir: Path
    ) -> dict[str, frozenset[tuple[str, str, int, str, str, bool]]]:
        """Request identities the backup already carries, per tenant.

        A legacy int-format manifest — or a coarser pre-release dict manifest
        whose request entries lack the full identity fields — yields no
        identities for that tenant: the restore then replays the WHOLE
        supplied ledger, which is safe (replay is idempotent — known rows are
        skipped) and never misses a deletion."""
        identities: dict[str, frozenset[tuple[str, str, int, str, str, bool]]] = {}
        for tenant_id, entry in self._manifest_ledger(backup_dir).items():
            if not isinstance(entry, dict):
                continue
            rows = entry.get("requests", [])
            if not isinstance(rows, list):
                continue
            full_shape = all(
                isinstance(item, dict) and all(field in item for field in _LEDGER_IDENTITY_FIELDS)
                for item in rows
            )
            if not full_shape:
                # Coarse manifest: no precise identities, whole-ledger replay.
                continue
            tuples = {
                (
                    str(item.get("app_instance_id", "")),
                    str(item.get("selector_key", "")),
                    int(item.get("created_us", 0)),
                    str(item.get("idempotency_key", "")),
                    str(item.get("reason_code", "")),
                    bool(item.get("erase_content", False)),
                )
                for item in rows
                if isinstance(item, dict)
            }
            identities[str(tenant_id)] = frozenset(tuples)
        return identities

    @staticmethod
    def _verify_database_invariants(database: Path) -> tuple[str, ...]:
        return verify_database_invariants(database)

    # -- smoke and measurement -----------------------------------------------------

    def smoke_restored(self, database: Path) -> SmokeReport:
        """Read watermarks and perform one canonical write on a restored store."""
        started = time.monotonic()
        runtime = SQLiteRuntime(
            database, allowed_versions=(self._store.runtime.runtime_report.sqlite_version,)
        )
        store = Store(runtime, clock=self._store.clock, ids=self._store.ids)
        with store.read() as tx:
            count = int(tx.raw().execute("SELECT COUNT(*) FROM agent_watermarks").fetchone()[0])
        with store.write() as tx:
            tx.audit(
                tenant_id="system",
                actor="restore",
                action="restore.smoke_write",
                resource_type="backup",
                resource_id="smoke",
                reason_code="verification",
            )
        return SmokeReport(
            read_watermarks=count,
            wrote_probe_event=True,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def measure_recovery(self, backup_dir: Path, target_dir: Path) -> RecoveryMeasurement:
        backup_started = time.monotonic()
        report = self.restore_backup(backup_dir, target_dir)
        if not report.check.ok:
            raise ConflictError(
                "recovery measurement failed", details={"problems": list(report.check.problems)}
            )
        restore_ms = int((time.monotonic() - backup_started) * 1000)
        smoke = self.smoke_restored(target_dir / CANONICAL_NAME)
        return RecoveryMeasurement(
            backup_ms=0,
            restore_ms=restore_ms,
            smoke_ms=smoke.duration_ms,
            rpo_note="RPO equals the last committed transaction at online-backup time",
        )
