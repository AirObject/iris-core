"""RS10/11/13/18: real process exits and sequential schema upgrade."""
import json
import subprocess
import sys
from datetime import timedelta
from importlib.resources import files
from types import SimpleNamespace

import pytest

from conftest import msg
from test_lifecycle import Clock
from iris.db import Store, now
from iris.maintenance import Maintenance
from iris.queue import claim_batch, get_batch
from test_refusal_split_queue import finish, root_batch, split


@pytest.mark.parametrize("phase", ["before_split", "left_child", "ownership", "gap",
                                   "parent", "before_commit", "after_commit"])
def test_rs10_real_process_exit_has_no_partial_split(tmp_path, phase):
    path = tmp_path / "crash.db"
    store = Store(path)
    root = root_batch(store)
    claim_batch(store, root.id, force=True)
    with store.write() as conn:
        conn.execute("""INSERT INTO memory_gaps(batch_id,entry_id,started_at,ended_at,reason,created_at)
            VALUES(?,'A',?,?,'content_rejection',?)""", (root.id, now(), now(), now()))
    store.close()
    script = r"""
import os, sys
from iris.db import Store
from iris.queue import split_refused_batch
store = Store(sys.argv[1])
phase = sys.argv[2]
with store.write() as conn:
    conn.create_function('crash_now', 0, lambda: os._exit(73))
    event = {
        'left_child': "AFTER INSERT ON batches WHEN new.split_slot=0",
        'ownership': "AFTER UPDATE OF batch_id ON messages WHEN new.batch_id!=old.batch_id",
        'gap': "AFTER INSERT ON memory_gaps WHEN new.batch_id!=1",
        'parent': "AFTER UPDATE OF state ON batches WHEN new.state='split'",
    }.get(phase)
    if event:
        conn.execute('CREATE TEMP TRIGGER crash_point '+event+' BEGIN SELECT crash_now(); END')
    if phase == 'before_split':
        os._exit(73)
    split_refused_batch(conn, 1, prompt_version='rs-test')
    if phase == 'before_commit':
        os._exit(73)
os._exit(73)
"""
    result = subprocess.run([sys.executable, "-c", script, str(path), phase], timeout=20)
    assert result.returncode == 73
    reopened = Store(path)
    try:
        committed = phase == "after_commit"
        assert get_batch(reopened, root.id).state == ("split" if committed else "running")
        with reopened.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == (3 if committed else 1)
            gaps = {r[0] for r in conn.execute("SELECT batch_id FROM memory_gaps")}
            owners = {r[0] for r in conn.execute("SELECT DISTINCT batch_id FROM messages")}
            assert gaps == owners == ({2, 3} if committed else {1})
            assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        reopened.recover_inflight()
        reopened.recover_inflight()
        with reopened.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM batch_attempts").fetchone()[0] == (0 if committed else 1)
            assert conn.execute("SELECT COUNT(*) FROM memory_gaps").fetchone()[0] == (2 if committed else 1)
    finally:
        reopened.close()


def test_rs18_upgrade_from_029_preserves_history_and_does_not_replay_refused(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    resources = files("iris")
    class BeforeRS:
        def joinpath(self, name):
            if name == "migrations":
                return SimpleNamespace(iterdir=lambda: [p for p in resources.joinpath(name).iterdir()
                                                        if p.name < "030"])
            return resources.joinpath(name)
    with monkeypatch.context() as patch:
        patch.setattr("iris.db.files", lambda _: BeforeRS())
        old = Store(path)
    source = msg(old, 1, "历史拒绝不可自行重放")
    with old.write() as conn:
        bid = conn.execute("""INSERT INTO batches(entry_id,history_ids,target_ids,future_ids,state,
            attempt_count,prompt_version,created_at,finished_at) VALUES('A','[]',?,'[]','refused',1,'historic',?,?)""",
            (json.dumps([source]), now(), now())).lastrowid
        conn.execute("UPDATE messages SET batch_id=?,learning_state='refused' WHERE id=?", (bid, source))
        conn.execute("""INSERT INTO batch_attempts(batch_id,number,started_at,finished_at,raw_output,
            parse_status,duration_ms) VALUES(?,1,?,?,'old output','failed',10)""", (bid, now(), now()))
        conn.execute("""INSERT INTO memory_gaps(batch_id,entry_id,started_at,ended_at,reason,created_at)
            VALUES(?,'A',?,?,'content_rejection',?)""", (bid, now(), now(), now()))
        before = {table: [dict(r) for r in conn.execute('SELECT * FROM '+table)]
                  for table in ("batches", "batch_attempts", "memory_gaps", "messages")}
    old.close()
    upgraded = Store(path)
    try:
        upgraded.recover_inflight()
        with upgraded.read() as conn:
            for table, original in before.items():
                after = [dict(r) for r in conn.execute('SELECT * FROM '+table)]
                assert len(after) == len(original)
                assert [{k: r[k] for k in original[0]} for r in after] == original
            row = conn.execute("SELECT * FROM batches").fetchone()
            assert row["parent_batch_id"] is None and row["split_path"] == "" and row["context_mode"] == "full"
            attempt = conn.execute("SELECT * FROM batch_attempts").fetchone()
            assert all(attempt[k] is None for k in ("context_mode", "sent_context_ids", "prompt_version"))
            assert conn.execute("SELECT COUNT(*) FROM batch_memory_effects").fetchone()[0] == 0
            assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        assert list(tmp_path.glob("old.db.*.bak"))
    finally:
        upgraded.close()


def test_rs13_maintenance_retries_abandoned_leaf_once_not_parent_or_refused(store):
    root = root_batch(store)
    left, right = split(store, root.id)
    first, second = split(store, left)
    finish(store, first, "abandoned")
    finish(store, second, "refused")
    finish(store, right)
    clock = Clock("2026-10-13T12:00:00+08:00")
    yesterday = (clock() - timedelta(days=1)).isoformat()
    with store.write() as conn:
        conn.execute("UPDATE batches SET finished_at=?", (yesterday,))
    maintenance = Maintenance(store, clock=clock)
    maintenance.run(maintenance.request())
    assert get_batch(store, first).state == "waiting"
    assert get_batch(store, second).state == "refused"
    assert get_batch(store, left).state == get_batch(store, root.id).state == "split"
    finish(store, first, "abandoned")
    with store.write() as conn:
        conn.execute("UPDATE batches SET finished_at=? WHERE id=?", (yesterday, first))
    maintenance.run(maintenance.request())
    assert get_batch(store, first).state == "abandoned"
    with store.read() as conn:
        assert [r[0] for r in conn.execute("SELECT batch_id FROM maintenance_batch_retries")] == [first]
        assert conn.execute("SELECT 1 FROM memory_gaps WHERE batch_id=?", (first,)).fetchone()
