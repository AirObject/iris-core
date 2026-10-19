"""PE: counted completions and negative intentions cannot inherit summary aspect."""
import pytest

from iris.memory_ops import edit_memory
from iris.persona import PersonaEngine
from test_persona import add_self
from test_persona_evidence_guards import Permissive, memory, problems, sentence
from test_persona_optimization import prepared


@pytest.mark.parametrize('original,allowed', [
    ('我从这一处重新核对。', False),
    ('我来把这页重新核对一遍。', False),
    ('我标好了页码，再重新核对一遍。', False),
    ('我没有重新核对过一次。', False),
    ('我没重新核对过一次。', False),
    ('如果我重新核对了一遍，就会标注结果。', False),
    ('我重新核对了一遍。', True),
    ('我已经重新核对一遍。', True),
    ('我刚才把这一页重新核对一遍。', True),
    ('这一页我核对过一次。', True),
    ('我重新核对了一遍，随后提出再核对一次。', True),
])
def test_counted_completion_needs_same_action_aspect_in_original(original, allowed):
    row = sentence('在那次复核中，我重新核对了一遍。')
    source = memory(original=original, content=row['text'])
    found = problems(row, source)
    assert bool(found) is not allowed
    if not allowed:
        assert 'counted completion' in ' '.join(found[1])


@pytest.mark.parametrize('text,original', [
    ('在那次复核中，我扫描过两次。', '我准备扫描两次。'),
    ('在那次复核中，我重新画了一张。', '我先画这一张。'),
    ('在那次复核中，我重录了一段。', '我会从这里重录一段。'),
])
def test_counted_completion_is_not_specific_to_one_verb(text, original):
    row = sentence(text)
    assert problems(row, memory(original=original, content=text))


@pytest.mark.parametrize('original,allowed', [
    ('我先不替对方补结局。', False),
    ('我暂不替对方补结局。', False),
    ('我不打算替对方补结局。', False),
    ('我计划不替对方补结局。', False),
    ('我先不替你补结局。', False),
    ('我先不替对方补结局，已经标清讲述人了。', False),
    ('我没有改题目，先不替对方补结局。', False),
    ('我先不补结局；没有替对方改题目。', False),
    ('我没有替对方补结局。', True),
    ('我先不替对方补结局，最后也没有补结局。', True),
    ('我先不替对方补结局，后来没有再补结局。', True),
])
def test_negative_intention_is_not_a_negative_event_result(original, allowed):
    row = sentence('在那次接龙中，我没有替对方补结局。')
    found = problems(row, memory(original=original, content=row['text']))
    assert bool(found) is not allowed
    if not allowed:
        assert 'negative intention' in ' '.join(found[1])


@pytest.mark.parametrize('text,original', [
    ('在那次整理中，我没有再发链接。', '我暂不再发链接。'),
    ('在那次观察中，我没移动标尺。', '我先不移动标尺。'),
    ('在那次复核中，我未改页码。', '我打算不改页码。'),
])
def test_negative_result_guard_is_not_specific_to_one_action(text, original):
    assert problems(sentence(text), memory(original=original, content=text))


@pytest.mark.parametrize('other', [
    memory(mid=2, original='我没有替对方补结局。'),
    memory(revision=2, original='我没有替对方补结局。'),
])
def test_unbound_memory_or_revision_cannot_supply_negative_result(other):
    row = sentence('在那次接龙中，我没有替对方补结局。')
    source = memory(original='我先不替对方补结局。', content=row['text'])
    assert problems(row, source, other)


@pytest.mark.parametrize('text', [
    '在那次复核中，我重新核对了一遍。',
    '在那次接龙中，我没有替对方补结局。',
])
def test_source_less_memory_keeps_its_own_aspect(text):
    assert not problems(sentence(text), memory(original=None, content=text))


@pytest.mark.parametrize('change', [
    {'origin': 'admin'},
    {'origin': 'initial_template'},
    {'initial_setting': True},
])
def test_action_aspect_guards_do_not_rewrite_settings_or_manual(change):
    row = sentence('在那次接龙中，我没有替对方补结局，也重新核对了一遍。', **change)
    assert not problems(row, memory(original='我先不替对方补结局；我来重新核对一遍。',
                                   content=row['text']))


@pytest.mark.parametrize('text,original', [
    ('在那次复核中，我提出如果重新核对了一遍，就标注结果。',
     '我提出如果重新核对了一遍，就标注结果。'),
    ('在那次接龙中，我说明先不替对方补结局。',
     '我先不替对方补结局。'),
    ('在那次共读中，我提到篇名《没有补结局》。',
     '这一篇的名字是《没有补结局》。'),
])
def test_non_result_claims_remain_for_semantic_judging(text, original):
    assert not problems(sentence(text), memory(original=original, content=text))


@pytest.mark.parametrize('original,completed,candidate,issue', [
    ('在那次复核中，我从这一处重新核对。',
     '在那次复核中，我重新核对了一遍。',
     '在那次复核中，我重新核对了一遍，也承认记录有误。',
     'counted completion'),
    ('在那次接龙中，我先不替对方补结局。',
     '在那次接龙中，我没有替对方补结局。',
     '在那次接龙中，我没有替对方补结局，也问过对方想不想继续。',
     'negative intention'),
])
def test_completed_memory_repair_cannot_override_original_intention(
        prepared, original, completed, candidate, issue):
    mid = add_self(prepared, original)
    edit_memory(prepared, mid, 1, content=completed)
    model = Permissive(prepared, mid, candidate, completed)
    result = PersonaEngine(prepared, model).regenerate(expected_version=1)
    version = result['version']
    assert all(row['supported'] for batch in version['checks']['initial_model']['batches']
               for row in batch['sentences'])
    assert completed not in version['content'] and candidate not in version['content']
    assert not version['checks']['repaired_sentences']
    rejected = version['checks']['repair_options_rejected']
    assert any(item['option']['text'] == completed and
               issue in ' '.join(item['reasons']) for item in rejected)
    assert all(not payload['candidate'] for purpose, payload, _ in model.calls
               if purpose == 'persona_recheck')
    assert result['status'] == 'current'


@pytest.mark.parametrize('candidate,intention,outcome,issue', [
    ('在那次直播中，我重新核对了一遍。', '我来重新核对一遍。',
     '我重新核对了一遍。', 'counted completion'),
    ('在那次直播中，我没有替对方补结局。', '我先不替对方补结局。',
     '我没有替对方补结局。', 'negative intention'),
])
@pytest.mark.parametrize('other_location', ['same_memory', 'other_memory'])
@pytest.mark.parametrize('select_outcome', [False, True])
def test_each_focus_needs_aspect_from_its_selected_source(
        candidate, intention, outcome, issue, other_location, select_outcome):
    from test_persona_bounded_context import fixture
    from iris.persona import _validate_clause_checks

    payload, output = fixture(candidate, source=intention)
    first = payload['evidence_by_ref']['M1']
    first['content'] = candidate
    actual = {'excerpt': outcome, 'date': '2028-02-10', 'entry_kind': 'live'}
    if other_location == 'same_memory':
        first['excerpts'].append(actual)
        ref, source_index = 'M1', 2
    else:
        payload['evidence_by_ref']['M2'] = {
            'content': candidate, 'entry_kind': 'live', 'excerpts': [actual]}
        payload['candidate'][0]['evidence_refs'].append('M2')
        ref, source_index = 'M2', 1
    # The scene focus can quote the second event. Its aspect must not lend a
    # completed action to the action focus that still quotes the first event.
    parts = output['sentences'][0]['clauses']
    parts[0]['evidence'] = [{'ref': ref, 'source_index': source_index, 'quote': outcome}]
    if select_outcome:
        parts[-1]['evidence'] = [{'ref': ref, 'source_index': source_index, 'quote': outcome}]
    errors, problems = _validate_clause_checks(output, payload)
    assert not errors
    if select_outcome:
        assert not problems
    else:
        assert issue in ' '.join(problems.get(1, []))
