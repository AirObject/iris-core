"""PE-16: output-limit splitting and unpublished incomplete checkpoints; no network."""
import copy
import json

import pytest

from iris.db import dumps
from iris.models import ModelError, ModelReply
from iris.persona import PersonaEngine, current_persona
from iris.persona_evaluation import run_persona_eval, _metrics
from test_persona import store, clock, add_self, FakeGateway as ProductGateway
from test_persona_check_resilience import Checks
from test_persona_clause_checks import batch_material, batch_sentences
from test_persona_evaluation import FakeGateway, TIMELINE, tmp_path


class Limited(Checks):
    def __init__(self, store, maximum, *, terminal=False, cost=1, mutate=None):
        super().__init__(store, mutate)
        self.maximum, self.terminal, self.cost = maximum, terminal, cost

    def chat(self, messages, purpose, **kwargs):
        reply = super().chat(messages, purpose, **kwargs)
        self.now += self.cost - 1
        payload = self.calls[-1][1]
        if len(payload['candidate']) > self.maximum or self.terminal:
            reply.finish_reason = 'length'
        return reply


@pytest.mark.parametrize('purpose', ['persona_check', 'persona_recheck'])
@pytest.mark.parametrize('maximum,counts,splits', [
    (2, [4, 2, 2], 1), (1, [4, 2, 1, 1, 2, 1, 1], 3),
])
def test_truncation_splits_recursively_preserving_order_and_budget(store, purpose, maximum, counts, splits):
    model, outputs = Limited(store, maximum), {}
    sentences = batch_sentences()[:4]
    result = PersonaEngine(store, model)._check_all(purpose, batch_material(), sentences,
        full_content=''.join(s['text'] for s in sentences), outputs=outputs, assess_degree=False)
    assert [len(c[1]['candidate']) for c in model.calls] == counts
    assert [r['index'] for r in result['sentences']] == [1, 2, 3, 4]
    assert [r['reason'] for r in result['sentences']] == ['独立核对完整依据。'] * 4
    assert all(r['supported'] for r in result['sentences'])
    assert not result['validation_errors']
    assert all(c[2] <= 900 for c in model.calls)
    diagnostics = result['output_limit_diagnostics']
    assert diagnostics['truncations'] == diagnostics['splits'] == splits
    assert diagnostics['split_results'] == {'recovered': splits}
    assert diagnostics['terminal_truncations'] == 0


@pytest.mark.parametrize('purpose', ['persona_check', 'persona_recheck'])
def test_single_sentence_truncation_is_incomplete_without_accepting_partial_json(store, purpose):
    model, outputs = Limited(store, 1, terminal=True), {}
    result = PersonaEngine(store, model)._check_all(purpose, batch_material(), batch_sentences()[:1],
        full_content='draft', outputs=outputs, assess_degree=False)
    assert len(model.calls) == 1 and result['validation_errors']
    assert not result['sentences']
    assert result['output_limit_diagnostics']['terminal_truncations'] == 1


def test_split_children_do_not_renew_the_stage_budget(store):
    model, outputs = Limited(store, 1, cost=350), {}
    with pytest.raises(ModelError) as caught:
        PersonaEngine(store, model)._check_all('persona_check', batch_material(), batch_sentences()[:4],
            full_content='draft', outputs=outputs, assess_degree=False)
    assert caught.value.reason == 'timeout'
    assert model.now == 1050 and len(model.calls) == 3
    assert model.calls[-1][2] == 900
    split = outputs['persona_check_batches'][0]['persona_check_output_splits'][0]
    assert split['status'] == 'incomplete_timeout'


@pytest.mark.parametrize('stage', ['field', 'focus', 'degree'])
def test_single_unit_truncation_in_nested_check_is_incomplete(store, stage):
    from test_persona_check_resilience import general_case
    material, sentence = general_case()
    def mutate(value, payload, number):
        if number != 1 or stage == 'degree': return
        if stage == 'field': value['sentences'][0]['reason'] = ''
        else:
            value['sentences'][0]['clauses'] = value['sentences'][0]['clauses'][:1]
    if stage == 'focus':
        sentence['text'] = '合奏排练中我倾向先听节拍，也会复述拍点。'
        memory = material['evidence']['memories'][0]
        memory['content'] = sentence['text']
        for source in memory['excerpts']:
            source['excerpt'] = '合奏排练中我先听了节拍，也复述了拍点。'
    class Nested(Checks):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            if len(self.calls) > 1: reply.finish_reason = 'length'
            return reply
    model, outputs = Nested(store, mutate), {}
    result = PersonaEngine(store, model)._check_all('persona_check', material, [sentence],
        full_content=sentence['text'], outputs=outputs)
    assert result['validation_errors'] and result['output_limit_diagnostics']['truncations'] == 1
    assert len(model.calls) == 2


@pytest.mark.parametrize('mode', ['all_auto', 'small_medium_auto', 'all_manual'])
def test_product_never_publishes_incomplete_check_even_with_auto_mode(store, clock, mode):
    store.set_setting('persona_publish_mode', mode)
    add_self(store)
    previous = current_persona(store)
    class Truncated(ProductGateway):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            if purpose == 'persona_check': reply.finish_reason = 'length'
            return reply
    model = Truncated(store, {'sentences': []})
    result = PersonaEngine(store, model, clock=clock).regenerate(expected_version=previous['id'])
    assert result['status'] == 'rejected' and not result['version']['is_current']
    assert not result['version']['checks']['passed']
    assert current_persona(store) == previous


def limited_evaluation(tmp_path, incomplete, *, timelines=1, infra=None):
    class Recorded(FakeGateway):
        def __init__(self, configs, store, **kwargs):
            super().__init__(configs, store, **kwargs)
            self.clock, self.generation = kwargs['clock'], 0
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            if purpose == 'persona_generate': self.generation += 1
            limited = purpose == 'persona_check' and self.generation <= incomplete
            if limited: reply.finish_reason = 'length'
            with self.store.write() as conn:
                conn.execute("""INSERT INTO model_calls
                    (purpose,model,duration_ms,result_category,status_code,timed_out,finish_reason,created_at)
                    VALUES(?,'fake',1,?,?,?,?,?)""",
                    (purpose, 'retryable' if limited and infra else 'success',
                     429 if limited and infra == 'rate_limited' else 200,
                     int(limited and infra == 'timeout'), reply.finish_reason, self.clock().isoformat()))
            return reply
    case = copy.deepcopy(TIMELINE)
    event = case['events'][1]
    case['events'] = case['events'][:1] + [
        {**event, 'id':f'c{i}', 'action':'regenerate'} for i in range(1, 5)]
    cases = [{**copy.deepcopy(case), 'id':f't{i}'} for i in range(timelines)]
    corpus = tmp_path/'fixture.jsonl'
    corpus.write_text('\n'.join(dumps(c) for c in cases), encoding='utf-8')
    return run_persona_eval({}, tmp_path, corpus=corpus, out=tmp_path/'out',
        judge_mode='external', gateway_factory=Recorded, runner_identity='PE16-format')


@pytest.mark.parametrize('incomplete,valid', [(1, True), (2, True), (3, False)])
def test_runner_keeps_current_continues_and_counts_incomplete_checkpoints(tmp_path, incomplete, valid):
    _, report = limited_evaluation(tmp_path, incomplete)
    metrics, row = report['metrics'], report['rows'][0]
    assert row['complete'] and row['remaining_events'] == 0
    assert len(row['checkpoints']) == 4
    assert metrics['run_valid'] is valid
    assert metrics['incomplete_check_count'] == incomplete
    assert metrics['incomplete_check_limit'] == 2
    assert metrics['invalid_call_count'] == 0
    assert metrics['calls']['tolerated_output_limited_calls'] >= incomplete
    assert all(c['outcome'] == 'unpublished_current_retained' for c in metrics['incomplete_checks'])
    previous = row['observations'][0]['candidate']
    for checkpoint in row['checkpoints'][:incomplete]:
        assert checkpoint['result']['status'] == 'rejected'
        assert checkpoint['current']['id'] == previous['id']
        assert checkpoint['current']['content'] == previous['content']
    assert row['checkpoints'][-1]['result']['version']['checks']['passed']
    _, cached = limited_evaluation(tmp_path, incomplete)
    assert cached['resumed_cases'] == 1 and cached['metrics']['run_valid'] is valid


def test_limit_is_per_run_not_per_timeline(tmp_path):
    _, report = limited_evaluation(tmp_path, 2, timelines=2)
    assert report['executed_timelines'] == 2
    assert all(row['complete'] for row in report['rows'])
    assert report['metrics']['incomplete_check_count'] == 4
    assert not report['metrics']['run_valid']
    assert 'incomplete_check_limit_exceeded' in report['metrics']['run_invalid_reasons']


@pytest.mark.parametrize('infra', ['timeout', 'rate_limited', 'transport_failure'])
def test_tolerated_incomplete_check_does_not_hide_infrastructure_failure(tmp_path, infra):
    _, report = limited_evaluation(tmp_path, 1, infra=infra)
    assert not report['metrics']['run_valid']
    assert report['metrics']['invalid_call_count'] >= 1
    assert len(report['rows'][0]['checkpoints']) == 1


@pytest.mark.parametrize('purpose', ['persona_check', 'persona_recheck'])
def test_split_children_reuse_structure_recovery_and_keep_negative_verdicts(store, purpose):
    def mutate(value, payload, number):
        if number == 2:
            value['change_degree'] = 'invalid'
            value['sentences'][0]['supported'] = False
    model, outputs = Limited(store, 2, mutate=mutate), {}
    result = PersonaEngine(store, model)._check_all(purpose, batch_material(), batch_sentences()[:4],
        full_content='draft', outputs=outputs, assess_degree=False)
    assert [len(c[1]['candidate']) for c in model.calls] == [4, 2, 2, 2]
    assert [row['supported'] for row in result['sentences']] == [False, True, True, True]
    assert result['structure_diagnostics']['initial_failures']['batch_degree'] == 1
    assert result['output_limit_diagnostics']['split_results'] == {'recovered':1}


@pytest.mark.parametrize('purpose', ['persona_check', 'persona_recheck'])
def test_truncated_batch_correction_preserves_original_aligned_negatives(store, purpose):
    def mutate(value, payload, number):
        if number == 1:
            value['change_degree'] = 'invalid'
            value['sentences'][0]['supported'] = False
    class CorrectionLimited(Checks):
        def chat(self, messages, called_purpose, **kwargs):
            reply = super().chat(messages, called_purpose, **kwargs)
            if len(self.calls) == 2: reply.finish_reason = 'length'
            return reply
    model, outputs = CorrectionLimited(store, mutate), {}
    result = PersonaEngine(store, model)._check_all(purpose, batch_material(), batch_sentences()[:4],
        full_content='draft', outputs=outputs, assess_degree=False)
    assert [len(c[1]['candidate']) for c in model.calls] == [4, 4, 2, 2]
    assert [row['supported'] for row in result['sentences']] == [False, True, True, True]
    assert not result['validation_errors']
    split = outputs[purpose+'_batches'][0][purpose+'_output_splits'][0]
    assert split['preserved_adverse_sentence_indices'] == [1]


def test_terminal_child_does_not_run_its_sibling_or_accept_completed_fragments(store):
    model, outputs = Limited(store, 1, terminal=True), {}
    result = PersonaEngine(store, model)._check_all('persona_check', batch_material(), batch_sentences()[:4],
        full_content='draft', outputs=outputs, assess_degree=False)
    assert [len(c[1]['candidate']) for c in model.calls] == [4, 2, 1]
    assert not result['sentences'] and result['validation_errors']
    assert result['output_limit_diagnostics'] == {
        'truncations':3, 'splits':2, 'terminal_truncations':1,
        'split_results':{'incomplete_output_limit':2}}


def test_successful_split_is_recovered_by_its_own_complete_operation(tmp_path):
    class Split(FakeGateway):
        def __init__(self, configs, store, **kwargs):
            super().__init__(configs, store, **kwargs)
            self.clock = kwargs['clock']
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            payload = json.loads(messages[-1]['content'])
            if purpose == 'persona_check' and len(payload['candidate']) > 1:
                reply.finish_reason = 'length'
            with self.store.write() as conn:
                conn.execute("""INSERT INTO model_calls
                    (purpose,model,duration_ms,result_category,status_code,timed_out,finish_reason,created_at)
                    VALUES(?,'fake',1,'success',200,0,?,?)""",
                    (purpose, reply.finish_reason, self.clock().isoformat()))
            return reply
    case = copy.deepcopy(TIMELINE); case['events'] = case['events'][:2]
    corpus = tmp_path/'fixture.json'; corpus.write_text(dumps(case), encoding='utf-8')
    _, report = run_persona_eval({}, tmp_path, corpus=corpus, out=tmp_path/'out',
        judge_mode='external', gateway_factory=Split, runner_identity='split')
    metrics = report['metrics']
    assert metrics['run_valid'] and metrics['incomplete_check_count'] == 0
    assert metrics['calls']['recovered_output_limited_calls'] == 1
    assert metrics['calls']['tolerated_output_limited_calls'] == 0
    assert metrics['check_output_limits']['split_results'] == {'recovered':1}
    rows = copy.deepcopy(report['rows'])
    rows[0]['checkpoints'][0].pop('call_ids')
    assert not _metrics(rows)['run_valid']


@pytest.mark.parametrize('damage', ['published', 'current_changed', 'missing_ownership', 'duplicate_ownership'])
def test_incomplete_tolerance_requires_unpublished_retained_current_and_unique_owner(tmp_path, damage):
    _, report = limited_evaluation(tmp_path, 1)
    rows = copy.deepcopy(report['rows']); row = rows[0]; first = row['checkpoints'][0]
    if damage == 'published': first['result']['status'] = 'current'
    elif damage == 'current_changed': first['current']['content'] = 'different'
    elif damage == 'missing_ownership': first.pop('call_ids')
    else: row['checkpoints'][1]['call_ids'].extend(first['call_ids'])
    assert not _metrics(rows)['run_valid']
