"""Design 17.7: abandoned batches retry on days 1, 3 and 7, then only keep the gap."""
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import iris.admin_data as admin_data
from conftest import FakeGateway, authorize_host, login_admin, msg
from iris.api import create_app
from iris.db import Store
from iris.learning import LearningEngine, PROMPT_VERSION
from iris.maintenance import Maintenance
from iris.models import ModelError
from iris.queue import claim_batch, form_batch, get_batch, reset_batch
from test_lifecycle import Clock, configure

DAY_ZERO = datetime.fromisoformat("2026-11-01T12:00:00+08:00")


def day(number, hour=4):
    return (DAY_ZERO+timedelta(days=number)).replace(hour=hour)


def fail_all_attempts(store, batch_id):
    engine = LearningEngine(store, FakeGateway(ModelError("retryable", "deterministic failure")))
    while get_batch(store, batch_id).state == "waiting":
        engine.run_batch(batch_id, force=True)
    assert get_batch(store, batch_id).state == "abandoned"


def abandoned(store, *, entry="A", at=DAY_ZERO):
    msg(store, 1, "我喜欢猫", entry=entry)
    batch_id = form_batch(store, entry, PROMPT_VERSION).id
    fail_all_attempts(store, batch_id)
    with store.write() as conn:
        conn.execute("UPDATE batches SET finished_at=? WHERE id=?", (at.isoformat(), batch_id))
    return batch_id


def maintain(store, current):
    maintenance = Maintenance(store, clock=Clock(current.isoformat()))
    rid = maintenance.request()
    maintenance.run(rid)
    return maintenance.report(rid)


def retried(report):
    return report["summary"]["batches_retried"]["object_ids"]


def skipped(report):
    return report["summary"]["skipped"]["reasons"]


def retry_rows(store, batch_id):
    with store.read() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT number,local_day FROM maintenance_batch_retries WHERE batch_id=? ORDER BY number", (batch_id,))]


def gap_count(store):
    with store.read() as conn:
        return conn.execute("SELECT COUNT(*) FROM memory_gaps").fetchone()[0]


def gap_view(store, monkeypatch, current):
    monkeypatch.setattr(admin_data, "utc_now", lambda: current)
    return admin_data.memory_gaps(store)["items"][0]["auto_retry"]


def retry_days_seen(store, batch_id, last_day):
    seen = []
    for number in range(last_day+1):
        if retried(maintain(store, day(number))) == [batch_id]:
            seen.append(number)
            fail_all_attempts(store, batch_id)
    return seen


def test_retries_on_days_1_3_7_after_first_abandonment_with_full_quota(store):
    batch_id = abandoned(store)
    report = maintain(store, day(1))
    assert retried(report) == [batch_id]
    item = next(i for i in report["items"] if i["outcome"] == "batches_retried")
    assert item["details"] == {"retry_number": 1, "schedule_day": 1, "missed_days": []}
    batch = get_batch(store, batch_id)
    assert batch.state == "waiting" and batch.attempt_count == 0
    fail_all_attempts(store, batch_id)  # finished_at moves to the new failure
    assert retry_days_seen(store, batch_id, 12)[0:2] == [3, 7]
    assert [r[1] for r in retry_rows(store, batch_id)] == ["2026-11-02", "2026-11-04", "2026-11-08"]
    assert [r[0] for r in retry_rows(store, batch_id)] == [1, 2, 3]
    with store.read() as conn:
        numbers = [r[0] for r in conn.execute("SELECT number FROM batch_attempts WHERE batch_id=? ORDER BY number", (batch_id,))]
    assert numbers == list(range(1, 17))  # four attempts per round, history keeps counting
    assert gap_count(store) == 1
    assert get_batch(store, batch_id).state == "abandoned"


def test_day_boundaries_use_the_role_timezone(store):
    store.set_setting("timezone", "Asia/Shanghai")
    # 2026-11-01 23:30 in Shanghai, written as a UTC instant.
    batch_id = abandoned(store, at=datetime.fromisoformat("2026-11-01T15:30:00+00:00"))
    assert retried(maintain(store, day(0, 23))) == []
    assert retried(maintain(store, day(1))) == [batch_id]


def test_batch_abandoned_after_todays_run_waits_for_tomorrow(store):
    batch_id = abandoned(store, at=day(0, 12))
    assert retried(maintain(store, day(0, 13))) == []
    assert get_batch(store, batch_id).state == "abandoned"


def test_outage_past_every_slot_catches_up_once(store):
    batch_id = abandoned(store)
    report = maintain(store, day(10))
    assert retried(report) == [batch_id]
    item = next(i for i in report["items"] if i["outcome"] == "batches_retried")
    assert item["details"] == {"retry_number": 1, "schedule_day": 7, "missed_days": [1, 3]}
    fail_all_attempts(store, batch_id)
    for number in range(11, 30):
        assert retried(maintain(store, day(number))) == []
    assert len(retry_rows(store, batch_id)) == 1


def test_outage_catches_up_once_and_keeps_later_slots(store):
    batch_id = abandoned(store)
    assert retried(maintain(store, day(4))) == [batch_id]  # days 1 and 3 collapse
    fail_all_attempts(store, batch_id)
    for number in (5, 6):
        assert retried(maintain(store, day(number))) == []
    assert retried(maintain(store, day(7))) == [batch_id]
    assert len(retry_rows(store, batch_id)) == 2


def test_same_batch_is_retried_at_most_once_on_the_same_day(store):
    batch_id = abandoned(store)
    assert retried(maintain(store, day(1))) == [batch_id]
    fail_all_attempts(store, batch_id)
    assert retried(maintain(store, day(1, 20))) == []
    store.set_setting("abandoned_retry_days", [1, 2, 3])
    assert retried(maintain(store, day(2))) == [batch_id]
    fail_all_attempts(store, batch_id)
    # A schedule edit cannot add a second retry to the same local date.
    with store.write() as conn:
        conn.execute("UPDATE batch_retry_schedules SET covered_day=1 WHERE batch_id=?", (batch_id,))
    report = maintain(store, day(2, 20))
    assert retried(report) == [] and skipped(report)["already_retried"] == 1


def test_switch_off_keeps_the_schedule_and_catches_up_once_when_on_again(store, monkeypatch):
    batch_id = abandoned(store)
    configure(store, abandoned_retry_enabled=False)
    for number in (1, 3, 7):
        assert retried(maintain(store, day(number))) == []
    assert retry_rows(store, batch_id) == []
    assert gap_view(store, monkeypatch, day(7))["status"] == "disabled"
    configure(store, abandoned_retry_enabled=True)
    assert retried(maintain(store, day(8))) == [batch_id]
    fail_all_attempts(store, batch_id)
    assert retried(maintain(store, day(9))) == []


def test_single_message_still_refused_is_never_retried(store, monkeypatch):
    msg(store, 1, "被拒绝的消息")
    batch_id = form_batch(store, "A", PROMPT_VERSION).id
    LearningEngine(store, FakeGateway(ModelError("content_rejection", "refused"))).run_batch(batch_id, force=True)
    with store.write() as conn:
        conn.execute("UPDATE batches SET finished_at=?", (DAY_ZERO.isoformat(),))
    assert get_batch(store, batch_id).state == "refused"
    for number in (1, 3, 7, 8):
        assert retried(maintain(store, day(number))) == []
    assert get_batch(store, batch_id).state == "refused"
    assert retry_rows(store, batch_id) == [] and gap_count(store) == 1
    assert gap_view(store, monkeypatch, day(8))["status"] == "refused"


def test_retry_that_ends_refused_stops_the_schedule(store, monkeypatch):
    batch_id = abandoned(store)
    assert retried(maintain(store, day(1))) == [batch_id]
    LearningEngine(store, FakeGateway(ModelError("content_rejection", "refused"))).run_batch(batch_id, force=True)
    assert get_batch(store, batch_id).state == "refused"
    for number in (3, 7):
        assert retried(maintain(store, day(number))) == []
    view = gap_view(store, monkeypatch, day(8))
    assert view["status"] == "refused" and view["attempted"] == 1


def test_cleared_targets_are_reported_once_and_never_retried(store, monkeypatch):
    batch_id = abandoned(store)
    with store.write() as conn:
        conn.execute("DELETE FROM messages")
    assert gap_view(store, monkeypatch, day(0, 13))["status"] == "targets_cleared"
    report = maintain(store, day(1))
    assert retried(report) == [] and skipped(report)["target_messages_cleared"] == 1
    later = maintain(store, day(3))
    assert retried(later) == [] and "target_messages_cleared" not in skipped(later)
    assert get_batch(store, batch_id).state == "abandoned"
    assert retry_rows(store, batch_id) == [] and gap_count(store) == 1


def test_retention_protects_only_the_retry_due_in_this_run(store):
    configure(store, message_retention_days=1)
    batch_id = abandoned(store)
    with store.write() as conn:
        conn.execute("UPDATE messages SET received_at=?", ((DAY_ZERO-timedelta(days=30)).isoformat(),))
    def message_count():
        with store.read() as conn:
            return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    report = maintain(store, day(1))
    assert retried(report) == [batch_id] and skipped(report)["batch_retry_pending"] == 1
    fail_all_attempts(store, batch_id)
    assert message_count() == 1
    assert "batch_retry_pending" not in skipped(maintain(store, day(2)))
    assert message_count() == 0
    report = maintain(store, day(3))
    assert retried(report) == [] and skipped(report)["target_messages_cleared"] == 1
    assert gap_count(store) == 1


def test_retention_does_not_wait_for_retries_while_the_switch_is_off(store):
    configure(store, message_retention_days=1, abandoned_retry_enabled=False)
    abandoned(store)
    with store.write() as conn:
        conn.execute("UPDATE messages SET received_at=?", ((DAY_ZERO-timedelta(days=30)).isoformat(),))
    maintain(store, day(1))
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_manual_relearn_neither_uses_nor_resets_automatic_retries(store):
    batch_id = abandoned(store)
    reset_batch(store, batch_id)
    fail_all_attempts(store, batch_id)  # finished_at is now the wall clock
    assert retry_rows(store, batch_id) == []
    assert retried(maintain(store, day(1))) == [batch_id]  # still anchored on the first abandonment
    fail_all_attempts(store, batch_id)
    reset_batch(store, batch_id)
    fail_all_attempts(store, batch_id)
    assert retried(maintain(store, day(2))) == []
    assert retried(maintain(store, day(3))) == [batch_id]
    assert [r[0] for r in retry_rows(store, batch_id)] == [1, 2]
    fail_all_attempts(store, batch_id)
    assert retried(maintain(store, day(7))) == [batch_id]
    fail_all_attempts(store, batch_id)
    # Three automatic retries are spent; a manual relearn still works.
    reset_batch(store, batch_id)
    assert get_batch(store, batch_id).attempt_count == 0
    fail_all_attempts(store, batch_id)
    assert retried(maintain(store, day(8))) == []


def test_slot_reached_during_a_manual_relearn_is_kept_for_the_next_run(store):
    batch_id = abandoned(store)
    assert retried(maintain(store, day(1))) == [batch_id]
    fail_all_attempts(store, batch_id)
    reset_batch(store, batch_id)
    assert retried(maintain(store, day(3))) == []  # still waiting on the manual round
    assert len(retry_rows(store, batch_id)) == 1
    fail_all_attempts(store, batch_id)
    assert retried(maintain(store, day(4))) == [batch_id]


def test_successful_retry_clears_the_gap_in_the_learning_transaction(store):
    batch_id = abandoned(store)
    assert gap_count(store) == 1
    assert retried(maintain(store, day(1))) == [batch_id]
    assert gap_count(store) == 1
    LearningEngine(store, FakeGateway({})).run_batch(batch_id, force=True)
    assert get_batch(store, batch_id).state == "succeeded"
    assert gap_count(store) == 0
    for number in (3, 7):
        assert retried(maintain(store, day(number))) == []


def test_interrupted_run_and_new_process_do_not_repeat_a_retry(tmp_path):
    path = tmp_path/"iris.db"
    store = Store(path)
    first = abandoned(store, entry="A")
    second = abandoned(store, entry="B")
    calls = {"count": 0}
    def stop():
        return calls["count"] >= 1
    maintenance = Maintenance(store, clock=Clock(day(1).isoformat()))
    original = maintenance._process_item
    def count_retries(run, phase, candidate):
        original(run, phase, candidate)
        calls["count"] += phase == "retry"
    maintenance._process_item = count_retries
    rid = maintenance.request()
    maintenance.run(rid, stop=stop)
    assert get_batch(store, first).state == "waiting" and get_batch(store, second).state == "abandoned"
    store.close()

    reopened = Store(path)
    try:
        restarted = Maintenance(reopened, clock=Clock(day(1, 5).isoformat()))
        restarted.run(rid)
        assert retried(restarted.report(rid)) == [first, second]
        assert [len(retry_rows(reopened, b)) for b in (first, second)] == [1, 1]
        fail_all_attempts(reopened, first)
        assert retried(maintain(reopened, day(1, 6))) == []
    finally:
        reopened.close()


def test_abandonment_by_restart_recovery_follows_the_same_schedule(store):
    batch_id = abandoned(store)
    assert retried(maintain(store, day(1))) == [batch_id]
    with store.write() as conn:
        conn.execute("UPDATE batches SET attempt_count=3 WHERE id=?", (batch_id,))
    claim_batch(store, batch_id, force=True)
    store.recover_inflight(current=lambda: day(1, 8))
    assert get_batch(store, batch_id).state == "abandoned"
    assert retried(maintain(store, day(2))) == []
    assert retried(maintain(store, day(3))) == [batch_id]


def test_configured_days_are_frozen_with_each_run(store):
    batch_id = abandoned(store)
    store.set_setting("abandoned_retry_days", [2])
    maintenance = Maintenance(store, clock=Clock(day(1).isoformat()))
    rid = maintenance.request()
    store.set_setting("abandoned_retry_days", [1])
    maintenance.run(rid)
    assert retried(maintenance.report(rid)) == []
    store.set_setting("abandoned_retry_days", [2])
    assert retried(maintain(store, day(2))) == [batch_id]
    fail_all_attempts(store, batch_id)
    assert retried(maintain(store, day(3))) == []


def test_run_accepted_before_the_setting_existed_uses_the_default_days(store):
    batch_id = abandoned(store)
    maintenance = Maintenance(store, clock=Clock(day(1).isoformat()))
    rid = maintenance.request()
    with store.write() as conn:
        settings = json.loads(conn.execute("SELECT settings_json FROM maintenance_runs WHERE id=?", (rid,)).fetchone()[0])
        del settings["abandoned_retry_days"]
        conn.execute("UPDATE maintenance_runs SET settings_json=? WHERE id=?", (json.dumps(settings), rid))
    maintenance.run(rid)
    assert retried(maintenance.report(rid)) == [batch_id]


def test_gap_page_projection_counts_and_next_date(store, monkeypatch):
    batch_id = abandoned(store)
    # Before the first run on day 1, the slot is that morning.
    assert gap_view(store, monkeypatch, day(0, 13)) == {
        "attempted": 0, "total": 3, "next_date": "2026-11-02", "status": "scheduled"}
    assert gap_view(store, monkeypatch, day(1, 2))["next_date"] == "2026-11-02"
    assert retried(maintain(store, day(1))) == [batch_id]
    assert gap_view(store, monkeypatch, day(1, 5)) == {
        "attempted": 1, "total": 3, "next_date": None, "status": "in_progress"}
    fail_all_attempts(store, batch_id)
    assert gap_view(store, monkeypatch, day(1, 5)) == {
        "attempted": 1, "total": 3, "next_date": "2026-11-04", "status": "scheduled"}
    # Overdue during an outage: shown as the next daily run, not a past date.
    assert gap_view(store, monkeypatch, day(5, 2))["next_date"] == "2026-11-06"
    assert gap_view(store, monkeypatch, day(5, 5))["next_date"] == "2026-11-07"
    for number in (3, 7):
        assert retried(maintain(store, day(number))) == [batch_id]
        fail_all_attempts(store, batch_id)
    assert gap_view(store, monkeypatch, day(8)) == {
        "attempted": 3, "total": 3, "next_date": None, "status": "finished"}


def test_gap_retried_today_never_shows_today_as_the_next_date(store, monkeypatch):
    batch_id = abandoned(store)
    assert retried(maintain(store, day(1))) == [batch_id]
    fail_all_attempts(store, batch_id)
    store.set_setting("abandoned_retry_days", [1, 2])
    with store.write() as conn:
        conn.execute("UPDATE batch_retry_schedules SET abandoned_at=? WHERE batch_id=?",
                     ((DAY_ZERO-timedelta(days=5)).isoformat(), batch_id))
    configure(store, maintenance_time="23:00")  # today's slot looks still ahead
    assert gap_view(store, monkeypatch, day(1, 5))["next_date"] == "2026-11-03"


@pytest.fixture
def client(store):
    with TestClient(create_app(store=store), base_url="http://127.0.0.1", client=("127.0.0.1", 1000)) as value:
        authorize_host(value)
        login_admin(value)
        yield value


def test_settings_api_reads_saves_and_validates_retry_days(client, store):
    assert client.get("/admin/api/settings").json()["lifecycle"]["abandoned_retry_days"] == [1, 3, 7]
    saved = client.patch("/admin/api/settings/lifecycle", json={"abandoned_retry_days": [2, 5]})
    assert saved.status_code == 200 and saved.json()["lifecycle"]["abandoned_retry_days"] == [2, 5]
    assert store.setting("abandoned_retry_days") == [2, 5]
    assert "abandoned_retry_days" not in store.setting("lifecycle")
    untouched = client.patch("/admin/api/settings/lifecycle", json={"abandoned_retry_enabled": False})
    assert untouched.json()["lifecycle"]["abandoned_retry_days"] == [2, 5]
    assert untouched.json()["lifecycle"]["abandoned_retry_enabled"] is False
    for invalid in ([], [3, 1], [1, 1], [0], [366], ["1"], [True], [1.5], list(range(1, 12)), None):
        response = client.patch("/admin/api/settings/lifecycle", json={"abandoned_retry_days": invalid})
        assert response.status_code == 400, invalid
    assert store.setting("abandoned_retry_days") == [2, 5]


def test_gap_endpoint_and_manual_relearn_route_share_the_schedule(client, store):
    batch_id = abandoned(store)
    assert retried(maintain(store, day(1))) == [batch_id]
    fail_all_attempts(store, batch_id)
    assert client.post(f"/admin/api/batches/{batch_id}/relearn", json={}).status_code == 200
    fail_all_attempts(store, batch_id)
    item = client.get("/admin/api/memory-gaps").json()["items"][0]
    assert item["auto_retry"]["attempted"] == 1 and item["auto_retry"]["total"] == 3
    assert not {"abandoned_at", "covered_day", "closed_reason", "retries", "last_retry_day", "targets_missing"} & set(item)


def test_upgrade_from_single_retry_keeps_history_and_schedules_the_rest(tmp_path, monkeypatch):
    import iris.db as db
    path = tmp_path/"old.db"
    root = db.files("iris")
    class OldScripts:
        def iterdir(self):
            return [p for p in root.joinpath("migrations").iterdir() if p.name < "036"]
    class OldRoot:
        def joinpath(self, *args):
            return OldScripts() if args == ("migrations",) else root.joinpath(*args)
    with monkeypatch.context() as patch:
        patch.setattr(db, "files", lambda _: OldRoot())
        old = Store(path)
        retried_once = abandoned(old, entry="A", at=day(2, 9))  # failed again after its one retry
        untouched = abandoned(old, entry="B", at=day(3, 1))
        with old.write() as conn:
            rid = conn.execute("""INSERT INTO maintenance_runs(trigger,state,settings_json,timezone,memory_through,
                message_through,batch_through,created_at,finished_at,goal_through)
                VALUES('manual','completed','{}','Asia/Shanghai',0,0,0,?,?,0)""",
                (day(1).isoformat(), day(1).isoformat())).lastrowid
            conn.execute("INSERT INTO maintenance_batch_retries VALUES(?,?,?)", (retried_once, rid, day(1).isoformat()))
        old.close()
    store = Store(path)
    try:
        with store.read() as conn:
            assert not conn.execute("PRAGMA foreign_key_check").fetchall()
            assert [tuple(r) for r in conn.execute("SELECT batch_id,number,run_id,local_day FROM maintenance_batch_retries")] == [
                (retried_once, 1, rid, "2026-11-02")]
            schedule = conn.execute("SELECT * FROM batch_retry_schedules").fetchall()
        assert [(r["batch_id"], r["covered_day"]) for r in schedule] == [(retried_once, 1)]
        assert schedule[0]["abandoned_at"] == "2026-10-31T20:00:00Z"  # the day before 11-02 04:00+08:00
        assert retried(maintain(store, day(3))) == [retried_once]  # day 3 of its original schedule
        assert retry_rows(store, retried_once)[-1][0] == 2
        fail_all_attempts(store, retried_once)
        assert retried(maintain(store, day(4))) == [untouched]  # day 1 from its own abandonment
        assert retried(maintain(store, day(5))) == []
    finally:
        store.close()
