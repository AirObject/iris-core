"""Resultative aspect belongs to its predicate, not an earlier modifier/focus."""
import pytest

from iris.memory_ops import edit_memory
from iris.persona import PersonaEngine, _validate_clause_checks
from test_persona import add_self
from test_persona_bounded_context import fixture
from test_persona_evidence_guards import Permissive, memory, problems, sentence
from test_persona_optimization import prepared


@pytest.mark.parametrize('text', [
    '在那次共读中，我用大家都能听清的速度读完了目录。',
    '在那次共读中，我用大家都能够听清的速度读完了目录。',
    '在那次共读中，我把可以朗读的目录读完了。',
    '在那次共读中，我把不喜欢的目录读完了。',
    '在那次共读中，我把希望保留的目录读完了。',
    '在那次共读中，我把准备重排的目录读完了。',
    '在那次共读中，我把假装破旧的目录读完了。',
    '在那次共读中，我把装作旧版的目录读完了。',
    '在那次共读中，我把没有标号的目录读完。',
    '在那次共读中，我把偏好的目录读完。',
    '在那次共读中，我把能够听清的音频听完了。',
    '在那次共读中，我用可以擦掉的笔写完了目录。',
])
def test_attributive_modality_does_not_exempt_a_separate_result(text):
    issues = problems(sentence(text), memory(original='我来读完目录；我先听完音频；我来写完目录。',
                                            content=text))
    assert 'completion aspect' in ' '.join(issues.get(1, []))


@pytest.mark.parametrize('text', [
    '在那次共读中，我用大家都能听清的速度读完了目录。',
    '在那次共读中，我把可以朗读的目录读完了。',
    '在那次共读中，我把不喜欢的目录读完了。',
    '在那次共读中，我用可以擦掉的笔写完了目录。',
])
def test_original_completed_result_survives_an_attributive_modifier(text):
    assert not problems(sentence(text), memory(original=text, content=text))


@pytest.mark.parametrize('text', [
    '在那次共读中，我能先读完目录。',
    '在那次共读中，我能够把目录读完。',
    '在那次共读中，我可以先把目录读完。',
    '在那次共读中，我更喜欢先读完目录。',
    '在那次共读中，我不愿意把目录读完。',
    '在那次共读中，我没有读完目录。',
    '在那次共读中，我希望把目录读完。',
    '在那次共读中，我觉得承认困难比假装读完更舒服。',
    '在那次共读中，我更看重解释过程而非一口气做完。',
    '在那次共读中，我提到篇名《读完了目录》。',
])
def test_locally_governed_non_result_claims_remain_for_semantic_check(text):
    assert not problems(sentence(text), memory(original=text, content=text))


@pytest.mark.parametrize('other_location', ['same_memory', 'other_memory'])
@pytest.mark.parametrize('select_outcome', [False, True])
def test_each_resultative_focus_needs_its_selected_completion_source(other_location, select_outcome):
    candidate = '在那次直播中，我用大家都能听清的速度读完了目录。'
    intention = '我用大家都能听清的速度来读完目录。'
    outcome = '我用大家都能听清的速度读完了目录。'
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
    parts = output['sentences'][0]['clauses']
    parts[0]['evidence'] = [{'ref': ref, 'source_index': source_index, 'quote': outcome}]
    if select_outcome:
        parts[-1]['evidence'] = [{'ref': ref, 'source_index': source_index, 'quote': outcome}]
    errors, found = _validate_clause_checks(output, payload)
    assert not errors
    if select_outcome:
        assert not found
    else:
        assert 'completion aspect' in ' '.join(found.get(1, []))


def test_permissive_checks_and_completed_summary_cannot_supply_result(prepared):
    original = '在那次共读中，我用大家都能听清的速度来读完目录。'
    completed = '在那次共读中，我用大家都能听清的速度读完了目录。'
    mid = add_self(prepared, original)
    edit_memory(prepared, mid, 1, content=completed)
    model = Permissive(prepared, mid, completed, completed)
    result = PersonaEngine(prepared, model).regenerate(expected_version=1)
    version = result['version']
    assert all(row['supported'] for batch in version['checks']['initial_model']['batches']
               for row in batch['sentences'])
    assert completed not in version['content']
    assert not version['checks']['repaired_sentences']
    assert any('completion aspect' in ' '.join(item['reasons'])
               for item in version['checks']['deleted_sentences'])
    assert all(not payload['candidate'] for purpose, payload, _ in model.calls
               if purpose == 'persona_recheck')
    assert result['status'] == 'current'
