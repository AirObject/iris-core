from conftest import authorize_host
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin, msg
from iris.api import create_app
from iris.memory_ops import adjust_retention
from test_lifecycle import row
from test_retrieval import put
from test_scheduler import wait_for


@pytest.fixture
def client(store):
    with TestClient(create_app(store=store), base_url="http://127.0.0.1", client=("127.0.0.1",1000)) as client:
        authorize_host(client)
        login_admin(client)
        yield client


def test_admin_actions_expected_revision_history_recreate_and_purge(client, store):
    mid = put(store, "记忆正文")
    url = f"/admin/api/memories/{mid}"
    assert client.post(url+"/forget", json={"expected_revision": 2}).status_code == 409
    forgotten = client.post(url+"/forget", json={"expected_revision": 1}).json()
    assert forgotten["lifecycle"] == "forgotten" and forgotten["forgotten_at"]
    assert client.patch(url, json={"expected_revision": 1, "content": "编辑遗忘记忆"}).status_code == 200
    pinned = client.patch(url+"/lifecycle", json={"expected_revision": 2,"pinned": True,"importance": 80,"retention": 20}).json()
    assert pinned["pinned"] and pinned["revision"] == 3 and pinned["lifecycle"] == "forgotten"
    restored = client.post(url+"/restore", json={"expected_revision": 3}).json()
    assert restored["retention"] == 35 and restored["lifecycle"] == "active"
    assert client.request("DELETE",url,json={"expected_revision":3}).status_code == 200
    new = client.post(url+"/recreate",json={"expected_revision":4,"source_revision":1})
    assert new.status_code == 201 and new.json()["id"] != mid and new.json()["content"] == "记忆正文"
    assert client.post(url+"/purge",json={"expected_revision":4}).status_code == 400
    assert client.post(url+"/purge",json={"expected_revision":3,"confirm":True}).status_code == 409
    assert client.post(url+"/purge",json={"expected_revision":4,"confirm":True}).status_code == 200
    assert client.get(url).status_code == 404
    assert mid not in [i["id"] for i in client.get("/admin/api/memories?lifecycle=all").json()["items"]]
    assert client.post(url+"/recreate",json={"expected_revision":5,"source_revision":1}).status_code == 404


@pytest.mark.parametrize("settings", [
    {"forget_threshold":35,"restore_threshold":35},
    {"forget_threshold":0}, {"restore_threshold":101},
    {"decay_amount":-1}, {"feedback_increment":True},
    {"auto_delete_enabled":"false"}, {"auto_delete_days":0},
    {"maintenance_time":"24:00"}, {"maintenance_time":"3:00"},
    {"message_retention_days":False}, {"dependency_penalty":101}, {"unknown":1},
])
def test_settings_reject_invalid_atomically(client, store, settings):
    before = store.setting("lifecycle")
    response = client.patch("/admin/api/settings/lifecycle",json=settings)
    assert response.status_code == 400, response.text
    assert store.setting("lifecycle") == before


def test_settings_partial_update_immediate_feedback_and_no_revision(client, store):
    settings = client.get("/admin/api/settings").json()["lifecycle"]
    assert (settings["forget_threshold"],settings["restore_threshold"]) == (20,35)
    mid = put(store,"天文摄影")
    adjust_retention(store,mid,value=19)
    response = client.patch("/admin/api/settings/lifecycle",json={"feedback_increment":16,"maintenance_time":"04:30"})
    assert response.status_code == 200
    assert response.json()["lifecycle"]["auto_delete_days"] == 180
    recall = client.post("/api/v1/memories/search",json={"text":"天文摄影","include_forgotten":True}).json()
    assert recall["memories"][0]["lifecycle"] == "forgotten"
    assert row(store,mid)["retention"] == 19
    assert client.post("/api/v1/feedback",json={"recall_id":recall["recall_id"],"memory_ids":[mid]}).status_code == 200
    assert row(store,mid)["retention"] == 35 and row(store,mid)["revision"] == 1
    assert row(store,mid)["lifecycle"] == "active"


def test_upcoming_delete_window_disabled_and_read_only(client, store):
    from datetime import datetime, timezone
    current = datetime.now(timezone.utc)
    soon = put(store,"快到期")
    later = put(store,"还早")
    adjust_retention(store,soon,value=19,current=current-timedelta(days=170))
    adjust_retention(store,later,value=19,current=current-timedelta(days=150))
    before = row(store,soon)
    result = client.get("/admin/api/memories/upcoming-deletion").json()
    assert result["total"] == 1 and result["items"][0]["id"] == soon
    assert result["items"][0]["delete_after"]
    assert row(store,soon) == before
    client.patch("/admin/api/settings/lifecycle",json={"auto_delete_enabled":False})
    assert client.get("/admin/api/memories/upcoming-deletion").json()["items"] == []


def test_operations_type_object_time_pagination_and_safe_payloads(client, store):
    mid = put(store,"正文不能进入操作记录")
    client.patch(f"/admin/api/memories/{mid}",json={"expected_revision":1,"content":"新的私密正文"})
    msg(store,1,"消息正文不要记录")
    for _ in range(2):
        assert client.post("/api/v1/entries/A/learn").status_code == 200
    result = client.get("/admin/api/operations",params={"object_type":"memory","object_id":str(mid),"limit":1}).json()
    assert result["total"] >= 1 and len(result["items"]) == 1
    result = client.get("/admin/api/operations",params={"action":"learn_requested"}).json()
    assert result["total"] == 2 and all(i["actor"] == "test-host" for i in result["items"])
    assert client.get("/admin/api/operations?time_from=2100-01-01").json()["total"] == 0
    assert client.get("/admin/api/operations?time_from=2026-10-08&time_to=2026-10-07").status_code == 400
    assert client.get("/admin/api/operations?limit=0").status_code == 400
    all_ops = client.get("/admin/api/operations").text
    assert not any(text in all_ops for text in ("私密正文","消息正文","正文不能进入","deterministic-test-admin","iris_session"))


def test_manual_maintenance_returns_report_and_operations(client, store):
    mid = put(store,"衰减信息",importance=0)
    requested = client.post("/admin/api/maintenance",json={})
    assert requested.status_code == 202
    rid = requested.json()["run_id"]
    wait_for(lambda: client.get(f"/admin/api/maintenance/{rid}").json()["state"] == "completed")
    report = client.get(f"/admin/api/maintenance/{rid}").json()
    assert mid in report["summary"]["decayed"]["memory_ids"]
    assert client.get("/admin/api/maintenance").json()["items"][0]["id"] == rid
    ops = client.get("/admin/api/operations?object_type=maintenance").json()["items"]
    assert {i["action"] for i in ops} >= {"maintenance_completed","maintenance_requested"}


def test_admin_csrf_host_and_confirmation_remain_required(client, store):
    mid=put(store,"保护对象")
    csrf = client.headers.pop("X-Iris-CSRF")
    assert client.post(f"/admin/api/memories/{mid}/forget",json={"expected_revision":1}).status_code == 403
    assert client.post("/admin/api/maintenance",json={}).status_code == 403
    client.headers["X-Iris-CSRF"]=csrf
    assert client.post("/admin/api/maintenance",json={},headers={"Host":"attacker.invalid"}).status_code == 400
    assert row(store,mid)["lifecycle"] == "active"


def test_feedback_each_request_logged_including_repeat_rejection_and_schema_failure(client,store):
    mid=put(store,"天文摄影")
    recall=client.post("/api/v1/memories/search",json={"text":"天文摄影"}).json()
    payload={"recall_id":recall["recall_id"],"memory_ids":[mid]}
    assert client.post("/api/v1/feedback",json=payload).status_code == 200
    assert client.post("/api/v1/feedback",json=payload).status_code == 200
    assert client.post("/api/v1/feedback",json={**payload,"memory_ids":[999]}).status_code == 400
    assert client.post("/api/v1/feedback",json={"bad":"private text"}).status_code == 400
    assert client.get("/admin/api/operations?action=feedback").json()["total"] == 2
    rejected=client.get("/admin/api/operations?action=feedback_rejected").json()
    assert rejected["total"] == 2 and "private text" not in json.dumps(rejected)


def test_trial_writes_have_operations_without_body(client,store):
    entry=client.post("/admin/api/trial/entries",json={"name":"试用","kind":"private"}).json()
    speaker=client.post("/admin/api/trial/speakers",json={"name":"参与者"}).json()
    sent=client.post(f"/admin/api/trial/entries/{entry['id']}/messages",json={"speaker_id":speaker["id"],"content":"不要写到操作记录","dedupe_key":"trial-1"})
    assert sent.status_code == 201
    assert client.post(f"/admin/api/trial/entries/{entry['id']}/learn",json={}).status_code == 200
    ops=client.get("/admin/api/operations").json()
    assert {i["action"] for i in ops["items"]} >= {"trial_entry_created","trial_speaker_created","trial_message_received","learn_requested"}
    assert "不要写到操作记录" not in json.dumps(ops,ensure_ascii=False)
