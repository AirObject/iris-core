"""PO2: conservative mechanical boundaries, including an overly permissive judge."""
from persona_fakes import add_clause_proofs
import json

import pytest

from iris.db import dumps
from iris.memory_ops import edit_memory, setup_role
from iris.models import ModelReply
from iris.persona import PersonaEngine, _check_verdicts, admin_edit, current_persona
from test_persona import add_self
from test_persona_optimization import Model, prepared


def approving_verdict(sentences, payload=None):
    result = {'sentences': [{'index': i, 'supported': True, 'fabricated': False,
        'scene_qualified': True, 'violations': [], 'reason': '假检查器全部放行。'}
        for i, _ in enumerate(sentences, 1)], 'change_degree': 'small', 'reason': '假检查器低估变化。'}
    return add_clause_proofs(result, payload) if payload is not None else result


@pytest.mark.parametrize('text', [
    '我通常先核对目录。', '我一向先核对目录。', '我经常先核对目录。',
    '我总是先核对目录。', '我向来先核对目录。', '我惯常先核对目录。',
    '我习惯于先核对目录。', '我每次都会先核对目录。', '我往往先核对目录。',
    '遇到记录不齐时，我先核对目录。', '在遇到记录不齐时，我先核对目录。',
    '只要有人问起，我就先核对目录。', '每当有人问起，我就先核对目录。',
    '如果有人问起，我会先核对目录。', '一旦有人问起，我就先核对目录。',
])
@pytest.mark.parametrize('date_count', [0, 1])
def test_single_date_generic_language_overrides_scene_qualified_true(text, date_count):
    sentence = {'text': text, 'origin': 'memory', 'initial_setting': False, 'date_count': date_count}
    _, errors, problems = _check_verdicts(approving_verdict([sentence]), [sentence])
    assert not errors and 1 in problems
    assert 'single-date' in ' '.join(problems[1])


@pytest.mark.parametrize('text', [
    '在那次直播中，我先核对了目录。', '这次遇到记录不齐时，我先核对了目录。',
    '遇到那一次记录不齐时，我先核对了目录。', '在十月一日的整理中，我更倾向先核对目录。',
    '我在同一天的两次演练里都先核对了目录。',
])
def test_bounded_experience_is_not_confused_with_habit(text):
    sentence = {'text': text, 'origin': 'memory', 'initial_setting': False, 'date_count': 1}
    assert not _check_verdicts(approving_verdict([sentence]), [sentence])[2]


@pytest.mark.parametrize('change', [
    {'date_count': 2}, {'origin': 'admin'}, {'origin': 'initial_template'}, {'initial_setting': True},
])
def test_generic_language_guard_does_not_replace_semantic_judgment_or_edit_admin(change):
    sentence = {'text': '我通常先核对目录。', 'origin': 'memory', 'initial_setting': False,
                'date_count': 1, **change}
    assert not _check_verdicts(approving_verdict([sentence]), [sentence])[2]


@pytest.mark.parametrize('text', ['我等你说完。', '我等您说完。', '我听了你们的讲解。', '我记下妳的选择。'])
def test_automatic_second_person_cannot_leave_an_unbound_reader(text):
    sentence = {'text': text, 'origin': 'memory', 'initial_setting': False, 'date_count': 2}
    assert 1 in _check_verdicts(approving_verdict([sentence]), [sentence])[2]
    for origin in ('admin', 'initial_template'):
        assert not _check_verdicts(approving_verdict([sentence]), [{**sentence, 'origin': origin}])[2]


@pytest.mark.parametrize('replacement', [
    '我在十月一日的直播中向小岚展示了浅灰底。',  # New person.
    '我在十月二日的直播中选择了浅灰底。',       # Changed time.
    '我在十月一日的直播中选择了三块浅灰底。',   # Added quantity.
    '我在十月一日的直播中解释了规则。',         # Other problem/candidate's evidence.
    '我在直播中选择了浅灰底。',                 # Unrestricted word deletion.
    '我在十月一日的直播中选用了浅灰底。',       # Semantic paraphrase still needs later validation.
])
def test_repair_cannot_rewrite_or_borrow_details_even_when_recheck_would_approve(prepared, replacement):
    model = Model(prepared, repair=replacement)
    result = PersonaEngine(prepared, model).regenerate(expected_version=1)
    checks = result['version']['checks']
    assert checks['passed'] and not checks['repaired_sentences']
    deleted = checks['deleted_sentences']
    assert len(deleted) == 1 and deleted[0]['replacement'] == replacement
    assert deleted[0]['stage'] == 'repair_extract'
    assert 'complete bound memory' in ' '.join(deleted[0]['reasons'])
    assert model.calls[-1][1]['candidate'] == []
    assert '解释了规则' in result['version']['content']


def test_repair_options_are_complete_own_memory_and_keep_only_its_basis(prepared):
    other = add_self(prepared, '我在十月二日的直播里读了目录。', stamp='2026-10-02T12:00:00+00:00', importance=70)
    class SeveralReferences(Model):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            if purpose == 'persona_generate':
                value = json.loads(reply.content)
                memories = json.loads(messages[-1]['content'])['evidence']['memories']
                value['sentences'][-1]['basis'] = [m['ref'] for m in memories if m['memory_id'] in (5, other)]
                reply.content = dumps(value)
            return reply
    model = SeveralReferences(prepared, repair='我在十月一日的直播中选择了浅灰底。')
    result = PersonaEngine(prepared, model).regenerate(expected_version=1)
    request = next(r for purpose,r,*_ in model.calls if purpose=='persona_sentence_repair')
    refs = request['problems'][0]['allowed_replacement_refs']
    options = request['replacement_text_by_ref']
    assert {options[ref] for ref in refs} == {
        '我在十月二日的直播里读了目录。', '我在十月一日的直播中选择了浅灰底。'}
    changed = result['version']['checks']['repaired_sentences'][0]
    assert len(changed['before']['basis']) == 2 and changed['before']['date_count'] == 2
    assert changed['after']['basis'] == [{'memory_id': 5, 'revision': 1}]
    assert changed['after']['dates'] == ['2026-10-01']
    assert model.calls[-1][1]['candidate'][0]['evidence_refs'] == [refs[0]]
    assert model.calls[-1][1]['candidate'][0]['date_count'] == 1


@pytest.mark.parametrize('memory, replacement, retained', [
    ('我没有在十月一日的直播中选择浅灰底。', '我在十月一日的直播中选择浅灰底。', False),
    ('我在十月一日的直播中选择了浅灰底。只是临时试用，并非偏好。',
     '我在十月一日的直播中选择了浅灰底。', False),
    ('我在十月一日的直播中选择了浅灰底', '我在十月一日的直播中选择了浅灰底。', True),
])
def test_repair_preserves_negation_whole_context_and_only_allows_terminal_period(prepared, memory, replacement, retained):
    edit_memory(prepared, 5, 1, content=memory)
    model = Model(prepared, repair=replacement)
    result = PersonaEngine(prepared, model).regenerate(expected_version=1)
    checks = result['version']['checks']
    assert checks['passed'] and bool(checks['repaired_sentences']) is retained
    assert bool(checks['deleted_sentences']) is (not retained)


def test_exact_repair_cannot_bypass_single_date_guard(prepared):
    edit_memory(prepared, 5, 1, content='我通常喜欢浅灰底。')
    model = Model(prepared, repair='我通常喜欢浅灰底。')
    result = PersonaEngine(prepared, model).regenerate(expected_version=1)
    checks = result['version']['checks']
    # Exact extraction is not proof; the known scope failure is removed before recheck.
    assert checks['recheck']['sentences'] == []
    assert checks['deleted_sentences'][0]['stage'] == 'repair_options'
    assert not checks['repaired_sentences'] and '通常' not in result['version']['content']
    assert result['version']['change_degree'] == 'small'


def test_single_date_condition_is_removed_even_when_model_approves_everything(prepared):
    class Permissive(Model):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            if purpose == 'persona_generate':
                value = json.loads(reply.content)
                value['sentences'][-1]['text'] = '遇到画面太亮时，我选择浅灰底。'
                reply.content = dumps(value)
            elif purpose in ('persona_check', 'persona_recheck'):
                reply.content = dumps(approving_verdict(json.loads(messages[-1]['content'])['candidate'], json.loads(messages[-1]['content'])))
            return reply
    model = Permissive(prepared, repair=None)
    result = PersonaEngine(prepared, model).regenerate(expected_version=1)
    checks = result['version']['checks']
    assert all(row['supported'] for batch in checks['initial_model']['batches'] for row in batch['sentences'])
    assert checks['initial_model_sentence_indices'] == [1, 2]
    assert checks['precheck_rejections'][0]['index'] == 3
    assert all('遇到' not in row['text'] for purpose,payload,_ in model.calls
               if purpose == 'persona_check' for row in payload['candidate'])
    assert checks['passed'] and len(checks['deleted_sentences']) == 1
    assert '遇到' not in result['version']['content']
    assert any('single-date' in e for e in checks['initial_model_errors'])


def test_admin_text_and_setup_remain_exact_under_new_style_guards(store):
    setup_role(store, 'Iris', '你们称我为目录管理员。')
    initial = current_persona(store)
    manual = '我通常等你们说完。'
    edited = admin_edit(store, initial['content'] + manual, expected_version=initial['id'])
    class Preserve:
        def chat(self, messages, purpose, **kwargs):
            assert not store._writer.in_transaction
            payload = json.loads(messages[-1]['content'])
            if purpose == 'persona_generate':
                result = {'sentences': [{'text': text, 'basis': [], 'admin_sentence': aid}
                    for aid, text in payload['admin_sentences'].items()]}
            else:
                result = approving_verdict(payload['candidate'], payload)
            return ModelReply(dumps(result), 'stop', {})
    result = PersonaEngine(store, Preserve()).regenerate(expected_version=edited['id'])
    assert result['status'] == 'pending'  # Product default remains all_manual.
    assert '初始设定：你们称我为目录管理员。' in result['version']['content']
    assert manual in result['version']['content']
    assert not result['version']['checks']['deleted_sentences']
    assert not result['version']['checks']['admin_content_removed_or_changed']


def test_repair_request_deduplicates_repeated_complete_replacement_text(prepared):
    class RepeatedProblems(Model):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            if purpose == 'persona_generate':
                value = json.loads(reply.content)
                value['sentences'] = [value['sentences'][-1], value['sentences'][-1]]
                reply.content = dumps(value)
            return reply
    model = RepeatedProblems(prepared)
    result = PersonaEngine(prepared, model).regenerate(expected_version=1)
    request = next(r for purpose,r,*_ in model.calls if purpose=='persona_sentence_repair')
    assert len(request['problems']) == 2
    assert len(request['replacement_text_by_ref']) == 1
    assert all(len(p['allowed_replacement_refs']) == 1 for p in request['problems'])
    assert '我在十月一日的直播中选择了浅灰底。' not in dumps(request['problems'])
    assert len(result['version']['checks']['repaired_sentences']) == 2
