"""GD4: model evidence is required for new weak review hints."""
import json
from datetime import datetime, timezone

import httpx
import pytest

from iris import goal_dedup_judge as dedup
from iris.goals import Goals, write_learning_goal
from iris.models import ModelError
from test_goal_dedup_apply import Judge
from test_goal_dedup_judge import gateway


@pytest.mark.parametrize('origin', ['host', 'internal', 'admin'])
@pytest.mark.parametrize('verdict,marked', [('different', False), ('uncertain', True), ('same', True)])
def test_sequence_conflict_requires_model_and_never_merges(store, origin, verdict, marked):
    store.set_setting('goal_dedup_judge', {'enabled': True, 'method': 'D2'})
    judge = Judge(store, verdict=verdict)
    goals = Goals(store, gateway=judge, clock=lambda: datetime(2026, 10, 12, tzinfo=timezone.utc))
    old = goals.create(content='周五20点检查服务器')['goal']
    if origin == 'internal':
        with store.write() as conn:
            result = write_learning_goal(conn, content='周五晚上检查服务器', kind='normal', deadline=None,
                entry_id=None, evidence=[], current=goals.clock())
            assert result['dedup']['status'] == 'pending'
            assert result['goal']['possible_duplicate_ids'] == [] and judge.calls == []
        result = goals.review(result['submitted_id'])
    else:
        result = goals.create(content='周五晚上检查服务器', origin=origin)
    assert result['dedup']['status'] == ('possible_duplicate' if marked else 'created')
    assert result['goal']['possible_duplicate_ids'] == ([old['id']] if marked else [])
    assert result['goal']['merged_into'] is None and len(judge.calls) == 1
    assert [r['id'] for r in judge.calls[0]['candidates']] == [old['id']]


def test_suppressed_sequence_hint_remains_merge_blocker():
    result = dedup.decision_from_verdicts({1: 'same', 2: 'different'}, origin='host', guard_ids=[2])
    assert result == {'status': 'possible_duplicate', 'target_id': 1, 'target_ids': [1]}
    assert dedup.decision_from_verdicts({2: 'same'}, origin='host', guard_ids=[2])['status'] == 'possible_duplicate'
    assert dedup.decision_from_verdicts({2: 'different'}, origin='host', guard_ids=[2])['status'] == 'created'
    assert dedup.decision_from_verdicts({2: 'uncertain'}, origin='host', guard_ids=[2],
        scores={2: .3}, min_similarity=.4)['status'] == 'created'


def test_failure_does_not_manufacture_hint_and_existing_pending_policy_survives(store):
    store.set_setting('goal_dedup_judge', {'enabled': True, 'method': 'D2'})
    judge = Judge(store, verdict=ModelError('retryable', 'unit timeout', reason='timeout'))
    goals = Goals(store, gateway=judge)
    goals.create(content='检查三台服务器')
    result = goals.create(content='检查四台服务器')
    assert result['dedup']['status'] == 'pending'
    assert result['goal']['dedup_pending'] and not result['goal']['dedup_injectable']
    assert result['goal']['possible_duplicate_ids'] == []
    judge.verdict = 'different'
    result = goals.review(result['submitted_id'])
    assert result['dedup']['status'] == 'created' and result['goal']['dedup_injectable']


def test_conflict_revision_change_during_call_requires_review_again(store):
    store.set_setting('goal_dedup_judge', {'enabled': True, 'method': 'D2'})
    judge = Judge(store, verdict='uncertain')
    goals = Goals(store, gateway=judge)
    old = goals.create(content='检查三台服务器')['goal']
    def change():
        with store.write() as conn:
            conn.execute('UPDATE goals SET revision=revision+1 WHERE id=?', (old['id'],))
    judge.hook = change
    result = goals.create(content='检查服务器')
    assert result['dedup']['status'] == 'pending' and result['dedup']['reason'] == 'stale'
    assert result['goal']['possible_duplicate_ids'] == []


@pytest.mark.parametrize('bad', [
    '{"decisions":[{"id":1,"verdict":"possible"}]}',
    '{"decisions":[{"id":1,"verdict":"uncertain","confidence":0.9}]}',
    '{"decisions":[{"id":true,"verdict":"uncertain"}]}',
    '{"decisions":[]}', '{"answer":"可能是同一件事"}',
])
def test_new_judgment_invalid_schema_uses_existing_failure_policy(store, bad):
    model = gateway(store, lambda _: httpx.Response(200, json={
        'choices': [{'message': {'content': bad}, 'finish_reason': 'stop'}]}))
    try:
        with pytest.raises(ModelError) as error:
            dedup.judge(model, {'id': 2}, [{'id': 1}], method='D2', budget_seconds=1)
        assert error.value.category == 'invalid_output'
        with store.read() as conn:
            assert conn.execute('SELECT result_category FROM model_calls').fetchone()[0] == 'invalid_output'
    finally:
        model.close()


def test_disabled_model_does_not_create_similarity_only_hints(store):
    store.set_setting('goal_dedup_judge', {'enabled': False})
    goals = Goals(store)
    first = goals.create(content='周五问小林面试结果')['goal']
    second = goals.create(content='周五问小林的面试结果')
    assert second['dedup']['status'] == 'created'
    assert second['goal']['possible_duplicate_ids'] == []
    assert goals.create(content=first['content'])['dedup']['status'] == 'merged'
