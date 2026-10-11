"""GD2: pending review visibility and delayed reminder publication, without models."""
from contextlib import closing
from datetime import timedelta

import pytest

from iris.db import Store
from iris.goals import Goals, goal_list, goal_partition, write_learning_goal
from iris.models import ModelError
from iris.retrieval import Retrieval
from iris.scheduler import Scheduler
from test_goal_dedup_apply import Judge
from test_goals_api import Clock
from test_scheduler import wait_for


@pytest.fixture
def setup(store):
    store.set_setting('goal_dedup_judge', {'enabled': True, 'method': 'C2', 'budget_seconds': 1,
        'pending_fallback_minutes': 30, 'possible_min_similarity': 0})
    clock = Clock()
    judge = Judge(store, verdict=ModelError('retryable', 'fake timeout', reason='timeout'))
    judge.configs = {}
    goals = Goals(store, clock=clock, gateway=judge)
    with store.write() as conn:
        conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES('A','A','unit','private')")
    original = goals.create(content='整理操作手册', entry_id='A')['goal']
    return goals, judge, clock, original


def pending(setup, *, deadline=None, origin='host', kind='normal'):
    goals, _, clock, _ = setup
    fields = {'content': '整理操作说明手册', 'deadline': deadline, 'entry_id': 'A', 'kind': kind}
    if origin == 'internal':
        with goals.store.write() as conn:
            return write_learning_goal(conn, **fields, evidence=[], current=clock())
    return goals.create(**fields, origin=origin)


def ids(store, clock):
    with store.read() as conn:
        return [g['id'] for g in goal_partition(conn, entry_id='A', current=clock())]


@pytest.mark.parametrize('origin', ['host', 'internal', 'admin'])
def test_pending_hidden_from_prepare_search_but_admin_list_and_detail_show_it(store, setup, origin):
    goals, judge, clock, old = setup
    new = pending(setup, origin=origin)
    gid = new['submitted_id']
    assert new['dedup']['status'] == 'pending'
    assert new['goal']['dedup_pending'] is True
    assert new['goal']['dedup_injectable'] is False
    assert ids(store, clock) == [old['id']]
    retrieval = Retrieval(store, clock=clock)
    assert [r['id'] for r in retrieval.prepare('A', text='', judge=False)['goals']] == [old['id']]
    assert [r['id'] for r in retrieval.search(text='', entry_id='A', include_goals=True)['goals']] == [old['id']]
    with store.read() as conn:
        listed = goal_list(conn, current=clock())
    row = next(r for r in listed['items'] if r['id'] == gid)
    assert row['dedup_pending'] and not row['dedup_injectable']
    assert goals.get(gid)['dedup_review']['state'] == 'pending'
    assert goals.get(gid)['dedup_fallback_at'] is not None


@pytest.mark.parametrize('verdict', ['same', 'different', 'uncertain'])
def test_background_completion_releases_or_merges_and_notifies_once(store, setup, verdict):
    goals, judge, clock, old = setup
    new = pending(setup, deadline=(clock() - timedelta(minutes=1)).isoformat())
    gid = new['submitted_id']
    assert goals.pull()['items'] == []
    goals.generate_notifications()
    assert goals.pull()['items'] == []
    judge.verdict = verdict
    clock.advance(seconds=30)
    result = goals.review(gid)
    target = old['id'] if verdict == 'same' else gid
    assert not result['goal']['dedup_pending']
    assert result['goal']['dedup_injectable']
    assert target in ids(store, clock)
    notes = goals.pull()['items']
    reminders = [n for n in notes if n['kind'] == 'goal_reminder']
    assert len(reminders) == 1 and reminders[0]['goal_id'] == target
    assert reminders[0]['reminder_kind'] == 'immediate'
    assert len([n for n in notes if n['kind'] == 'goal_dedup_result']) == 1
    goals.generate_notifications()
    assert goals.pull(after=notes[-1]['id'])['items'] == []
    assert goals.get(gid)['state'] == 'open'


def test_fallback_is_from_first_enqueue_not_latest_retry_and_does_not_lose_cursor(store, setup):
    goals, judge, clock, _ = setup
    gid = pending(setup, deadline=(clock() + timedelta(minutes=5)).isoformat())['submitted_id']
    cutoff = goals.get(gid)['dedup_fallback_at']
    unrelated = goals.create(content='付电费', deadline=(clock() - timedelta(days=1)).isoformat())['goal']
    first = goals.pull()
    assert [n['goal_id'] for n in first['items']] == [unrelated['id']]
    clock.advance(minutes=29, seconds=59)
    goals.review(gid)
    assert goals.get(gid)['dedup_fallback_at'] == cutoff
    assert gid not in ids(store, clock)
    assert goals.pull(after=first['next_cursor'])['items'] == []
    clock.advance(seconds=1)
    row = goals.get(gid)
    assert row['dedup_pending'] and row['dedup_injectable']
    assert row['overdue'] and row['state'] == 'open'
    assert gid in ids(store, clock)
    released = goals.pull(after=first['next_cursor'])
    assert len(released['items']) == 1
    note = released['items'][0]
    assert note['id'] > first['next_cursor'] and note['goal_id'] == gid
    assert note['reminder_kind'] == 'immediate'
    assert '已过期' in note['content']
    assert note['published_at'] == row['dedup_fallback_at']
    assert goals.pending_reviews() == []  # The failed late attempt still has its own backoff.


def test_retry_delays_are_30_120_600_seconds_and_persist_on_restart(store, setup):
    goals, judge, clock, _ = setup
    gid = pending(setup)['submitted_id']
    for delay in (30, 120, 600, 600):
        assert goals.pending_reviews() == []
        clock.advance(seconds=delay - 1)
        assert goals.pending_reviews() == []
        clock.advance(seconds=1)
        with closing(Store(store.path)) as reopened:
            restarted = Goals(reopened, clock=clock, gateway=Judge(reopened, verdict=judge.verdict))
            assert restarted.pending_reviews() == [gid]
            restarted.review(gid)


def test_paused_purpose_still_releases_at_cap_without_model_or_attempts(store, setup):
    goals, judge, clock, _ = setup
    gid = pending(setup, deadline=(clock() + timedelta(minutes=5)).isoformat())['submitted_id']
    class Health:
        def snapshot(self): return {'goal_dedup_judge': {'state': 'temporarily_unavailable'}}
        def due_probes(self): return []
        def allowed(self, kind): return False
        def learning_allowed(self): return False
    judge.health = Health()
    calls = len(judge.calls)
    scheduler = Scheduler(store, judge, clock=clock)
    try:
        clock.advance(minutes=30)
        scheduler.tick()
        assert len(judge.calls) == calls
        assert gid in ids(store, clock)
        assert any(n['goal_id'] == gid for n in goals.pull()['items'])
        assert goals.get(gid)['dedup_review']['attempts'] == 1
    finally:
        scheduler.stop()


def test_internal_job_is_due_immediately_and_review_runs_while_learning_paused(store, setup):
    goals, judge, clock, _ = setup
    gid = pending(setup, origin='internal')['submitted_id']
    assert gid in goals.pending_reviews()
    judge.verdict = 'different'
    class Health:
        def snapshot(self): return {'goal_dedup_judge': {'state': 'normal'}}
        def due_probes(self): return []
        def allowed(self, kind): return kind == 'goal_dedup_judge'
        def learning_allowed(self): return False
    judge.health = Health()
    scheduler = Scheduler(store, judge, clock=clock)
    try:
        scheduler.tick()
        wait_for(lambda: not goals.get(gid)['dedup_pending'])
        assert gid in ids(store, clock)
    finally:
        scheduler.stop()


def test_closed_while_hidden_never_publishes_missed_reminder(store, setup):
    goals, _, clock, _ = setup
    gid = pending(setup, deadline=(clock() - timedelta(minutes=1)).isoformat())['submitted_id']
    goals.update(gid, state='completed')
    clock.advance(minutes=31)
    assert gid not in ids(store, clock)
    assert goals.pull()['items'] == []


def test_configurable_zero_cap_and_disable_release_pending_without_erasing_pending_label(store, setup):
    goals, _, clock, _ = setup
    gid = pending(setup)['submitted_id']
    options = store.setting('goal_dedup_judge')
    store.set_setting('goal_dedup_judge', {**options, 'pending_fallback_minutes': 0})
    assert gid in ids(store, clock) and goals.get(gid)['dedup_pending']
    store.set_setting('goal_dedup_judge', {**options, 'enabled': False})
    assert gid in ids(store, clock) and goals.get(gid)['dedup_pending']


def test_filter_precedes_limit_and_pending_duplicate_ids_are_not_injected(store, setup):
    goals, _, clock, old = setup
    gid = pending(setup, deadline=(clock()-timedelta(minutes=1)).isoformat())['submitted_id']
    with store.write() as conn:
        from iris.goals import _possible
        _possible(conn, old['id'], gid, clock())
    with store.read() as conn:
        partition = goal_partition(conn, entry_id='A', limit=1, current=clock())
    assert [r['id'] for r in partition] == [old['id']]
    assert partition[0]['possible_duplicate_ids'] == []
    assert goals.get(old['id'])['possible_duplicate_ids'] == [gid]


def test_changing_hidden_deadline_uses_latest_schedule_at_release(store, setup):
    goals, judge, clock, _ = setup
    original_deadline = (clock()+timedelta(minutes=1)).isoformat()
    gid = pending(setup, deadline=original_deadline)['submitted_id']
    new_deadline = (clock()+timedelta(hours=2)).isoformat()
    goals.update(gid, deadline=new_deadline, reminder_minutes=20)
    clock.advance(minutes=1)
    judge.verdict = 'different'
    goals.review(gid)
    first = goals.pull()
    assert not any(n['kind'] == 'goal_reminder' for n in first['items'])
    clock.advance(minutes=99)
    notes = goals.pull(after=first['next_cursor'])['items']
    assert len(notes) == 1 and notes[0]['reminder_kind'] == 'soon'
    assert notes[0]['goal_id'] == gid
    clock.advance(minutes=20)
    later = goals.pull(after=notes[-1]['id'])['items']
    assert len(later) == 1 and later[0]['reminder_kind'] == 'due'


def test_old_unclaimed_notifications_are_republished_after_later_cursor(store, setup):
    goals, judge, clock, _ = setup
    gid = pending(setup, deadline=(clock()-timedelta(minutes=1)).isoformat())['submitted_id']
    with store.write() as conn:
        # Simulate rows already published under the previous pending policy.
        old_ids = []
        for kind in ('goal_reminder', 'goal_dedup_result'):
            old_ids.append(conn.execute("""INSERT INTO notifications(kind,goal_id,reminder_kind,scheduled_at,published_at,content)
                VALUES(?,?,'immediate',?,?,?)""",(kind,gid,clock().isoformat(),clock().isoformat(),'旧政策通知')).lastrowid)
    other = goals.create(content='付电费', deadline=(clock()-timedelta(days=1)).isoformat())['goal']
    delivered = goals.pull()
    assert [n['goal_id'] for n in delivered['items']] == [other['id']]
    with store.read() as conn:
        assert {r[0] for r in conn.execute('SELECT status FROM notifications WHERE id IN (?,?)',old_ids)} == {'cancelled'}
    judge.verdict = 'different'
    clock.advance(seconds=30)
    goals.review(gid)
    released = goals.pull(after=delivered['next_cursor'])['items']
    assert len([n for n in released if n['kind'] == 'goal_reminder']) == 1
    assert len([n for n in released if n['content'] == '旧政策通知']) == 1
    assert all(n['id'] > delivered['next_cursor'] for n in released)
    assert goals.pull(after=released[-1]['id'])['items'] == []


def test_merged_into_another_pending_goal_delays_both_reminder_and_review_notice(store, setup):
    goals, judge, clock, _ = setup
    first = pending(setup, origin='internal')['submitted_id']
    second = pending(setup, origin='internal', deadline=(clock()-timedelta(minutes=1)).isoformat())['submitted_id']
    original = judge.goal_dedup_judge
    judge.verdict = 'different'
    def merge_first(messages, candidate_ids, **kwargs):
        result = original(messages,candidate_ids,**kwargs)
        result['decisions'][first] = 'same'
        return result
    judge.goal_dedup_judge = merge_first
    result = goals.review(second)
    assert result['dedup']['status'] == 'merged' and result['goal']['id'] == first
    assert result['goal']['dedup_pending']
    assert goals.pull()['items'] == []
    judge.goal_dedup_judge = original
    goals.review(first)
    notes = goals.pull()['items']
    assert len([n for n in notes if n['kind'] == 'goal_reminder']) == 1
    assert len([n for n in notes if n['kind'] == 'goal_dedup_result']) == 2
    assert all(n['goal_id'] == first for n in notes)


def test_real_host_routes_keep_receipt_contract_and_expose_pending_only_in_lists(store, setup, monkeypatch):
    from fastapi.testclient import TestClient
    from conftest import authorize_host, login_admin
    from iris import api
    goals, judge, clock, old = setup
    class FrozenRetrieval(api.HostRetrieval):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            self.clock = clock
    monkeypatch.setattr(api,'HostRetrieval',FrozenRetrieval)
    with TestClient(api.create_app(store=store,gateway=judge),base_url='http://127.0.0.1',
                    client=('127.0.0.1',1234)) as client:
        client.app.state.scheduler.stop()
        client.app.state.goals = goals
        authorize_host(client)
        login_admin(client)
        response = client.post('/api/v1/goals',json={'content':'整理操作说明手册','entry_id':'A',
            'deadline':(clock()-timedelta(minutes=1)).isoformat(),'host_key':'pending-once'})
        assert response.status_code == 201
        receipt = response.json()
        gid = receipt['submitted_id']
        assert receipt['dedup']['status'] == 'pending' and not receipt['goal']['dedup_injectable']
        prepared = client.post('/api/v1/entries/A/prepare',json={'text':'','judge':False}).json()
        assert [g['id'] for g in prepared['goals']] == [old['id']]
        searched = client.post('/api/v1/memories/search',json={'text':'','entry_id':'A','include_goals':True}).json()
        assert [g['id'] for g in searched['goals']] == [old['id']]
        assert client.get('/api/v1/notifications').json()['items'] == []
        listed = client.get('/admin/api/goals').json()
        assert next(g for g in listed['items'] if g['id']==gid)['dedup_pending']
        assert client.get(f'/admin/api/goals/{gid}').json()['dedup_review']['state'] == 'pending'
        clock.advance(minutes=30)
        prepared = client.post('/api/v1/entries/A/prepare',json={'text':'','judge':False}).json()
        assert gid in [g['id'] for g in prepared['goals']]
        assert any(g['id']==gid and g['dedup_pending'] for g in prepared['goals'])
        assert [n['goal_id'] for n in client.get('/api/v1/notifications').json()['items']] == [gid]
        assert client.post('/api/v1/goals',json={'content':'retry','host_key':'pending-once'}).json() == receipt


def test_already_taken_legacy_notification_stays_in_history_but_is_hidden_on_replay(store, setup):
    goals, _, clock, _ = setup
    gid = pending(setup)['submitted_id']
    with store.write() as conn:
        nid = conn.execute("""INSERT INTO notifications(kind,goal_id,scheduled_at,published_at,content,status,taken_at)
            VALUES('goal_reminder',?,?,?,'旧策略已取走','taken',?)""",
            (gid,clock().isoformat(),clock().isoformat(),clock().isoformat())).lastrowid
    assert goals.pull()['items'] == []
    with store.read() as conn:
        assert conn.execute('SELECT status FROM notifications WHERE id=?',(nid,)).fetchone()[0] == 'taken'
    clock.advance(minutes=30)
    assert [n['id'] for n in goals.pull()['items']] == [nid]
    assert goals.pull(after=nid)['items'] == []


@pytest.mark.parametrize('deadline_minutes,new_immediate', [(-1,False),(5,True),(50,False)])
def test_release_preserves_taken_phase_but_never_skips_a_new_due_phase(store, setup, deadline_minutes, new_immediate):
    from iris.goals import _publish, _row
    goals, _, clock, _ = setup
    gid = pending(setup, deadline=(clock()+timedelta(minutes=deadline_minutes)).isoformat())['submitted_id']
    with store.write() as conn:
        # Under the old policy a pending goal could already have been pulled by a host.
        nid = _publish(conn,_row(conn,gid),'immediate',clock().isoformat(),clock())
        conn.execute("UPDATE notifications SET status='taken',taken_at=? WHERE id=?",(clock().isoformat(),nid))
        conn.execute('UPDATE goals SET schedule_initialized=1 WHERE id=?',(gid,))
    assert goals.pull(after=nid)['items'] == []
    clock.advance(minutes=30)
    released = goals.pull(after=nid)
    assert len(released['items']) == int(new_immediate)
    if deadline_minutes == 50:
        clock.advance(minutes=20)
        assert [r['reminder_kind'] for r in goals.pull(after=released['next_cursor'])['items']] == ['due']
    elif deadline_minutes == -1:
        clock.advance(days=1)
        notes = goals.pull(after=released['next_cursor'])['items']
        assert len(notes) == 1 and notes[0]['reminder_kind'] == 'overdue'
