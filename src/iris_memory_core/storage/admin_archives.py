"""Server-owned, separately retained backup and export artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from iris_memory_core.domain.console_operations import TrustedBackupPayload
from iris_memory_core.domain.hashing import canonical_json
from iris_memory_core.storage.backup import (
    create_standalone_backup,
    verify_backup,
    verify_database_invariants,
    write_backup_files,
)
from iris_memory_core.storage.uow import Store

EXPORT_TABLES = (
    "agents",
    "spaces",
    "space_groups",
    "entities",
    "external_identities",
    "bindings",
    "observations",
    "claims",
    "claim_revisions",
    "episodes",
    "episode_revisions",
    "relations",
    "relation_revisions",
    "notes",
    "note_revisions",
    "tasks",
    "task_revisions",
    "persona_records",
    "persona_proposals",
    "audit_events",
)


class AdminArchiveService:
    def __init__(
        self,
        store: Store,
        *,
        backup_root: Path,
        export_root: Path,
        backup_signing_key: bytes | None = None,
    ) -> None:
        if backup_root.resolve() == export_root.resolve():
            raise ValueError("backup and export roots must be separate")
        self._store = store
        self._backup_root = backup_root
        self._export_root = export_root
        self._signing_key = backup_signing_key

    def _trusted_path(self, reference: str) -> Path:
        if str(UUID(reference)) != reference:
            raise ValueError("invalid internal backup reference")
        return self._backup_root / "trusted" / reference

    def create_verified(self, reference: str) -> TrustedBackupPayload:
        """Each attempt has its own private, atomically published directory."""
        import shutil

        destination = self._trusted_path(reference)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not destination.exists():
            staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=destination.parent))
            snapshot = staging / "snapshot"
            try:
                write_backup_files(
                    self._store.runtime.database,
                    snapshot,
                    reference,
                    signing_key=self._signing_key,
                    artifact_root=self._store.artifact_root,
                )
                if not verify_backup(snapshot, signing_key=self._signing_key).ok:
                    raise RuntimeError("backup verification failed")
                snapshot.chmod(0o700)
                snapshot.rename(destination)
                descriptor = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        manifest = (destination / "manifest.json").read_bytes()
        result = TrustedBackupPayload(
            reference, hashlib.sha256(manifest).hexdigest(), self._store.clock.now_us()
        )
        if not self.verify_result(result):
            raise RuntimeError("backup verification failed")
        return result

    def verify_result(self, result: TrustedBackupPayload) -> bool:
        if result.result_ref is None or result.manifest_hash is None or result.verified_us is None:
            return False
        try:
            destination = self._trusted_path(result.result_ref)
            return (
                not destination.is_symlink()
                and verify_backup(destination, signing_key=self._signing_key).ok
                and hashlib.sha256((destination / "manifest.json").read_bytes()).hexdigest()
                == result.manifest_hash
                and json.loads((destination / "manifest.json").read_bytes())["backup_id"]
                == result.result_ref
                and not verify_database_invariants(destination / "canonical.sqlite3")
            )
        except (OSError, ValueError, KeyError, sqlite3.Error):
            return False

    def create_backup(self, operation_id: str) -> dict[str, object]:
        self._backup_root.mkdir(parents=True, exist_ok=True)
        destination = self._backup_root / operation_id
        if destination.exists():
            check = verify_backup(destination, signing_key=self._signing_key)
            if not check.ok:
                raise RuntimeError("stored backup did not verify")
            manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        else:
            manifest = create_standalone_backup(
                self._store.runtime.database,
                destination,
                signing_key=self._signing_key,
            )
        check = verify_backup(destination, signing_key=self._signing_key)
        if not check.ok:
            raise RuntimeError("new backup did not verify")
        return {
            "operation_id": operation_id,
            "kind": "backup",
            "status": "completed",
            "created_us": self._store.clock.now_us(),
            "manifest_hash": hashlib.sha256(canonical_json(manifest).encode()).hexdigest(),
        }

    def create_export(self, operation_id: str, *, tenant_id: str) -> dict[str, object]:
        self._export_root.mkdir(parents=True, exist_ok=True)
        destination = self._export_root / operation_id
        if destination.exists():
            loaded = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise RuntimeError("stored export manifest is invalid")
            return {str(key): value for key, value in loaded.items()}
        staging = Path(tempfile.mkdtemp(prefix=f".{operation_id}-", dir=self._export_root))
        data_path = staging / "export.jsonl"
        digest = hashlib.sha256()
        rows_written = 0
        connection = self._store.runtime.connect(verify_schema=True)
        try:
            with data_path.open("wb") as handle:
                for table in EXPORT_TABLES:
                    columns = {
                        str(row["name"])
                        for row in connection.execute(f"PRAGMA table_info({table})")
                    }
                    if not columns or "tenant_id" not in columns:
                        continue
                    for row in connection.execute(
                        f"SELECT * FROM {table} WHERE tenant_id=? ORDER BY rowid", (tenant_id,)
                    ):
                        value: dict[str, Any] = {
                            "table": table,
                            "record": dict(row),
                        }
                        encoded = (canonical_json(value) + "\n").encode()
                        handle.write(encoded)
                        digest.update(encoded)
                        rows_written += 1
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            connection.close()
        manifest: dict[str, object] = {
            "operation_id": operation_id,
            "kind": "export",
            "status": "completed",
            "created_us": self._store.clock.now_us(),
            "rows": rows_written,
            "sha256": digest.hexdigest(),
        }
        (staging / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
        os.replace(staging, destination)
        return manifest


__all__ = ["EXPORT_TABLES", "AdminArchiveService"]
