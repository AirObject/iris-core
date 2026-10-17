"""MM-04: deterministic expiry of learning-created questions, without model calls."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from conftest import authorize_host, login_admin
from iris.api import create_app
from iris.db import Store
from iris.goals import Goals, GoalError
from iris.maintenance import Maintenance, PHASES


KEY = 'internal_question_expiry_days'


class Clock:
    def __init__(self, value='2026-10-18T04:00:00+00:00'):
        self.value = datetime.fromisoformat(value)

    def __call__(self):
        return self.value


def create(store, *, created='2026-10-10T01:00:00+00:00', origin='internal',
           kind='question', content='想了解小林的露营经历'):
    store.set_setting('goal_dedup_judge', {'enabled': False, 'method': 'A'})
    return Goals(store, clock=Clock(created)).create(
        content=content, origin=origin, kind=kind, actor='learning' if origin == 'internal' else origin)


def start(store, clock, *, enabled=False, review=True):
    # Isolate the goal-review phase so unrelated memory decay/model work cannot
    # change the promise-memory and notification assertions below.
    store.set_setting('consolidation', {'enabled': enabled, 'goal_review_enabled': review})
    engine = Maintenance(store, clock=clock)
    rid = engine.request()
    with store.write() as conn:
        conn.execute('UPDATE maintenance_runs SET phase=? WHERE id=?', (PHASES.index('goals'), rid))
    return engine, rid


def finish(store, clock, **settings):
    engine, rid = start(store, clock, **settings)
    engine.run(rid)
    return engine.report(rid)


def row(store, gid):
    with store.read() as conn:
        return dict(conn.execute('SELECT * FROM goals WHERE id=?', (gid,)).fetchone())


def expirations(store):
    with store.read() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM goal_revisions WHERE reason='question_expired' ORDER BY id")]


def test_expiry_closes_question_and_reports_only_metadata(store):
    goal = create(store)['goal']
    before = row(store, goal['id'])
    clock = Clock()
    report = finish(store, clock)
    after = row(store, goal['id'])
    assert after['state'] == 'abandoned'
    assert after['closed_by'] == 'maintenance'
    assert after['closed_at'] == clock().isoformat()
    assert after['revision'] == before['revision'] + 1
    assert after['content'] == before['content']
    assert after['created_at'] == before['created_at']
    audit, = expirations(store)
    assert audit['action'] == 'expire_question' and audit['actor'] == 'maintenance'
    assert (audit['revision_before'], audit['revision_after']) == (before['revision'], after['revision'])
    assert json.loads(audit['before_json']) == {'state': 'open', 'closed_at': None, 'closed_by': None}
    assert json.loads(audit['after_json']) == {
        'state': 'abandoned', 'closed_at': clock().isoformat(), 'closed_by': 'maintenance'}
    assert before['content'] not in json.dumps(audit, ensure_ascii=False)
    assert report['question_expiry'] == {'count': 1, 'goal_ids': [goal['id']]}
    assert report['summary']['questions_expired']['object_ids'] == [goal['id']]
    item, = [i for i in report['items'] if i['outcome'] == 'questions_expired']
    assert item['reason'] == 'question_expired'
    assert before['content'] not in json.dumps(item, ensure_ascii=False)


@pytest.mark.parametrize('zone,created', [
    ('Asia/Shanghai', '2026-10-10T15:59:59+00:00'),
    ('America/Los_Angeles', '2026-10-11T06:59:59+00:00'),
    ('America/New_York', '2026-10-31T23:50:00-04:00'),
])
@pytest.mark.parametrize('days', [1, 7, 365])
def test_day_n_retained_then_local_day_n_plus_one_expires(store, zone, created, days):
    store.set_setting('timezone', zone)
    store.set_setting('goals', {KEY: days})
    goal = create(store, created=created)['goal']
    local = datetime.fromisoformat(created).astimezone(ZoneInfo(zone))
    boundary = (local.replace(hour=0, minute=0, second=0, microsecond=0)
                + timedelta(days=days + 1)).astimezone(timezone.utc)
    clock = Clock((boundary - timedelta(microseconds=1)).isoformat())
    assert finish(store, clock)['question_expiry']['count'] == 0
    assert row(store, goal['id'])['state'] == 'open'
    clock.value = boundary
    assert finish(store, clock)['question_expiry']['goal_ids'] == [goal['id']]
    assert row(store, goal['id'])['state'] == 'abandoned'


@pytest.mark.parametrize('origin,kind', [
    ('host', 'question'), ('admin', 'question'),
    ('internal', 'normal'), ('host', 'normal'), ('admin', 'normal'),
])
def test_other_origins_and_normal_goals_are_unchanged(store, origin, kind):
    goal = create(store, origin=origin, kind=kind)['goal']
    before = row(store, goal['id'])
    assert finish(store, Clock())['question_expiry']['count'] == 0
    assert row(store, goal['id']) == before
    assert expirations(store) == []


@pytest.mark.parametrize('state', ['completed', 'abandoned'])
def test_already_closed_questions_are_unchanged(store, state):
    goal = create(store)['goal']
    Goals(store, clock=Clock()).update(goal['id'], expected_revision=goal['revision'], state=state)
    before = row(store, goal['id'])
    assert finish(store, Clock())['question_expiry']['count'] == 0
    assert row(store, goal['id']) == before
    assert expirations(store) == []


@pytest.mark.parametrize('origin', ['internal', 'host', 'admin'])
def test_merged_source_skipped_and_canonical_uses_own_origin_and_created_at(store, origin):
    target = create(store, created='2026-10-01T00:00:00+00:00', origin=origin)['goal']
    source = create(store, created='2026-10-17T00:00:00+00:00')
    assert source['dedup']['status'] == 'merged'
    source_before = row(store, source['submitted_id'])
    target_before = row(store, target['id'])
    report = finish(store, Clock())
    expected = [target['id']] if origin == 'internal' else []
    assert report['question_expiry']['goal_ids'] == expected
    assert [r['goal_id'] for r in expirations(store)] == expected
    assert row(store, source['submitted_id']) == source_before
    if origin != 'internal':
        assert row(store, target['id']) == target_before


def test_revision_conflict_skips_until_next_run(store):
    goal = create(store)['goal']
    engine, rid = start(store, Clock())
    with store.read() as conn:
        run = dict(conn.execute('SELECT * FROM maintenance_runs WHERE id=?', (rid,)).fetchone())
    candidate, = engine._candidates(run, 'goals')
    changed = Goals(store, clock=Clock()).update(goal['id'], expected_revision=goal['revision'],
        content='想了解小林这次露营的地点', actor='admin')
    engine._process_item(run, 'goals', candidate)
    engine.run(rid)
    assert row(store, goal['id'])['state'] == 'open'
    assert expirations(store) == []
    assert engine.report(rid)['summary']['skipped']['reasons']['revision_conflict'] == 1
    assert finish(store, Clock())['question_expiry']['count'] == 1
    assert expirations(store)[0]['revision_before'] == changed['revision']


@pytest.mark.parametrize('crash_after_commit', [False, True])
def test_restart_resumes_atomic_goal_audit_and_cursor(tmp_path, monkeypatch, crash_after_commit):
    path = tmp_path / 'restart.db'
    clock = Clock()
    original_store = Store(path)
    try:
        goal = create(original_store)['goal']
        engine, rid = start(original_store, clock)
        if crash_after_commit:
            original = engine._process_item
            def interrupt(*args):
                original(*args)
                raise KeyboardInterrupt()
            monkeypatch.setattr(engine, '_process_item', interrupt)
        else:
            def interrupt(*args):
                raise KeyboardInterrupt()
            monkeypatch.setattr(engine, '_record', interrupt)
        with pytest.raises(KeyboardInterrupt):
            engine.run(rid)
        assert len(expirations(original_store)) == int(crash_after_commit)
        assert row(original_store, goal['id'])['state'] == ('abandoned' if crash_after_commit else 'open')
    finally:
        original_store.close()
    restarted = Store(path)
    try:
        engine = Maintenance(restarted, clock=clock)
        engine.run(rid)
        engine.run(rid)
        assert engine.report(rid)['question_expiry']['goal_ids'] == [goal['id']]
        assert finish(restarted, clock)['question_expiry']['count'] == 0
        assert len(expirations(restarted)) == 1
    finally:
        restarted.close()


def test_run_cutoff_and_timezone_stay_fixed_across_resume(store):
    store.set_setting('timezone', 'America/Los_Angeles')
    goal = create(store, created='2026-10-10T23:30:00-07:00')['goal']
    clock = Clock('2026-10-18T06:59:59+00:00')
    engine, rid = start(store, clock)
    clock.value += timedelta(days=1)
    store.set_setting('timezone', 'Asia/Shanghai')
    engine.run(rid)
    assert row(store, goal['id'])['state'] == 'open'
    assert finish(store, clock)['question_expiry']['count'] == 1
    assert row(store, goal['id'])['closed_at'] == clock().isoformat()


def test_request_goal_bound_excludes_later_creation(store):
    first = create(store)['goal']
    engine, rid = start(store, Clock())
    later = create(store, content='想了解小陈的陶艺作品')['goal']
    engine.run(rid)
    assert engine.report(rid)['question_expiry']['goal_ids'] == [first['id']]
    assert row(store, later['id'])['state'] == 'open'
    assert finish(store, Clock())['question_expiry']['goal_ids'] == [later['id']]


@pytest.mark.parametrize('enabled,review', [(False, True), (True, False), (True, True)])
def test_expiry_does_not_depend_on_model_or_basis_review_switch(store, enabled, review):
    goal = create(store)['goal']
    assert finish(store, Clock(), enabled=enabled, review=review)['question_expiry']['goal_ids'] == [goal['id']]


def test_expiry_preserves_promise_memory_duplicates_and_notifications(store):
    from test_retrieval import put
    goal = create(store)['goal']
    other = create(store, content='想了解小陈的陶艺作品')['goal']
    mid = put(store, '我答应替小林照顾植物')
    with store.write() as conn:
        conn.execute('INSERT INTO goal_memories VALUES(?,?,1)', (goal['id'], mid))
        conn.execute("INSERT INTO goal_duplicates(goal_a,goal_b,status,created_at) VALUES(?,?,'possible',?)",
                     (goal['id'], other['id'], Clock()().isoformat()))
    tables = ('memories', 'memory_revisions', 'goal_memories', 'goal_duplicates', 'notifications', 'goal_reminder_plans')
    def snapshot():
        with store.read() as conn:
            return {name: [tuple(r) for r in conn.execute('SELECT * FROM ' + name)] for name in tables}
    before = snapshot()
    finish(store, Clock(), enabled=True)
    Goals(store, clock=Clock()).generate_notifications()
    assert row(store, goal['id'])['state'] == 'abandoned'
    assert snapshot() == before


@pytest.mark.parametrize('stored', [None, {}, {'overdue_reminders': False}])
def test_missing_setting_uses_seven_days_without_persisting_default(store, stored):
    from iris.goals import question_expiry_days
    if stored is not None:
        store.set_setting('goals', stored)
    with store.read() as conn:
        before = conn.execute("SELECT value_json FROM runtime_settings WHERE key='goals'").fetchone()
        assert question_expiry_days(conn) == 7
        after = conn.execute("SELECT value_json FROM runtime_settings WHERE key='goals'").fetchone()
        assert before == after


@pytest.mark.parametrize('value', [0, 366, -1, True, False, 7.0, '7', None])
def test_setting_rejects_invalid_write_and_stored_values(store, value):
    from iris.goals import question_expiry_days
    with pytest.raises(GoalError) as caught:
        Goals(store).configure_question_expiry(value)
    assert caught.value.field == KEY
    with store.read() as conn:
        assert question_expiry_days(conn) == 7
    store.set_setting('goals', {KEY: value})
    with store.read() as conn, pytest.raises(GoalError):
        question_expiry_days(conn)


@pytest.mark.parametrize('days', [1, 7, 365])
def test_setting_write_persists_and_existing_admin_patch_preserves_internal_key(store, days):
    from iris.goals import question_expiry_days
    Goals(store).configure_question_expiry(days)
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1',
                    client=('127.0.0.1', 1000)) as client:
        client.app.state.scheduler.stop()
        login_admin(client)
        response = client.patch('/admin/api/settings/goals', json={'default_reminder_minutes': 90, 'overdue_reminders': False})
        assert response.status_code == 200
        assert response.json()['goals'] == {'default_reminder_minutes': 90, 'overdue_reminders': False, KEY: days}
        assert client.patch('/admin/api/settings/goals', json={KEY: days}).status_code == 200
    with store.read() as conn:
        assert question_expiry_days(conn) == days
        assert json.loads(conn.execute("SELECT value_json FROM runtime_settings WHERE key='goals'").fetchone()[0]) == {
            KEY: days, 'default_reminder_minutes': 90, 'overdue_reminders': False}


def test_host_goal_fields_unchanged_when_expired(store):
    goal = create(store)['goal']
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1',
                    client=('127.0.0.1', 1000)) as client:
        client.app.state.scheduler.stop()
        authorize_host(client)
        before = client.get('/api/v1/goals').json()['items'][0]
        finish(store, Clock())
        after = client.get('/api/v1/goals?state=abandoned').json()['items'][0]
        assert before.keys() == after.keys()
        assert after['id'] == goal['id'] and after['state'] == 'abandoned'
        assert after['closed_by'] == 'maintenance'
        assert client.get('/api/v1/goals?state=open').json()['items'] == []


def test_expiry_cancels_inflight_dedup_without_publishing_or_merging(store):
    from test_goal_dedup_apply import Judge
    from iris.goals import write_learning_goal
    store.set_setting('goal_dedup_judge', {'enabled': True, 'method': 'C', 'budget_seconds': 1})
    clock = Clock('2026-10-10T01:00:00+00:00')
    judge = Judge(store)
    goals = Goals(store, clock=clock, gateway=judge)
    target = goals.create(content='想了解小林的露营经历', kind='question')['goal']
    with store.write() as conn:
        conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES('A','A','test','group')")
        new = write_learning_goal(conn, content='想了解小林的露营经验', kind='question', deadline=None,
                                  entry_id='A', evidence=[], current=clock())
    gid = new['submitted_id']
    assert new['dedup']['status'] == 'pending'
    clock.value = Clock()()
    judge.hook = lambda: finish(store, clock)
    result = goals.review(gid)
    assert result['goal']['state'] == 'abandoned'
    assert row(store, gid)['merged_into'] is None
    assert row(store, target['id'])['state'] == 'open'
    with store.read() as conn:
        assert conn.execute('SELECT state FROM goal_dedup_jobs WHERE goal_id=?', (gid,)).fetchone()[0] == 'cancelled'
        assert conn.execute('SELECT COUNT(*) FROM notifications').fetchone()[0] == 0
    assert goals.pending_reviews() == [] and goals.pull()['items'] == []


def test_invalid_stored_setting_fails_safely_without_closing(store):
    goal = create(store)['goal']
    store.set_setting('goals', {KEY: 0})
    report = finish(store, Clock())
    assert row(store, goal['id'])['state'] == 'open'
    assert expirations(store) == []
    assert report['question_expiry']['count'] == 0
    assert report['summary']['failed']['object_ids'] == [goal['id']]


def test_scheduled_full_maintenance_expires_old_internal_question(store):
    goal = create(store)['goal']
    clock = Clock()
    store.set_setting('consolidation', {'enabled': False})
    engine = Maintenance(store, clock=clock)
    rid = engine.request(trigger='scheduled', schedule_key='2026-10-18')
    engine.run(rid)
    assert engine.report(rid)['state'] == 'completed'
    assert engine.report(rid)['question_expiry']['goal_ids'] == [goal['id']]
