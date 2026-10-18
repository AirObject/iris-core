"""PE-10: bounded check materials and local, conservative protocol recovery."""
import copy
import json

import pytest

from iris.db import dumps
from iris.models import ModelError, ModelReply
from iris.persona import PersonaEngine, _check_verdicts
from persona_fakes import add_clause_proofs
from test_persona import store
from test_persona_clause_checks import batch_material, batch_sentences
from test_persona_focus_windows import material_and_sentence


def general_case():
    material, sentence = material_and_sentence(2)
    text = '合奏排练中我倾向先听节拍。'
    sentence.update(text=text, date_count=2, dates=['2026-10-01', '2026-10-04'])
    memory = material['evidence']['memories'][0]
    memory.update(content=text, date_count=2, dates=sentence['dates'], excerpts=[
        {'excerpt': '合奏排练中我先听了节拍。', 'date': '2026-10-01'},
        {'excerpt': '合奏排练中我又先听了节拍。', 'date': '2026-10-04'}])
    return material, sentence


class Checks:
    def __init__(self, store, mutate=None, fail=()):
        self.store, self.mutate, self.fail = store, mutate, fail
        self.calls, self.now = [], 0.

    def monotonic(self):
        return self.now

    def chat(self, messages, purpose, **kwargs):
        assert not self.store._writer.in_transaction
        payload = json.loads(messages[-1]['content'])
        self.calls.append((copy.deepcopy(messages), payload, kwargs['_deadline']))
        if len(self.calls) <= len(self.fail):
            reason = self.fail[len(self.calls)-1]
            if reason:
                self.now = kwargs['_deadline'] if reason == 'timeout' else self.now + 1
                raise ModelError('retryable', reason, reason=reason)
        self.now += 1
        output = add_clause_proofs({'sentences': [
            {'index': row['index'], 'supported': True, 'fabricated': False,
             'scene_qualified': False, 'violations': [], 'reason': '独立核对完整依据。'}
            for row in payload['candidate']], 'change_degree': 'small', 'reason': '格式完整。'}, payload)
        if self.mutate:
            self.mutate(output, payload, len(self.calls))
        return ModelReply(dumps(output), 'stop', {})


def check(store, model, material, sentences, outputs=None):
    return PersonaEngine(store, model)._check_all('persona_check', material, sentences,
        full_content=''.join(s['text'] for s in sentences),
        outputs=outputs if outputs is not None else {}, assess_degree=False)


def damage(row, field):
    if field == 'scope_missing': row.pop('scope_check')
    elif field == 'scope_bool': row['scope_check']['preserved'] = 'true'
    elif field == 'scope_sources': row['scope_check']['source_scopes'] = None
    elif field == 'scope_quote': row['scope_check']['source_scopes'][0]['quote'] = None
    elif field == 'clause_bool': row['clauses'][0]['supported'] = 1
    elif field == 'reason': row['reason'] = ''
    else: row['supported'] = 1


@pytest.mark.parametrize('field', ['scope_missing','scope_bool','scope_sources','scope_quote','clause_bool','reason','supported'])
@pytest.mark.parametrize('persistent', [False, True])
def test_invalid_fields_reask_only_the_mapped_sentence_then_accept_or_reject(store, field, persistent):
    material, sentence = general_case()
    def mutate(output, payload, number):
        for row, candidate in zip(output['sentences'], payload['candidate']):
            if candidate['origin'] == 'memory' and (number == 1 or persistent):
                damage(row, field)
    model, outputs = Checks(store, mutate), {}
    original = copy.deepcopy(material)
    result = check(store, model, material, batch_sentences()[:1] + [sentence], outputs)
    assert len(model.calls) == 2
    assert [s['origin'] for s in model.calls[1][1]['candidate']] == ['memory']
    assert model.calls[1][1]['candidate'][0]['index'] == 1
    assert model.calls[1][1]['evidence_by_ref'] == model.calls[0][1]['evidence_by_ref']
    assert [r['index'] for r in result['sentences']] == [1, 2]
    assert result['sentences'][0]['supported']
    assert result['sentences'][1]['supported'] is (not persistent)
    assert not result['incomplete_checks'] and not result['validation_errors']
    assert material == original
    audit = result['invalid_sentence_checks'][0]
    assert audit['sentence_index'] == 2
    assert audit['status'] == ('conservative_rejection' if persistent else 'corrected')
    raw = outputs['persona_check_batches'][0]
    assert raw['persona_check_field_corrections'][0]['outputs']['persona_check']
    assert json.loads(raw['persona_check'])['sentences'][1] != result['sentences'][1]


def test_valid_fields_do_not_request_a_correction(store):
    material, sentence = general_case(); model = Checks(store)
    result = check(store, model, material, [sentence])
    assert len(model.calls) == 1 and result['sentences'][0]['supported']
    assert not result.get('invalid_sentence_checks')


@pytest.mark.parametrize('adverse', ['row', 'clause', 'fabricated', 'violation', 'scope', 'excluded_kind'])
def test_field_correction_never_erases_an_existing_adverse_verdict(store, adverse):
    material, sentence = general_case()
    def mutate(output, payload, number):
        if number != 1: return
        row = output['sentences'][0]
        damage(row, 'scope_sources')
        if adverse == 'row': row['supported'] = False
        elif adverse == 'clause': row['clauses'][0]['supported'] = False
        elif adverse == 'fabricated': row['fabricated'] = True
        elif adverse == 'violation': row['violations'] = ['无依据的完成状态']
        elif adverse == 'scope': row['scope_check']['preserved'] = False
        else: row['persona_kind'] = 'task'
    model = Checks(store, mutate)
    result = check(store, model, material, [sentence])
    assert not result['sentences'][0]['supported']
    assert _check_verdicts(result, [sentence], material=material)[2]
    assert not result['incomplete_checks']


def test_unmapped_sentence_is_reasked_then_conservatively_rejected(store):
    material, sentence = general_case()
    def mutate(output, payload, number): output['sentences'][0]['index'] = 99
    model = Checks(store, mutate)
    result = check(store, model, material, [sentence])
    assert not result['validation_errors'] and len(model.calls) == 2
    assert not result['sentences'][0]['supported']
    assert result['invalid_batch_checks'][0]['conservative_sentence_indices'] == [1]


def large_pair():
    material, sentence = general_case()
    first = material['evidence']['memories'][0]
    for source in first['excerpts']:
        source['excerpt'] += '背景资料' * 250
    second = copy.deepcopy(first); second.update(ref='M2', memory_id=8)
    material['evidence']['memories'].append(second)
    other = {**sentence, 'basis':[{'memory_id':8,'revision':1}]}
    return material, [sentence, other]


def test_material_bytes_split_small_clause_batches_without_truncating_sources(store):
    from iris import persona
    material, sentences = large_pair(); before = copy.deepcopy(material)
    model = Checks(store)
    result = check(store, model, material, sentences)
    assert len(model.calls) == 2  # Both fit by clause count, not by byte size.
    for messages, payload, _ in model.calls:
        assert len(dumps(messages).encode('utf-8')) <= persona.MAX_CHECK_REQUEST_BYTES
        assert len(payload['candidate']) == len(payload['evidence_by_ref']) == 1
        ref, memory = next(iter(payload['evidence_by_ref'].items()))
        original = next(m for m in material['evidence']['memories'] if m['ref'] == ref)
        assert [s['excerpt'] for s in memory['excerpts']] == [s['excerpt'] for s in original['excerpts']]
    assert all(r['supported'] for r in result['sentences']) and material == before


def test_one_oversized_sentence_is_rejected_without_sending_or_truncating_it(store):
    from iris import persona
    material, sentence = general_case()
    material['evidence']['memories'][0]['excerpts'][0]['excerpt'] += '完整背景' * 5000
    before = copy.deepcopy(material); model = Checks(store)
    result = check(store, model, material, [sentence] + batch_sentences()[:1])
    assert all(s['origin'] == 'admin' for _, p, _ in model.calls for s in p['candidate'])
    assert not result['sentences'][0]['supported'] and result['sentences'][1]['supported']
    assert result['material_rejections'][0]['sentence_index'] == 1
    assert result['material_rejections'][0]['request_bytes'] > persona.MAX_CHECK_REQUEST_BYTES
    assert not result['incomplete_checks'] and material == before


@pytest.mark.parametrize('failures', [('timeout',), ('timeout', 'timeout')])
def test_timeout_retries_same_batch_once_inside_900_second_stage(store, failures):
    material, sentence = general_case(); model, outputs = Checks(store, fail=failures), {}
    if len(failures) == 2:
        with pytest.raises(ModelError) as error: check(store, model, material, [sentence], outputs)
        assert error.value.reason == 'timeout'
    else:
        result = check(store, model, material, [sentence], outputs)
        assert result['sentences'][0]['supported'] and not result['incomplete_checks']
    assert len(model.calls) == 2
    assert model.calls[0][1] == model.calls[1][1]
    assert [deadline for _, _, deadline in model.calls] == [360, 720]
    retries = outputs['persona_check_batches'][0]['persona_check_timeout_retries']
    assert len(retries) == 1 and retries[0]['error_reason'] == 'timeout'


@pytest.mark.parametrize('reason', ['rate_limited', 'paused', 'call_budget'])
def test_non_timeout_errors_never_use_timeout_retry(store, reason):
    material, sentence = general_case(); model = Checks(store, fail=(reason,))
    with pytest.raises(ModelError): check(store, model, material, [sentence])
    assert len(model.calls) == 1


def test_field_correction_and_timeout_share_original_stage_deadline(store):
    material, sentence = general_case()
    def mutate(output, payload, number):
        if number == 2:
            damage(output['sentences'][0], 'scope_bool')
    model = Checks(store, mutate, fail=('timeout',)); outputs = {}
    result = check(store, model, material, [sentence], outputs)
    assert result['sentences'][0]['supported']
    assert len(model.calls) == 3
    assert [d for _, _, d in model.calls] == [360, 720, 721]
    assert all(d <= 900 for _, _, d in model.calls)


def test_invalid_suffix_correction_cannot_erase_negative_from_the_suffix(store):
    material, sentence = material_and_sentence(2)
    def mutate(output, payload, number):
        row = output['sentences'][0]
        row['scene_qualified'] = True
        for part in row['clauses']:
            part['scene_qualified'] = True
        if number == 1:
            row['clauses'] = row['clauses'][:1]
        elif number == 2:
            row['clauses'][0]['supported'] = False
            row['reason'] = ''  # Invalid completion, with a real adverse opinion.
    model = Checks(store, mutate)
    result = check(store, model, material, [sentence])
    assert not result['sentences'][0]['supported']
    assert not result['incomplete_checks']
    assert result['invalid_sentence_checks'][0]['preserved_adverse_reasons']
    assert len(model.calls) == 3


@pytest.mark.parametrize('mutation', ['none', 'memory', 'current'])
def test_field_reask_runs_outside_transactions_and_keeps_publication_cas(store, mutation):
    from test_persona import FakeGateway, add_self
    from iris.memory_ops import edit_memory
    from iris.persona import current_persona, select_evidence, admin_edit
    text = '在那次共读中，我核对了目录。'
    mid = add_self(store, text)
    ref = next(m['ref'] for m in select_evidence(store)['memories'] if m['memory_id'] == mid)
    class Correcting(FakeGateway):
        damaged = False
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            payload = self.calls[-1][1]
            if payload.get('format_retry'):
                if mutation == 'memory': edit_memory(store, mid, 1, content='在那次共读中，我核对了标题。')
                elif mutation == 'current': admin_edit(store, '管理员保留的内容。', expected_version=1)
            elif purpose == 'persona_check' and not self.damaged:
                value = json.loads(reply.content)
                for row, candidate in zip(value['sentences'], payload['candidate']):
                    if candidate['origin'] == 'memory' and not candidate.get('initial_setting'):
                        row['supported'] = 'true'; self.damaged = True
                reply.content = dumps(value)
            return reply
    model = Correcting(store, {'sentences':[{'text':text,'basis':[ref]}]})
    if mutation == 'current':
        from iris.persona import PersonaConflict
        with pytest.raises(PersonaConflict):
            PersonaEngine(store, model).regenerate(expected_version=1)
        assert current_persona(store)['content'] == '管理员保留的内容。'
        return
    result = PersonaEngine(store, model).regenerate(expected_version=1)
    assert any(p.get('format_retry') for _, p in model.calls)
    assert result['status'] == ('current' if mutation == 'none' else 'conflict')
    if mutation != 'none': assert current_persona(store)['content'] != text


def test_outbound_material_guard_also_covers_json_format_repair(store):
    from iris import persona
    class BadJSON(Checks):
        def chat(self, messages, purpose, **kwargs):
            self.calls.append(messages)
            return ModelReply('坏格式' * 15000, 'stop', {})
    model, outputs = BadJSON(store), {}
    with pytest.raises(ModelError) as error:
        PersonaEngine(store, model)._call('persona_check', {'candidate': []}, outputs)
    assert error.value.reason == 'check_request_limit'
    assert len(model.calls) == 1
    assert outputs['persona_check_request_bytes'][-1] > persona.MAX_CHECK_REQUEST_BYTES


def test_degree_timeout_also_retries_without_leaving_check_stage(store):
    model, outputs = Checks(store, fail=('timeout',)), {}
    result = PersonaEngine(store, model)._check_all('persona_check', batch_material(), [],
        full_content='已完成事实检查的正文。', outputs=outputs)
    assert result['degree_assessment']['status'] == 'complete'
    assert len(model.calls) == 2 and [d for _, _, d in model.calls] == [360, 720]


def test_later_success_without_operation_ownership_does_not_prove_recovery():
    from iris.persona_evaluation import _invalid_model_calls
    assert _invalid_model_calls([{'id': 1, 'purpose':'persona_check', 'timed_out':True},
        {'id':2, 'purpose':'persona_check', 'result_category':'success', 'timed_out':False}])[0]['reason'] == 'timeout'


@pytest.mark.parametrize('health', [False, True])
def test_timeout_retry_preserves_real_gateway_stream_and_call_accounting(store, health):
    from test_persona_streaming import engine_for, response, envelope
    def respond(request, now, number):
        return response(envelope(), now, [0, 361 if number == 1 else 1, 0, 0, 0, 0])
    outputs = {}
    with engine_for(store, respond, health=health) as (engine, now, requests, client):
        assert engine._check_call('persona_check', {}, outputs,
            deadline=900, retry_state={'timeouts': 0}) == {}
        assert len(requests) == 2
        assert all(json.loads(r.content)['stream'] for r in requests)
    assert not outputs['persona_check_transport'][0]['complete']
    retry = outputs['persona_check_timeout_retries'][0]
    assert retry['status'] == 'complete'
    assert retry['outputs']['persona_check_transport'][0]['complete']
    with store.read() as conn:
        calls = [dict(r) for r in conn.execute('SELECT * FROM model_calls ORDER BY id')]
    assert len(calls) == 2
    assert [r['timed_out'] for r in calls] == [1, 0]
    assert calls[-1]['result_category'] == 'success'


def test_timeout_retry_does_not_extend_exhausted_stage(store):
    model, outputs = Checks(store, fail=('timeout',)), {}
    with pytest.raises(ModelError) as error:
        PersonaEngine(store, model)._check_call('persona_check', {'candidate': []}, outputs,
            deadline=100, retry_state={'timeouts': 0})
    assert error.value.reason == 'timeout' and len(model.calls) == 1
    assert model.calls[0][2] == 100
    assert not outputs.get('persona_check_timeout_retries')


def test_field_correction_cannot_reset_a_batches_spent_timeout_retry(store):
    material, sentence = general_case()
    def mutate(output, payload, number):
        damage(output['sentences'][0], 'scope_bool')
    model = Checks(store, mutate, fail=('timeout', None, 'timeout'))
    with pytest.raises(ModelError) as error: check(store, model, material, [sentence])
    assert error.value.reason == 'timeout' and len(model.calls) == 3
    assert model.calls[-1][1]['format_retry']
