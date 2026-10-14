"""RS12/17: authenticated tree diagnostics and compatible host root projection."""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import FakeGateway, authorize_host, login_admin
from iris.api import create_app
from iris.learning import LearningEngine
from iris.queue import claim_batch, get_batch, reset_batch
from test_refusal_split_queue import finish, root_batch, split


@pytest.fixture
def client(store):
    with TestClient(create_app(store=store), base_url="http://127.0.0.1", client=("127.0.0.1", 1234)) as value:
        value.app.state.scheduler.stop()
        authorize_host(value)
        login_admin(value)
        yield value


def test_rs17_no_refusal_v1_batch_state_and_result_are_exactly_unchanged(client, store):
    root = root_batch(store, 2)
    for completed in (False, True):
        if completed:
            LearningEngine(store, FakeGateway({})).run_batch(root.id, force=True)
        with store.read() as conn:
            expected = dict(conn.execute("""SELECT id,entry_id,state,attempt_count,next_retry_at,
                last_error,created_at,finished_at,result_json FROM batches WHERE id=?""", (root.id,)).fetchone())
        expected["result"] = json.loads(expected.pop("result_json"))
        data = client.get("/api/v1/status").json()
        assert data["batches"] == [expected]
        assert data["entries"][0]["current_batch" if not completed else "latest_batch"] == expected


def test_rs17_root_aggregation_partial_terminal_and_relearn(client, store):
    root = root_batch(store)
    left, right = split(store, root.id)
    first, second = split(store, left)
    finish(store, first, result={"created": [101], "confirmed": [102]})
    finish(store, second, result={"created": [103], "confirmed": [102]})
    status = client.get("/api/v1/status").json()
    assert len(status["batches"]) == 1
    projected = status["batches"][0]
    assert (projected["id"], projected["state"], projected["finished_at"]) == (root.id, "waiting", None)
    assert projected["result"]["created"] == [101, 103]
    assert projected["result"]["confirmed"] == [102]
    assert projected["split"]["leaf_count"] == 3
    assert projected["split"]["succeeded_messages"] == 2
    assert status["memory_gap_count"] == 1
    claim_batch(store, right, force=True)
    assert client.get("/api/v1/status").json()["batches"][0]["state"] == "running"
    finish(store, right, "refused")
    assert client.get("/api/v1/status").json()["batches"][0]["state"] == "refused"
    assert client.post(f"/admin/api/batches/{root.id}/relearn", json={}).status_code == 409
    assert client.post(f"/admin/api/batches/{right}/relearn", json={}).status_code == 200
    assert client.post(f"/admin/api/batches/{right}/relearn", json={}).status_code == 409
    assert client.get("/api/v1/status").json()["batches"][0]["state"] == "waiting"
    finish(store, right, result={"updated": [101]})
    data = client.get("/api/v1/status").json()["batches"][0]
    assert data["state"] == "succeeded" and data["finished_at"]
    assert data["result"] == {"created": [101, 103], "updated": [101], "confirmed": [102]}


def test_rs17_admin_tree_is_paginated_readonly_and_requires_admin_session(client, store):
    root = root_batch(store)
    left, right = split(store, root.id)
    first, second = split(store, left)
    with store.read() as conn:
        before = list(conn.iterdump())
    response = client.get(f"/admin/api/batches/{root.id}/tree?limit=2&offset=1")
    assert response.status_code == 200
    tree = response.json()
    assert (tree["total"], tree["root_batch_id"], tree["subtree_batch_id"]) == (5, root.id, root.id)
    assert [row["id"] for row in tree["items"]] == [left, first]
    leaf = tree["items"][1]
    assert leaf["target_range"] == {"start": 0, "end": 1, "count": 1}
    assert leaf["split_path"] == "00" and leaf["context_mode"] == "full"
    assert leaf["attempts_remaining"] == 4 and leaf["gap"]["batch_id"] == first
    subtree = client.get(f"/admin/api/batches/{left}/tree").json()
    assert [row["id"] for row in subtree["items"]] == [left, first, second]
    assert client.get(f"/admin/api/batches/{root.id}").json()["can_relearn"] is False
    with store.read() as conn:
        assert list(conn.iterdump()) == before
    client.cookies.clear()
    assert client.get(f"/admin/api/batches/{root.id}/tree").status_code == 401


def test_rs17_more_than_twenty_children_do_not_evict_active_root(client, store):
    root = root_batch(store, 16)
    def visit(bid):
        if len(get_batch(store, bid).target_ids) == 1:
            finish(store, bid)
        else:
            for child in split(store, bid):
                visit(child)
    visit(root.id)
    data = client.get("/api/v1/status").json()["batches"]
    assert len(data) == 1 and data[0]["split"]["node_count"] == 31
    with store.read() as conn:
        leaf = conn.execute("SELECT id FROM batches WHERE split_path='0000'").fetchone()[0]
    finish(store, leaf, "refused")
    reset_batch(store, leaf)
    assert client.get("/api/v1/status").json()["batches"][0]["state"] == "waiting"


def test_rs17_twenty_terminal_roots_and_reactivated_old_tree_survive_window(client, store):
    from conftest import msg
    from iris.queue import form_batch
    old = root_batch(store, 2)
    left, right = split(store, old.id)
    finish(store, left, "refused")
    finish(store, right)
    later_roots = []
    for index in range(3, 25):
        msg(store, index, "独立根的消息")
        later = form_batch(store, "A", "rs-test")
        finish(store, later.id)
        later_roots.append(later.id)
    before = client.get("/api/v1/status").json()["batches"]
    assert [b["id"] for b in before] == later_roots[-20:]
    reset_batch(store, left)
    after = client.get("/api/v1/status").json()["batches"]
    assert [b["id"] for b in after] == [old.id] + later_roots[-20:]
    assert after[0]["state"] == "waiting"


def test_rs17_host_scope_filters_trees_and_real_child_call_ids(client, store):
    from iris.db import now
    a = root_batch(store, 2)
    b = root_batch(store, 2, entry="B")
    a_left, _ = split(store, a.id)
    b_left, _ = split(store, b.id)
    with store.write() as conn:
        for bid in (a_left, b_left):
            conn.execute("""INSERT INTO model_calls(purpose,model,duration_ms,created_at,batch_id,result_category)
                VALUES('learning','fake',1,?,?,'content_rejection')""", (now(), bid))
    token = client.app.state.tokens.create(host="only-a", scope={"kind": "entries", "entries": ["A"]}, actor="local_cli")
    client.headers["Authorization"] = "Bearer " + token["token"]
    data = client.get("/api/v1/status").json()
    assert [row["id"] for row in data["batches"]] == [a.id]
    assert [row["batch_id"] for row in data["learning_calls_24h"]] == [a_left]


def test_rs17_existing_admin_list_keeps_public_state_and_parent_is_not_relearnable(client, store):
    root = root_batch(store, 2)
    left, right = split(store, root.id)
    item = next(row for row in client.get("/admin/api/batches").json()["items"] if row["id"] == root.id)
    assert item["state"] == "waiting" and item["execution_state"] == "split"
    assert item["can_relearn"] is False
    detail = client.get(f"/admin/api/batches/{root.id}").json()
    assert detail["state"] == "waiting" and detail["execution_state"] == "split"
    finish(store, left, "refused")
    finish(store, right)
    item = next(row for row in client.get("/admin/api/batches").json()["items"] if row["id"] == root.id)
    assert item["state"] == "refused" and item["can_relearn"] is False
