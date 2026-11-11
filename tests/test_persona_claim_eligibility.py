"""PE-23: each claim of a sentence keeps its own eligibility; markers only oblige the check."""
import json
from importlib.resources import files

import pytest

from iris.db import dumps
from iris.memory_ops import edit_memory
from iris.models import ModelReply
from iris.persona import (PROMPT_VERSIONS, PersonaEngine, _adverse_field_verdicts, _check_payload,
                          _focus_plan_markers, _generation_material, _settings, _validate_clause_checks,
                          current_persona)
from test_persona import FakeGateway, add_self, store


UTTERANCE = '那次直播里我承认自己漏核了页码，说以后先核页码再交稿'
RECOGNITION = '那次直播里，我承认自己漏核了页码。'
WITH_PLAN = '那次直播里，我承认自己漏核了页码，说以后先核页码再交稿。'
PRACTICE = '那次直播里，我按之前的打算先核了页码再交稿。'
PRACTICE_SOURCE = '那次直播里我按之前的打算先核了页码再交稿'


def claim(text, kind='recognition', eligible=True, **fields):
    return {'claim':text, 'claim_type':kind, 'eligible':eligible, 'reason':'逐项核对资格。', **fields}


def verdict(text, focus, claims=None, *, excerpt=UTTERANCE):
    memory = {'current_revision_text':excerpt, 'excerpts':[
        {'excerpt':excerpt, 'date':'2026-11-10', 'speaker_subject_id':'self', 'entry_kind':'live'}]}
    sentence = {'index':1, 'origin':'memory', 'initial_setting':False, 'text':text, 'evidence_refs':['M1'],
                'scope_check_required':False, 'clauses':[{'index':1, 'text':focus}]}
    part = {'index':1, 'supported':True, 'scene_qualified':True, 'reason':'当次原话支持。',
            'evidence':[{'ref':'M1', 'source_index':1, 'quote':excerpt}]}
    if claims is not None:
        part['claim_checks'] = claims
    return _validate_clause_checks({'sentences':[{'index':1, 'clauses':[part]}]},
                                   {'candidate':[sentence], 'evidence_by_ref':{'M1':memory}})


def problems_of(*args, **kwargs):
    errors, problems = verdict(*args, **kwargs)
    assert not errors  # Eligibility never becomes a structural reask.
    return ' '.join(problems.get(1, []))


@pytest.mark.parametrize('text,expected', [
    ('说以后先核页码再交稿', ['以后']), ('我下次先问', ['下次']), ('我会先问清楚', ['会']), ('我不会再抢话', ['会']),
    ('我要先问清楚', ['要']), ('我打算先核页码', ['打算']), ('我答应对方先核页码', ['答应']),
    ('我愿意保持这样相处', ['愿意']), ('我决定以后先问', ['决定', '以后'])])
def test_plan_markers_are_a_closed_class_of_whole_tokens(text, expected):
    assert _focus_plan_markers(text) == expected


@pytest.mark.parametrize('text', ['我承认自己漏核了页码', '这是一次机会', '我需要先核页码', '只要有空就去', '《以后再说》很好看',
                                  '要是有空就去', '我先问了要不要继续', '问对方会不会来', '我会不会太急', '打不打算一起去'])
def test_other_words_are_not_plan_markers(text):
    assert _focus_plan_markers(text) == []


def test_recognition_alone_needs_no_claim_contract():
    assert verdict(RECOGNITION, '我承认自己漏核了页码。') == ([], {})


def test_recognition_with_its_own_claim_verdict_passes():
    assert verdict(RECOGNITION, '我承认自己漏核了页码。', [claim('我承认自己漏核了页码')]) == ([], {})


def test_appended_plan_is_rejected_even_when_the_recognition_is_eligible():
    joined = problems_of(WITH_PLAN, '说以后先核页码再交稿。',
                         [claim('我承认自己漏核了页码'), claim('说以后先核页码再交稿', 'plan', False)])
    assert 'claim not eligible' in joined and '以后先核页码再交稿' in joined


def test_a_reported_plan_marked_eligible_is_still_rejected():
    # “说过”已经发生，但计划内容不因此获得资格；判成可收录的 plan 仍被拒绝。
    joined = problems_of(WITH_PLAN, '说以后先核页码再交稿。', [claim('说以后先核页码再交稿', 'plan', True)])
    assert '未来打算或承诺不能收录' in joined


def test_a_verdict_on_another_claim_cannot_stand_for_the_marked_one():
    joined = problems_of(WITH_PLAN, '说以后先核页码再交稿。', [claim('我承认自己漏核了页码')])
    assert '带“以后”的命题没有单独核对资格' in joined


def test_later_practice_bound_to_its_own_source_passes():
    assert verdict(PRACTICE, '我按之前的打算先核了页码再交稿。',
                   [claim('我按之前的打算先核了页码再交稿', 'action')], excerpt=PRACTICE_SOURCE) == ([], {})


@pytest.mark.parametrize('mutation', ['missing', 'empty', 'not_list', 'unknown_field', 'missing_field',
                                      'non_bool', 'bad_type', 'blank_claim', 'blank_reason'])
def test_missing_or_malformed_claim_verdicts_are_ineligible(mutation):
    value = claim('说以后先核页码再交稿', 'plan', False)
    if mutation == 'unknown_field': value['note'] = '多余'
    elif mutation == 'missing_field': value.pop('reason')
    elif mutation == 'non_bool': value['eligible'] = 0
    elif mutation == 'bad_type': value['claim_type'] = 'intention'
    elif mutation == 'blank_claim': value['claim'] = ' '
    elif mutation == 'blank_reason': value['reason'] = ''
    claims = {'missing':None, 'empty':[], 'not_list':value}.get(mutation, [value])
    assert 'claim_checks 缺失或格式不符' in problems_of(WITH_PLAN, '说以后先核页码再交稿。', claims)


def test_claim_must_be_quoted_from_the_sentence():
    joined = problems_of(WITH_PLAN, '说以后先核页码再交稿。', [claim('以后先核对页码', 'plan', False),
                                                               claim('说以后会认真', 'action')])
    assert '命题须原样取自本句' in joined


def test_supplied_verdicts_are_checked_without_a_marker_too():
    joined = problems_of(RECOGNITION, '我承认自己漏核了页码。', [claim('我承认自己漏核了页码', 'recognition', False)])
    assert 'claim not eligible' in joined


def test_rejected_focus_needs_no_claim_contract():
    memory = {'current_revision_text':UTTERANCE, 'excerpts':[{'excerpt':UTTERANCE, 'date':'2026-11-10'}]}
    sentence = {'index':1, 'origin':'memory', 'initial_setting':False, 'text':WITH_PLAN, 'evidence_refs':['M1'],
                'scope_check_required':False, 'clauses':[{'index':1, 'text':'说以后先核页码再交稿。'}]}
    part = {'index':1, 'supported':False, 'scene_qualified':True, 'reason':'计划不是亲历。', 'evidence':[]}
    errors, problems = _validate_clause_checks({'sentences':[{'index':1, 'clauses':[part]}]},
                                               {'candidate':[sentence], 'evidence_by_ref':{'M1':memory}})
    assert not errors and problems[1] == ['clause 1: 计划不是亲历。']


def test_an_ineligible_verdict_survives_a_malformed_row():
    assert 'original claim judged ineligible' in _adverse_field_verdicts(
        {'clauses':[{'claim_checks':[{'eligible':False}]}]})
    assert 'original claim judged ineligible' in _adverse_field_verdicts(
        {'clauses':[{'claim_checks':[{'claim_type':'plan', 'eligible':True}]}]})


def test_payload_marks_plan_markers_only_on_automatic_foci(store):
    from test_persona_source_payload import prepare
    _, material, _, sentence = prepare(store)
    automatic = {**sentence, 'text':WITH_PLAN}
    protected = {**sentence, 'text':WITH_PLAN, 'origin':'admin'}
    rows = _check_payload(material, [automatic, protected], full_content='')['candidate']
    assert rows[0]['clauses'][2] == {'index':3, 'text':'说以后先核页码再交稿。', 'plan_markers':['以后']}
    assert all('plan_markers' not in part for part in rows[0]['clauses'][:2])
    assert all('plan_markers' not in part for part in rows[1]['clauses'])


class ClaimGateway(FakeGateway):
    """Lists each marked focus as one claim; a claim with a plan word is a plan.

    It judges nothing else: `plan_words` stands in for the checker's reading.
    """
    plan_words, omit = ('以后',), False

    def chat(self, messages, purpose, **kwargs):
        reply = super().chat(messages, purpose, **kwargs)
        request = self.calls[-1][1]
        if purpose not in ('persona_check', 'persona_recheck') or request.get('task') != 'check_facts_and_rules':
            return reply
        output = json.loads(reply.content)
        for row, sentence in zip(output['sentences'], request['candidate']):
            for part, focus in zip(row['clauses'], sentence['clauses']):
                if self.omit:
                    part.pop('claim_checks', None)
                elif focus.get('plan_markers'):
                    text = focus['text'].rstrip('。')
                    plan = any(word in text for word in self.plan_words)
                    part['claim_checks'] = [claim(text, 'plan' if plan else 'action', not plan)]
        return ModelReply(dumps(output), 'stop', {})


def run(store, text, memory, *, summary=None, **fields):
    mid = add_self(store, memory)
    if summary is not None:
        edit_memory(store, mid, 1, content=summary)
    previous = current_persona(store)
    with store.read() as conn:
        material = _generation_material(conn, _settings(conn), previous, '2026-11-12T12:00:00+00:00')
    ref = next(m['ref'] for m in material['evidence']['memories'] if m['memory_id'] == mid)
    gateway = ClaimGateway(store, {'sentences':[{'text':text, 'basis':[ref]}]})
    for key, value in fields.items():
        setattr(gateway, key, value)
    return PersonaEngine(store, gateway).regenerate(expected_version=previous['id']), gateway


def test_plan_is_stopped_and_repair_keeps_only_the_recognition(store):
    result, gateway = run(store, WITH_PLAN, WITH_PLAN, summary=RECOGNITION)
    checks = result['version']['checks']
    assert any('claim not eligible' in error for error in checks['initial_model_errors'])
    assert result['version']['content'].endswith(RECOGNITION)
    assert '以后' not in result['version']['content']
    assert checks['repaired_sentences'][0]['after']['text'] == RECOGNITION
    assert checks['passed']


def test_plan_without_a_recognition_only_memory_is_deleted(store):
    result, _ = run(store, WITH_PLAN, WITH_PLAN)
    checks = result['version']['checks']
    assert '以后' not in result['version']['content']
    assert checks['deleted_sentences'][0]['sentence']['text'] == WITH_PLAN


def test_practice_bound_to_its_own_source_is_kept(store):
    result, gateway = run(store, PRACTICE, PRACTICE)
    assert PRACTICE in result['version']['content']
    assert result['version']['checks']['deleted_sentences'] == []
    marked = [part for purpose, request in gateway.calls if purpose == 'persona_check'
              and request.get('task') == 'check_facts_and_rules'
              for row in request['candidate'] for part in row['clauses'] if part.get('plan_markers')]
    assert marked and marked[0]['plan_markers'] == ['打算']


def test_omitted_claim_verdict_is_not_eligible(store):
    result, _ = run(store, WITH_PLAN, WITH_PLAN, omit=True)
    checks = result['version']['checks']
    assert any('claim_checks 缺失' in error for error in checks['initial_model_errors'])
    assert WITH_PLAN not in result['version']['content']


def test_prompts_require_per_claim_eligibility_and_stay_free_of_corpus_words():
    check = files('iris').joinpath(f"prompts/{PROMPT_VERSIONS['persona_check']}.md").read_text(encoding='utf-8')
    generate = files('iris').joinpath(f"prompts/{PROMPT_VERSIONS['persona_generate']}.md").read_text(encoding='utf-8')
    for field in ('claim_checks', 'plan_markers', 'claim_type', 'eligible', 'recognition', 'action', 'result', 'plan'):
        assert field in check
    assert '保留当次认识时，省去随附的未来打算' in generate
    for corpus_word in ('纸风车', '夜航', '点歌台', '换牌', '南柚', '小舒', '接龙'):
        assert corpus_word not in check and corpus_word not in generate
