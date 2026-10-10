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

from .search_text import segmented
from .vector_index import VectorIndex


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
        self._vector_indexes: dict[tuple[str, str], VectorIndex] = {}
        self._writer = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self._writer.row_factory = sqlite3.Row
        self._writer.create_function("iris_terms", 1, segmented, deterministic=True)
        self._writer.execute("PRAGMA journal_mode=WAL")
        self._writer.execute("PRAGMA foreign_keys=ON")
        self._writer.execute("PRAGMA busy_timeout=30000")
        self._writer.execute("PRAGMA checkpoint_fullfsync=ON")
        self._writer.execute("PRAGMA journal_size_limit=67108864")
        try:
            self._migrate(existed)
        except BaseException:
            self._writer.close()
            raise
        self._writer.execute(
            "INSERT OR IGNORE INTO subjects(id,kind,name,created_at) VALUES('self','self','我',?)", (now(),)
        )
        self._writer.execute("INSERT OR IGNORE INTO runtime_settings(key,value_json) VALUES('retrieval',?)",
            (files("iris").joinpath("retrieval_defaults.json").read_text(encoding="utf-8"),))

    def recover_inflight(self, *, current=None) -> None:
        # A running batch had no committed result. Count the interrupted attempt once.
        with self.write() as conn:
            for batch in conn.execute("SELECT id,entry_id,target_ids,attempt_count FROM batches WHERE state='running'").fetchall():
                count = batch["attempt_count"] + 1
                state = "abandoned" if count >= 4 else "waiting"
                stamp = current().isoformat() if current else now()
                number = conn.execute("SELECT COALESCE(MAX(number),0)+1 FROM batch_attempts WHERE batch_id=?", (batch["id"],)).fetchone()[0]
                conn.execute("""INSERT INTO batch_attempts(batch_id,number,started_at,finished_at,parse_status,error,duration_ms)
                    VALUES(?,?,?,?,'failed','interrupted by restart',0)""", (batch["id"], number, stamp, stamp))
                conn.execute("UPDATE batches SET state=?,attempt_count=?,next_retry_at=?,last_error=?,finished_at=? WHERE id=?",
                             (state, count, stamp if state == "waiting" else None, "interrupted by restart",
                              stamp if state == "abandoned" else None, batch["id"]))
                if state == "abandoned":
                    ids = json.loads(batch["target_ids"])
                    conn.executemany("UPDATE messages SET learning_state='abandoned' WHERE id=?", ((i,) for i in ids))
                    placeholders = ",".join("?" for _ in ids)
                    bounds = conn.execute(f"SELECT MIN(occurred_at),MAX(occurred_at) FROM messages WHERE id IN ({placeholders})", ids).fetchone()
                    conn.execute("""INSERT OR REPLACE INTO memory_gaps(batch_id,entry_id,started_at,ended_at,reason,created_at)
                        VALUES(?,?,?,?,?,?)""", (batch["id"], batch["entry_id"], bounds[0], bounds[1], "attempts_exhausted", stamp))

    def _migrate(self, existed: bool) -> None:
        migration_dir = files("iris").joinpath("migrations")
        scripts = sorted(p for p in migration_dir.iterdir() if p.name.endswith(".sql"))
        has_table = self._writer.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
        applied = {r[0] for r in self._writer.execute("SELECT version FROM schema_migrations")} if has_table else set()
        known = [p.name for p in scripts]
        if sorted(applied) != known[:len(applied)]:
            raise ValueError("数据库迁移版本比当前程序新或迁移历史不兼容；请升级代码，或使用迁移前的 .bak 恢复。")
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
                self._refresh_vectors()

    def vector_index(self, model: str, dtype: str = "float32") -> VectorIndex:
        # Published indexes have their own copy-on-write lock. The steady-state
        # lookup must not queue a reader behind the database writer.
        key = (model, dtype)
        if key in self._vector_indexes:
            return self._vector_indexes[key]
        with self._lock:
            assert not self._writer.in_transaction
            if key not in self._vector_indexes:
                index = VectorIndex(model, dtype)
                with self.read() as conn:
                    for row in conn.execute("""SELECT id,revision,lifecycle,embedding,embedding_model FROM memories
                            WHERE lifecycle!='deleted' AND embedding IS NOT NULL AND embedding_model=?""", (model,)):
                        index.upsert(row)
                if not self._vector_indexes:
                    # The first complete startup snapshot already includes these commits.
                    # No other in-memory index can still need their notifications.
                    self._writer.execute("DELETE FROM vector_dirty")
                self._vector_indexes[key] = index
            return self._vector_indexes[key]

    def _refresh_vectors(self) -> None:
        """Called under the writer lock, after commit; rollback never reaches this hook."""
        if not self._vector_indexes:
            return
        assert not self._writer.in_transaction
        with self.read() as conn:
            for change in conn.execute("""SELECT d.memory_id,m.id,m.revision,m.lifecycle,m.embedding,m.embedding_model
                    FROM vector_dirty d LEFT JOIN memories m ON m.id=d.memory_id"""):
                for index in self._vector_indexes.values():
                    if change["id"] is None:
                        index.remove(change["memory_id"])
                    else:
                        index.upsert(change)
        self._writer.execute("DELETE FROM vector_dirty")

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
