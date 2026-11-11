"""A comparison quantifier keeps its own range: bindings here, equivalence with the model."""
import copy
import json
from importlib.resources import files

import pytest

from iris.db import dumps
from iris.models import ModelReply
from iris.persona import (PersonaEngine, _adverse_field_verdicts, _check_payload, _focus_quantifiers,
                          _generation_material, _settings, _validate_clause_checks, current_persona)
from test_persona import FakeGateway, add_self
from test_persona_source_payload import prepare, store


UTTERANCE = '这是第一次在这里演奏这首曲子'
CURRENT = '我在合奏团第一次演奏这首曲子'
KEPT = '2026年3月1日在合奏团第一次演奏这首曲子。'
LOST = '2026年3月1日那次直播是我第一次演奏这首曲子。'


def check(**fields):
    return {'candidate_quantifier':'第一次', 'candidate_scope':'在合奏团', 'source_quantifier':'第一次',
            'source_scopes':[{'ref':'M1', 'quote':'在这里'}], 'preserved':True, **fields}


def fixture(text=KEPT, checks=None, excerpt=UTTERANCE, current=CURRENT):
    memory = {'current_revision_text':current, 'excerpts':[
        {'excerpt':excerpt, 'date':'2026-03-01', 'speaker_subject_id':'self'}]}
    sentence = {'index':1, 'origin':'memory', 'initial_setting':False, 'text':text, 'evidence_refs':['M1'],
                'scope_check_required':False, 'clauses':[{'index':1, 'text':text}]}
    part = {'index':1, 'supported':True, 'scene_qualified':True, 'reason':'当次原话支持。',
            'evidence':[{'ref':'M1', 'source_index':1, 'quote':excerpt}]}
    if checks is not None:
        part['quantifier_checks'] = checks
    return {'sentences':[{'index':1, 'clauses':[part]}]}, {'candidate':[sentence], 'evidence_by_ref':{'M1':memory}}


def verdict(*args, **kwargs):
    output, payload = fixture(*args, **kwargs)
    return _validate_clause_checks(output, payload)


def problems_of(*args, **kwargs):
    errors, problems = verdict(*args, **kwargs)
    assert not errors
    return ' '.join(problems.get(1, []))


@pytest.mark.parametrize('text,expected', [
    ('我第一次演奏', ['第一次']), ('这是首次登台', ['首次']), ('这是头一次', ['头一次']), ('唯一的一次', ['唯一']),
    ('我只喜欢慢节拍', ['只']), ('只有我知道', ['只有']), ('全部听完', ['全部']), ('所有的曲子', ['所有']),
    ('每次都先听', ['每次']), ('我从不喝咖啡', ['从不']), ('我从来没去过', ['从来没']), ('总是先听', ['总是']),
    ('我只在每次合奏时先听', ['只', '每次'])])
def test_quantifiers_are_a_closed_class_of_whole_tokens(text, expected):
    assert _focus_quantifiers(text) == expected


@pytest.mark.parametrize('text', ['这只猫', '一只猫和两只狗', '这只是打算', '只要有空', '不是每次', '不总是先听',
                                  '我不只喜欢慢节拍', '不是所有事情都这样', '《只此青绿》很好看', '很有成就感', '我喜欢慢节拍'])
def test_other_uses_and_negated_forms_are_not_quantifier_claims(text):
    assert _focus_quantifiers(text) == []


def test_scope_kept_under_an_equivalent_name_passes():
    # “这里”和具体名称指同一处；范围不要求逐字相同，语义等价由模型判断。
    assert verdict(checks=[check()]) == ([], {})


def test_scope_kept_verbatim_from_the_current_text_passes():
    assert verdict(checks=[check(source_scopes=[{'ref':'M1', 'quote':'在合奏团'}])]) == ([], {})


def test_quantifier_without_any_range_on_either_side_passes():
    text = '2026年3月1日那次我第一次演奏这首曲子。'
    assert verdict(text, [check(candidate_scope='', source_scopes=[])],
                   excerpt='这是我第一次演奏这首曲子', current='我第一次演奏这首曲子') == ([], {})


def test_source_that_really_names_an_entry_as_its_range_passes():
    text = '2026年3月1日第一次在直播里演奏这首曲子。'
    excerpt = '这是第一次在直播里演奏这首曲子'
    assert verdict(text, [check(candidate_scope='在直播里', source_scopes=[{'ref':'M1', 'quote':'在直播里'}])],
                   excerpt=excerpt, current=excerpt) == ([], {})


def test_lost_scope_reported_by_the_model_rejects_the_focus():
    joined = problems_of(LOST, [check(candidate_scope='', source_scopes=[], preserved=False)])
    assert 'quantifier scope not preserved' in joined


def test_lost_scope_cannot_be_declared_preserved():
    assert 'quantifier scope omitted' in problems_of(LOST, [check(candidate_scope='')])


@pytest.mark.parametrize('scope', ['2026年3月1日', '那次', '那次直播', '直播', '第一次'])
def test_date_occasion_or_entry_name_cannot_impersonate_the_range(scope):
    assert 'cannot be the range of a quantifier' in problems_of(LOST, [check(candidate_scope=scope)])


def test_date_cannot_impersonate_the_range_even_when_the_source_also_states_it():
    current = '2026年3月1日我第一次演奏这首曲子'
    joined = problems_of(LOST, [check(candidate_scope='2026年3月1日',
                                      source_scopes=[{'ref':'M1', 'quote':'2026年3月1日'}])], current=current)
    assert 'cannot be the range of a quantifier' in joined


def test_qualifier_of_another_candidate_clause_cannot_impersonate_the_range():
    text = '在合奏团排练那天，我第一次演奏这首曲子。'
    output, payload = fixture(text, [check(candidate_scope='在合奏团')])
    parts = [{'index':1, 'text':'在合奏团排练那天'}, {'index':2, 'text':'我第一次演奏这首曲子。'}]
    payload['candidate'][0]['clauses'] = parts
    first = copy.deepcopy(output['sentences'][0]['clauses'][0])
    first.pop('quantifier_checks')
    second = {**output['sentences'][0]['clauses'][0], 'index':2}
    output['sentences'][0]['clauses'] = [first, second]
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and 'same candidate clause' in ' '.join(problems[1])


def test_qualifier_of_another_source_clause_cannot_be_the_source_range():
    excerpt = '在这里演奏过，这是第一次演奏这首曲子'
    joined = problems_of(checks=[check(source_scopes=[{'ref':'M1', 'quote':'在这里'}])],
                         excerpt=excerpt, current=excerpt)
    assert 'same clause of an own bound source' in joined


@pytest.mark.parametrize('proof', [{'ref':'M9', 'quote':'在这里'}, {'ref':'M1', 'quote':'在别处'}])
def test_source_range_must_be_literal_and_bound_to_this_sentence(proof):
    assert 'own bound source' in problems_of(checks=[check(source_scopes=[proof])])


def test_quantifier_absent_from_the_source_is_not_kept():
    assert 'own bound source' in problems_of(
        checks=[check(source_scopes=[])], excerpt='我演奏了这首曲子', current='我演奏了这首曲子')


def test_another_speakers_words_cannot_set_the_range():
    output, payload = fixture(checks=[check(source_scopes=[{'ref':'M1', 'quote':'在别处'}])],
                              excerpt='第一次在别处演奏', current='')
    payload['evidence_by_ref']['M1']['excerpts'][0]['speaker_subject_id'] = 'other'
    _, problems = _validate_clause_checks(output, payload)
    assert 'own bound source' in ' '.join(problems[1])


@pytest.mark.parametrize('mutation', ['missing', 'not_list', 'unknown_field', 'missing_field', 'non_bool',
                                      'non_string', 'bad_proof', 'wrong_quantifier'])
def test_contract_is_complete_and_typed(mutation):
    value = check()
    if mutation == 'missing': value = None
    elif mutation == 'not_list': value = check()
    elif mutation == 'unknown_field': value['note'] = '多余'
    elif mutation == 'missing_field': value.pop('source_quantifier')
    elif mutation == 'non_bool': value['preserved'] = 1
    elif mutation == 'non_string': value['candidate_scope'] = None
    elif mutation == 'bad_proof': value['source_scopes'] = [{'ref':'M1'}]
    else: value['candidate_quantifier'] = '首次'
    checks = None if value is None else value if mutation == 'not_list' else [value]
    errors, _ = verdict(checks=checks)
    assert any('invalid quantifier scope verdict' in error for error in errors)


def test_redundant_per_source_verdict_is_typed_and_can_only_reject():
    assert verdict(checks=[check(source_scopes=[{'ref':'M1', 'quote':'在这里', 'preserved':True}])]) == ([], {})
    assert 'bound source M1' in problems_of(checks=[check(source_scopes=[{'ref':'M1', 'quote':'在这里', 'preserved':False}])])
    errors, _ = verdict(checks=[check(source_scopes=[{'ref':'M1', 'quote':'在这里', 'preserved':1}])])
    assert any('invalid quantifier scope verdict' in error for error in errors)


def test_every_quantifier_of_the_focus_needs_its_own_item():
    text = '2026年3月1日在合奏团第一次只演奏这首曲子。'
    errors, _ = verdict(text, [check()])
    assert any('invalid quantifier scope verdict' in error for error in errors)


def test_rejected_focus_does_not_need_a_quantifier_contract():
    output, payload = fixture(LOST)
    part = output['sentences'][0]['clauses'][0]
    part.update(supported=False, reason='范围扩大。')
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors and '范围扩大' in ' '.join(problems[1])


def test_focus_without_a_quantifier_needs_no_contract():
    assert verdict('2026年3月1日我在合奏团演奏这首曲子。') == ([], {})


def test_setting_and_handwritten_rows_are_not_checked():
    output, payload = fixture(LOST)
    payload['candidate'][0]['initial_setting'] = True
    assert _validate_clause_checks(output, payload) == ([], {})
    output, payload = fixture(LOST)
    payload['candidate'][0]['origin'] = 'admin'
    assert _validate_clause_checks(output, payload) == ([], {})


def test_negative_verdict_survives_a_malformed_row():
    row = {'clauses':[{'quantifier_checks':[{'preserved':False}]}]}
    assert 'original quantifier scope rejected' in _adverse_field_verdicts(row)


def test_real_payload_marks_only_automatic_foci_with_quantifiers(store):
    _, material, _, sentence = prepare(store)
    automatic = {**sentence, 'text':'那次配音里，我第一次请求再试一次。'}
    protected = {**sentence, 'text':'我第一次写下这句。', 'origin':'admin'}
    rows = _check_payload(material, [automatic, protected], full_content='')['candidate']
    assert rows[0]['clauses'] == [{'index':1, 'text':'那次配音里'},
                                  {'index':2, 'text':'我第一次请求再试一次。', 'quantifiers':['第一次']}]
    assert 'quantifiers' not in rows[1]['clauses'][0]


class RangeGateway(FakeGateway):
    """Fills the quantifier contract the way a checker reading the given range would."""
    source_scope, lost_scope = '在合奏团', ''

    def chat(self, messages, purpose, **kwargs):
        reply = super().chat(messages, purpose, **kwargs)
        if purpose not in ('persona_check', 'persona_recheck') or self.calls[-1][1].get('task') == 'degree_only':
            return reply
        output = json.loads(reply.content)
        request = self.calls[-1][1]
        for row, sentence in zip(output['sentences'], request['candidate']):
            for part, focus in zip(row['clauses'], sentence['clauses']):
                if focus.get('quantifiers'):
                    kept = self.source_scope in focus['text']
                    part['quantifier_checks'] = [{'candidate_quantifier':'第一次',
                        'candidate_scope':self.source_scope if kept else self.lost_scope,
                        'source_quantifier':'第一次',
                        'source_scopes':[{'ref':sentence['evidence_refs'][0], 'quote':self.source_scope}],
                        'preserved':True}]
        return ModelReply(dumps(output), 'stop', {})


def run_with_range(store, text, memory, **fields):
    mid = add_self(store, memory)
    previous = current_persona(store)
    with store.read() as conn:
        material = _generation_material(conn, _settings(conn), previous, '2026-10-13T12:00:00+00:00')
    ref = next(m['ref'] for m in material['evidence']['memories'] if m['memory_id'] == mid)
    gateway = RangeGateway(store, {'sentences':[{'text':text, 'basis':[ref]}]})
    for key, value in fields.items():
        setattr(gateway, key, value)
    return PersonaEngine(store, gateway).regenerate(expected_version=previous['id'])


MEMORY = '在这次演出中，我在合奏团第一次演奏这首曲子。'


def test_sentence_keeping_the_range_is_published_with_the_fake_checker(store):
    result = run_with_range(store, MEMORY, MEMORY)
    assert MEMORY in result['version']['content']
    assert result['version']['checks']['deleted_sentences'] == []


@pytest.mark.parametrize('lost_scope', ['', '这次演出', '2026年10月1日'])
def test_lost_or_impersonated_range_is_repaired_from_the_whole_memory(store, lost_scope):
    broad = '在这次演出中，我第一次演奏这首曲子。'
    result = run_with_range(store, broad, MEMORY, lost_scope=lost_scope)
    checks = result['version']['checks']
    assert any('quantifier' in error for error in checks['initial_model_errors'])
    assert broad not in result['version']['content']
    assert MEMORY in result['version']['content']


def test_new_prompts_state_the_contract_and_stay_free_of_corpus_words():
    check_prompt = files('iris').joinpath('prompts/persona_check_v36.md').read_text(encoding='utf-8')
    generate_prompt = files('iris').joinpath('prompts/persona_generate_v23.md').read_text(encoding='utf-8')
    for field in QUANTIFIER_FIELDS:
        assert field in check_prompt
    assert '比较范围' in check_prompt and '比较范围' in generate_prompt
    for corpus_word in ('纸风车', '夜航', '点歌台', '换牌', '南柚'):
        assert corpus_word not in check_prompt and corpus_word not in generate_prompt


QUANTIFIER_FIELDS = ('quantifier_checks', 'candidate_quantifier', 'candidate_scope', 'source_quantifier', 'source_scopes')
