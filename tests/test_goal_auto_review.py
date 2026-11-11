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


def test_own_source_is_never_evidence_but_a_later_candidate_alongside_it_is(store):
    gid, source, done = basic(store)
    enable(store)
    run_goals(store, Reviewer(store, cite('completed', lambda payload: [payload['sources'][0]['id']])))
    _kept(store, gid, 'evidence_only_sources')
    # Passing direction: one later qualifying candidate among the evidence suffices.
    message(store, '还有个事', at='2026-11-02T20:00:00+08:00')
    run_goals(store, Reviewer(store, cite('completed', lambda payload: [source, done])))
    assert row(store, gid)['state'] == 'completed'
    record = reviews(store, gid)[-1]
    with store.read() as conn:
        protected = [r[0] for r in conn.execute('SELECT message_id FROM goal_auto_review_evidence WHERE review_id=?',
                                                (record['id'],))]
    assert protected == [done]


def test_sources_inherited_by_a_merge_are_not_evidence_even_when_later(store):
    from iris.goals import _merge, _row
    subject(store, 'p1', '程栗')
    source = message(store, '能帮我取一下南门驿站的电热毯吗', at='2026-11-02T17:10:00+08:00')
    gid = goal(store, '帮程栗取南门驿站的电热毯', sources=[source])
    intent = message(store, '我明天自己去驿站把电热毯拿回来', at='2026-11-02T18:00:00+08:00')
    later = goal(store, '帮程栗取南门驿站电热毯', sources=[intent], created='2026-11-02T10:05:00+00:00')
    with store.write() as conn:
        _merge(conn, _row(conn, gid), _row(conn, later), Clock()(), actor='admin', semantic=True)
    assert row(store, later)['merged_into'] == gid
    chat = message(store, '今天好冷', at='2026-11-02T19:00:00+08:00')
    enable(store)
    reviewer = Reviewer(store, cite('completed', lambda payload: [intent]))
    run_goals(store, reviewer)
    payload = reviewer.calls[0]['payload']
    assert intent in [m['id'] for m in payload['sources']] and intent not in [m['id'] for m in payload['messages']]
    _kept(store, gid, 'evidence_only_sources')
    done = message(store, '电热毯拿回来了，放餐桌上了', at='2026-11-02T20:00:00+08:00')
    run_goals(store, Reviewer(store, cite('completed', lambda payload: [intent, done])))
    assert row(store, gid)['state'] == 'completed' and chat


def _apply(store, gid, material, evidence):
    from iris.goals import apply_goal_review
    with store.write() as conn:
        return apply_goal_review(conn, Clock()(), material=material,
                                 result={'decision': 'completed', 'evidence': evidence})


def test_code_checks_reject_early_and_uninvolved_evidence_even_in_a_crafted_material(store):
    """Selection already excludes these; the write-time checks must hold on their own."""
    from iris.goals import auto_review_material
    gid, source, done = basic(store)
    early = message(store, '电热毯拿到了', at='2026-11-02T17:12:00+08:00')
    proxy = message(store, '程栗的电热毯我替她拿了', sender='p2', at='2026-11-02T18:00:00+08:00')
    with store.read() as conn:
        material = auto_review_material(conn, gid, current=Clock()())
    assert early not in material['candidate_ids'] and proxy not in material['candidate_ids']
    for extra, reason in ((early, 'evidence_not_after_creation'), (proxy, 'evidence_sender_not_involved')):
        crafted = {**material, 'message_ids': [*material['message_ids'], extra],
                   'candidate_ids': [*material['candidate_ids'], extra], 'latest_message_id': extra + 1000}
        assert _apply(store, gid, crafted, [extra])['reason'] == reason
        assert row(store, gid)['state'] == 'open'
    crafted = {**material, 'latest_message_id': material['latest_message_id'] + 2000}
    assert _apply(store, gid, crafted, [done])['outcome'] == 'closed'


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


def test_uninvolved_sender_never_becomes_a_candidate(store):
    subject(store, 'p1', '程栗')
    subject(store, 'p2', '路人')
    gid = goal(store, people=('p1',))
    proxy = message(store, '程栗那个快递我替她拿了', sender='p2', at='2026-11-02T18:00:00+08:00')
    message(store, '今天好冷', at='2026-11-02T18:30:00+08:00')
    enable(store)
    reviewer = Reviewer(store, cite('completed', lambda payload: [proxy]))
    run_goals(store, reviewer)
    assert proxy not in [m['id'] for m in reviewer.calls[0]['payload']['messages']]
    _kept(store, gid, 'evidence_outside_material')
    own = message(store, '我自己拿到了', at='2026-11-02T19:00:00+08:00')
    run_goals(store, Reviewer(store, cite('completed', lambda payload: [own])))
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


def test_newest_qualified_messages_are_kept_when_priority_messages_fill_the_material(store):
    subject(store, 'p1', '程栗')
    gid = goal(store, '帮程栗取南门驿站的电热毯快递')
    for minute in range(35):
        message(store, f'电热毯快递的进展{minute}', at=f'2026-11-02T18:{minute:02d}:00+08:00')
    retraction = message(store, '刚才说错了，还没弄完', entry_id='e2', at='2026-11-02T19:30:00+08:00')
    enable(store)
    reviewer = Reviewer(store, {'decision': 'open', 'evidence': [], 'reason': ''})
    run_goals(store, reviewer)
    picked = [m['id'] for m in reviewer.calls[0]['payload']['messages']]
    assert len(picked) == 30 and retraction in picked
    # A newer qualified message changes the fingerprint and is judged again.
    run_goals(store, reviewer)
    assert len(reviewer.calls) == 1
    message(store, '这次真的弄完了', entry_id='e3', at='2026-11-02T20:00:00+08:00')
    run_goals(store, reviewer)
    assert len(reviewer.calls) == 2


def test_priority_order_still_fills_beyond_the_newest_ten(store):
    subject(store, 'p1', '程栗')
    gid = goal(store, '帮程栗取南门驿站的电热毯快递')
    keyword = message(store, '电热毯还在驿站', entry_id='e2', at='2026-11-02T17:20:00+08:00')
    for minute in range(25):
        message(store, f'闲聊{minute}', entry_id='e3', at=f'2026-11-02T18:{minute:02d}:00+08:00')
    enable(store)
    reviewer = Reviewer(store, {'decision': 'open', 'evidence': [], 'reason': ''})
    run_goals(store, reviewer)
    picked = [m['id'] for m in reviewer.calls[0]['payload']['messages']]
    assert len(picked) == 26 and keyword in picked
    assert gid


def _gateway(store, clock, hits, *, limit_used):
    import httpx
    from iris.models import Gateway, ModelConfig
    from iris.model_health import ModelHealth

    def respond(request):
        hits.append(request.url.path)
        body = json.loads(request.content)
        material = json.loads(body['messages'][-1]['content'])
        if 'incoming' in material:
            content = json.dumps({'decisions': [{'id': c['id'], 'verdict': 'same'} for c in material['candidates']]})
        else:
            content = json.dumps({'decision': 'completed', 'evidence': [material['messages'][-1]['id']], 'reason': 'x'})
        return httpx.Response(200, json={'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}],
                                         'usage': {'prompt_tokens': 1, 'completion_tokens': 1}})
    configs = {'chat': ModelConfig('https://g-c1.invalid/v1', '', 'unit')}
    health = ModelHealth(store, configs, clock=clock)
    health.set_daily_token_limit(10)
    if limit_used:
        with store.write() as conn:
            conn.execute("""INSERT INTO model_calls(purpose,model,duration_ms,prompt_tokens,completion_tokens,
                result_category,created_at) VALUES('learning','unit',1,1000,1000,'success',?)""", (clock().isoformat(),))
    client = httpx.Client(transport=httpx.MockTransport(respond))
    return Gateway(configs, store, health=health, clock=clock, client=client, sleeper=lambda s: None), client


@pytest.mark.parametrize('limit_used', [True, False])
def test_daily_token_limit_stops_g1_without_requests_or_fingerprints(store, limit_used):
    gid, _, _ = basic(store)
    enable(store)
    clock, hits = Clock(), []
    gateway, client = _gateway(store, clock, hits, limit_used=limit_used)
    try:
        engine, rid = run_goals(store, gateway, clock)
    finally:
        gateway.close()
        client.close()
    if limit_used:
        assert hits == [] and row(store, gid)['state'] == 'open' and reviews(store) == []
        summary = engine.report(rid)['goal_auto_review']
        assert summary['stopped'] == 'usage_limit' and summary['calls'] == 0
        assert [i for i in items(store, rid) if i['outcome'] == 'failed'] == []
    else:
        assert len(hits) == 1 and row(store, gid)['state'] == 'completed'


@pytest.mark.parametrize('limit_used', [True, False])
def test_daily_token_limit_also_stops_g2(store, limit_used):
    subject(store, 'p1', '程栗')
    first = goal(store, '周五前整理操作手册', people=(), created='2026-11-02T09:00:00+00:00')
    second = goal(store, '周五前整理操作说明手册', people=(), created='2026-11-02T10:00:00+00:00')
    store.set_setting('goal_dedup_judge', {'enabled': True, 'budget_seconds': 1})
    with store.write() as conn:
        conn.execute("INSERT INTO goal_duplicates(goal_a,goal_b,status,created_at) VALUES(?,?,'possible',?)",
                     (first, second, CREATED))
    enable(store, review=False, duplicates=True)
    clock, hits = Clock(), []
    gateway, client = _gateway(store, clock, hits, limit_used=limit_used)
    try:
        engine, rid = run_goals(store, gateway, clock)
    finally:
        gateway.close()
        client.close()
    if limit_used:
        assert hits == [] and row(store, second)['merged_into'] is None
        assert engine.report(rid)['goal_duplicate_review']['stopped'] == 'usage_limit'
    else:
        assert len(hits) == 1 and row(store, second)['merged_into'] == first


@pytest.mark.parametrize('reopened', [True, False])
def test_merge_keeps_the_reopen_watermark(store, reopened):
    """G1 closes X, the administrator reopens X, G2 merges X into Y: Y keeps X's rejection."""
    gid, source, done = basic(store)
    enable(store)
    run_goals(store, Reviewer(store, cite('completed', lambda payload: [done])))
    if reopened:
        Goals(store, clock=Clock()).reopen(gid, expected_revision=row(store, gid)['revision'])
    else:
        with store.write() as conn:  # Same goal reopened without a watermark (control).
            conn.execute("UPDATE goals SET state='open',closed_at=NULL,closed_by=NULL,revision=revision+1 WHERE id=?", (gid,))
    earlier = goal(store, '帮程栗取南门驿站电热毯的快递', created='2026-11-02T09:00:00+00:00')
    store.set_setting('goal_dedup_judge', {'enabled': True, 'budget_seconds': 1})
    with store.write() as conn:
        conn.execute("INSERT INTO goal_duplicates(goal_a,goal_b,status,created_at) VALUES(?,?,'possible',?)",
                     (min(earlier, gid), max(earlier, gid), CREATED))

    class Both(Reviewer):
        def goal_dedup_judge(self, messages, ids, *, budget_seconds):
            return {'decisions': {i: 'same' for i in ids}}
    enable(store, review=True, duplicates=True)
    reviewer = Both(store, cite('completed', lambda payload: [done]))
    run_goals(store, reviewer)
    assert row(store, gid)['merged_into'] == earlier
    # The survivor of this run's merge is left alone until the next run.
    assert row(store, earlier)['state'] == 'open' and reviewer.calls == []
    run_goals(store, reviewer, Clock('2026-11-04T01:00:00+00:00'))
    if reopened:
        assert row(store, earlier)['auto_review_after_message_id'] == row(store, gid)['auto_review_after_message_id']
        assert row(store, earlier)['state'] == 'open' and reviewer.calls == []
    else:
        assert row(store, earlier)['state'] == 'completed'


def test_closure_evidence_is_protected_from_message_retention(store):
    from iris.memory_ops import message_references
    gid, source, done = basic(store)
    chat = message(store, '今天好冷', at='2026-11-02T19:30:00+08:00')
    enable(store)
    run_goals(store, Reviewer(store, cite('completed', lambda payload: [done])))
    with store.read() as conn:
        assert 'goal_auto_evidence' in message_references(conn, done)
        assert message_references(conn, chat) == []
    # Evidence of a judgment that kept the goal open protects nothing.
    other = goal(store, '帮程栗交水费')
    note = message(store, '水费的事回头再说', at='2026-11-02T20:00:00+08:00')
    run_goals(store, Reviewer(store, {'decision': 'open', 'evidence': [note], 'reason': ''}))
    assert reviews(store, other)[0]['outcome'] == 'kept_open'
    with store.read() as conn:
        assert 'goal_auto_evidence' not in message_references(conn, note)
    # The daily message cleanup keeps the protected message and removes the other old one.
    from iris.maintenance import Maintenance
    store.set_setting('lifecycle', {'message_retention_days': 1})
    late = Clock('2026-12-20T01:00:00+00:00')
    engine = Maintenance(store, gateway=None, clock=late)
    rid = engine.request()
    engine.run(rid)
    with store.read() as conn:
        left = {r[0] for r in conn.execute('SELECT id FROM messages')}
    assert done in left and chat not in left


def test_report_counts_accumulate_across_an_interrupted_run(store):
    enable(store)
    subject(store, 'p1', '程栗')
    first = goal(store, '帮程栗取快递')
    second = goal(store, '帮程栗交水费')
    message(store, '快递和水费都弄好了', at='2026-11-02T19:00:00+08:00')
    stopped = []
    reviewer = Reviewer(store, cite('completed'), hook=lambda payload: stopped.append(True))
    engine, rid = run_goals(store, reviewer, stop=lambda: bool(stopped))
    assert engine.report(rid)['goal_auto_review']['calls'] == 1
    reviewer.hook = None
    engine.run(rid)
    summary = engine.report(rid)['goal_auto_review']
    assert (summary['goals'], summary['calls'], summary['closed']) == (2, 2, 2)
    assert 'already_judged' not in summary and 'cursor' not in summary
    assert row(store, first)['state'] == row(store, second)['state'] == 'completed'


def test_call_cap_reports_budget_exhausted_and_the_rest_waits(store, monkeypatch):
    from iris import goals as goals_module
    monkeypatch.setattr(goals_module, 'AUTO_REVIEW_MAX_CALLS', 1)
    enable(store)
    subject(store, 'p1', '程栗')
    first = goal(store, '帮程栗取快递')
    second = goal(store, '帮程栗交水费')
    message(store, '快递和水费都弄好了', at='2026-11-02T19:00:00+08:00')
    reviewer = Reviewer(store, cite('completed'))
    engine, rid = run_goals(store, reviewer)
    summary = engine.report(rid)['goal_auto_review']
    assert (summary['calls'], summary['budget_exhausted']) == (1, 1)
    assert row(store, first)['state'] == 'completed' and row(store, second)['state'] == 'open'
    run_goals(store, reviewer)
    assert row(store, second)['state'] == 'completed'


def test_role_messages_in_unrelated_entries_do_not_cause_daily_rejudgment(store):
    from datetime import datetime, timedelta
    subject(store, 'p1', '程栗')
    gid = goal(store, '帮程栗取快递')
    message(store, '在吗', at='2026-11-02T18:00:00+08:00')
    unrelated = message(store, '好的，收到', sender='self', kind='self_output', entry_id='other',
                        at='2026-11-02T18:30:00+08:00')
    enable(store)
    reviewer = Reviewer(store, {'decision': 'open', 'evidence': [], 'reason': ''})
    day = datetime.fromisoformat('2026-11-03T01:00:00+00:00')
    run_goals(store, reviewer, Clock(day.isoformat()))
    assert unrelated not in [m['id'] for m in reviewer.calls[0]['payload']['messages']]
    for n in range(1, 4):  # the role speaks once a day in an unrelated entry only
        message(store, '好的，收到', sender='self', kind='self_output', entry_id='other',
                at=(day + timedelta(days=n - 1, hours=2)).isoformat())
        run_goals(store, reviewer, Clock((day + timedelta(days=n)).isoformat()))
    assert len(reviewer.calls) == 1
    # Passing direction: the role's reply in the goal's own conversation is a candidate.
    message(store, '快递我拿到了', sender='self', kind='self_output', entry_id='e1',
            at=(day + timedelta(days=4)).isoformat())
    run_goals(store, reviewer, Clock((day + timedelta(days=5)).isoformat()))
    assert len(reviewer.calls) == 2 and gid


def test_role_messages_count_where_the_goal_people_speak(store):
    subject(store, 'p1', '程栗')
    gid = goal(store, '帮程栗取快递')
    message(store, '在群里也问一下', entry_id='group', at='2026-11-02T18:00:00+08:00')
    there = message(store, '快递我拿到了', sender='self', kind='self_output', entry_id='group',
                    at='2026-11-02T18:10:00+08:00')
    elsewhere = message(store, '快递我拿到了', sender='self', kind='self_output', entry_id='other',
                        at='2026-11-02T18:20:00+08:00')
    enable(store)
    reviewer = Reviewer(store, {'decision': 'open', 'evidence': [], 'reason': ''})
    run_goals(store, reviewer)
    picked = [m['id'] for m in reviewer.calls[0]['payload']['messages']]
    assert there in picked and elsewhere not in picked and gid


def test_call_cap_leftover_goal_is_first_in_line_next_time(store, monkeypatch):
    from datetime import datetime, timedelta
    from iris import goals as goals_module
    monkeypatch.setattr(goals_module, 'AUTO_REVIEW_MAX_CALLS', 2)
    for sid, name in (('p1', '程栗'), ('p2', '路人'), ('p3', '老周')):
        subject(store, sid, name)
    texts = ['帮程栗取快递', '帮路人交水费', '提醒老周买药']
    for text, sid in zip(texts, ('p1', 'p2', 'p3')):
        goal(store, text, people=(sid,))
        message(store, '在吗', sender=sid, at='2026-11-02T18:00:00+08:00')
    enable(store)
    reviewer = Reviewer(store, {'decision': 'open', 'evidence': [], 'reason': ''})
    day = datetime.fromisoformat('2026-11-03T01:00:00+00:00')
    engine, rid = run_goals(store, reviewer, Clock(day.isoformat()))
    assert [c['payload']['goal']['text'] for c in reviewer.calls] == texts[:2]
    assert engine.report(rid)['goal_auto_review']['budget_exhausted'] == 1
    run_goals(store, reviewer, Clock((day + timedelta(days=1)).isoformat()))
    assert [c['payload']['goal']['text'] for c in reviewer.calls] == texts
    # Once all have a judgment, the least recently reviewed go first; new evidence every day.
    for n, expected in ((2, [texts[0], texts[1]]), (3, [texts[2], texts[0]])):
        for sid in ('p1', 'p2', 'p3'):
            message(store, '还在弄', sender=sid, at=(day + timedelta(days=n - 1, hours=12)).isoformat())
        before = len(reviewer.calls)
        run_goals(store, reviewer, Clock((day + timedelta(days=n)).isoformat()))
        assert [c['payload']['goal']['text'] for c in reviewer.calls[before:]] == expected


def test_goal_merged_into_this_run_is_reviewed_next_run(store):
    gid, source, done = basic(store)
    earlier = goal(store, '帮程栗取南门驿站电热毯的快递', created='2026-11-02T09:00:00+00:00')
    store.set_setting('goal_dedup_judge', {'enabled': True, 'budget_seconds': 1})
    with store.write() as conn:
        conn.execute("INSERT INTO goal_duplicates(goal_a,goal_b,status,created_at) VALUES(?,?,'possible',?)",
                     (min(earlier, gid), max(earlier, gid), CREATED))

    class Both(Reviewer):
        def goal_dedup_judge(self, messages, ids, *, budget_seconds):
            return {'decisions': {i: 'same' for i in ids}}
    enable(store, review=True, duplicates=True)
    reviewer = Both(store, cite('completed', lambda payload: [done]))
    engine, rid = run_goals(store, reviewer)
    assert row(store, gid)['merged_into'] == earlier and reviewer.calls == []
    assert row(store, earlier)['state'] == 'open'
    assert engine.report(rid)['goal_auto_review']['merged_this_run'] == 1
    run_goals(store, reviewer, Clock('2026-11-04T01:00:00+00:00'))
    assert len(reviewer.calls) == 1 and row(store, earlier)['state'] == 'completed'


def test_reopen_releases_evidence_and_unqualified_co_citations_are_never_protected(store):
    from iris.memory_ops import message_references
    gid, source, done = basic(store)
    event = message(store, '程栗把待办标成完成', kind='event', at='2026-11-02T19:10:00+08:00')
    enable(store)
    run_goals(store, Reviewer(store, cite('completed', lambda payload: [done, event, source])))
    assert row(store, gid)['state'] == 'completed'
    with store.read() as conn:
        assert 'goal_auto_evidence' in message_references(conn, done)
        assert 'goal_auto_evidence' not in message_references(conn, event)
    Goals(store, clock=Clock()).reopen(gid, expected_revision=row(store, gid)['revision'])
    with store.read() as conn:
        assert 'goal_auto_evidence' not in message_references(conn, done)
    from iris.maintenance import Maintenance
    store.set_setting('lifecycle', {'message_retention_days': 1})
    engine = Maintenance(store, gateway=None, clock=Clock('2026-12-20T01:00:00+00:00'))
    engine.run(engine.request())
    with store.read() as conn:
        left = {r[0] for r in conn.execute('SELECT id FROM messages')}
    assert done not in left and source in left  # the goal source stays protected as before
