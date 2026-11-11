"""PE-23: a frequency or universal word may not rise above what its own source says."""
import json
from importlib.resources import files

import pytest

from iris.db import dumps
from iris.models import ModelReply
from iris.persona import (FREQUENCY_STRENGTH, PROMPT_VERSIONS, PersonaEngine, _check_payload, _focus_quantifiers,
                          _frequency_rank, _generation_material, _settings, _validate_clause_checks, current_persona)
from test_persona import FakeGateway, add_self, store


FIRST, SECOND = '接龙那天对方停住时我先问了要不要继续', '私聊时对方又停了，我也先问了，我愿意保持这样相处'
UNIVERSAL = '在接龙和私聊里，对方停住时我都先问要不要继续。'
BOUNDED = '在接龙和私聊里，那两次停住我都先问了要不要继续。'
HABIT_SOURCE = '合奏排练时我每次都先听节拍'
HABIT = '合奏排练时我每次都先听节拍。'


def check(word='都', scope='', source='', instances=2, **fields):
    return {'candidate_quantifier':word, 'candidate_scope':scope, 'source_quantifier':source,
            'source_scopes':[], 'source_instances':instances, 'preserved':True, **fields}


def verdict(text, focus, checks, *, excerpts=(FIRST, SECOND), current='停住时我先问对方要不要继续，愿意保持这样相处'):
    sources = [{'excerpt':excerpt, 'date':f'2026-11-{10 + 9 * i}', 'speaker_subject_id':'self'}
               for i, excerpt in enumerate(excerpts)]
    memory = {'current_revision_text':current, 'excerpts':sources}
    sentence = {'index':1, 'origin':'memory', 'initial_setting':False, 'text':text, 'evidence_refs':['M1'],
                'scope_check_required':False, 'clauses':[{'index':1, 'text':focus}]}
    part = {'index':1, 'supported':True, 'scene_qualified':False, 'reason':'两日原话都支持。',
            'evidence':[{'ref':'M1', 'source_index':i, 'quote':source['excerpt']}
                        for i, source in enumerate(sources, 1)], 'quantifier_checks':checks}
    return _validate_clause_checks({'sentences':[{'index':1, 'clauses':[part]}]},
                                   {'candidate':[sentence], 'evidence_by_ref':{'M1':memory}})


def problems_of(*args, **kwargs):
    errors, problems = verdict(*args, **kwargs)
    assert not errors
    return ' '.join(problems.get(1, []))


@pytest.mark.parametrize('text,expected', [
    ('对方停住时我都先问', ['都']), ('我都会先问', ['都']), ('我经常先听', ['经常']), ('有时候我会先问', ['有时候']),
    ('我习惯先听节拍', ['习惯']), ('我一直先听节拍', ['一直']), ('我从来没有抢话', ['从来没有']),
    ('每次都先听', ['每次']), ('所有的曲子都听过', ['所有']), ('我偶尔先听', ['偶尔'])])
def test_frequency_words_join_the_closed_quantifier_class(text, expected):
    assert _focus_quantifiers(text) == expected


@pytest.mark.parametrize('text', ['不总是先听', '并不是每次都先听', '不是所有事情都这样', '我不经常先听', '《总是有人》很好听',
                                  '我先听了节拍', '用大家都能听清的速度', '什么都想试试', '一点都不难', '我们都觉得好'])
def test_negated_correlative_and_title_uses_are_not_frequency_claims(text):
    assert _focus_quantifiers(text) == []


def test_strength_order_is_closed_and_ranked():
    assert [_frequency_rank(word) for word in ('偶尔', '有时', '经常', '通常', '习惯', '总是', '都', '每次', '从不')] \
        == [1, 2, 3, 4, 4, 5, 5, 5, 5]
    assert _frequency_rank('每次都') == 5 and _frequency_rank('好像') is None and _frequency_rank('愿意') is None
    assert set(FREQUENCY_STRENGTH.values()) == {1, 2, 3, 4, 5}


def test_universal_supported_by_a_source_frequency_word_passes():
    assert verdict(HABIT, HABIT, [check('每次', '合奏排练时', '每次', source_scopes=[{'ref':'M1', 'quote':'合奏排练时'}])],
                   excerpts=(HABIT_SOURCE, HABIT_SOURCE), current=HABIT_SOURCE) == ([], {})


@pytest.mark.parametrize('candidate,source', [('经常', '经常'), ('有时', '经常'), ('偶尔', '总是')])
def test_equal_or_weaker_frequency_passes(candidate, source):
    text = f'合奏排练时我{candidate}先听节拍。'
    excerpt = f'合奏排练时我{source}先听节拍'
    assert verdict(text, text, [check(candidate, '合奏排练时', source, source_scopes=[{'ref':'M1', 'quote':'合奏排练时'}])],
                   excerpts=(excerpt, excerpt), current=excerpt) == ([], {})


def test_a_few_instances_cannot_become_a_universal():
    joined = problems_of(UNIVERSAL, '对方停住时我都先问要不要继续。', [check('都', '对方停住时')])
    assert 'frequency raised above the source' in joined


def test_universal_with_will_is_rejected_whatever_the_claim_verdict():
    text = '在接龙和私聊里，对方停住时我都会先问要不要继续。'
    checks = [check('都', '')]
    errors, problems = verdict(text, '对方停住时我都会先问要不要继续。', checks)
    assert not errors and 'frequency raised above the source' in ' '.join(problems[1])


@pytest.mark.parametrize('candidate,source', [('总是', '有时'), ('经常', '偶尔'), ('通常', '经常')])
def test_stronger_word_than_the_source_is_rejected(candidate, source):
    text = f'合奏排练时我{candidate}先听节拍。'
    excerpt = f'合奏排练时我{source}先听节拍'
    joined = problems_of(text, text, [check(candidate, '合奏排练时', source,
                                            source_scopes=[{'ref':'M1', 'quote':'合奏排练时'}])],
                         excerpts=(excerpt, excerpt), current=excerpt)
    assert 'frequency strength raised' in joined


def test_bounded_experience_of_those_instances_passes():
    assert verdict(BOUNDED, '那两次停住我都先问了要不要继续。', [check('都', '那两次停住')]) == ([], {})


def test_bounded_experience_cannot_count_more_occasions_than_the_source():
    joined = problems_of(BOUNDED.replace('两', '三'), '那三次停住我都先问了要不要继续。', [check('都', '那三次停住')])
    assert 'more occasions' in joined


def test_willingness_is_not_a_frequency_word():
    joined = problems_of(UNIVERSAL, '对方停住时我都先问要不要继续。', [check('都', '对方停住时', '愿意')])
    assert 'frequency needs the frequency word of its own bound source' in joined


@pytest.mark.parametrize('text,focus,source', [
    ('那次接龙里我好像有点急，对方停住时我都先问要不要继续。', '对方停住时我都先问要不要继续。', ''),
    ('在接龙和私聊里，对方停住时我大概都先问要不要继续。', '对方停住时我大概都先问要不要继续。', ''),
    ('那次接龙里我好像有点急，对方停住时我都先问要不要继续。', '对方停住时我都先问要不要继续。', '好像')])
def test_a_hedge_cannot_stand_in_for_the_frequency_check(text, focus, source):
    excerpts = (FIRST + '，我好像有点急', SECOND)
    joined = problems_of(text, focus, [check('都', '对方停住时' if '大概' not in focus else '', source)],
                         excerpts=excerpts)
    assert 'frequency' in joined


@pytest.mark.parametrize('mutation', ['missing', 'bool', 'negative', 'text'])
def test_frequency_item_needs_its_instance_count(mutation):
    value = check('都', '那两次停住')
    if mutation == 'missing': value.pop('source_instances')
    elif mutation == 'bool': value['source_instances'] = True
    elif mutation == 'negative': value['source_instances'] = -1
    else: value['source_instances'] = '2'
    errors, _ = verdict(BOUNDED, '那两次停住我都先问了要不要继续。', [value])
    assert any('invalid quantifier scope verdict' in error for error in errors)


def test_preserved_false_rejects_the_focus():
    joined = problems_of(UNIVERSAL, '对方停住时我都先问要不要继续。', [check('都', preserved=False)])
    assert 'quantifier scope not preserved' in joined


def test_payload_names_frequency_words_within_quantifiers(store):
    from test_persona_source_payload import prepare
    _, material, _, sentence = prepare(store)
    rows = _check_payload(material, [{**sentence, 'text':'那次配音里，我每次都先请求再试一次，经常先听。'}],
                          full_content='')['candidate']
    assert rows[0]['clauses'][1] == {'index':2, 'text':'我每次都先请求再试一次', 'quantifiers':['每次'],
                                     'frequency_words':['每次']}
    assert rows[0]['clauses'][2] == {'index':3, 'text':'经常先听。', 'quantifiers':['经常'], 'frequency_words':['经常']}


class FrequencyGateway(FakeGateway):
    """Reports what the source states: instances only, never a frequency word."""

    def chat(self, messages, purpose, **kwargs):
        reply = super().chat(messages, purpose, **kwargs)
        request = self.calls[-1][1]
        if purpose not in ('persona_check', 'persona_recheck') or request.get('task') != 'check_facts_and_rules':
            return reply
        output = json.loads(reply.content)
        for row, sentence in zip(output['sentences'], request['candidate']):
            for part, focus in zip(row['clauses'], sentence['clauses']):
                if focus.get('frequency_words'):
                    part['quantifier_checks'] = [check(word, '', '', 1) for word in focus['quantifiers']]
                if focus.get('plan_markers'):
                    part['claim_checks'] = [{'claim':focus['text'].rstrip('。'), 'claim_type':'recognition',
                                             'eligible':True, 'reason':'假模型只列出命题。'}]
        return ModelReply(dumps(output), 'stop', {})


def test_universal_from_one_instance_is_repaired_to_the_bounded_memory(store):
    memory = '那次接龙里，对方停住时我先问了要不要继续。'
    candidate = '那次接龙里，对方停住时我都会先问要不要继续。'
    mid = add_self(store, memory)
    previous = current_persona(store)
    with store.read() as conn:
        material = _generation_material(conn, _settings(conn), previous, '2026-11-12T12:00:00+00:00')
    ref = next(m['ref'] for m in material['evidence']['memories'] if m['memory_id'] == mid)
    result = PersonaEngine(store, FrequencyGateway(store, {'sentences':[{'text':candidate, 'basis':[ref]}]})
                           ).regenerate(expected_version=previous['id'])
    checks = result['version']['checks']
    assert any('frequency raised above the source' in error for error in checks['initial_model_errors'])
    assert candidate not in result['version']['content'] and result['version']['content'].endswith(memory)
    assert checks['passed']


def test_prompts_state_the_order_and_the_bounded_rule():
    check_prompt = files('iris').joinpath(f"prompts/{PROMPT_VERSIONS['persona_check']}.md").read_text(encoding='utf-8')
    generate = files('iris').joinpath(f"prompts/{PROMPT_VERSIONS['persona_generate']}.md").read_text(encoding='utf-8')
    for field in ('frequency_words', 'source_instances', '偶尔＜有时＜经常＜通常／习惯＜总是', '有界的经历'):
        assert field in check_prompt
    assert '不要把有限次数写成“都／总是／每次”' in generate
