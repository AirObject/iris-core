"""PE-14: recover malformed sentence coverage without guessing row identity."""
import copy
import json

import pytest

from iris.db import dumps
from iris.models import ModelError
from iris.persona import PersonaEngine, _check_verdicts
from test_persona_check_resilience import Checks, general_case
from test_persona_clause_checks import batch_material, batch_sentences
from test_persona_optimization import prepared


def run_check(store, model, *, purpose='persona_check', sentences=None, material=None, outputs=None):
    return PersonaEngine(store, model)._check_all(purpose, material or batch_material(),
        sentences if sentences is not None else batch_sentences()[:3],
        full_content='完整稿', outputs=outputs if outputs is not None else {}, assess_degree=False)


def damage_coverage(output, kind):
    rows = output['sentences']
    if kind == 'missing':
        rows.pop(1)
    elif kind == 'extra':
        rows.append({**copy.deepcopy(rows[0]), 'index': 99})
    elif kind == 'reordered':
        rows.reverse()
    elif kind == 'unknown':
        rows[1]['index'] = 99
    elif kind == 'duplicate':
        rows.append(copy.deepcopy(rows[1]))
    elif kind == 'not_list':
        output['sentences'] = None
    elif kind == 'string_index':
        rows[1]['index'] = '2'
    elif kind == 'bool_index':
        rows[1]['index'] = True
    elif kind == 'non_dict':
        rows[1] = None


@pytest.mark.parametrize('purpose', ['persona_check', 'persona_recheck'])
@pytest.mark.parametrize('kind', ['missing', 'extra', 'reordered', 'unknown', 'duplicate', 'not_list', 'string_index', 'bool_index', 'non_dict'])
@pytest.mark.parametrize('persistent', [False, True])
def test_batch_reask_then_use_only_uniquely_identified_rows(store, purpose, kind, persistent):
    def mutate(output, payload, number):
        for row in output['sentences']:
            row['reason'] = f"explicit sentence {row['index']}"
        if number == 1 or persistent:
            damage_coverage(output, kind)
    model, outputs = Checks(store, mutate), {}
    result = run_check(store, model, purpose=purpose, outputs=outputs)
    assert len(model.calls) == 2
    first, retry = [c[1] for c in model.calls]
    assert retry['candidate'] == first['candidate']
    assert retry['evidence_by_ref'] == first['evidence_by_ref']
    assert retry == first and 'format_retry' not in retry
    assert not result['validation_errors'] and not result['incomplete_checks']
    assert [r['index'] for r in result['sentences']] == [1, 2, 3]
    rejected = ({1, 2, 3} if kind == 'not_list' else
                {2} if kind in ('missing', 'unknown', 'duplicate', 'string_index', 'bool_index', 'non_dict') else set()) if persistent else set()
    for row in result['sentences']:
        assert row['supported'] is (row['index'] not in rejected)
        if row['index'] not in rejected:
            assert row['reason'] == f"explicit sentence {row['index']}"
    audit = result['invalid_batch_checks'][0]
    assert audit['batch'] == 1
    assert audit['status'] == ('partial' if persistent else 'corrected')
    assert audit['conservative_sentence_indices'] == sorted(rejected)
    assert audit['aligned_sentence_indices'] == [i for i in (1, 2, 3) if i not in rejected]
    assert not result['invalid_sentence_checks']
    raw = outputs[purpose + '_batches'][0]
    correction = raw[purpose + '_batch_corrections'][0]
    assert correction['outputs'][purpose]
    assert json.loads(raw[purpose])['sentences'] != result['sentences']


def test_reask_is_local_to_bad_middle_batch_and_keeps_global_sentence_indices(store):
    def mutate(output, payload, number):
        if number in (2, 3):
            damage_coverage(output, 'missing')
    model, outputs = Checks(store, mutate), {}
    result = run_check(store, model, sentences=batch_sentences(), outputs=outputs)
    assert [len(c[1]['candidate']) for c in model.calls] == [4, 4, 4, 1]
    assert model.calls[1][1]['candidate'] == model.calls[2][1]['candidate']
    assert [r['index'] for r in result['sentences'] if not r['supported']] == [6]
    assert result['invalid_batch_checks'][0]['batch'] == 2
    assert result['invalid_batch_checks'][0]['conservative_sentence_indices'] == [6]
    assert len(outputs['persona_check_batches']) == 3


@pytest.mark.parametrize('purpose', ['persona_check', 'persona_recheck'])
@pytest.mark.parametrize('adverse', ['supported', 'fabricated', 'violation'])
def test_batch_correction_cannot_erase_an_aligned_adverse_verdict(store, purpose, adverse):
    def mutate(output, payload, number):
        if number != 1:
            return
        row = output['sentences'][1]
        if adverse == 'supported':
            row['supported'] = False
        elif adverse == 'fabricated':
            row['fabricated'] = True
        else:
            row['violations'] = ['当前状态或待办']
        output['sentences'].reverse()
    model = Checks(store, mutate)
    result = run_check(store, model, purpose=purpose)
    assert [r['supported'] for r in result['sentences']] == [True, False, True]
    assert result['invalid_batch_checks'][0]['preserved_adverse_reasons']
    assert not result['validation_errors']
    if adverse == 'fabricated':
        assert result['sentences'][1]['fabricated']
    if adverse == 'violation':
        assert result['sentences'][1]['violations'] == ['当前状态或待办']


def test_wrong_identity_is_never_assigned_by_position(store):
    material, sentence = general_case()
    def mutate(output, payload, number):
        output['sentences'][1]['index'] = 99
        output['sentences'][0]['supported'] = False
    model = Checks(store, mutate)
    result = run_check(store, model, material=material, sentences=batch_sentences()[:1] + [sentence])
    assert len(model.calls) == 2
    assert [r['supported'] for r in result['sentences']] == [False, False]
    assert result['sentences'][1]['verdict_origin'] == 'conservative_rejection'
    assert not result['validation_errors'] and not result['invalid_sentence_checks']


def test_duplicate_index_does_not_select_a_favorable_verdict(store):
    def mutate(output, payload, number):
        other = copy.deepcopy(output['sentences'][1])
        other['supported'] = False
        output['sentences'].append(other)
    result = run_check(store, Checks(store, mutate))
    assert [r['supported'] for r in result['sentences']] == [True, False, True]
    assert not result['validation_errors']


def test_salvaged_row_still_must_pass_unchanged_proof_validation(store):
    material, sentence = general_case()
    def mutate(output, payload, number):
        row = output['sentences'][1]
        row['clauses'][0]['evidence'][0]['ref'] = 'not-bound'
        output['sentences'].reverse()
    result = run_check(store, Checks(store, mutate), material=material,
                       sentences=batch_sentences()[:1] + [sentence])
    assert [r['supported'] for r in result['sentences']] == [True, False]
    assert not result['validation_errors']
    assert _check_verdicts(result, batch_sentences()[:1] + [sentence], material=material)[2]


def test_batch_correction_and_timeout_share_original_stage_budget(store):
    def mutate(output, payload, number):
        if number == 2:
            damage_coverage(output, 'missing')
    model = Checks(store, mutate, fail=('timeout',))
    result = run_check(store, model)
    assert all(r['supported'] for r in result['sentences'])
    assert [d for _, _, d in model.calls] == [360, 720, 721]


def test_batch_correction_cannot_reset_spent_timeout_retry(store):
    def mutate(output, payload, number):
        damage_coverage(output, 'missing')
    model = Checks(store, mutate, fail=('timeout', None, 'timeout'))
    with pytest.raises(ModelError) as error:
        run_check(store, model)
    assert error.value.reason == 'timeout' and len(model.calls) == 3


def test_correction_material_limit_falls_back_without_another_call(store, monkeypatch):
    from iris import persona
    model = None
    def mutate(output, payload, number):
        damage_coverage(output, 'missing')
        monkeypatch.setattr(persona, 'MAX_CHECK_REQUEST_BYTES',
                            len(dumps(model.calls[-1][0]).encode('utf-8')) - 1)
    model = Checks(store, mutate)
    result = run_check(store, model)
    assert len(model.calls) == 1
    assert [r['supported'] for r in result['sentences']] == [True, False, True]
    assert not result['validation_errors']
    audit = result['invalid_batch_checks'][0]
    assert audit['status'] == 'partial' and audit['retry_errors']


@pytest.mark.parametrize('reason', ['rate_limited', 'output_limit'])
def test_batch_reask_does_not_hide_infrastructure_failures(store, reason):
    def mutate(output, payload, number):
        if number == 1: damage_coverage(output, 'missing')
    model = Checks(store, mutate, fail=(None, reason))
    if reason == 'output_limit':
        result = run_check(store, model)
        assert not result['validation_errors']
        assert result['output_limit_diagnostics']['split_results'] == {'recovered':1}
    else:
        with pytest.raises(ModelError) as error:
            run_check(store, model)
        assert error.value.reason == reason
    assert len(model.calls) == (4 if reason == 'output_limit' else 2)


def test_invalid_json_correction_rejects_the_affected_batch(store):
    class InvalidCorrection(Checks):
        def chat(self, messages, purpose, **kwargs):
            if len(self.calls) == 1:
                assert not self.store._writer.in_transaction
                self.calls.append((messages, json.loads(messages[-1]['content']), kwargs['_deadline']))
                raise ModelError('invalid_output', 'invalid correction JSON')
            return super().chat(messages, purpose, **kwargs)
    def mutate(output, payload, number):
        damage_coverage(output, 'missing')
    model = InvalidCorrection(store, mutate)
    result = run_check(store, model)
    assert len(model.calls) == 2
    assert [r['supported'] for r in result['sentences']] == [False, False, False]
    assert not result['validation_errors']


def test_valid_batches_never_enter_batch_correction(store):
    model = Checks(store)
    result = run_check(store, model)
    assert len(model.calls) == 1
    assert not result.get('invalid_batch_checks')


def test_exhausted_stage_skips_batch_reask_without_renewing_budget(store):
    from iris.persona import _fact_payload, _request_messages
    payload = _fact_payload(batch_material(), batch_sentences()[:3])
    model, outputs = Checks(store), {}
    batch = json.loads(model.chat(_request_messages('persona_check', payload),
                                  'persona_check', _deadline=360).content)
    damage_coverage(batch, 'missing')
    model.now = 900
    with pytest.raises(ModelError) as error:
        PersonaEngine(store, model)._correct_batch_coverage(
            'persona_check', payload, batch, outputs, deadline=900,
            retry_state={'timeouts': 0}, sentence_indices=[1, 2, 3])
    assert len(model.calls) == 1 and error.value.reason == 'timeout'
    assert outputs['persona_check_batch_corrections'][0]['status'] == 'infrastructure_failure'


def test_corrected_positive_cannot_erase_original_bound_proof_failure(store):
    material, sentence = general_case()
    def mutate(output, payload, number):
        if number == 1:
            output['sentences'][1]['clauses'][0]['evidence'][0]['ref'] = 'not-bound'
            output['sentences'].reverse()
    result = run_check(store, Checks(store, mutate), material=material,
                       sentences=batch_sentences()[:1] + [sentence])
    assert [r['supported'] for r in result['sentences']] == [True, False]
    audit = result['invalid_batch_checks'][0]
    assert audit['status'] == 'corrected' and audit['preserved_adverse_reasons']


@pytest.mark.parametrize('purpose', ['persona_check', 'persona_recheck'])
@pytest.mark.parametrize('persistent', [False, True])
def test_batch_coverage_recovery_uses_existing_sentence_repair_and_deletion(prepared, purpose, persistent):
    from test_persona_optimization import Model
    class Malformed(Model):
        damaged = False
        def chat(self, messages, called_purpose, **kwargs):
            reply = super().chat(messages, called_purpose, **kwargs)
            payload = json.loads(messages[-1]['content'])
            if called_purpose == purpose and payload['task'] == 'check_facts_and_rules':
                if persistent or not self.damaged:
                    value = json.loads(reply.content)
                    value['sentences'][-1]['index'] = 99
                    self.damaged = True
                    reply.content = dumps(value)
            return reply
    model = Malformed(prepared)
    result = PersonaEngine(prepared, model).regenerate(expected_version=1)
    checks = result['version']['checks']
    assert checks['passed']
    checked = checks['initial_model'] if purpose == 'persona_check' else checks['recheck']
    assert checked['invalid_batch_checks']
    assert not checked['validation_errors']
    requests = [r for p, r, _ in model.calls if p == purpose and r['task'] == 'check_facts_and_rules']
    assert any(a == b for a, b in zip(requests, requests[1:]))
    assert '一向' not in result['version']['content']
    if persistent and purpose == 'persona_recheck':
        assert checks['deleted_sentences']
    else:
        assert checks['repaired_sentences']


@pytest.mark.parametrize('mutation', ['none', 'memory', 'current'])
def test_batch_reask_keeps_evidence_and_current_version_cas(store, mutation):
    from test_persona import FakeGateway, add_self
    from iris.memory_ops import edit_memory
    from iris.persona import current_persona, select_evidence, admin_edit, PersonaConflict
    from iris.memory_ops import setup_role
    setup_role(store, 'Iris', 'Initial persona.')
    store.set_setting('persona_publish_mode', 'small_medium_auto')
    text = '在那次共读中，我核对了目录。'
    mid = add_self(store, text)
    ref = next(m['ref'] for m in select_evidence(store)['memories'] if m['memory_id'] == mid)
    class Correcting(FakeGateway):
        damaged = False
        corrected = False
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            payload = self.calls[-1][1]
            if purpose == 'persona_check' and self.damaged and not self.corrected:
                self.corrected = True
                if mutation == 'memory':
                    edit_memory(store, mid, 1, content='在那次共读中，我核对了标题。')
                elif mutation == 'current':
                    admin_edit(store, '管理员保留的内容。', expected_version=1)
            elif purpose == 'persona_check' and not self.damaged:
                value = json.loads(reply.content)
                value['sentences'][-1]['index'] = 99
                self.damaged = True
                reply.content = dumps(value)
            return reply
    model = Correcting(store, {'sentences': [{'text': text, 'basis': [ref]}]})
    if mutation == 'current':
        with pytest.raises(PersonaConflict):
            PersonaEngine(store, model).regenerate(expected_version=1)
        assert current_persona(store)['content'] == '管理员保留的内容。'
    else:
        result = PersonaEngine(store, model).regenerate(expected_version=1)
        assert model.corrected
        assert result['status'] == ('current' if mutation == 'none' else 'conflict')
        if mutation != 'none':
            assert current_persona(store)['content'] != text


def test_batch_envelope_recovery_never_supplies_the_final_change_degree(store):
    def mutate(output, payload, number):
        if payload['task'] == 'check_facts_and_rules':
            output['sentences'].reverse()
            output.update(change_degree='invalid', reason='')
        else:
            output['change_degree'] = 'large'
    model = Checks(store, mutate)
    result = PersonaEngine(store, model)._check_all('persona_check', batch_material(),
        batch_sentences()[:3], full_content='Complete draft', outputs={})
    assert len(model.calls) == 2
    assert result['invalid_batch_checks'][0]['normalized_fact_envelope']
    assert result['change_degree'] == 'large'
    assert result['degree_assessment']['status'] == 'deferred_after_rejection'
    assert all(not row['supported'] for row in result['sentences'])
    assert not result['validation_errors']


def test_invalid_degree_without_coverage_error_reasks_and_rejects_the_batch(store):
    def mutate(output, payload, number):
        output['change_degree'] = 'invalid'
    model = Checks(store, mutate)
    result = run_check(store, model)
    assert len(model.calls) == 2 and not result['validation_errors']
    assert all(not row['supported'] for row in result['sentences'])
    assert result['invalid_batch_checks'][0]['conservative_sentence_indices'] == [1, 2, 3]
