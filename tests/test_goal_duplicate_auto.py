"""G2: daily judgment of "possible duplicate" goal pairs with the shared D6 material."""
import json

import pytest

from iris import goals as goals_module
from iris.goals import DUPLICATE_AUTO_BUDGET_SECONDS, Goals
from iris.models import ModelError
from goal_auto_helpers import Clock, enable, items, row, run_goals


class Judge:
    """Fake goal_dedup_judge purpose; also serves G1's chat purpose as unused."""

    def __init__(self, store, verdict='uncertain'):
        self.store, self.verdict = store, verdict
        self.calls = []

    def goal_dedup_judge(self, messages, ids, *, budget_seconds):
        assert not self.store._writer.in_transaction
        self.calls.append({'payload': json.loads(messages[-1]['content']), 'budget_seconds': budget_seconds,
                           'system': messages[0]['content']})
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return {'decisions': {gid: self.verdict for gid in ids}}


@pytest.fixture
def pair(store):
    store.set_setting('goal_dedup_judge', {'enabled': True, 'budget_seconds': 1})
    judge = Judge(store)
    goals = Goals(store, gateway=judge, clock=Clock('2026-11-02T09:00:00+00:00'))

    def make(first_origin='host', second_origin='host'):
        first = goals.create(content='周五前整理操作手册', origin=first_origin, actor=first_origin)
        goals.clock = Clock('2026-11-02T10:00:00+00:00')
        second = goals.create(content='周五前整理操作说明手册', origin=second_origin, actor=second_origin)
        assert second['dedup']['status'] == 'possible_duplicate'
        judge.calls.clear()
        return first['goal']['id'], second['submitted_id']
    return make, judge


def status(store, a, b):
    with store.read() as conn:
        return conn.execute('SELECT status,resolved_by FROM goal_duplicates WHERE goal_a=? AND goal_b=?',
                            (min(a, b), max(a, b))).fetchone()


def judgments(store):
    with store.read() as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM goal_duplicate_judgments ORDER BY id')]


def test_same_merges_into_the_earlier_goal_with_60_second_budget(store, pair):
    make, judge = pair
    first, second = make()
    enable(store, review=False, duplicates=True)
    judge.verdict = 'same'
    engine, rid = run_goals(store, judge)
    call, = judge.calls
    assert DUPLICATE_AUTO_BUDGET_SECONDS == 60 and 59 < call['budget_seconds'] <= 60  # not the 5 s inline budget
    assert call['payload']['incoming']['id'] == second
    assert [c['id'] for c in call['payload']['candidates']] == [first]
    assert row(store, second)['merged_into'] == first and row(store, first)['state'] == 'open'
    assert tuple(status(store, first, second)) == ('merged', 'maintenance')
    record, = judgments(store)
    assert (record['outcome'], record['verdict'], record['reason'], record['target_id']) == ('merged', 'same', 'auto_merged', first)
    with store.read() as conn:
        merge = conn.execute("SELECT * FROM goal_revisions WHERE action='merge' AND goal_id=?", (first,)).fetchone()
    assert (merge['actor'], merge['reason']) == ('maintenance', 'auto_merged')
    item, = items(store, rid)
    assert (item['outcome'], item['object_id']) == ('goal_duplicates_merged', first)
    assert engine.report(rid)['goal_duplicate_review']['merged'] == 1


def test_different_withdraws_the_hint(store, pair):
    make, judge = pair
    first, second = make()
    enable(store, review=False, duplicates=True)
    judge.verdict = 'different'
    run_goals(store, judge)
    assert tuple(status(store, first, second)) == ('dismissed', 'maintenance')
    assert row(store, first)['merged_into'] is None and row(store, second)['merged_into'] is None
    assert judgments(store)[0]['reason'] == 'auto_different'
    with store.read() as conn:
        snapshot = conn.execute("SELECT * FROM goal_revisions WHERE action='duplicate_dismiss' AND goal_id=?", (first,)).fetchone()
    assert (snapshot['actor'], snapshot['reason']) == ('maintenance', 'auto_different')


def test_uncertain_keeps_the_hint_and_is_not_judged_again(store, pair):
    make, judge = pair
    first, second = make()
    enable(store, review=False, duplicates=True)
    run_goals(store, judge)
    assert tuple(status(store, first, second))[0] == 'possible'
    assert judgments(store)[0]['outcome'] == 'kept' and judgments(store)[0]['reason'] == 'uncertain'
    engine, rid = run_goals(store, judge)
    assert len(judge.calls) == 1
    assert engine.report(rid)['goal_duplicate_review']['already_judged'] == 1


@pytest.mark.parametrize('origins', [('admin', 'host'), ('host', 'admin')])
def test_admin_goal_is_never_merged_and_the_hint_is_withdrawn(store, pair, origins):
    make, judge = pair
    first, second = make(*origins)
    enable(store, review=False, duplicates=True)
    judge.verdict = 'same'
    run_goals(store, judge)
    assert row(store, first)['merged_into'] is None and row(store, second)['merged_into'] is None
    assert tuple(status(store, first, second)) == ('dismissed', 'maintenance')
    assert judgments(store)[0]['reason'] == 'admin_not_merged'


def test_timeout_keeps_the_hint_and_retries_next_time(store, pair):
    make, judge = pair
    first, second = make()
    enable(store, review=False, duplicates=True)
    judge.verdict = ModelError('retryable', 'goal judgment total timeout', reason='timeout')
    engine, rid = run_goals(store, judge)
    assert tuple(status(store, first, second))[0] == 'possible' and judgments(store) == []
    item, = items(store, rid)
    assert (item['outcome'], item['reason']) == ('failed', 'timeout')
    judge.verdict = 'same'
    run_goals(store, judge)
    assert len(judge.calls) == 2 and row(store, second)['merged_into'] == first


def test_switch_off_means_no_dedup_call(store, pair):
    make, judge = pair
    make()
    run_goals(store, judge)
    enable(store, review=True, duplicates=False)
    run_goals(store, judge)
    assert judge.calls == [] and judgments(store) == []


def test_disabled_dedup_judge_stops_g2(store, pair):
    make, judge = pair
    make()
    store.set_setting('goal_dedup_judge', {'enabled': False})
    enable(store, review=False, duplicates=True)
    engine, rid = run_goals(store, judge)
    assert judge.calls == [] and engine.report(rid)['goal_duplicate_review']['stopped'] == 'dedup_judge_disabled'


def test_changed_pair_during_call_is_skipped_without_fingerprint(store, pair):
    make, judge = pair
    first, second = make()
    enable(store, review=False, duplicates=True)
    judge.verdict = 'same'
    original = judge.goal_dedup_judge

    def edit(messages, ids, *, budget_seconds):
        result = original(messages, ids, budget_seconds=budget_seconds)
        current = row(store, first)
        Goals(store, clock=Clock()).update(first, expected_revision=current['revision'], deadline='2026-11-06', actor='admin')
        return result
    judge.goal_dedup_judge = edit
    engine, rid = run_goals(store, judge)
    assert judgments(store) == [] and row(store, second)['merged_into'] is None
    assert engine.report(rid)['goal_duplicate_review']['revision_conflict'] == 1


def test_creation_review_and_g2_share_one_material_assembly(store, pair, monkeypatch):
    make, judge = pair
    calls = []
    original = goals_module.dedup_review_material

    def spy(conn, goal_id, options, **kwargs):
        calls.append(goal_id)
        return original(conn, goal_id, options, **kwargs)
    monkeypatch.setattr(goals_module, 'dedup_review_material', spy)
    first, second = make()
    assert second in calls  # the creation-time review (what goal_dedup_probe drives)
    calls.clear()
    enable(store, review=False, duplicates=True)
    run_goals(store, judge)
    assert calls == [second]
    with store.read() as conn:
        review = original(conn, second, goals_module.dedup_judge.connection_settings(conn))
    assert judge.calls[0]['payload'] == {'incoming': review['incoming'], 'candidates': review['materials']}
    assert judge.calls[0]['system'] == goals_module.dedup_judge.judgment_prompt('D6')


class RuleJudge(Judge):
    """Verdict per (incoming, candidate); unknown pairs are uncertain."""

    def __init__(self, store):
        super().__init__(store)
        self.verdicts = {}

    def goal_dedup_judge(self, messages, ids, *, budget_seconds):
        payload = json.loads(messages[-1]['content'])
        self.calls.append({'incoming': payload['incoming']['id'], 'candidates': [c['id'] for c in payload['candidates']]})
        return {'decisions': {gid: self.verdicts.get((payload['incoming']['id'], gid), 'uncertain') for gid in ids}}


def _scoped_flow(store, host_entries):
    """Natural flow: I2 merges into I, re-pointing (I2,H) to (I,H) across the host's scope."""
    from iris.goals import write_learning_goal
    from iris.tokens import Scope
    from goal_auto_helpers import entry, goal
    store.set_setting('goal_dedup_judge', {'enabled': True, 'budget_seconds': 1})
    judge = RuleJudge(store)
    with store.write() as conn:
        for name in ('e1', 'e2'):
            entry(conn, name)
    inner = goal(store, '周五前整理操作手册', people=(), entry_id='e2', created='2026-11-02T09:00:00+00:00')
    scope = Scope(kind='entries', entries=host_entries)
    goals = Goals(store, gateway=judge, clock=Clock('2026-11-02T10:00:00+00:00'))
    host = goals.create(content='周五前整理操作说明手册', origin='host', actor='h', host='h', entry_id='e1',
                        scope=scope)['submitted_id']
    goals.clock = Clock('2026-11-02T11:00:00+00:00')
    with store.write() as conn:
        third = write_learning_goal(conn, content='周五前把操作手册整理好', kind='normal', deadline=None, entry_id='e1',
                                    evidence=[], current=goals.clock())['submitted_id']
    goals.review(third)
    enable(store, review=False, duplicates=True)
    judge.verdicts.update({(third, inner): 'same', (third, host): 'different'})
    run_goals(store, judge)
    assert row(store, third)['merged_into'] == inner
    assert tuple(status(store, inner, host))[0] == 'possible'  # re-pointed by the merge
    judge.verdicts[(host, inner)] = 'same'
    return inner, host, third, scope, judge


def test_host_goal_is_never_merged_outside_its_token_scope(store):
    inner, host, third, scope, judge = _scoped_flow(store, ('e1',))
    calls = len(judge.calls)
    engine, rid = run_goals(store, judge)
    assert len(judge.calls) == calls  # no model call for a pair that can never merge
    assert row(store, host)['merged_into'] is None and row(store, inner)['merged_into'] is None
    assert tuple(status(store, inner, host))[0] == 'possible'  # the hint stays for the administrator
    record = judgments(store)[-1]
    assert (record['outcome'], record['reason'], record['verdict']) == ('kept', 'host_scope_not_merged', None)
    assert engine.report(rid)['goal_duplicate_review']['kept'] == 1
    engine, rid = run_goals(store, judge)
    assert len(judge.calls) == calls and engine.report(rid)['goal_duplicate_review']['already_judged'] == 1
    # The host keeps writing its own goal within its token scope.
    updated = Goals(store, clock=Clock()).update(host, content='周五前整理操作说明', actor='h', scope=scope)
    assert updated['content'] == '周五前整理操作说明'


def test_host_goal_merges_when_the_partner_is_inside_its_scope(store):
    inner, host, third, scope, judge = _scoped_flow(store, ('e1', 'e2'))
    run_goals(store, judge)
    assert row(store, host)['merged_into'] == inner
    assert judgments(store)[-1]['reason'] == 'auto_merged'


def _merged(store, pair, *, deadline=None):
    make, judge = pair
    first, second = make()
    if deadline:
        current = row(store, second)
        Goals(store, clock=Clock('2026-11-02T10:30:00+00:00')).update(
            second, expected_revision=current['revision'], deadline=deadline, actor='admin')
    enable(store, review=False, duplicates=True)
    judge.verdict = 'same'
    engine, rid = run_goals(store, judge)
    assert row(store, second)['merged_into'] == first
    return first, second, judgments(store)[-1], judge, engine, rid


def test_undo_restores_both_goals_and_never_merges_the_pair_again(store, pair):
    first, second, judgment, judge, engine, rid = _merged(store, pair, deadline='2026-11-20')
    merged_target = row(store, first)
    assert merged_target['deadline'] is not None  # filled from the merged goal
    item, = [i for i in items(store, rid) if i['outcome'] == 'goal_duplicates_merged']
    assert item['details']['target_revision_after'] == merged_target['revision']
    goals = Goals(store, clock=Clock())
    restored = goals.undo_auto_merge(judgment['id'], expected_revision=merged_target['revision'])
    assert restored['id'] == first and restored['deadline'] is None
    assert row(store, second)['merged_into'] is None and row(store, second)['deadline'] is not None
    assert tuple(status(store, first, second)) == ('dismissed', 'admin')
    with store.read() as conn:
        record = dict(conn.execute('SELECT * FROM goal_duplicate_judgments WHERE id=?', (judgment['id'],)).fetchone())
        actions = [r[0] for r in conn.execute("SELECT action FROM goal_revisions WHERE action='merge_undo'")]
        operation = conn.execute("SELECT * FROM admin_operations WHERE action='goal_merge_undo'").fetchone()
        plans = conn.execute("SELECT COUNT(*) FROM goal_reminder_plans WHERE goal_id=? AND status='scheduled'", (second,)).fetchone()[0]
    assert record['undone_by'] == 'admin' and record['undone_at'] and len(actions) >= 2 and operation['actor'] == 'admin'
    assert plans >= 1  # the restored goal's own deadline is scheduled again
    calls = len(judge.calls)
    run_goals(store, judge)
    assert len(judge.calls) == calls and row(store, second)['merged_into'] is None
    with pytest.raises(goals_module.GoalError):
        goals.undo_auto_merge(judgment['id'], expected_revision=row(store, first)['revision'])


def test_undo_refuses_when_the_survivor_changed_after_the_merge(store, pair):
    first, second, judgment, judge, engine, rid = _merged(store, pair)
    goals = Goals(store, clock=Clock())
    current = row(store, first)
    with pytest.raises(goals_module.GoalConflict):
        goals.undo_auto_merge(judgment['id'], expected_revision=current['revision'] - 1)
    edited = goals.update(first, expected_revision=current['revision'], content='周五前整理好操作手册', actor='admin')
    with pytest.raises(goals_module.GoalConflict):
        goals.undo_auto_merge(judgment['id'], expected_revision=edited['revision'])
    assert row(store, second)['merged_into'] == first


def test_only_automatic_merges_can_be_undone(store, pair):
    make, judge = pair
    first, second = make()
    goals = Goals(store, clock=Clock())
    a, b = row(store, first), row(store, second)
    goals.merge(first, second, expected_revision=a['revision'], other_revision=b['revision'])
    with store.write() as conn:
        jid = conn.execute("""INSERT INTO goal_duplicate_judgments(goal_a,goal_b,fingerprint,method,verdict,outcome,
            reason,target_id,created_at) VALUES(?,?,'x','D6','same','merged','auto_merged',?,?)""",
            (first, second, first, '2026-11-03T00:00:00+00:00')).lastrowid
    with pytest.raises(goals_module.GoalError):
        goals.undo_auto_merge(jid, expected_revision=row(store, first)['revision'])
    with pytest.raises(KeyError):
        goals.undo_auto_merge(jid + 100, expected_revision=1)


def test_undo_returns_moved_reminders_to_the_restored_goal(store, pair):
    make, judge = pair
    first, second = make()
    goals = Goals(store, clock=Clock('2026-11-02T10:30:00+00:00'))
    current = row(store, second)
    goals.update(second, expected_revision=current['revision'], deadline='2026-11-02T11:00:00+00:00', actor='admin')
    with store.read() as conn:
        pending = [r[0] for r in conn.execute("SELECT id FROM notifications WHERE goal_id=? AND status='pending'", (second,))]
    assert pending  # immediate "due soon" reminder for the second goal
    enable(store, review=False, duplicates=True)
    judge.verdict = 'same'
    run_goals(store, judge)
    with store.read() as conn:
        assert conn.execute('SELECT goal_id FROM notifications WHERE id=?', (pending[0],)).fetchone()[0] == first
    judgment = judgments(store)[-1]
    Goals(store, clock=Clock()).undo_auto_merge(judgment['id'], expected_revision=row(store, first)['revision'])
    with store.read() as conn:
        note = conn.execute('SELECT * FROM notifications WHERE id=?', (pending[0],)).fetchone()
    assert (note['goal_id'], note['status']) == (second, 'pending')
    assert note['content'].endswith(row(store, second)['content'])


def test_undo_removes_relations_the_merge_copied_and_restores_watermarks(store):
    from goal_auto_helpers import CREATED, goal, message, subject
    subject(store, 'p1', '程栗')
    subject(store, 'p2', '路人')
    s1 = message(store, '周五前整理操作手册', at='2026-11-02T16:00:00+08:00')
    s2 = message(store, '周五前把操作说明手册整理好', sender='p2', at='2026-11-02T17:30:00+08:00')
    first = goal(store, '周五前整理操作手册', people=(), sources=[s1], created='2026-11-02T09:00:00+00:00')
    second = goal(store, '周五前整理操作说明手册', people=(), sources=[s2], created='2026-11-02T10:00:00+00:00')
    with store.write() as conn:
        conn.execute('UPDATE goals SET auto_review_after_message_id=? WHERE id=?', (s2, second))
        conn.execute("INSERT INTO goal_duplicates(goal_a,goal_b,status,created_at) VALUES(?,?,'possible',?)",
                     (first, second, CREATED))
    store.set_setting('goal_dedup_judge', {'enabled': True, 'budget_seconds': 1})
    judge = Judge(store, 'same')
    enable(store, review=False, duplicates=True)
    run_goals(store, judge)
    with store.read() as conn:
        merged_sources = sorted(r[0] for r in conn.execute('SELECT message_id FROM goal_sources WHERE goal_id=?', (first,)))
    assert merged_sources == [s1, s2] and row(store, first)['auto_review_after_message_id'] == s2
    Goals(store, clock=Clock()).undo_auto_merge(judgments(store)[-1]['id'], expected_revision=row(store, first)['revision'])
    with store.read() as conn:
        assert [r[0] for r in conn.execute('SELECT message_id FROM goal_sources WHERE goal_id=?', (first,))] == [s1]
        assert [r[0] for r in conn.execute('SELECT message_id FROM goal_sources WHERE goal_id=?', (second,))] == [s2]
    assert row(store, first)['auto_review_after_message_id'] is None
    assert row(store, second)['auto_review_after_message_id'] == s2 and row(store, second)['merged_into'] is None



def test_internal_goal_merges_into_an_earlier_scoped_host_goal_as_at_creation(store):
    from iris.goals import write_learning_goal
    from iris.tokens import Scope
    from goal_auto_helpers import entry
    store.set_setting('goal_dedup_judge', {'enabled': True, 'budget_seconds': 1})
    judge = Judge(store, 'uncertain')
    with store.write() as conn:
        for name in ('e1', 'e2'):
            entry(conn, name)
    goals = Goals(store, gateway=judge, clock=Clock('2026-11-02T09:00:00+00:00'))
    host = goals.create(content='周五前整理操作说明手册', origin='host', actor='h', host='h', entry_id='e1',
                        scope=Scope(kind='entries', entries=('e1',)))['submitted_id']
    goals.clock = Clock('2026-11-02T10:00:00+00:00')
    with store.write() as conn:
        later = write_learning_goal(conn, content='周五前整理操作手册', kind='normal', deadline=None, entry_id='e2',
                                    evidence=[], current=goals.clock())['submitted_id']
    assert goals.review(later)['dedup']['status'] == 'possible_duplicate'
    judge.verdict, judge.calls = 'same', []
    enable(store, review=False, duplicates=True)
    run_goals(store, judge)
    assert [c['payload']['incoming']['id'] for c in judge.calls] == [later]
    assert row(store, later)['merged_into'] == host and row(store, host)['entry_id'] == 'e1'
    assert judgments(store)[-1]['reason'] == 'auto_merged'


def test_undo_keeps_the_survivors_own_reminders_and_cancels_merged_ones(store):
    from iris import goals as gm
    from goal_auto_helpers import goal, subject
    store.set_setting('goal_dedup_judge', {'enabled': True, 'budget_seconds': 1})
    subject(store, 'p1', '程栗')
    own = goal(store, '周五前整理操作手册', people=('p1',), created='2026-11-02T08:10:00+00:00',
               deadline='2026-11-03T03:00:00+00:00')
    other = goal(store, '周五前整理操作说明手册', people=(), created='2026-11-02T09:40:00+00:00')
    bare = goal(store, '周五前整理产品手册', people=(), created='2026-11-02T08:00:00+00:00')
    later = goal(store, '周五前整理产品说明手册', people=(), created='2026-11-02T09:50:00+00:00',
                 deadline='2026-11-03T03:00:00+00:00')
    with store.write() as conn:
        for a, b in ((own, other), (bare, later)):
            conn.execute("INSERT INTO goal_duplicates(goal_a,goal_b,status,created_at) VALUES(?,?,'possible','x')", (a, b))
        for gid in (own, later):
            gm._schedule(conn, gm._row(conn, gid), Clock('2026-11-02T10:00:00+00:00')())
    judge = RuleJudge(store)
    judge.verdicts.update({(other, own): 'same', (other, bare): 'different', (other, later): 'different',
                           (later, bare): 'same', (later, own): 'different', (later, other): 'different'})
    enable(store, review=False, duplicates=True)
    run_goals(store, judge, Clock('2026-11-03T01:00:00+00:00'))
    assert row(store, other)['merged_into'] == own and row(store, later)['merged_into'] == bare
    Goals(store, clock=Clock('2026-11-03T02:30:00+00:00')).generate_notifications()

    def pending(gid):
        with store.read() as conn:
            return [r[0] for r in conn.execute(
                "SELECT id FROM notifications WHERE goal_id=? AND kind='goal_reminder' AND status='pending'", (gid,))]
    own_note, merged_note = pending(own), pending(bare)
    assert own_note and merged_note  # the survivor's own deadline / the deadline the merge brought in
    undo = Goals(store, clock=Clock('2026-11-03T02:40:00+00:00'))
    for jid, survivor in ((j['id'], j['target_id']) for j in judgments(store) if j['outcome'] == 'merged'):
        undo.undo_auto_merge(jid, expected_revision=row(store, survivor)['revision'])
    assert pending(own) == own_note  # still waiting to be taken
    assert pending(bare) == []  # it came from the merged-in deadline
    assert row(store, bare)['deadline'] is None and row(store, later)['deadline'] is not None
