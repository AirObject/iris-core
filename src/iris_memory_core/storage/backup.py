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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iris_memory_core.domain.errors import ConflictError
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
        if entry.is_symlink() or not stat.S_ISREG(mode):
            problems.append(f"backup entry {entry.name} is not a regular file")
        if entry.name not in _ALLOWED_BACKUP_NAMES:
            problems.append(f"unexpected backup entry {entry.name}")
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
    finally:
        connection.close()
    return tuple(problems)


def write_backup_files(
    database: Path,
    destination: Path,
    backup_id: str,
    *,
    signing_key: bytes | None = None,
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
        manifest = _write_backup_files_into(database, staging, backup_id, signing_key)
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
    finally:
        snapshot.close()
    manifest: dict[str, Any] = {
        "backup_id": backup_id,
        "schema_version": schema_version,
        "sqlite_runtime": sqlite_runtime_version_string(),
        "agent_watermarks": agent_watermarks,
        "tombstone_watermark": int(tombstone_row[0]) if tombstone_row is not None else 0,
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
    finally:
        connection.close()
    return tuple(problems)


class BackupService:
    def __init__(self, store: Store) -> None:
        self._store = store

    # -- backup ------------------------------------------------------------------

    def create_backup(self, destination: Path, *, signing_key: bytes | None = None) -> BackupReport:
        started = time.monotonic()
        backup_id = str(self._store.ids.new())
        manifest = write_backup_files(
            self._store.runtime.database, destination, backup_id, signing_key=signing_key
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
        self, backup_dir: Path, target_dir: Path, *, signing_key: bytes | None = None
    ) -> RestoreReport:
        return restore_backup(backup_dir, target_dir, signing_key=signing_key)

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
