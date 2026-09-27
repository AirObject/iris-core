"""SQLite storage with one serial writer and snapshot readers."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Iterator


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        self._lock = threading.RLock()
        self._writer = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self._writer.row_factory = sqlite3.Row
        self._writer.execute("PRAGMA journal_mode=WAL")
        self._writer.execute("PRAGMA foreign_keys=ON")
        self._writer.execute("PRAGMA busy_timeout=30000")
        self._migrate(existed)
        self._writer.execute(
            "INSERT OR IGNORE INTO subjects(id,kind,name,created_at) VALUES('self','self','我',?)", (now(),)
        )
        # A running batch had no committed result. Count that interrupted attempt once.
        self._writer.execute("""UPDATE batches SET state='waiting',attempt_count=attempt_count+1,
            next_retry_at=?,last_error='interrupted by restart' WHERE state='running'""", (now(),))

    def _migrate(self, existed: bool) -> None:
        migration_dir = files("iris").joinpath("migrations")
        scripts = sorted(p for p in migration_dir.iterdir() if p.name.endswith(".sql"))
        has_table = self._writer.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
        applied = {r[0] for r in self._writer.execute("SELECT version FROM schema_migrations")} if has_table else set()
        pending = [p for p in scripts if p.name not in applied]
        if pending and existed:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = sqlite3.connect(self.path.with_name(f"{self.path.name}.{stamp}.bak"))
            try:
                self._writer.backup(backup)
            finally:
                backup.close()
        self._writer.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        for script in pending:
            sql = script.read_text(encoding="utf-8")
            self._writer.executescript(
                "BEGIN IMMEDIATE;\n" + sql + "\nINSERT INTO schema_migrations(version,applied_at) VALUES(" +
                "'" + script.name + "','" + now() + "');\nCOMMIT;"
            )

    @contextlib.contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._writer.execute("BEGIN IMMEDIATE")
            try:
                yield self._writer
            except BaseException:
                self._writer.rollback()
                raise
            else:
                self._writer.commit()

    @contextlib.contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        uri = self.path.as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            conn.rollback()
            conn.close()

    def setting(self, key: str, default: object = None) -> object:
        with self.read() as conn:
            row = conn.execute("SELECT value_json FROM runtime_settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value: object) -> None:
        with self.write() as conn:
            conn.execute("INSERT INTO runtime_settings(key,value_json) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json", (key, dumps(value)))

    def close(self) -> None:
        with self._lock:
            self._writer.close()
