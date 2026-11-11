"""G1: learning-created goals closed only on explicit, code-checked evidence."""
import json

import pytest

from iris.goals import (AUTO_REVIEW_KEY, AUTO_REVIEW_PURPOSE, DUPLICATE_AUTO_KEY, GoalConflict, GoalError, Goals,
                        auto_review_prompt, goal_settings)
from iris.models import ModelError
from goal_auto_helpers import (CREATED, Clock, Reviewer, basic, cite, enable, entry, goal, items, message, reviews,
                               row, run_goals, subject)


def test_switches_default_off_and_no_model_call(store):
    with store.read() as conn:
        settings = goal_settings(conn)
    assert settings[AUTO_REVIEW_KEY] is False and settings[DUPLICATE_AUTO_KEY] is False
    gid, _, _ = basic(store)
    reviewer = Reviewer(store, cite('completed'))
    engine, rid = run_goals(store, reviewer)
    assert reviewer.calls == [] and row(store, gid)['state'] == 'open' and reviews(store) == []
    report = engine.report(rid)
    assert report['goal_auto_review'] == {'enabled': False, 'skip_reason': 'disabled'}
    assert report['goal_duplicate_review']['enabled'] is False


def test_explicit_completion_closes_with_evidence_snapshot_and_report(store):
    gid, source, done = basic(store)
    enable(store)
    reviewer = Reviewer(store, cite('completed'))
    before = row(store, gid)
    clock = Clock()
    engine, rid = run_goals(store, reviewer, clock)
    call, = reviewer.calls
    assert call['purpose'] == AUTO_REVIEW_PURPOSE and call['max_tokens'] == 16000
    assert call['system'] == auto_review_prompt()
    payload = call['payload']
    assert [m['id'] for m in payload['sources']] == [source]
    assert [m['id'] for m in payload['messages']] == [done]
    assert payload['goal']['people'] == ['P1'] and payload['people'][0]['name'] == '程栗'
    after = row(store, gid)
    assert (after['state'], after['closed_by'], after['closed_at']) == ('completed', 'maintenance', clock().isoformat())
    assert after['revision'] == before['revision'] + 1 and after['content'] == before['content']
    record, = reviews(store, gid)
    assert (record['outcome'], record['decision'], record['reason']) == ('closed', 'completed', 'auto_completed')
    assert json.loads(record['evidence_json']) == [done] and record['revision_after'] == after['revision']
    assert record['latest_message_id'] == done and record['prompt_version'] == 'goal_review_auto_v1'
    with store.read() as conn:
        snapshot = dict(conn.execute("SELECT * FROM goal_revisions WHERE goal_id=? AND action='auto_review'", (gid,)).fetchone())
        operation = dict(conn.execute("SELECT * FROM admin_operations WHERE action='goal_auto_review'").fetchone())
    assert (snapshot['actor'], snapshot['reason']) == ('maintenance', 'auto_completed')
    assert json.loads(snapshot['after_json'])['state'] == 'completed'
    assert operation['actor'] == 'maintenance' and json.loads(operation['details_json'])['evidence'] == [done]
    item, = items(store, rid)
    assert (item['outcome'], item['reason'], item['object_id']) == ('goals_auto_completed', 'auto_completed', gid)
    assert item['details']['evidence'] == [done]
    report = engine.report(rid)
    assert report['goal_auto_review']['enabled'] is True and report['goal_auto_review']['calls'] == 1
    assert report['summary']['goals_auto_completed']['object_ids'] == [gid]


def test_explicit_cancellation_abandons(store):
    gid, _, done = basic(store)
    enable(store)
    run_goals(store, Reviewer(store, cite('abandoned')))
    assert row(store, gid)['state'] == 'abandoned'
    assert reviews(store, gid)[0]['reason'] == 'auto_abandoned'


def _kept(store, gid, reason):
    record, = reviews(store, gid)
    assert (record['outcome'], record['reason']) == ('kept_open', reason)
    assert row(store, gid)['state'] == 'open' and row(store, gid)['closed_by'] is None


@pytest.mark.parametrize('answer,reason', [
    ({'decision': 'open', 'evidence': [], 'reason': '还没做完'}, 'decision_open'),
    ({'decision': 'completed', 'evidence': [], 'reason': ''}, 'evidence_empty'),
    ({'decision': 'completed', 'evidence': [987654], 'reason': ''}, 'evidence_outside_material'),
    ({'decision': 'completed', 'evidence': ['1'], 'reason': ''}, 'invalid_output'),
    ({'decision': 'done', 'evidence': [], 'reason': ''}, 'invalid_output'),
    ({'decision': 'completed', 'evidence': [], 'note': 'extra'}, 'invalid_output'),
])
def test_rule_one_failures_keep_goal_open(store, answer, reason):
    gid, _, _ = basic(store)
    enable(store)
    run_goals(store, Reviewer(store, answer))
    _kept(store, gid, reason)


def test_unparseable_answer_after_repair_keeps_goal_open(store):
    gid, _, _ = basic(store)
    enable(store)
    run_goals(store, Reviewer(store, ModelError('retryable', 'JSON parse failed after repair', 'not json')))
    _kept(store, gid, 'invalid_output')


def test_rule_two_time_failure_when_only_a_source_before_creation_is_cited(store):
    gid, source, done = basic(store)
    enable(store)
    run_goals(store, Reviewer(store, cite('completed', lambda payload: [payload['sources'][0]['id']])))
    _kept(store, gid, 'evidence_not_after_creation')
    # Passing direction: one later qualifying message among the evidence suffices.
    message(store, '还有个事', at='2026-11-02T20:00:00+08:00')
    run_goals(store, Reviewer(store, cite('completed', lambda payload: [source, done])))
    assert row(store, gid)['state'] == 'completed'


def test_time_is_compared_as_instants_not_local_strings(store):
    subject(store, 'p1', '程栗')
    # 17:12 in UTC+8 is before the 09:13 UTC creation even though "17:12" > "09:13".
    early = message(store, '电热毯拿到了', at='2026-11-02T17:12:00+08:00')
    gid = goal(store)
    assert message(store, '随便聊聊', at='2026-11-02T17:14:00+08:00')
    enable(store)
    reviewer = Reviewer(store, cite('completed'))
    run_goals(store, reviewer)
    assert early not in [m['id'] for m in reviewer.calls[0]['payload']['messages']]


def test_rule_two_sender_failure_and_pass(store):
    subject(store, 'p1', '程栗')
    subject(store, 'p2', '路人')
    gid = goal(store, people=('p1',))
    proxy = message(store, '程栗那个快递我替她拿了', sender='p2', at='2026-11-02T18:00:00+08:00')
    with store.write() as conn:  # A later source (e.g. inherited) from someone not involved.
        conn.execute('INSERT INTO goal_sources(goal_id,message_id) VALUES(?,?)', (gid, proxy))
    message(store, '今天好冷', at='2026-11-02T18:30:00+08:00')
    enable(store)
    run_goals(store, Reviewer(store, cite('completed', lambda payload: [proxy])))
    _kept(store, gid, 'evidence_sender_not_involved')
    own = message(store, '我自己拿到了', at='2026-11-02T19:00:00+08:00')
    run_goals(store, Reviewer(store, cite('completed', lambda payload: [proxy, own])))
    assert row(store, gid)['state'] == 'completed'


def test_self_counts_as_involved(store):
    gid, _, _ = basic(store)
    mine = message(store, '电热毯取回来了，放在餐桌上', sender='self', kind='self_output', at='2026-11-02T19:04:00+08:00')
    enable(store)
    run_goals(store, Reviewer(store, cite('completed', lambda payload: [mine])))
    assert row(store, gid)['state'] == 'completed'


def test_rule_two_kind_failure_for_event_and_pass_for_action_result(store):
    subject(store, 'p1', '程栗')
    gid = goal(store)
    event = message(store, '程栗将群待办标记为完成', kind='event', at='2026-11-02T18:00:00+08:00')
    enable(store)
    run_goals(store, Reviewer(store, cite('completed', lambda payload: [event])))
    _kept(store, gid, 'evidence_kind_not_allowed')
    result = message(store, '取件成功', sender='self', kind='action_result', at='2026-11-02T18:30:00+08:00')
    run_goals(store, Reviewer(store, cite('completed', lambda payload: [result])))
    assert row(store, gid)['state'] == 'completed'


def test_people_empty_falls_back_to_source_senders(store):
    subject(store, 'p1', '程栗')
    subject(store, 'p2', '路人')
    source = message(store, '帮我记着周五交表', at='2026-11-02T17:00:00+08:00')
    gid = goal(store, '提醒程栗周五交表', people=(), sources=[source])
    done = message(store, '表交了', at='2026-11-02T19:00:00+08:00')
    other = message(store, '程栗表交了', sender='p2', at='2026-11-02T19:01:00+08:00')
    enable(store)
    reviewer = Reviewer(store, cite('completed', lambda payload: [done]))
    run_goals(store, reviewer)
    ids = [m['id'] for m in reviewer.calls[0]['payload']['messages']]
    assert done in ids and other not in ids
    assert row(store, gid)['state'] == 'completed'


@pytest.mark.parametrize('change', ['edit', 'complete', 'merge_origin'])
def test_rule_three_goal_changed_during_call_skips_without_fingerprint(store, change):
    gid, _, _ = basic(store)
    enable(store)
    goals = Goals(store, clock=Clock())

    def hook(payload):
        current = row(store, gid)
        if change == 'edit':
            goals.update(gid, expected_revision=current['revision'], content='帮程栗取两件快递', actor='admin')
        elif change == 'complete':
            goals.update(gid, expected_revision=current['revision'], state='completed', actor='admin')
        else:
            with store.write() as conn:
                conn.execute("UPDATE goals SET origin='host' WHERE id=?", (gid,))
    reviewer = Reviewer(store, cite('completed'), hook=hook)
    engine, rid = run_goals(store, reviewer)
    assert len(reviewer.calls) == 1 and reviews(store) == []
    assert engine.report(rid)['goal_auto_review']['revision_conflict'] == 1
    if change == 'edit':
        assert row(store, gid)['state'] == 'open'
        reviewer.hook = None
        run_goals(store, reviewer)  # New revision, new fingerprint: judged again.
        assert len(reviewer.calls) == 2 and row(store, gid)['state'] == 'completed'
    elif change == 'complete':
        assert row(store, gid)['closed_by'] == 'admin'
    else:
        assert row(store, gid)['state'] == 'open'


def test_fingerprint_prevents_repeat_calls_until_a_new_message(store):
    gid, _, _ = basic(store)
    enable(store)
    reviewer = Reviewer(store, {'decision': 'open', 'evidence': [], 'reason': '未完成'})
    run_goals(store, reviewer)
    engine, rid = run_goals(store, reviewer)
    assert len(reviewer.calls) == 1
    assert engine.report(rid)['goal_auto_review']['already_judged'] == 1
    message(store, '电热毯拿到啦', at='2026-11-02T21:00:00+08:00')
    run_goals(store, reviewer)
    assert len(reviewer.calls) == 2 and len(reviews(store, gid)) == 2


def test_interrupted_run_resumes_without_reprocessing(store):
    enable(store)
    subject(store, 'p1', '程栗')
    first = goal(store, '帮程栗取快递')
    second = goal(store, '帮程栗交水费')
    message(store, '快递和水费都弄好了', at='2026-11-02T19:00:00+08:00')
    stopped = []
    reviewer = Reviewer(store, cite('completed'), hook=lambda payload: stopped.append(True))
    engine, rid = run_goals(store, reviewer, stop=lambda: bool(stopped))
    assert len(reviewer.calls) == 1 and row(store, first)['state'] == 'completed'
    with store.read() as conn:
        assert conn.execute('SELECT state FROM maintenance_runs WHERE id=?', (rid,)).fetchone()[0] == 'running'
    reviewer.hook = None
    engine.run(rid)
    engine.run(rid)
    assert len(reviewer.calls) == 2 and [c['payload']['goal']['text'] for c in reviewer.calls] == ['帮程栗取快递', '帮程栗交水费']
    assert row(store, second)['state'] == 'completed'
    assert len(reviews(store)) == 2


def test_model_failure_is_retried_later_without_fingerprint(store):
    gid, _, _ = basic(store)
    enable(store)
    reviewer = Reviewer(store, ModelError('retryable', 'total timeout', reason='timeout'))
    engine, rid = run_goals(store, reviewer)
    assert reviews(store) == [] and row(store, gid)['state'] == 'open'
    item, = items(store, rid)
    assert (item['outcome'], item['reason']) == ('failed', 'timeout')
    reviewer.answer = cite('completed')
    run_goals(store, reviewer)
    assert len(reviewer.calls) == 2 and row(store, gid)['state'] == 'completed'


def test_paused_purpose_stops_the_pass(store):
    enable(store)
    subject(store, 'p1', '程栗')
    for text in ('帮程栗取快递', '帮程栗交水费'):
        goal(store, text)
    message(store, '都好了', at='2026-11-02T19:00:00+08:00')
    reviewer = Reviewer(store, ModelError('paused', 'paused', paused=True, reason='paused'))
    engine, rid = run_goals(store, reviewer)
    assert len(reviewer.calls) == 1
    assert engine.report(rid)['goal_auto_review']['stopped'] == 'paused'


@pytest.mark.parametrize('origin,kind', [('host', 'normal'), ('admin', 'normal'), ('internal', 'question')])
def test_host_admin_and_questions_never_enter_material(store, origin, kind):
    subject(store, 'p1', '程栗')
    gid = goal(store, origin=origin, kind=kind)
    message(store, '电热毯拿到了，搞定', at='2026-11-02T19:00:00+08:00')
    enable(store)
    reviewer = Reviewer(store, cite('completed'))
    run_goals(store, reviewer)
    assert reviewer.calls == [] and row(store, gid)['state'] == 'open'


def test_internal_goal_that_absorbed_a_host_goal_is_not_reviewed(store):
    subject(store, 'p1', '程栗')
    gid = goal(store)
    absorbed = goal(store, origin='host')
    with store.write() as conn:
        conn.execute('UPDATE goals SET merged_into=? WHERE id=?', (gid, absorbed))
    message(store, '电热毯拿到了', at='2026-11-02T19:00:00+08:00')
    enable(store)
    reviewer = Reviewer(store, cite('completed'))
    run_goals(store, reviewer)
    assert reviewer.calls == [] and row(store, gid)['state'] == 'open'


def test_no_candidate_message_means_no_model_call(store):
    subject(store, 'p1', '程栗')
    subject(store, 'p2', '路人')
    gid = goal(store)
    message(store, '早就拿到了', at='2026-11-02T16:00:00+08:00')  # before creation
    message(store, '程栗拿到了', sender='p2', at='2026-11-02T19:00:00+08:00')  # not involved
    enable(store)
    reviewer = Reviewer(store, cite('completed'))
    engine, rid = run_goals(store, reviewer)
    assert reviewer.calls == [] and engine.report(rid)['goal_auto_review']['no_candidates'] == 1


def test_candidates_prefer_goal_entry_and_keywords_then_recent_at_most_30(store):
    subject(store, 'p1', '程栗')
    gid = goal(store, '帮程栗取南门驿站的电热毯快递')
    for minute in range(40):
        message(store, f'闲聊{minute}', entry_id='e2', at=f'2026-11-02T18:{minute:02d}:00+08:00')
    keyword = message(store, '电热毯还在驿站吗', entry_id='e2', at='2026-11-02T17:20:00+08:00')
    home = message(store, '今天风好大', at='2026-11-02T17:30:00+08:00')
    enable(store)
    reviewer = Reviewer(store, {'decision': 'open', 'evidence': [], 'reason': ''})
    run_goals(store, reviewer)
    picked = [m['id'] for m in reviewer.calls[0]['payload']['messages']]
    assert len(picked) == 30 and keyword in picked and home in picked
    times = [m['at'] for m in reviewer.calls[0]['payload']['messages']]
    assert times == sorted(times)  # chronological in the material


def test_messages_from_more_restricted_entries_are_not_candidates(store):
    subject(store, 'p1', '程栗')
    gid = goal(store)
    with store.write() as conn:
        entry(conn, 'secret', visibility='entry_only')
    hidden = message(store, '电热毯拿到了', entry_id='secret', at='2026-11-02T19:00:00+08:00')
    shown = message(store, '在路上', at='2026-11-02T19:10:00+08:00')
    enable(store)
    reviewer = Reviewer(store, {'decision': 'open', 'evidence': [], 'reason': ''})
    run_goals(store, reviewer)
    picked = [m['id'] for m in reviewer.calls[0]['payload']['messages']]
    assert hidden not in picked and shown in picked


def test_closing_cancels_untaken_reminders(store):
    subject(store, 'p1', '程栗')
    source = message(store, '帮我取一下快递', at='2026-11-02T17:00:00+08:00')
    goals = Goals(store, clock=Clock(CREATED))
    store.set_setting('goal_dedup_judge', {'enabled': False})
    with store.write() as conn:
        from iris.goals import write_learning_goal
        gid = write_learning_goal(conn, content='帮程栗取快递', kind='normal', deadline='2026-11-02T20:00:00+08:00',
                                  entry_id='e1', evidence=[source], current=Clock(CREATED)())['submitted_id']
    Goals(store, clock=Clock()).generate_notifications()
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM notifications WHERE goal_id=? AND status='pending'", (gid,)).fetchone()[0]
    message(store, '快递拿到了', at='2026-11-02T19:00:00+08:00')
    enable(store)
    run_goals(store, Reviewer(store, cite('completed')))
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM notifications WHERE goal_id=? AND status='pending'", (gid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM goal_reminder_plans WHERE goal_id=? AND status='scheduled'", (gid,)).fetchone()[0] == 0
    assert goals.get(gid)['state'] == 'completed'


def test_reopen_sticks_until_new_evidence_then_can_be_reviewed_again(store):
    gid, _, _ = basic(store)
    enable(store)
    reviewer = Reviewer(store, cite('completed'))
    run_goals(store, reviewer)
    closed = row(store, gid)
    goals = Goals(store, clock=Clock())
    with pytest.raises(GoalConflict):
        goals.reopen(gid, expected_revision=closed['revision'] - 1)
    reopened = goals.reopen(gid, expected_revision=closed['revision'])
    assert reopened['state'] == 'open' and reopened['closed_by'] is None
    assert reopened['revision'] == closed['revision'] + 1
    with store.read() as conn:
        snapshot = dict(conn.execute("SELECT * FROM goal_revisions WHERE goal_id=? AND action='reopen'", (gid,)).fetchone())
    assert json.loads(snapshot['after_json'])['state'] == 'open' and snapshot['actor'] == 'admin'
    engine, rid = run_goals(store, reviewer)
    assert len(reviewer.calls) == 1 and engine.report(rid)['goal_auto_review']['no_candidates'] == 1
    assert row(store, gid)['state'] == 'open'
    later = message(store, '这次真的收到了', at='2026-11-03T10:00:00+08:00')
    run_goals(store, reviewer)
    assert len(reviewer.calls) == 2 and [m['id'] for m in reviewer.calls[1]['payload']['messages']] == [later]
    assert row(store, gid)['state'] == 'completed'


def test_only_automatic_closures_can_be_reopened(store):
    gid, _, _ = basic(store)
    goals = Goals(store, clock=Clock())
    closed = goals.update(gid, expected_revision=row(store, gid)['revision'], state='completed', actor='admin')
    with pytest.raises(GoalError):
        goals.reopen(gid, expected_revision=closed['revision'])


def test_crash_after_commit_is_not_judged_again_and_crash_before_commit_is_retried(tmp_path, monkeypatch):
    from iris.db import Store
    from iris import goals as goals_module
    from iris.maintenance import Maintenance
    path = tmp_path / 'crash.db'
    store = Store(path)
    try:
        enable(store)
        subject(store, 'p1', '程栗')
        first = goal(store, '帮程栗取快递')
        second = goal(store, '帮程栗交水费')
        message(store, '快递和水费都弄好了', at='2026-11-02T19:00:00+08:00')
        original = goals_module.apply_goal_review
        calls = []

        def crash(conn, current, **kwargs):
            calls.append(kwargs['material']['goal_id'])
            if len(calls) == 1:
                return original(conn, current, **kwargs)  # committed, then the process dies
            raise KeyboardInterrupt()  # dies inside the second write: rolled back
        monkeypatch.setattr(goals_module, 'apply_goal_review', crash)
        reviewer = Reviewer(store, cite('completed'))
        with pytest.raises(KeyboardInterrupt):
            run_goals(store, reviewer)
        assert row(store, first)['state'] == 'completed' and row(store, second)['state'] == 'open'
        assert [r['goal_id'] for r in reviews(store)] == [first]
        with store.read() as conn:
            rid = conn.execute("SELECT id FROM maintenance_runs WHERE state='running'").fetchone()[0]
    finally:
        store.close()
    monkeypatch.undo()
    restarted = Store(path)
    try:
        resumed = Reviewer(restarted, cite('completed'))
        Maintenance(restarted, gateway=resumed, clock=Clock()).run(rid)
        assert [c['payload']['goal']['text'] for c in resumed.calls] == ['帮程栗交水费']
        assert row(restarted, second)['state'] == 'completed'
        assert len(reviews(restarted)) == 2
    finally:
        restarted.close()


def test_switch_turned_off_after_the_run_was_accepted_stops_calls(store):
    gid, _, _ = basic(store)
    enable(store)
    store.set_setting('consolidation', {'enabled': False})
    store.set_setting('scheduled_backup', {'enabled': False})
    from iris.maintenance import Maintenance, PHASES
    reviewer = Reviewer(store, cite('completed'))
    engine = Maintenance(store, gateway=reviewer, clock=Clock())
    rid = engine.request()
    enable(store, review=False)
    with store.write() as conn:
        conn.execute('UPDATE maintenance_runs SET phase=? WHERE id=?', (PHASES.index('goals'), rid))
    engine.run(rid)
    assert reviewer.calls == [] and row(store, gid)['state'] == 'open'
    assert engine.report(rid)['goal_auto_review'] == {'enabled': True, 'stopped': 'disabled'}
