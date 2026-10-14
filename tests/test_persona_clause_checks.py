"""PO3: clause-local proof boundaries and one deadline across small check batches."""
import copy
import json

import pytest

from iris.db import dumps
from iris.models import ModelError, ModelReply
from iris.persona import PersonaEngine, current_persona
from test_persona import add_self, store


def payload():
    return {'candidate': [{'index': 1, 'text': '在两次闲聊中，我喜欢听故事，再问细节。',
        'origin': 'memory', 'initial_setting': False, 'evidence_refs': ['M1'],
        'clauses': [{'index': 1, 'text': '在两次闲聊中'}, {'index': 2, 'text': '我喜欢听故事'},
                    {'index': 3, 'text': '再问细节。'}]}],
        'evidence_by_ref': {'M1': {'content': '摘要不能补证。', 'date': '2026-10-02',
            'excerpts': [{'excerpt': '我喜欢听故事。', 'date': '2026-10-01'},
                         {'excerpt': '我喜欢听故事，也问细节。', 'date': '2026-10-02'}]}}}


def verdict():
    return {'sentences': [{'index': 1, 'supported': True, 'fabricated': False,
        'scene_qualified': False, 'violations': [], 'reason': '分别核对。', 'clauses': [
            {'index': i, 'supported': True, 'scene_qualified': False, 'reason': '各自引用。',
             'evidence': [{'ref': 'M1', 'source_index': 1, 'quote': '我喜欢听故事。'},
                          {'ref': 'M1', 'source_index': 2, 'quote': '我喜欢听故事，也问细节。'}]}
            for i in range(1, 4)]}], 'change_degree': 'small', 'reason': '有界变化。'}


def validate(result=None, request=None):
    from iris.persona import _validate_clause_checks
    return _validate_clause_checks(result or verdict(), request or payload())


def test_each_clause_counts_its_own_dates_not_the_sentence_union():
    result = verdict()
    result['sentences'][0]['clauses'][2]['evidence'] = result['sentences'][0]['clauses'][2]['evidence'][1:]
    structural, problems = validate(result)
    assert not structural
    assert 'clause 3' in ' '.join(problems[1]) and 'distinct dates' in ' '.join(problems[1])


def test_repeated_quote_or_multiple_sources_on_same_day_cannot_supply_two_dates():
    request = payload()
    request['evidence_by_ref']['M1']['excerpts'][1]['date'] = '2026-10-01'
    assert validate(request=request)[1]


@pytest.mark.parametrize('change', [
    {'ref': 'M2'}, {'source_index': 0}, {'source_index': 3},
    {'quote': '没有这句话'}, {'quote': ''}, {'source_index': True},
])
def test_proof_must_be_exact_quote_from_own_bound_original(change):
    result = verdict()
    result['sentences'][0]['clauses'][0]['evidence'][0].update(change)
    assert validate(result)[1]


def test_unbound_shared_record_cannot_support_another_sentences_clause():
    request = payload()
    request['evidence_by_ref']['M2'] = copy.deepcopy(request['evidence_by_ref']['M1'])
    result = verdict()
    result['sentences'][0]['clauses'][0]['evidence'][0]['ref'] = 'M2'
    assert validate(result, request)[1]


def test_summary_fallback_only_when_no_original_excerpt_exists():
    request = payload()
    from iris.persona import _clause_parts
    text = '在那次闲聊中，我喜欢听故事。'
    request['candidate'][0].update(text=text, clauses=_clause_parts(text))
    request['evidence_by_ref']['M1'].update(content=text, excerpts=[])
    result = verdict()
    result['sentences'][0]['clauses'] = result['sentences'][0]['clauses'][:len(_clause_parts(text))]
    for part in result['sentences'][0]['clauses']:
        part.update(scene_qualified=True, evidence=[{'ref': 'M1', 'source_index': 0, 'quote': text}])
    assert validate(result, request) == ([], {})
    request['evidence_by_ref']['M1']['excerpts'] = payload()['evidence_by_ref']['M1']['excerpts']
    assert validate(result, request)[1]


def test_model_cannot_label_generic_activity_as_specific_single_date_scene():
    result = verdict()
    for part in result['sentences'][0]['clauses']:
        part.update(scene_qualified=True, evidence=part['evidence'][:1])
    assert 'explicit bounded scene' in ' '.join(validate(result)[1][1])


def test_one_false_clause_overrides_supported_whole_sentence():
    result = verdict()
    result['sentences'][0]['clauses'][1]['supported'] = False
    assert validate(result)[1]


@pytest.mark.parametrize('change', ['missing', 'reordered', 'bad_type'])
def test_clause_coverage_and_types_fail_closed(change):
    result = verdict()
    parts = result['sentences'][0]['clauses']
    if change == 'missing':
        parts.pop()
    elif change == 'reordered':
        parts.reverse()
    else:
        parts[0]['supported'] = 1
    assert validate(result)[0]


@pytest.mark.parametrize('origin', ['admin', 'initial_template'])
def test_protected_sentences_do_not_require_memory_proofs(origin):
    request, result = payload(), verdict()
    request['candidate'][0]['origin'] = origin
    result['sentences'][0].pop('clauses')
    assert validate(result, request) == ([], {})


def test_check_payload_exposes_actual_role_name_and_focus_parts(store):
    from iris.persona import _check_payload, _generation_material, _settings
    add_self(store, '在那次共读中，我读了目录，也圈出了两行。')
    engine = PersonaEngine(store, None)
    with store.read() as conn:
        material = _generation_material(conn, _settings(conn), current_persona(store), engine.clock().isoformat())
    memory = next(m for m in material['evidence']['memories'] if m['stance'] != '设定')
    row = {'text': memory['content'], 'origin': 'memory', 'basis': [{'memory_id': memory['memory_id'], 'revision': memory['revision']}]}
    request = _check_payload(material, [row], full_content=row['text'])
    assert request['role_name'] == 'Iris'
    assert [part['text'] for part in request['candidate'][0]['clauses']] == ['在那次共读中', '我读了目录', '也圈出了两行。']


class BatchGateway:
    def __init__(self, duration=20, *, broken=None):
        self.now, self.duration, self.broken = 0.0, duration, broken
        self.calls = []

    def monotonic(self):
        return self.now

    def chat(self, messages, purpose, **kwargs):
        request = json.loads(messages[-1]['content'])
        self.calls.append((purpose, request, kwargs['_deadline']))
        self.now += self.duration
        rows = [{'index': row['index'], 'supported': True, 'fabricated': False, 'persona_kind':'admin',
            'scene_qualified': True, 'violations': [], 'reason': '手写句。'} for row in request['candidate']]
        if self.broken == len(self.calls):
            rows[0]['index'] = 99
        return ModelReply(dumps({'sentences': rows,
            'change_degree': 'large' if len(self.calls) == 2 else 'small', 'reason': '完整稿幅度。'}), 'stop', {})


def batch_material():
    return {'role_name': 'Iris', 'settings': {}, 'as_of': '2026-10-01', 'previous': {'content': ''},
            'evidence': {'memories': []}}


def batch_sentences():
    return [{'text': f'手写第{i}句。', 'origin': 'admin', 'basis': []} for i in range(9)]


def test_batches_keep_local_indices_deadlines_final_degree_and_all_raw_outputs(store):
    gateway, outputs = BatchGateway(), {}
    result = PersonaEngine(store, gateway)._check_all('persona_check', batch_material(), batch_sentences(), full_content='整稿', outputs=outputs)
    assert [len(c[1]['candidate']) for c in gateway.calls] == [4, 4, 1, 0]
    assert [c[2] for c in gateway.calls] == [360, 380, 400, 420]
    assert all([s['index'] for s in c[1]['candidate']] == list(range(1, len(c[1]['candidate']) + 1)) for c in gateway.calls)
    assert [s['index'] for s in result['sentences']] == list(range(1, 10))
    assert result['change_degree'] == 'small'
    assert len(outputs['persona_check_batches']) == 4
    assert all('persona_check' in batch for batch in outputs['persona_check_batches'])


def test_bad_local_index_is_not_silently_renumbered(store):
    gateway = BatchGateway(broken=2)
    result = PersonaEngine(store, gateway)._check_all('persona_check', batch_material(), batch_sentences(), full_content='整稿', outputs={})
    from iris.persona import _check_verdicts
    assert _check_verdicts(result, batch_sentences())[1]


def test_whole_stage_timeout_preserves_partial_outputs_without_resetting_budget(store):
    gateway, outputs = BatchGateway(duration=130), {}
    with pytest.raises(ModelError) as error:
        PersonaEngine(store, gateway)._check_all('persona_check', batch_material(), batch_sentences()*3, full_content='整稿', outputs=outputs)
    assert error.value.reason == 'timeout'
    assert len(gateway.calls) == 7 and [c[2] for c in gateway.calls] == [360,490,620,750,880,900,900]
    assert len(outputs['persona_check_batches']) == 7
    assert current_persona(store)['id'] == 1


@pytest.mark.parametrize('text,allowed', [
    ('在那次共读中，我把这一页读完。', False),
    ('在那次共读中，我没有读完这一页。', True),
    ('在那次共读中，我更喜欢先读完这一页。', True),
    ('在那次共读中，我没有偏好变化，把这一页读完。', False),
])
def test_bare_result_cannot_borrow_earlier_clauses_negation_or_preference(text, allowed):
    from test_persona_evidence_guards import problems, sentence, memory
    assert bool(problems(sentence(text), memory())) is not allowed


def test_timeout_in_later_batch_records_attempt_and_keeps_current_version(store):
    from test_persona import FakeGateway
    from iris.persona import select_evidence
    mid = add_self(store, '在那次直播中，我解释了规则。')
    ref = next(m['ref'] for m in select_evidence(store)['memories'] if m['memory_id'] == mid)
    class SlowCheck(FakeGateway):
        now = 0.0
        def monotonic(self):
            return self.now
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            if purpose == 'persona_check':
                self.now += 361 if sum(p == 'persona_check' for p,_ in self.calls) == 3 else 130
            return reply
    model = SlowCheck(store, {'sentences': [{'text': f'在那次直播中，我解释了第{i}条规则。',
        'basis': [ref]} for i in range(9)]})
    result = PersonaEngine(store, model).regenerate(expected_version=1)
    assert result['status'] == 'failed' and result['reason'] == 'timeout'
    assert current_persona(store)['id'] == 1
    with store.read() as conn:
        row = conn.execute('SELECT outputs_json FROM persona_attempts WHERE id=?', (result['attempt_id'],)).fetchone()
    assert len(json.loads(row[0])['persona_check_batches']) == 3
    assert [purpose for purpose, _ in model.calls] == ['persona_generate', 'persona_check', 'persona_check', 'persona_check']


def test_model_cannot_inject_computed_diagnostics_or_crash_batch_validation(store):
    class Noisy(BatchGateway):
        def chat(self, *args, **kwargs):
            reply = super().chat(*args, **kwargs)
            value = json.loads(reply.content)
            if value['sentences']:
                value['validation_errors'] = 4
                value['sentences'][0]['clause_errors'] = {'bogus': True}
            reply.content = dumps(value)
            return reply
    result = PersonaEngine(store, Noisy())._check_all('persona_check', batch_material(), batch_sentences(), full_content='整稿', outputs={})
    assert len(result['sentences']) == 9
    assert all(row['clause_errors'] == [] for row in result['sentences'])


def test_one_batch_still_has_only_360_seconds(store):
    gateway = BatchGateway(duration=361)
    with pytest.raises(ModelError) as error:
        PersonaEngine(store, gateway)._check_all('persona_check', batch_material(), batch_sentences()[:4], full_content='整稿', outputs={})
    assert error.value.reason == 'timeout'
    assert len(gateway.calls) == 1 and gateway.calls[0][2] == 360


def test_multiple_batches_use_full_request_budget_within_fixed_stage(store):
    gateway = BatchGateway(duration=100)
    result = PersonaEngine(store, gateway)._check_all('persona_check', batch_material(), batch_sentences(), full_content='整稿', outputs={})
    assert len(result['sentences']) == 9
    assert [c[2] for c in gateway.calls] == [360, 460, 560, 660]
    assert gateway.now == 400


@pytest.mark.parametrize('summary', ['ReadTimeout', 'WriteTimeout', 'ConnectTimeout', 'PoolTimeout'])
def test_known_gateway_timeout_summary_keeps_timeout_attempt_reason(store, summary):
    class TimedOut:
        def chat(self, *args, **kwargs):
            raise ModelError('retryable', summary)
    result = PersonaEngine(store, TimedOut()).regenerate(expected_version=1)
    assert result['status'] == 'failed' and result['reason'] == 'timeout'


def test_long_compound_sentences_are_batched_by_focus_work_as_well_as_sentence_count(store):
    class FocusGateway(BatchGateway):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            request = json.loads(messages[-1]['content'])
            result = json.loads(reply.content)
            for row, candidate in zip(result['sentences'], request['candidate'], strict=True):
                row['clauses'] = [{'index': p['index'], 'evidence': [], 'supported': False,
                    'scene_qualified': True, 'reason': '格式测试中没有来源。'} for p in candidate['clauses']]
            reply.content = dumps(result)
            return reply
    sentences = [{'text': f'在那次共读中，我标记了第{i}段，也核对了目录。',
        'origin': 'memory', 'basis': [], 'initial_setting': False, 'date_count': 1} for i in range(5)]
    gateway, outputs = FocusGateway(), {}
    result = PersonaEngine(store, gateway)._check_all('persona_check', batch_material(), sentences,
        full_content=''.join(s['text'] for s in sentences), outputs=outputs)
    assert [len(c[1]['candidate']) for c in gateway.calls] == [1]*5
    assert [r['index'] for r in result['sentences']] == list(range(1, 6))
    assert all(len(r['clauses'])==2 and r['unchecked_clause_indices']==[3] for r in result['sentences'])
    assert len(result['skipped_windows']) == 5
    assert [c[2] for c in gateway.calls] == [360+20*i for i in range(5)]
    assert len(outputs['persona_check_batches']) == 5


def test_degree_only_request_explicitly_has_no_fact_check_work(store):
    gateway = BatchGateway()
    PersonaEngine(store, gateway)._check_all('persona_recheck', batch_material(), [], full_content='整稿', outputs={})
    assert gateway.calls[0][1]['task'] == 'degree_only'
    assert gateway.calls[0][1]['candidate'] == []
    assert gateway.calls[0][2] == 360


def test_oversized_sentence_keeps_full_text_while_proof_windows_are_split():
    from iris.persona import _check_batches
    text = '，'.join(f'分句{i}' for i in range(9)) + '。'
    sentence = {'text':text, 'origin':'memory', 'initial_setting':False}
    following = {'text':'管理员原文。', 'origin':'admin'}
    batches=_check_batches([sentence,following])
    automatic=[s for batch in batches for s in batch if s['origin']=='memory']
    assert len(batches)==5 and all(s['text']==text for s in automatic)
    assert [p['index'] for s in automatic for p in s['_check_focus']]==list(range(1,10))
    assert all(sum(len(s.get('_check_focus',[])) for s in batch)<=2 for batch in batches)
    assert sum(s['origin']=='admin' for batch in batches for s in batch)==1
    assert '_check_focus' not in sentence and '_check_sentence' not in following
