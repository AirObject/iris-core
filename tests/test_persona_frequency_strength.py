"""PE-23: a frequency or universal word may not rise above what its own source asserts."""
import json
from importlib.resources import files

import pytest

from iris.db import dumps
from iris.memory_ops import edit_memory
from iris.models import ModelReply
from iris.persona import (FREQUENCY_STRENGTH, PROMPT_VERSIONS, PersonaEngine, _asserted_frequency, _check_payload,
                          _focus_quantifiers, _generation_material, _settings,
                          _validate_clause_checks, current_persona)
from test_persona import FakeGateway, add_self, store


FIRST, SECOND = '共读那天对方读错时我先问了要不要重读', '第二次共读对方又读错了，我也先问了，以后也想这样'
UNIVERSAL = '在两次共读里，对方读错时我都先问要不要重读。'
UNIVERSAL_FOCUS = '对方读错时我都先问要不要重读。'
BOUNDED = '在两次共读里，那两次我都先问了要不要重读。'
BOUNDED_FOCUS = '那两次我都先问了要不要重读。'
HABIT_SOURCE = '合奏排练时我每次都先听节拍'
HABIT = '合奏排练时我每次都先听节拍。'
CURRENT = '对方读错时我先问要不要重读，以后也想这样'


def check(word='都', scope='', source='', instances=2, **fields):
    return {'candidate_quantifier':word, 'candidate_scope':scope, 'source_quantifier':source,
            'source_scopes':[], 'source_instances':instances, 'preserved':True, **fields}


def verdict(text, focus, checks, *, excerpts=(FIRST, SECOND), current=CURRENT, final=False):
    sources = [{'excerpt':excerpt, 'date':f'2026-11-{10 + 9 * i}', 'speaker_subject_id':'self'}
               for i, excerpt in enumerate(excerpts)]
    memory = {'current_revision_text':current, 'excerpts':sources} if sources else {'content':current, 'excerpts':[]}
    sentence = {'index':1, 'origin':'memory', 'initial_setting':False, 'text':text, 'evidence_refs':['M1'],
                'scope_check_required':False, 'clauses':[{'index':1, 'text':focus}]}
    evidence = ([{'ref':'M1', 'source_index':i, 'quote':source['excerpt']} for i, source in enumerate(sources, 1)]
                or [{'ref':'M1', 'source_index':0, 'quote':current}])
    part = {'index':1, 'supported':True, 'scene_qualified':not sources, 'reason':'两日原话都支持。',
            'evidence':evidence, 'quantifier_checks':checks}
    return _validate_clause_checks({'sentences':[{'index':1, 'clauses':[part]}]},
                                   {'candidate':[sentence], 'evidence_by_ref':{'M1':memory}}, final=final)


def problems_of(*args, **kwargs):
    errors, problems = verdict(*args, **kwargs)
    assert not errors
    return ' '.join(problems.get(1, []))


def habit(candidate, source, scope='合奏排练时', **fields):
    text, excerpt = f'合奏排练时我{candidate}先听节拍。', f'合奏排练时我{source}先听节拍'
    return verdict(text, text, [check(candidate, scope, fields.pop('quoted', source),
                                      source_scopes=[{'ref':'M1', 'quote':'合奏排练时'}], **fields)],
                   excerpts=(excerpt, excerpt), current=excerpt)


@pytest.mark.parametrize('text,expected', [
    ('对方读错时我都先问', ['都']), ('我都会先问', ['都']), ('我经常先听', ['经常']), ('有时候我会先问', ['有时候']),
    ('我习惯先听节拍', ['习惯']), ('我一直先听节拍', ['一直']), ('我从来没有抢话', ['从来没有']),
    ('每次都先听', ['每次']), ('所有的曲子都听过', ['所有']), ('我偶尔先听', ['偶尔']),
    ('我每天都练琴', ['每天']), ('我天天练琴', ['天天']), ('我每晚写日记', ['每晚']), ('我每周都去', ['每周']),
    ('我每月都去', ['每月']), ('我每年都去', ['每年']), ('我全都问了', ['全都']),
    ('第一次合奏后我都先听节拍', ['第一次', '都']), ('唯一的变化是我都先问', ['唯一', '都']),
    ('歌我都喜欢', ['都']), ('在共读和合奏里我都先问', ['都']), ('大家都说我都先问', ['都'])])
def test_frequency_words_join_the_closed_quantifier_class(text, expected):
    assert _focus_quantifiers(text) == expected


@pytest.mark.parametrize('text', ['不总是先听', '并不是每次都先听', '不是所有事情都这样', '我不经常先听', '《总是有人》很好听',
                                  '我先听了节拍', '用大家都能听清的速度', '我们都觉得好',
                                  '那三本书我都读完了', '两门课都过了', '两个人都来了', '这几首歌我都听过', '我和对方都觉得好',
                                  '我常去那家店', '我总熬夜', '我一般先听节拍'])
def test_negated_distributive_and_ambiguous_uses_are_not_frequency_claims(text):
    assert not _asserted_frequency(text)


def test_strength_order_is_closed_and_ranked():
    assert [FREQUENCY_STRENGTH[word] for word in ('偶尔', '有时', '经常', '通常', '习惯', '总是', '都', '每次', '从不', '每天')] \
        == [1, 2, 3, 4, 4, 5, 5, 5, 5, 5]
    assert not {'好像', '愿意', '想'} & set(FREQUENCY_STRENGTH)
    assert set(FREQUENCY_STRENGTH.values()) == {1, 2, 3, 4, 5}
    assert not {'常', '总', '一般'} & set(FREQUENCY_STRENGTH)


def test_source_and_candidate_share_one_reading():
    assert _asserted_frequency('合奏排练时我每次都先听节拍') == {'每次':5}
    assert _asserted_frequency('我不经常先听') == {} and _asserted_frequency('我们都在群里') == {}


def test_universal_supported_by_a_source_frequency_word_passes():
    assert habit('每次', '每次都', quoted='每次') == ([], {})


@pytest.mark.parametrize('candidate,source', [('经常', '经常'), ('有时', '经常'), ('偶尔', '总是')])
def test_equal_or_weaker_frequency_passes(candidate, source):
    assert habit(candidate, source) == ([], {})


@pytest.mark.parametrize('candidate,source', [('总是', '有时'), ('经常', '偶尔'), ('通常', '经常')])
def test_stronger_word_than_the_source_is_rejected(candidate, source):
    errors, problems = habit(candidate, source)
    assert not errors and 'frequency strength raised' in ' '.join(problems[1])


@pytest.mark.parametrize('candidate,source,quoted', [
    ('经常', '不经常', '经常'), ('每次', '不是每次都', '每次'), ('总是', '不总是', '总是'), ('每次', '从没每次都', '每次')])
def test_a_negated_source_word_is_no_frequency_evidence(candidate, source, quoted):
    errors, problems = habit(candidate, source, quoted=quoted)
    assert not errors and 'frequency raised above the source' in ' '.join(problems[1])


def test_a_distributive_source_dou_is_no_frequency_evidence():
    excerpt = '我们都在共读群里，对方读错时我先问了要不要重读'
    joined = problems_of(UNIVERSAL, UNIVERSAL_FOCUS, [check('都', '', '都', 1)], excerpts=(excerpt, excerpt), current=excerpt)
    assert 'frequency raised above the source' in joined


def test_a_few_instances_cannot_become_a_universal():
    assert 'frequency raised above the source' in problems_of(UNIVERSAL, UNIVERSAL_FOCUS, [check('都', '对方读错时')])


def test_universal_with_an_auxiliary_is_still_a_frequency_claim():
    text = '在两次共读里，对方读错时我都会先问要不要重读。'
    joined = problems_of(text, '对方读错时我都会先问要不要重读。', [check('都', '')])
    assert 'frequency raised above the source' in joined


def test_bounded_experience_of_those_instances_passes():
    assert verdict(BOUNDED, BOUNDED_FOCUS, [check('都', '那两次')]) == ([], {})


@pytest.mark.parametrize('scope,focus', [
    ('那次之后', '那次之后我每次都先问要不要重读。'), ('那两次之后', '那两次之后我都先问要不要重读。'),
    ('那天', '从那天起我总是先问要不要重读。'), ('三天两头', '我三天两头都先问要不要重读。'),
    ('两次', '我两次都先问了要不要重读。')])
def test_only_a_counted_range_before_the_word_bounds_it(scope, focus):
    word = next(w for w in ('每次', '总是', '都') if w in focus)
    joined = problems_of('在两次共读里，' + focus, focus, [check(word, scope, '', 2)])
    assert 'frequency raised above the source' in joined


def test_count_after_the_frequency_word_does_not_bound_it():
    focus = '我都先问了要不要重读那两次。'
    assert 'frequency raised above the source' in problems_of('在两次共读里，' + focus, focus, [check('都', '那两次')])


def test_bounded_experience_cannot_count_more_occasions_than_the_source():
    focus = BOUNDED_FOCUS.replace('两', '三')
    assert 'more occasions' in problems_of(BOUNDED.replace('那两次', '那三次'), focus, [check('都', '那三次')])
    assert 'more occasions' in problems_of(BOUNDED, BOUNDED_FOCUS, [check('都', '那两次', instances=1)])


@pytest.mark.parametrize('quoted', ['想这样', '不经常', '每次都', '我每次都先听节拍偶尔也先看谱'])
def test_source_word_outside_the_closed_list_is_asked_again_then_no_evidence(quoted):
    errors, _ = verdict(UNIVERSAL, UNIVERSAL_FOCUS, [check('都', '对方读错时', quoted)])
    assert any('one frequency word' in error for error in errors)
    errors, problems = verdict(UNIVERSAL, UNIVERSAL_FOCUS, [check('都', '对方读错时', quoted)], final=True)
    assert not errors and 'frequency raised above the source' in ' '.join(problems[1])


def test_one_clause_with_two_frequency_words_supports_only_the_quoted_one():
    excerpt = '我每次都先听节拍偶尔也先看谱'
    text = '合奏排练时我总是先看谱。'
    def run(quoted, final=False):
        return verdict(text, text, [check('总是', '', quoted, 2)], excerpts=(excerpt, excerpt), current=excerpt,
                       final=final)
    assert 'frequency strength raised' in ' '.join(run('偶尔')[1][1])
    assert run(excerpt)[0] and 'frequency raised above the source' in ' '.join(run(excerpt, True)[1][1])


def test_current_text_cannot_supply_a_frequency_the_utterances_lack():
    current = '对方读错时我总是先问要不要重读'
    text = '在两次共读里，对方读错时我总是先问要不要重读。'
    joined = problems_of(text, '对方读错时我总是先问要不要重读。', [check('总是', '', '总是')], current=current)
    assert 'frequency raised above the source' in joined


def test_memory_without_utterances_can_state_its_frequency():
    text = '在那次合奏排练时，我每次都先听节拍。'
    assert verdict(text, '我每次都先听节拍。', [check('每次', '', '每次', 1)], excerpts=(),
                   current='在那次合奏排练时我每次都先听节拍') == ([], {})


@pytest.mark.parametrize('text,focus,source', [
    ('那次共读里我好像有点急，对方读错时我都先问要不要重读。', UNIVERSAL_FOCUS, ''),
    ('在两次共读里，对方读错时我大概都先问要不要重读。', '对方读错时我大概都先问要不要重读。', ''),
    ('那次共读里我好像有点急，对方读错时我都先问要不要重读。', UNIVERSAL_FOCUS, '好像')])
def test_a_hedge_cannot_stand_in_for_the_frequency_check(text, focus, source):
    excerpts = (FIRST + '，我好像有点急', SECOND)
    errors, problems = verdict(text, focus, [check('都', '对方读错时' if '大概' not in focus else '', source)],
                               excerpts=excerpts, final=True)
    assert not errors and 'frequency raised above the source' in ' '.join(problems[1])


@pytest.mark.parametrize('mutation', ['missing', 'bool', 'negative', 'text'])
def test_frequency_item_needs_its_instance_count(mutation):
    value = check('都', '那两次')
    if mutation == 'missing': value.pop('source_instances')
    elif mutation == 'bool': value['source_instances'] = True
    elif mutation == 'negative': value['source_instances'] = -1
    else: value['source_instances'] = '2'
    errors, _ = verdict(BOUNDED, BOUNDED_FOCUS, [value])
    assert any('invalid quantifier scope verdict' in error for error in errors)


def test_instance_count_on_a_comparison_quantifier_is_ignored():
    text, excerpt = '那次合奏时我第一次先听节拍。', '那次合奏时我第一次先听节拍'
    value = {'candidate_quantifier':'第一次', 'candidate_scope':'', 'source_quantifier':'第一次', 'source_scopes':[],
             'preserved':True}
    for extra in ({}, {'source_instances':'任意'}):
        assert verdict(text, text, [{**value, **extra}], excerpts=(excerpt, excerpt), current=excerpt) == ([], {})
    errors, _ = verdict(text, text, [{**value, 'note':'多余'}], excerpts=(excerpt, excerpt), current=excerpt)
    assert any('invalid quantifier scope verdict' in error for error in errors)


def test_item_for_a_distributive_dou_binds_nothing_but_its_own_rejection():
    text, excerpt = '那三本书我都读完了。', '那三本书我读完了'
    assert verdict(text, text, [check('都', '那三本书', '', 1)], excerpts=(excerpt, excerpt), current=excerpt) == ([], {})
    joined = problems_of(text, text, [check('都', preserved=False)], excerpts=(excerpt, excerpt), current=excerpt)
    assert 'quantifier scope not preserved' in joined


def test_preserved_false_rejects_the_focus():
    assert 'quantifier scope not preserved' in problems_of(UNIVERSAL, UNIVERSAL_FOCUS, [check('都', preserved=False)])


def test_payload_names_frequency_words_within_quantifiers(store):
    from test_persona_source_payload import prepare
    _, material, _, sentence = prepare(store)
    rows = _check_payload(material, [{**sentence, 'text':'那次配音里，我每次都先请求再试一次，经常先听。'}],
                          full_content='')['candidate']
    assert rows[0]['clauses'][1] == {'index':2, 'text':'我每次都先请求再试一次', 'quantifiers':['每次'],
                                     'frequency_words':['每次']}
    assert rows[0]['clauses'][2] == {'index':3, 'text':'经常先听。', 'quantifiers':['经常'], 'frequency_words':['经常']}


class FrequencyGateway(FakeGateway):
    """Fills the frequency items with `quoted` as the source word (instances only by default)."""
    quoted = ''

    def chat(self, messages, purpose, **kwargs):
        reply = super().chat(messages, purpose, **kwargs)
        request = self.calls[-1][1]
        if purpose not in ('persona_check', 'persona_recheck') or request.get('task') != 'check_facts_and_rules':
            return reply
        output = json.loads(reply.content)
        for row, sentence in zip(output['sentences'], request['candidate']):
            for part, focus in zip(row['clauses'], sentence['clauses']):
                if focus.get('frequency_words'):
                    part['quantifier_checks'] = [check(word, '', self.quoted, 1) for word in focus['quantifiers']]
        return ModelReply(dumps(output), 'stop', {})


def add_utterance(store, memory_id, content, stamp='2026-10-05T12:00:00+00:00'):
    # A second dated utterance of the same memory, as learning would attach it.
    with store.write() as conn:
        message = conn.execute("""INSERT INTO messages(entry_id,kind,sender_subject_id,content,occurred_at,received_at,
            dedupe_key) VALUES('live','self_output','self',?,?,?,?)""", (content, stamp, stamp, content + stamp)).lastrowid
        conn.execute("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,?)",
                     (memory_id, message, stamp))


def regenerate(store, candidate, memory, *, summary=None, second=None, **fields):
    mid = add_self(store, memory)
    if second is not None:
        add_utterance(store, mid, second)
    if summary is not None:
        edit_memory(store, mid, 1, content=summary)
    previous = current_persona(store)
    with store.read() as conn:
        material = _generation_material(conn, _settings(conn), previous, '2026-11-12T12:00:00+00:00')
    ref = next(m['ref'] for m in material['evidence']['memories'] if m['memory_id'] == mid)
    gateway = FrequencyGateway(store, {'sentences':[{'text':candidate, 'basis':[ref]}]})
    for key, value in fields.items():
        setattr(gateway, key, value)
    return PersonaEngine(store, gateway).regenerate(expected_version=previous['id'])


def test_universal_from_one_instance_is_repaired_to_the_bounded_memory(store):
    memory = '那次共读里，对方读错时我先问了要不要重读。'
    result = regenerate(store, '那次共读里，对方读错时我都会先问要不要重读。', memory)
    checks = result['version']['checks']
    assert any('frequency raised above the source' in error for error in checks['initial_model_errors'])
    assert result['version']['content'].endswith(memory) and checks['passed']


def test_repair_from_a_universal_summary_faces_the_same_frequency_check(store):
    # The summary says 每次, the utterance does not: the replacement is rechecked and dropped.
    utterance = '那次共读里，对方读错时我先问了要不要重读。'
    summary = '那次共读里，对方读错时我每次都先问要不要重读。'
    result = regenerate(store, '那次共读里，对方读错时我都会先问要不要重读。', utterance, summary=summary,
                        second='第二次共读对方又读错了，我也先问了', quoted='每次')
    checks = result['version']['checks']
    assert summary not in result['version']['content']
    assert any(item['stage'] == 'recheck' and item.get('replacement') == summary for item in checks['deleted_sentences'])


def test_prompts_state_the_order_and_the_bounded_rule():
    check_prompt = files('iris').joinpath(f"prompts/{PROMPT_VERSIONS['persona_check']}.md").read_text(encoding='utf-8')
    generate = files('iris').joinpath(f"prompts/{PROMPT_VERSIONS['persona_generate']}.md").read_text(encoding='utf-8')
    for field in ('frequency_words', 'source_instances', '偶尔＜有时＜经常＜通常／习惯＜总是', '有界的经历',
                  '只有“都”', '自从'):
        assert field in check_prompt
    assert '不要把有限次数写成“都／总是／每次”' in generate


# Round 4: 都 is a frequency claim except in three closed forms; only 都 can be bounded.

NAMES = ('小舒', '南柚')


@pytest.mark.parametrize('text', [
    '这些天我都先问', '那些日子我都先问', '每个晚上我都写日记', '每个周末我都去爬山', '各场直播我都先问',
    '我跟以前一样都先问', '这阵子我都早睡', '这些歌我都听过', '每个人我都问了', '晚上和周末都先问',
    '我在家和学校都先问', '直播和私聊都先问', '接龙和私聊里我都先问', '我跟他聊天时都先问', '跟对方说话都先问',
    '和新朋友都先问', '跟她都先问', '平时和周末都练琴', '这几天的作业我都写完了', '那几次的录音我都听了',
    '那些天的照片我都存了', '什么时候我都愿意', '我跟人吵架都先道歉', '一点都不难', '什么都想试试',
    '每个人都来了', '任何人都可以问', '这些天对方读错时我都先问'])
def test_any_other_dou_is_a_frequency_or_universal_claim(text):
    assert _focus_quantifiers(text, NAMES) == ['都']


@pytest.mark.parametrize('text', [
    '我们都觉得好', '他们俩都来了', '大家都在', '我们也都先问', '各位都辛苦了', '两个人都来了', '几个人都来了',
    '那三本书我都读完了', '那两门课都过了', '这几首歌我都听过', '两样东西我都喜欢', '这几个人我都问过',
    '我和对方都先问', '我和她都喜欢共读', '我和小舒都喜欢共读', '我跟南柚都先问', '对小舒和南柚都先问',
    '我和小舒也都先问'])
def test_three_closed_forms_are_distributive(text):
    assert _focus_quantifiers(text, NAMES) == []


def test_coordination_needs_known_names_or_pronouns():
    assert _focus_quantifiers('我和小舒都喜欢共读', NAMES) == []
    assert _focus_quantifiers('我和小舒都喜欢共读') == ['都']
    assert _focus_quantifiers('共读和合奏我都喜欢', NAMES) == ['都']


def test_counted_universal_and_its_dou_are_counted_once():
    text = '每次排练我都先听节拍。'
    assert _focus_quantifiers(text) == ['每次']
    excerpt = '排练时我每次都先听节拍'
    assert verdict(text, text, [check('每次', '', '每次', 2)], excerpts=(excerpt, excerpt), current=excerpt) == ([], {})
    assert _focus_quantifiers('第一次排练我都先听节拍') == ['第一次', '都']
    assert _focus_quantifiers('不是所有事情都这样') == []


@pytest.mark.parametrize('focus,word,scope', [
    ('那次我才意识到自己总是太急。', '总是', '那次'), ('那天我才发现自己一直太急。', '一直', '那天'),
    ('这次让我明白我每次都太急。', '每次', '这次'), ('自从那次共读我每次都先问。', '每次', '那次'),
    ('那次共读过后我每次都先问。', '每次', '那次'), ('那次往后我都先问要不要重读。', '都', '那次'),
    ('从那次起我都先问要不要重读。', '都', '那次'), ('那两次读错后我都先问要不要重读。', '都', '那两次'),
    ('那两次读错我都先问了要不要重读。', '都', '那两次'), ('那两天我都先问了要不要重读。', '都', '那两天'),
    ('我两次都先问了要不要重读。', '都', '两次'), ('那次共读里我一直耐心解释。', '一直', '那次共读里')])
def test_only_a_count_right_before_dou_bounds_it(focus, word, scope):
    excerpt = '那次共读我先问了要不要重读'
    joined = problems_of(focus, focus, [check(word, scope, '', 2)], excerpts=(excerpt, excerpt), current=excerpt)
    assert 'frequency raised above the source' in joined


@pytest.mark.parametrize('focus,scope,instances', [
    ('那两次我都先问了对方。', '那两次', 2), ('那次我也都先问了要不要重读。', '那次', 1), ('这几回我都先问了。', '这几回', 2)])
def test_a_count_right_before_dou_bounds_it(focus, scope, instances):
    excerpt = '那次共读我先问了要不要重读'
    assert verdict(focus, focus, [check('都', scope, '', instances)], excerpts=(excerpt, excerpt),
                   current=excerpt) == ([], {})
    assert 'more occasions' in problems_of(focus, focus, [check('都', scope, '', instances - 1)],
                                           excerpts=(excerpt, excerpt), current=excerpt)


def test_a_bad_source_word_reask_keeps_the_other_items_adverse_verdicts():
    excerpt = '合奏排练时我有时先听节拍，偶尔先看谱'
    text = '合奏排练时我总是先听节拍，经常先看谱。'
    focus = '合奏排练时我总是先听节拍经常先看谱。'
    checks = [check('总是', '', '有时', 2), check('经常', '', '偶尔先看谱', 2)]
    errors, problems = verdict(focus, focus, checks, excerpts=('合奏排练时我有时先听节拍偶尔先看谱',) * 2,
                               current=excerpt)
    assert any('one frequency word' in error for error in errors)
    assert 'frequency strength raised' in ' '.join(problems[1])


# Round 5: a counted group needs two or more; each 都 bounded on its own; a source 都 is no evidence.

@pytest.mark.parametrize('focus', ['每遇到一位新朋友我都先问要不要重读。', '每见一个人我都先问要不要重读。',
                                   '遇到一个人我都先问要不要重读。', '每读完一本书我都写笔记。',
                                   '每拿到一张票我都很开心。', '一部分时候我都先问要不要重读。',
                                   '每读完两本书我都写笔记。'])
def test_one_item_or_an_each_clause_is_no_counted_group(focus):
    assert _focus_quantifiers(focus) == ['都']
    assert 'frequency raised above the source' in problems_of(focus, focus, [check('都', '', '', 2)])


@pytest.mark.parametrize('text', ['那三本书我都读完了', '两个人都来了', '两门课都过了', '这几首歌我都听过', '那十二本书我都读了'])
def test_two_or_more_counted_people_or_things_stay_distributive(text):
    assert _focus_quantifiers(text) == []


def test_a_count_bounds_only_the_dou_right_after_it():
    focus, excerpt = '这次我都明白了自己从来都太急。', '那次共读我发现自己有点急'
    joined = problems_of(focus, focus, [check('都', '这次', '', 1)], excerpts=(excerpt, excerpt), current=excerpt)
    assert 'frequency raised above the source' in joined
    focus = '这次我都明白了。'
    assert verdict(focus, focus, [check('都', '这次', '', 1)], excerpts=(excerpt, excerpt), current=excerpt) == ([], {})


@pytest.mark.parametrize('excerpt,word', [('这些歌我都听过', '都'), ('每个人我都问了', '都'), ('对方读错时我都先问了', '都'),
                                          ('这些歌我都听过', '总是')])
def test_a_bare_source_dou_supports_no_frequency(excerpt, word):
    focus = f'对方读错时我{word}先问要不要重读。'
    joined = problems_of(focus, focus, [check(word, '', '都', 2)], excerpts=(excerpt, excerpt), current=excerpt)
    assert 'frequency raised above the source' in joined


def test_an_explicit_source_word_still_supports_dou():
    excerpt = '对方读错时我每次都先问了'
    focus = '对方读错时我都先问要不要重读。'
    assert verdict(focus, focus, [check('都', '', '每次', 2)], excerpts=(excerpt, excerpt), current=excerpt) == ([], {})
