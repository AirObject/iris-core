"""Read-only people graph: live evidence, scopes, identities and bounded output."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import sqlite3

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin
from iris.api import create_app
from iris import admin_graph, people
from iris.db import now
from test_people import person, link, say, confirm
from test_retrieval import put


@pytest.fixture
def client(store):
    with TestClient(create_app(store=store), base_url="http://127.0.0.1",
                    client=("127.0.0.1", 1000)) as client:
        login_admin(client)
        client.app.state.scheduler.stop()
        yield client


@pytest.fixture
def cast(store):
    person(store, "a", "小林")
    person(store, "b", "小周")
    person(store, "c", "船长")
    person(store, "d", "远客")
    return store


def edge(graph, a="a", b="b"):
    return next(e for e in graph["edges"] if (e["source"], e["target"]) == (a, b))


def test_active_about_pairs_count_each_memory_once_and_keep_relationship_summary(cast):
    message = say(cast, "a")
    second = say(cast, "a", key="2")
    shared = put(cast, "小林和小周一起做饭", about=["a", "b"], evidence=[message, second])
    relation = put(cast, "小林是小周的同事", about=["a", "b"], kind="关系")
    put(cast, "三个人一起出游", about=["a", "b", "c"])
    put(cast, "只有小周被涉及，船长是说话人", about=["b"], speaker="c")
    graph = admin_graph.people_graph(cast)
    assert edge(graph)["memory_count"] == 3
    assert edge(graph)["relationship_count"] == 1
    assert edge(graph)["relationship_summaries"] == [{"id": relation, "summary": "小林是小周的同事"}]
    assert edge(graph, "b", "c")["memory_count"] == 1  # speaker is not an about edge
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert nodes["b"]["memory_count"] == 4
    assert nodes["c"]["memory_count"] == 2
    detail = admin_graph.edge_evidence(cast, source_id="a", target_id="b")
    assert detail["total"] == 3
    assert {m["id"] for m in detail["items"]} >= {shared, relation}
    assert all(m["cooccurrence"] for m in detail["items"])


def test_forgetting_deleting_and_purging_remove_memory_projections(cast):
    active = put(cast, "有效经历", about=["a", "b"])
    forgotten = put(cast, "不可出现的遗忘正文", about=["a", "c"], lifecycle="forgotten")
    deleted = put(cast, "不可出现的删除正文", about=["a", "d"], lifecycle="deleted")
    purged = put(cast, "不可出现的清除正文", about=["b", "d"])
    with cast.write() as conn:
        conn.execute("UPDATE memories SET purged_at=? WHERE id=?", (now(), purged))
    graph = admin_graph.people_graph(cast)
    assert {n["id"] for n in graph["nodes"]} == {"self", "a", "b"}
    assert len(graph["edges"]) == 1
    assert admin_graph.edge_evidence(cast, source_id="a", target_id="b")["items"][0]["id"] == active
    for a, b in [("a", "c"), ("a", "d"), ("b", "d")]:
        assert admin_graph.edge_evidence(cast, source_id=a, target_id=b)["total"] == 0
    assert not any(str(mid) in {str(m["id"]) for m in admin_graph.edge_evidence(cast, source_id="a", target_id="b")["items"]}
                   for mid in (forgotten, deleted, purged))


def test_entry_scope_requires_direct_message_source_not_legacy_entry_or_derived_source(cast):
    first = say(cast, "a", entry="first")
    second = say(cast, "b", entry="second")
    direct = put(cast, "两个入口均有直接来源", about=["a", "b"], evidence=[first, second])
    put(cast, "只有旧入口字段", about=["a", "c"], entry="first")
    derived = put(cast, "派生不继承直接来源筛选", about=["a", "d"])
    with cast.write() as conn:
        conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,created_at) VALUES(?,'memory',?,?)",
                     (derived, direct, now()))
    graph = admin_graph.people_graph(cast, entry_id="first")
    assert [(e["source"], e["target"]) for e in graph["edges"]] == [("a", "b")]
    assert edge(graph)["memory_count"] == 1
    assert admin_graph.edge_evidence(cast, source_id="a", target_id="b", entry_id="second")["total"] == 1
    assert admin_graph.edge_evidence(cast, source_id="a", target_id="d", entry_id="first")["items"] == []


def test_live_alias_account_count_self_and_scene_policy(cast):
    with cast.write() as conn:
        conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES('a','阿林')")
        conn.execute("INSERT INTO platform_identities VALUES('a','other','other-a','小林')")
    put(cast, "角色与小林一起看景色", about=["self", "a", "scene"])
    graph = admin_graph.people_graph(cast)
    assert {n["id"] for n in graph["nodes"]} == {"self", "a"}
    node = next(n for n in graph["nodes"] if n["id"] == "a")
    assert node["aliases"] == ["阿林"] and node["account_count"] == 2
    assert next(n for n in graph["nodes"] if n["id"] == "self")["kind"] == "self"
    assert len(graph["edges"]) == 1


def test_links_keep_pending_identity_and_roleplay_direction_without_inventing_memory(cast):
    same = link(cast, "a", "b")
    role = link(cast, "c", "a", kind="roleplay", world="海岛游戏")
    link(cast, "b", "d", status="denied")
    resolved = link(cast, "c", "d")
    with cast.write() as conn:
        conn.execute("UPDATE subject_links SET resolved_at=? WHERE id=?", (now(), resolved))
    graph = admin_graph.people_graph(cast, min_memories=5)
    assert {n["id"] for n in graph["nodes"]} == {"a", "b", "c"}
    assert edge(graph)["memory_count"] == 0
    assert edge(graph)["links"][0]["id"] == same
    relationship = edge(graph, "a", "c")["links"][0]
    assert relationship["id"] == role and relationship["source"] == "c" and relationship["target"] == "a"
    assert relationship["worlds"] == ["海岛游戏"]
    assert admin_graph.edge_evidence(cast, source_id="a", target_id="b")["total"] == 0


def test_link_evidence_uses_active_same_source_memories_and_entry_scopes(cast):
    first = say(cast, "a", entry="first")
    second = say(cast, "b", entry="second")
    same = link(cast, "a", "b", evidence=first)
    shared_source = put(cast, "小林说自己的旧账号", about=["a"], evidence=[first])
    put(cast, "已遗忘的同来源记忆", about=["a"], evidence=[first], lifecycle="forgotten")
    link(cast, "b", "c", kind="roleplay", evidence=second)
    graph = admin_graph.people_graph(cast, entry_id="first")
    assert [l["id"] for e in graph["edges"] for l in e["links"]] == [same]
    detail = admin_graph.edge_evidence(cast, source_id="a", target_id="b", entry_id="first")
    assert detail["total"] == 1 and detail["items"][0]["id"] == shared_source
    assert not detail["items"][0]["cooccurrence"] and detail["items"][0]["link_evidence"]
    assert admin_graph.edge_evidence(cast, source_id="a", target_id="b", entry_id="second")["total"] == 0


def test_merged_identities_and_folded_link_evidence(cast):
    first = say(cast, "a", entry="first")
    second = say(cast, "b", entry="second")
    original = link(cast, "a", "c", kind="roleplay", world="海岛", evidence=first)
    link(cast, "b", "c", kind="roleplay", world="山林", evidence=second)
    identity = link(cast, "a", "b")
    put(cast, "小周与船长相识", about=["b", "c"], evidence=[second])
    confirm(cast, identity)
    graph = admin_graph.people_graph(cast, entry_id="second")
    assert "b" not in {n["id"] for n in graph["nodes"]}
    role = edge(graph, "a", "c")["links"][0]
    assert role["id"] == original and role["worlds"] == ["山林"]
    assert edge(graph, "a", "c")["memory_count"] == 1
    assert admin_graph.edge_evidence(cast, source_id="a", target_id="c", entry_id="second")["total"] == 1


def test_focus_one_two_hops_and_threshold_before_traversal(cast):
    put(cast, "林周同行", about=["a", "b"])
    put(cast, "周与船长同行", about=["b", "c"])
    put(cast, "船长与远客同行", about=["c", "d"])
    assert {n["id"] for n in admin_graph.people_graph(cast, person_id="a")["nodes"]} == {"a", "b"}
    assert {n["id"] for n in admin_graph.people_graph(cast, person_id="a", hops=2)["nodes"]} == {"a", "b", "c"}
    assert admin_graph.people_graph(cast, person_id="a", hops=2, min_memories=2)["edges"] == []
    assert {n["id"] for n in admin_graph.people_graph(cast, person_id="a", min_memories=2)["nodes"]} == {"a"}


def test_caps_keep_heavier_edges_and_stable_ties_with_no_dangling_endpoints(cast):
    put(cast, "林周同游", about=["a", "b"])
    put(cast, "林周一起看展", about=["a", "b"])
    put(cast, "周船长同行", about=["b", "c"])
    put(cast, "船长远客同行", about=["c", "d"])
    graph = admin_graph.people_graph(cast, node_limit=2, edge_limit=1)
    assert graph == admin_graph.people_graph(cast, node_limit=2, edge_limit=1)
    assert {n["id"] for n in graph["nodes"]} == {"a", "b"}
    assert len(graph["edges"]) == 1 and edge(graph)["memory_count"] == 2
    assert graph["truncated"] and graph["total_nodes"] == 5 and graph["total_edges"] == 3
    focused = admin_graph.people_graph(cast, person_id="c", hops=2, node_limit=2, edge_limit=1)
    assert "c" in {n["id"] for n in focused["nodes"]}
    assert all(e["source"] in {n["id"] for n in focused["nodes"]} and e["target"] in {n["id"] for n in focused["nodes"]}
               for e in focused["edges"])


def test_evidence_pagination_and_truncated_prose(cast):
    one = put(cast, "同事关系" * 40, about=["a", "b"], kind="关系")
    two = put(cast, "一起散步", about=["a", "b"])
    graph = admin_graph.people_graph(cast)
    summary = edge(graph)["relationship_summaries"][0]["summary"]
    assert len(summary) <= 81 and summary.endswith("…")
    first = admin_graph.edge_evidence(cast, source_id="b", target_id="a", limit=1)
    second = admin_graph.edge_evidence(cast, source_id="a", target_id="b", limit=1, offset=1)
    assert first["total"] == second["total"] == 2
    assert {first["items"][0]["id"], second["items"][0]["id"]} == {one, two}


def test_snapshot_does_not_wait_for_writer_and_does_not_change_usage(cast):
    put(cast, "已提交的同行", about=["a", "b"])
    with cast.read() as conn:
        before = {table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")]
                  for table in ("memories", "recalls", "model_calls", "admin_operations")}
    with ThreadPoolExecutor(max_workers=1) as pool:
        with cast.write() as writer:
            writer.execute("UPDATE memories SET lifecycle='forgotten'")
            graph = pool.submit(admin_graph.people_graph, cast).result(timeout=3)
            assert edge(graph)["memory_count"] == 1
            writer.execute("UPDATE memories SET lifecycle='active'")
    with cast.read() as conn:
        after = {table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")]
                 for table in before}
    assert before == after


def test_all_projection_queries_share_one_read_only_snapshot(cast, monkeypatch):
    put(cast, "同游", about=["a", "b"])
    original = cast.read
    connections = []

    @contextmanager
    def read():
        with original() as conn:
            connections.append(conn)
            assert conn.in_transaction
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("UPDATE subjects SET name=name")
            yield conn

    monkeypatch.setattr(cast, "read", read)
    admin_graph.people_graph(cast)
    assert len(connections) == 1


def test_routes_require_admin_session_and_reject_invalid_filters(client, cast):
    put(cast, "同游", about=["a", "b"])
    assert client.get("/admin/api/people-graph").status_code == 200
    assert client.get("/admin/api/people-graph/edge?source_id=a&target_id=b").status_code == 200
    for query in ("node_limit=101", "edge_limit=201", "hops=3", "min_memories=0", "node_limit=1", "extra=1"):
        assert client.get("/admin/api/people-graph?" + query).status_code == 400
    for query in ("source_id=a&target_id=a", "source_id=a&target_id=b&limit=101", "source_id=a"):
        assert client.get("/admin/api/people-graph/edge?" + query).status_code == 400
    assert client.get("/admin/api/people-graph?person_id=missing").status_code == 404
    assert client.get("/admin/api/people-graph?entry_id=missing").status_code == 404
    client.cookies.clear()
    assert client.get("/admin/api/people-graph").status_code == 401
    assert client.get("/admin/api/people-graph/edge?source_id=a&target_id=b").status_code == 401


def test_empty_graph_and_missing_evidence_return_explicit_empty_data(store):
    assert admin_graph.people_graph(store)["nodes"] == []
    assert not admin_graph.people_graph(store)["truncated"]


def test_explicit_focus_keeps_an_isolated_person_and_refresh_drops_old_evidence(cast):
    focused = admin_graph.people_graph(cast, person_id="d")
    assert [n["id"] for n in focused["nodes"]] == ["d"]
    assert focused["nodes"][0]["memory_count"] == 0 and focused["edges"] == []
    memory = put(cast, "共同经历", about=["a", "b"], kind="关系")
    assert edge(admin_graph.people_graph(cast))["relationship_count"] == 1
    with cast.write() as conn:
        conn.execute("UPDATE memories SET lifecycle='forgotten' WHERE id=?", (memory,))
    assert admin_graph.edge_evidence(cast, source_id="a", target_id="b")["items"] == []
    assert admin_graph.people_graph(cast)["edges"] == []
