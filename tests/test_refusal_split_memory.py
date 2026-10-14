"""RS13/15: root effect ledger and erasing ancestor outputs."""
import pytest

from iris.db import dumps, now
from iris.memory_ops import (confirm_retention, purge_memory,
                             record_batch_memory_effect)
from iris.queue import claim_batch
from test_refusal_split_queue import finish, root_batch, split
from test_retrieval import put


def test_rs15_effects_are_once_per_root_transactional_and_only_for_trees(store):
    root = root_batch(store, 2)
    old = put(store, "保留强度不能因拆分增加多次")
    with store.write() as conn:
        assert record_batch_memory_effect(conn, root.id, old, "confirmed")
        assert conn.execute("SELECT COUNT(*) FROM batch_memory_effects").fetchone()[0] == 0
    left, right = split(store, root.id)
    claim_batch(store, left, force=True)
    with pytest.raises(RuntimeError), store.write() as conn:
        assert record_batch_memory_effect(conn, left, old, "confirmed")
        confirm_retention(store, old, _conn=conn)
        raise RuntimeError("rollback apply")
    with store.write() as conn:
        assert record_batch_memory_effect(conn, left, old, "confirmed")
        confirm_retention(store, old, _conn=conn)
    finish(store, left)
    claim_batch(store, right, force=True)
    with store.write() as conn:
        assert not record_batch_memory_effect(conn, right, old, "confirmed")
        assert conn.execute("SELECT retention FROM memories WHERE id=?", (old,)).fetchone()[0] == 55
    fresh = put(store, "左边新建的记忆")
    with store.write() as conn:
        assert record_batch_memory_effect(conn, right, fresh, "created")
        assert not record_batch_memory_effect(conn, right, fresh, "confirmed")


def test_rs13_purge_collects_ancestors_but_not_unrelated_sibling_and_scrubs_late_writes(store):
    root = root_batch(store)
    left, right = split(store, root.id)
    first, second = split(store, left)
    # Keep the source in another active child's optional context: deletion of a
    # shared message would correctly taint that child too, a different case.
    mid = put(store, "应清除的正文", evidence=[root.target_ids[0]])
    finish(store, first, result={"created": [mid]})
    with store.write() as conn:
        for bid in (root.id, left, first, second, right):
            conn.execute("""INSERT INTO batch_attempts(batch_id,number,started_at,finished_at,raw_output,
                repair_output,parse_status,error,duration_ms) VALUES(?,1,?,?,'raw','repair','failed','diagnostic',1)""",
                (bid, now(), now()))
    result = purge_memory(store, mid, 1, confirm=True)
    assert result["redaction_counts"]["batches"] == 3
    assert result["redaction_counts"]["batch_attempts"] == 3
    with store.write() as conn:
        # Old failed parent response and diagnostic writes must stay redacted.
        conn.execute("UPDATE batches SET result_json=? WHERE id=?", (dumps({"dropped": ["late"]}), root.id))
        conn.execute("UPDATE batch_attempts SET raw_output='late' WHERE batch_id=?", (left,))
    with store.read() as conn:
        rows = {r["batch_id"]: r for r in conn.execute("SELECT * FROM batch_attempts")}
        for bid in (root.id, left, first):
            assert rows[bid]["raw_output"] is None and rows[bid]["repair_output"] is None
        assert rows[right]["raw_output"] == rows[second]["raw_output"] == "raw"
        assert "late" not in conn.execute("SELECT result_json FROM batches WHERE id=?", (root.id,)).fetchone()[0]
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()


def test_rs13_late_child_result_redaction_propagates_to_preexisting_parent_attempt(store):
    root = root_batch(store, 2)
    left, _ = split(store, root.id)
    mid = put(store, "清除之后迟到的旧结果")
    purge_memory(store, mid, 1, confirm=True)
    with store.write() as conn:
        conn.execute("""INSERT INTO batch_attempts(batch_id,number,started_at,finished_at,raw_output,
            parse_status,duration_ms) VALUES(?,1,?,?,'parent prose','failed',1)""", (root.id, now(), now()))
        conn.execute("UPDATE batches SET result_json=? WHERE id=?", (dumps({"created": [mid]}), left))
    with store.read() as conn:
        assert conn.execute("SELECT raw_output FROM batch_attempts WHERE batch_id=?", (root.id,)).fetchone()[0] is None
