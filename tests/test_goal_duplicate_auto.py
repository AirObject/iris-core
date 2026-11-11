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
        fresh = conn.execute("SELECT * FROM notifications WHERE goal_id=? AND status='pending'", (second,)).fetchall()
    assert (note['goal_id'], note['status']) == (first, 'cancelled')  # the record itself is not rewritten
    assert len(fresh) == 1 and fresh[0]['id'] > pending[0]  # a new ID a host cursor can still reach
    assert fresh[0]['content'].endswith(row(store, second)['content'])


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


def test_undo_reissues_the_current_stage_for_both_goals_of_each_merge(store):
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
    before = _all_notes(store)
    undo = Goals(store, clock=Clock('2026-11-03T02:40:00+00:00'))
    for jid, survivor in ((j['id'], j['target_id']) for j in judgments(store) if j['outcome'] == 'merged'):
        undo.undo_auto_merge(jid, expected_revision=row(store, survivor)['revision'])
    _assert_undo_reminders(store, before, due_soon=[own, later], silent=[other, bare])



def test_absorbed_host_goal_blocks_a_merge_out_of_its_scope(store):
    """probe_scope_redirect: H merged into S; S must not then be merged into P outside H's scope."""
    _absorbed_scope_flow(store, host_entries=('e1',), blocked=True)


def test_absorbed_host_goal_allows_a_merge_inside_its_scope(store):
    _absorbed_scope_flow(store, host_entries=('e1', 'e2'), blocked=False)


def _absorbed_scope_flow(store, *, host_entries, blocked):
    from iris.goals import write_learning_goal
    from iris.tokens import Scope
    from goal_auto_helpers import entry, goal
    store.set_setting('goal_dedup_judge', {'enabled': True, 'budget_seconds': 1})
    judge = RuleJudge(store)
    with store.write() as conn:
        for name in ('e1', 'e2'):
            entry(conn, name)
    inner = goal(store, '周五前整理操作手册', people=(), entry_id='e2', created='2026-11-02T08:00:00+00:00')
    goals = Goals(store, gateway=judge, clock=Clock('2026-11-02T09:00:00+00:00'))
    with store.write() as conn:
        survivor = write_learning_goal(conn, content='周五前把操作手册整理好', kind='normal', deadline=None, entry_id='e1',
                                       evidence=[], current=goals.clock())['submitted_id']
    goals.review(survivor)
    scope = Scope(kind='entries', entries=host_entries)
    goals.clock = Clock('2026-11-02T10:00:00+00:00')
    host = goals.create(content='周五前整理操作说明手册', origin='host', actor='h', host='h', entry_id='e1',
                        scope=scope)['submitted_id']
    assert tuple(status(store, inner, survivor))[0] == 'possible'
    assert tuple(status(store, survivor, host))[0] == 'possible'
    enable(store, review=False, duplicates=True)
    judge.verdicts[(host, survivor)] = 'same'
    judge.verdicts[(host, inner)] = 'different'
    run_goals(store, judge, Clock('2026-11-03T01:00:00+00:00'))
    assert row(store, host)['merged_into'] == survivor and row(store, survivor)['merged_into'] is None
    judge.verdicts[(survivor, inner)] = 'same'
    calls = len(judge.calls)
    run_goals(store, judge, Clock('2026-11-04T01:00:00+00:00'))
    if blocked:
        assert len(judge.calls) == calls  # no model call
        assert row(store, survivor)['merged_into'] is None and row(store, host)['merged_into'] == survivor
        assert tuple(status(store, inner, survivor))[0] == 'possible'
        record = judgments(store)[-1]
        assert (record['outcome'], record['reason']) == ('kept', 'host_scope_not_merged')
        # The host still reaches its goal through the redirect, inside its scope.
        with pytest.raises(goals_module.GoalMerged) as merged:
            Goals(store, clock=Clock()).update(host, content='x', actor='h', scope=scope)
        assert merged.value.canonical_id == survivor
        updated = Goals(store, clock=Clock()).update(survivor, content='周五前整理好操作手册', actor='h', scope=scope)
        assert updated['id'] == survivor
    else:
        assert row(store, survivor)['merged_into'] == inner and row(store, host)['merged_into'] == inner


def test_undo_round3_reminder_fired_under_a_for_bs_deadline(store):
    """probe_round3: A has no deadline, B has one; B's warning fired under A before the undo."""
    from iris import goals as gm
    from goal_auto_helpers import goal, subject
    store.set_setting('goal_dedup_judge', {'enabled': True, 'budget_seconds': 1})
    subject(store, 'p1', '程栗')
    a = goal(store, '周五前整理操作手册', people=('p1',), created='2026-11-02T08:10:00+00:00')
    b = goal(store, '周五前整理操作说明手册', people=(), created='2026-11-02T09:40:00+00:00',
             deadline='2026-11-03T03:00:00+00:00')
    with store.write() as conn:
        conn.execute("INSERT INTO goal_duplicates(goal_a,goal_b,status,created_at) VALUES(?,?,'possible','x')", (a, b))
        gm._schedule(conn, gm._row(conn, b), Clock('2026-11-02T10:00:00+00:00')())
    enable(store, review=False, duplicates=True)
    run_goals(store, Judge(store, 'same'), Clock('2026-11-03T01:00:00+00:00'))
    assert row(store, b)['merged_into'] == a
    Goals(store, clock=Clock('2026-11-03T02:30:00+00:00')).generate_notifications()
    before = _all_notes(store)
    jid = judgments(store)[-1]['id']
    Goals(store, clock=Clock('2026-11-03T02:40:00+00:00')).undo_auto_merge(jid, expected_revision=row(store, a)['revision'])
    _assert_undo_reminders(store, before, due_soon=[b], silent=[a])
    with store.read() as conn:
        plans = [r[0] for r in conn.execute("SELECT reminder_kind FROM goal_reminder_plans WHERE goal_id=? AND status='scheduled'", (b,))]
    assert plans == ['due']  # B's own plan continues



def test_undo_with_equal_deadlines_reissues_for_both(store):
    from iris import goals as gm
    from goal_auto_helpers import goal, subject
    store.set_setting('goal_dedup_judge', {'enabled': True, 'budget_seconds': 1})
    subject(store, 'p1', '程栗')
    deadline = '2026-11-03T03:00:00+00:00'
    a = goal(store, '周五前整理操作手册', people=('p1',), created='2026-11-02T08:10:00+00:00', deadline=deadline)
    b = goal(store, '周五前整理操作说明手册', people=(), created='2026-11-02T09:40:00+00:00', deadline=deadline)
    with store.write() as conn:
        conn.execute("INSERT INTO goal_duplicates(goal_a,goal_b,status,created_at) VALUES(?,?,'possible','x')", (a, b))
        for gid in (a, b):
            gm._schedule(conn, gm._row(conn, gid), Clock('2026-11-02T10:00:00+00:00')())
    enable(store, review=False, duplicates=True)
    run_goals(store, Judge(store, 'same'), Clock('2026-11-03T01:00:00+00:00'))
    Goals(store, clock=Clock('2026-11-03T02:30:00+00:00')).generate_notifications()
    before = _all_notes(store)
    jid = judgments(store)[-1]['id']
    Goals(store, clock=Clock('2026-11-03T02:40:00+00:00')).undo_auto_merge(jid, expected_revision=row(store, a)['revision'])
    _assert_undo_reminders(store, before, due_soon=[a, b])



def _long_lead_merge(store, *, deadline, take):
    """probe_undo_dup: the survivor's longer lead fires B's deadline early under the survivor."""
    from iris import goals as gm
    from goal_auto_helpers import goal, subject
    store.set_setting('goal_dedup_judge', {'enabled': True, 'budget_seconds': 1})
    subject(store, 'p1', '程栗')
    a = goal(store, '周五前整理操作手册', people=('p1',), created='2026-11-02T08:10:00+00:00')
    b = goal(store, '周五前整理操作说明手册', people=(), created='2026-11-02T09:40:00+00:00', deadline=deadline)
    with store.write() as conn:
        conn.execute('UPDATE goals SET reminder_minutes=600 WHERE id=?', (a,))
        conn.execute("INSERT INTO goal_duplicates(goal_a,goal_b,status,created_at) VALUES(?,?,'possible','x')", (a, b))
        gm._schedule(conn, gm._row(conn, b), Clock('2026-11-02T10:00:00+00:00')())
    enable(store, review=False, duplicates=True)
    run_goals(store, Judge(store, 'same'), Clock('2026-11-03T01:00:00+00:00'))
    Goals(store, clock=Clock('2026-11-03T02:30:00+00:00')).generate_notifications()
    with store.write() as conn:
        early, = [r[0] for r in conn.execute("SELECT id FROM notifications WHERE goal_id=? AND status='pending'", (a,))]
        if take:
            conn.execute("UPDATE notifications SET status='taken',taken_at=? WHERE id=?", ('2026-11-03T02:35:00+00:00', early))
        before = dict(conn.execute('SELECT * FROM notifications WHERE id=?', (early,)).fetchone())
    Goals(store, clock=Clock('2026-11-03T03:00:00+00:00')).undo_auto_merge(
        judgments(store)[-1]['id'], expected_revision=row(store, a)['revision'])
    return a, b, early, before


def _notes(store, gid):
    with store.read() as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM notifications WHERE goal_id=? ORDER BY id', (gid,))]


@pytest.mark.parametrize('take', [False, True])
def test_undo_never_rewrites_existing_reminders_and_b_gets_exactly_one_on_time(store, take):
    a, b, early, before = _long_lead_merge(store, deadline='2026-11-03T12:00:00+00:00', take=take)
    with store.read() as conn:
        after = dict(conn.execute('SELECT * FROM notifications WHERE id=?', (early,)).fetchone())
    assert (after['goal_id'], after['content']) == (before['goal_id'], before['content']) == (a, before['content'])
    assert after['status'] == ('taken' if take else 'cancelled')
    assert _notes(store, b) == []  # B's own warning (11:00) is still ahead: no new record yet
    Goals(store, clock=Clock('2026-11-03T11:00:00+00:00')).generate_notifications()  # B's own warning time
    pending = [n for n in _notes(store, b) if n['status'] == 'pending']
    assert len(pending) == 1 and pending[0]['reminder_kind'] == 'soon' and pending[0]['id'] > early


def test_undo_reissues_immediately_when_bs_own_warning_time_has_passed(store):
    a, b, early, before = _long_lead_merge(store, deadline='2026-11-03T03:30:00+00:00', take=False)
    notes = _notes(store, b)
    assert len(notes) == 1 and notes[0]['status'] == 'pending' and notes[0]['id'] > early
    assert notes[0]['reminder_kind'] == 'immediate' and notes[0]['content'].startswith('目标临近截止')



def _all_notes(store):
    with store.read() as conn:
        return {r['id']: dict(r) for r in conn.execute('SELECT * FROM notifications ORDER BY id')}


def _assert_undo_reminders(store, before, *, due_soon=(), overdue=(), silent=()):
    """Undo never moves, rewrites or revives a record; each goal gets at most one fresh one."""
    after = _all_notes(store)
    involved = {*due_soon, *overdue, *silent}
    for nid, old in before.items():
        new = after[nid]
        assert (new['goal_id'], new['content']) == (old['goal_id'], old['content'])
        if old['goal_id'] in involved:  # pending ones are cancelled, nothing is revived, taken ones stay
            assert new['status'] == ('cancelled' if old['status'] == 'pending' else old['status'])
    fresh = [n for nid, n in after.items() if nid not in before]
    assert len(fresh) <= len(due_soon) + len(overdue)
    for gid in (*due_soon, *overdue):
        mine = [n for n in fresh if n['goal_id'] == gid]
        assert len(mine) == 1 and mine[0]['status'] == 'pending' and mine[0]['reminder_kind'] == 'immediate'
        assert mine[0]['content'].startswith('已过期' if gid in overdue else '目标临近截止')
        assert mine[0]['content'].endswith(row(store, gid)['content'])
    for gid in silent:
        assert [n for n in after.values() if n['goal_id'] == gid and n['status'] == 'pending'] == []


def _round5_setup(store, lead_a, lead_b):
    from iris import goals as gm
    from goal_auto_helpers import goal, subject
    store.set_setting('goal_dedup_judge', {'enabled': True, 'budget_seconds': 1})
    subject(store, 'p1', '程栗')
    a = goal(store, '周五前整理操作手册', people=('p1',), created='2026-11-02T08:10:00+00:00',
             deadline='2026-11-03T12:00:00+00:00')
    b = goal(store, '周五前整理操作说明手册', people=(), created='2026-11-02T09:40:00+00:00',
             deadline='2026-11-03T12:00:00+00:00')
    with store.write() as conn:
        conn.execute('UPDATE goals SET reminder_minutes=? WHERE id=?', (lead_a, a))
        conn.execute('UPDATE goals SET reminder_minutes=? WHERE id=?', (lead_b, b))
        conn.execute("INSERT INTO goal_duplicates(goal_a,goal_b,status,created_at) VALUES(?,?,'possible','x')", (a, b))
        for gid in (a, b):
            gm._schedule(conn, gm._row(conn, gid), Clock('2026-11-02T10:00:00+00:00')())
    return a, b


def _generate(store, at):
    goals = Goals(store, clock=Clock(at))
    goals._first_generation = False
    goals.generate_notifications()
    return goals


def test_undo_round5_after_the_kept_reminder_was_taken_both_goals_get_their_stage(store):
    """probe_undo_round5 (1): B warned first, A later; the merge kept one under A; the host took it."""
    a, b = _round5_setup(store, 60, 120)
    _generate(store, '2026-11-03T10:01:00+00:00')
    _generate(store, '2026-11-03T11:01:00+00:00')
    enable(store, review=False, duplicates=True)
    run_goals(store, Judge(store, 'same'), Clock('2026-11-03T11:30:00+00:00'))
    host = _generate(store, '2026-11-03T11:40:00+00:00')
    first = host.pull(after=0)
    assert first['items']
    before = _all_notes(store)
    jid = judgments(store)[-1]['id']
    Goals(store, clock=Clock('2026-11-03T11:50:00+00:00')).undo_auto_merge(jid, expected_revision=row(store, a)['revision'])
    _assert_undo_reminders(store, before, due_soon=[a, b])
    host.clock = Clock('2026-11-03T11:55:00+00:00')
    again = host.pull(after=first['next_cursor'])
    assert sorted(item['goal_id'] for item in again['items']) == sorted([a, b])  # neither goal loses its warning


def test_undo_round5_equal_deadlines_merged_before_the_warning(store):
    """probe_undo_round5 (2): one warning fired for A while merged; undo while it is pending."""
    a, b = _round5_setup(store, 60, 60)
    enable(store, review=False, duplicates=True)
    run_goals(store, Judge(store, 'same'), Clock('2026-11-03T01:00:00+00:00'))
    _generate(store, '2026-11-03T11:05:00+00:00')
    before = _all_notes(store)
    jid = judgments(store)[-1]['id']
    Goals(store, clock=Clock('2026-11-03T11:10:00+00:00')).undo_auto_merge(jid, expected_revision=row(store, a)['revision'])
    _assert_undo_reminders(store, before, due_soon=[a, b])
    with store.read() as conn:
        for gid in (a, b):
            assert [r[0] for r in conn.execute("SELECT reminder_kind FROM goal_reminder_plans WHERE goal_id=? AND status='scheduled'", (gid,))] == ['due']


def test_undo_past_the_deadline_issues_one_overdue_reminder_unless_turned_off(store):
    from iris import goals as gm
    from goal_auto_helpers import goal, subject
    store.set_setting('goal_dedup_judge', {'enabled': True, 'budget_seconds': 1})
    subject(store, 'p1', '程栗')
    a = goal(store, '周五前整理操作手册', people=('p1',), created='2026-11-02T08:10:00+00:00')
    b = goal(store, '周五前整理操作说明手册', people=(), created='2026-11-02T09:40:00+00:00',
             deadline='2026-11-03T03:00:00+00:00')
    with store.write() as conn:
        conn.execute("INSERT INTO goal_duplicates(goal_a,goal_b,status,created_at) VALUES(?,?,'possible','x')", (a, b))
    enable(store, review=False, duplicates=True)
    run_goals(store, Judge(store, 'same'), Clock('2026-11-03T01:00:00+00:00'))
    before = _all_notes(store)
    jid = judgments(store)[-1]['id']
    Goals(store, clock=Clock('2026-11-03T05:00:00+00:00')).undo_auto_merge(jid, expected_revision=row(store, a)['revision'])
    _assert_undo_reminders(store, before, overdue=[b], silent=[a])


def test_undo_past_the_deadline_respects_turned_off_overdue_reminders(store):
    from goal_auto_helpers import goal, subject
    store.set_setting('goal_dedup_judge', {'enabled': True, 'budget_seconds': 1})
    store.set_setting('goals', {'overdue_reminders': False})
    subject(store, 'p1', '程栗')
    a = goal(store, '周五前整理操作手册', people=('p1',), created='2026-11-02T08:10:00+00:00')
    b = goal(store, '周五前整理操作说明手册', people=(), created='2026-11-02T09:40:00+00:00',
             deadline='2026-11-03T03:00:00+00:00')
    with store.write() as conn:
        conn.execute("INSERT INTO goal_duplicates(goal_a,goal_b,status,created_at) VALUES(?,?,'possible','x')", (a, b))
    store.set_setting('goals', {'overdue_reminders': False, 'goal_duplicate_auto_judge_enabled': True})
    run_goals(store, Judge(store, 'same'), Clock('2026-11-03T01:00:00+00:00'))
    before = _all_notes(store)
    jid = judgments(store)[-1]['id']
    Goals(store, clock=Clock('2026-11-03T05:00:00+00:00')).undo_auto_merge(jid, expected_revision=row(store, a)['revision'])
    _assert_undo_reminders(store, before, silent=[a, b])
