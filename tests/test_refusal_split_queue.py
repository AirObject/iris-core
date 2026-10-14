"""RS01/07-12/14/18: frozen trees and transaction boundaries, no model calls."""
import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from conftest import msg
from iris.db import dumps, now
from iris.queue import (batch_rate_status, claim_batch, degrade_refused_context,
                        form_batch, get_batch, learning_batch, next_learning_batch,
                        reset_batch, split_refused_batch, split_targets)


def root_batch(store, count=4, *, entry="A", future=0):
    for i in range(1, count + future + 1):
        msg(store, i, f"消息{i}", entry=entry)
    return form_batch(store, entry, "rs-test", target_count=count)


def split(store, bid):
    claim_batch(store, bid, force=True)
    with store.write() as conn:
        return split_refused_batch(conn, bid, prompt_version="rs-test")


def finish(store, bid, state="succeeded", result=None):
    with store.write() as conn:
        conn.execute("UPDATE batches SET state=?,finished_at=?,result_json=? WHERE id=?",
                     (state, now(), dumps(result or {}), bid))
        conn.execute("UPDATE messages SET learning_state=? WHERE batch_id=?",
                     ("learned" if state == "succeeded" else state, bid))
        if state == "succeeded":
            conn.execute("DELETE FROM memory_gaps WHERE batch_id=?", (bid,))


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 30, 1000, 4097])
def test_rs01_balanced_complete_partition_without_large_database(n):
    leaves, depths = [], []
    def visit(ids, depth):
        if len(ids) == 1:
            leaves.extend(ids)
            depths.append(depth)
        else:
            left, right = split_targets(ids)
            assert left and right and left + right == ids
            assert len(right) - len(left) in (0, 1)
            visit(left, depth + 1)
            visit(right, depth + 1)
    visit(list(range(n)), 0)
    assert leaves == list(range(n))
    assert max(depths) == math.ceil(math.log2(n))
    with pytest.raises(ValueError):
        split_targets([1])


def test_rs01_split_transfers_targets_and_gaps_atomically(store):
    root = root_batch(store, 5, future=1)
    later = msg(store, 7, "冻结之后到达")
    left, right = split(store, root.id)
    l, r = get_batch(store, left), get_batch(store, right)
    assert (l.target_ids, r.target_ids) == (root.target_ids[:2], root.target_ids[2:])
    assert l.future_ids == root.target_ids[2:] + root.future_ids
    assert r.history_ids == root.target_ids[:2]
    assert later not in l.future_ids + r.future_ids
    assert (l.root_batch_id, r.parent_batch_id, l.split_path, r.split_path) == (root.id, root.id, "0", "1")
    assert get_batch(store, root.id).state == "split"
    assert form_batch(store, "A", "rs-test") is None
    with store.read() as conn:
        assert {r[0] for r in conn.execute("SELECT batch_id FROM memory_gaps")} == {left, right}
        for child in (l, r):
            bounds = conn.execute("SELECT MIN(occurred_at),MAX(occurred_at) FROM messages WHERE batch_id=?", (child.id,)).fetchone()
            gap = conn.execute("SELECT started_at,ended_at FROM memory_gaps WHERE batch_id=?", (child.id,)).fetchone()
            assert tuple(gap) == tuple(bounds)
        assert [tuple(r) for r in conn.execute("SELECT id,batch_id FROM messages WHERE learning_state='batched' ORDER BY id")] == [(i, left if i in l.target_ids else right) for i in root.target_ids]
        assert conn.execute("SELECT attempt_count FROM batches WHERE id=?", (root.id,)).fetchone()[0] == 1
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    with pytest.raises(ValueError), store.write() as conn:
        split_refused_batch(conn, root.id, prompt_version="rs-test")


def test_rs07_fourth_refusal_splits_but_singleton_never_gets_fifth_attempt(store):
    root = root_batch(store, 2)
    with store.write() as conn:
        conn.execute("UPDATE batches SET attempt_count=3 WHERE id=?", (root.id,))
    left, right = split(store, root.id)
    assert get_batch(store, root.id).attempt_count == 4
    assert get_batch(store, left).attempt_count == 0
    claim_batch(store, left, force=True)
    with store.write() as conn:
        assert not degrade_refused_context(conn, left, sent_context_ids=[])
        assert degrade_refused_context(conn, left, sent_context_ids=[2])
    leaf = get_batch(store, left)
    assert leaf.context_mode == "target_only" and leaf.attempt_count == 1
    assert learning_batch(leaf).future_ids == [] and leaf.future_ids == [2]
    claim_batch(store, left, force=True)
    with store.write() as conn:
        assert not degrade_refused_context(conn, left, sent_context_ids=[2])
    finish(store, left)
    claim_batch(store, right, force=True)
    with store.write() as conn:
        conn.execute("UPDATE batches SET attempt_count=3 WHERE id=?", (right,))
        assert not degrade_refused_context(conn, right, sent_context_ids=[1])
    assert get_batch(store, right).attempt_count == 3


def test_rs08_depth_first_order_and_backoff_block_right_sibling(store):
    root = root_batch(store)
    left, right = split(store, root.id)
    first, second = split(store, left)
    assert right < first < second
    with store.write() as conn:
        conn.execute("UPDATE batches SET next_retry_at=? WHERE id=?",
                     ((datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), first))
    with store.read() as conn:
        assert next_learning_batch(conn, "A")["id"] == first
    with pytest.raises(ValueError):
        claim_batch(store, first)
    with pytest.raises(ValueError):
        claim_batch(store, right, force=True)
    claim_batch(store, first, force=True)
    finish(store, first)
    claim_batch(store, second, force=True)
    finish(store, second)
    claim_batch(store, right, force=True)


def test_rs09_concurrent_claim_and_relearn_old_leaf_waits_for_any_running(store):
    root = root_batch(store, 2)
    left, right = split(store, root.id)
    gate = Barrier(2)
    def claim():
        gate.wait(timeout=5)
        try:
            claim_batch(store, left, force=True)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: claim(), range(2))) == [False, True]
    finish(store, left, "refused")
    claim_batch(store, right, force=True)
    reset_batch(store, left)
    with pytest.raises(ValueError):
        claim_batch(store, left, force=True)
    with store.read() as conn:
        assert next_learning_batch(conn, "A")["id"] == right
    finish(store, right)
    claim_batch(store, left, force=True)


def test_rs10_split_error_rolls_back_everything(store):
    root = root_batch(store)
    claim_batch(store, root.id, force=True)
    with store.read() as conn:
        before = list(conn.iterdump())
    with pytest.raises(RuntimeError), store.write() as conn:
        split_refused_batch(conn, root.id, prompt_version="rs-test")
        raise RuntimeError("crash before commit")
    with store.read() as conn:
        assert list(conn.iterdump()) == before
    with store.write() as conn:
        conn.execute("CREATE TEMP TRIGGER fail_second BEFORE INSERT ON batches WHEN new.split_slot=1 BEGIN SELECT RAISE(ABORT,'second child'); END")
    with pytest.raises(sqlite3.IntegrityError), store.write() as conn:
        split_refused_batch(conn, root.id, prompt_version="rs-test")
    with store.read() as conn:
        assert list(conn.iterdump()) == before


def test_rs11_restart_only_recovers_running_leaf_and_retains_mode_gap(store):
    root = root_batch(store, 2)
    left, right = split(store, root.id)
    claim_batch(store, left, force=True)
    with store.write() as conn:
        assert degrade_refused_context(conn, left, sent_context_ids=[2])
        conn.execute("UPDATE batches SET attempt_count=3 WHERE id=?", (left,))
    claim_batch(store, left, force=True)
    store.recover_inflight()
    store.recover_inflight()
    assert get_batch(store, root.id).state == "split"
    assert get_batch(store, left).state == "abandoned"
    assert get_batch(store, left).context_mode == "target_only"
    assert get_batch(store, right).attempt_count == 0
    with store.read() as conn:
        rows = list(conn.execute("SELECT * FROM batch_attempts"))
        assert len(rows) == 1 and rows[0]["context_mode"] == "target_only"
        assert rows[0]["sent_context_ids"] is None
        assert {r[0] for r in conn.execute("SELECT batch_id FROM memory_gaps")} == {left, right}


def test_rs12_reset_only_terminal_leaf_preserves_gap_and_rejects_missing_targets(store):
    root = root_batch(store, 2)
    left, right = split(store, root.id)
    with pytest.raises(ValueError):
        reset_batch(store, root.id)
    finish(store, left, "refused")
    reset_batch(store, left)
    with store.read() as conn:
        assert conn.execute("SELECT 1 FROM memory_gaps WHERE batch_id=?", (left,)).fetchone()
    finish(store, left, "refused")
    with store.write() as conn:
        conn.execute("DELETE FROM messages WHERE batch_id=?", (left,))
    with pytest.raises(ValueError):
        reset_batch(store, left)
    assert get_batch(store, right).state == "waiting"


def test_rs14_hourly_limit_counts_roots_only(store):
    root = root_batch(store)
    left, _ = split(store, root.id)
    split(store, left)
    with store.read() as conn:
        status = batch_rate_status(conn, "A", 2, datetime.now(timezone.utc))
        assert status["batches_last_hour"] == 1 and status["retry_at"] is None


def test_rs04_children_exclude_known_refused_context_and_later_arrivals(store):
    root = root_batch(store)
    left, right = split(store, root.id)
    first, second = split(store, left)
    finish(store, first, "refused")
    finish(store, second)
    later = msg(store, 5, "晚到的消息不能进入冻结树")
    third, fourth = split(store, right)
    for bid in (third, fourth):
        leaf = get_batch(store, bid)
        assert root.target_ids[0] not in leaf.history_ids + leaf.future_ids
        assert later not in leaf.history_ids + leaf.future_ids
    assert root.target_ids[1] in get_batch(store, third).history_ids


def test_rs08_next_root_history_uses_last_logical_leaf_not_largest_id(store):
    root = root_batch(store)
    left, right = split(store, root.id)
    first, second = split(store, left)
    finish(store, first)
    finish(store, second)
    finish(store, right)
    msg(store, 5, "下一根的新消息")
    following = form_batch(store, "A", "rs-test")
    assert following.history_ids == root.target_ids[2:]
