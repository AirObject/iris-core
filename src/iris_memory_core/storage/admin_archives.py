"""Server-owned, separately retained backup and export artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from iris_memory_core.domain.hashing import canonical_json
from iris_memory_core.storage.backup import create_standalone_backup, verify_backup
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
