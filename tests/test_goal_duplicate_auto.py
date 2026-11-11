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
