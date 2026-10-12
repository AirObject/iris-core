"""PO3: bound source context and resultative aspect, with permissive fake judges."""
import json

import pytest

from iris.db import dumps
from iris.memory_ops import edit_memory
from iris.persona import PersonaEngine, _check_verdicts, current_persona
from test_persona import add_self
from test_persona_deterministic_guards import approving_verdict
from test_persona_optimization import Model, prepared


def sentence(text, **changes):
    return {'text': text, 'origin': 'memory', 'initial_setting': False, 'date_count': 2,
            'basis': [{'memory_id': 1, 'revision': 1}], **changes}


def memory(*, mid=1, revision=1, kind='live', original='我也把这一页读完。',
           content='我在那次共读中读完了这一页。'):
    return {'memory_id': mid, 'revision': revision, 'entry_kind': kind, 'content': content,
            'excerpts': [{'excerpt': original}] if original is not None else []}


def problems(row, *memories):
    return _check_verdicts(approving_verdict([row]), [row],
                          material={'evidence': {'memories': list(memories)}})[2]


@pytest.mark.parametrize('original,allowed', [
    ('我也把这一页读完。', False),
    ('我先把这一页读完，再整理目录。', False),
    ('我整理好了目录，再把这一页读完。', False),
    ('我没有读完这一页。', False),
    ('我读完了这一页。', True),
    ('这一页我已经读完。', True),
    ('我把这一页读完了。', True),
    ('我读完这一页了。', True),
    ('我早已读完这一页。', True),
    ('我刚才读完这一页。', True),
])
def test_completion_aspect_needs_same_predicate_in_original_source(original, allowed):
    row = sentence('在那次共读中，我读完了这一页。')
    assert bool(problems(row, memory(original=original))) is not allowed


def test_completed_summary_cannot_override_intention_in_original():
    issues = problems(sentence('在那次整理中，我完成了目录编排。'),
                      memory(original='我也完成这份目录编排。', content='我完成了目录编排。'))
    assert 'completion aspect' in ' '.join(issues[1])


@pytest.mark.parametrize('other', [
    memory(mid=2, original='我读完了这一页。'),
    memory(revision=2, original='我读完了这一页。'),
])
def test_other_memory_or_revision_cannot_supply_completion_witness(other):
    assert problems(sentence('在那次共读中，我读完了这一页。'), memory(), other)


def test_source_less_memory_can_still_supply_its_own_explicit_completion():
    # No invented source requirement for existing manually recorded self memories.
    assert not problems(sentence('在那次共读中，我读完了这一页。'), memory(original=None))


@pytest.mark.parametrize('text,allowed', [
    ('在那次搭建中，我摆好了纸桥支架。', False),
    ('在那次游戏搭建中，我摆好了纸桥支架。', True),
    ('在那次联机搭建中，我摆好了纸桥支架。', True),
])
def test_game_only_evidence_requires_explicit_game_scope(text, allowed):
    assert bool(problems(sentence(text), memory(kind='game'))) is not allowed


def test_unbound_real_entry_cannot_dilute_game_context_requirement():
    assert problems(sentence('在那次搭建中，我摆好了纸桥支架。'),
                    memory(kind='game'), memory(mid=2, kind='group'))


def test_mixed_activity_evidence_is_left_to_semantic_check():
    row = sentence('在几次协作中，我选择先听同伴的方案。',
                   basis=[{'memory_id': 1, 'revision': 1}, {'memory_id': 2, 'revision': 1}])
    assert not problems(row, memory(kind='game'), memory(mid=2, kind='group'))


@pytest.mark.parametrize('change', [
    {'origin': 'admin'}, {'origin': 'initial_template'}, {'initial_setting': True},
])
def test_source_context_style_guards_do_not_rewrite_admin_or_settings(change):
    row = sentence('我完成了这里的建造。', **change)
    assert not problems(row, memory(kind='game'))


class Permissive(Model):
    def __init__(self, store, mid, text, repair):
        super().__init__(store, repair=repair)
        self.mid, self.text = mid, text

    def chat(self, messages, purpose, **kwargs):
        reply = super().chat(messages, purpose, **kwargs)
        payload = json.loads(messages[-1]['content'])
        if purpose == 'persona_generate':
            memories = payload['evidence']['memories']
            chosen = next(m for m in memories if m['memory_id'] == self.mid)
            good = next(m for m in memories if '解释了规则' in m['content'])
            reply.content = dumps({'sentences': [
                {'text': good['content'], 'basis': [good['ref']]},
                {'text': self.text, 'basis': [chosen['ref']]}]})
        elif purpose in ('persona_check', 'persona_recheck'):
            reply.content = dumps(approving_verdict(payload['candidate'], payload))
        return reply


def test_bound_source_guard_runs_again_on_exact_repair(prepared):
    mid = add_self(prepared, '在那次共读里，我也把这一页读完。')
    completed = '在那次共读里，我读完了这一页。'
    edit_memory(prepared, mid, 1, content=completed)
    model = Permissive(prepared, mid, '我在那次共读时读完了这一页。', completed)
    result = PersonaEngine(prepared, model).regenerate(expected_version=1)
    checks = result['version']['checks']
    assert checks['initial_model']['sentences'][-1]['supported']
    assert checks['recheck']['sentences'] == []
    assert checks['deleted_sentences'][0]['stage'] == 'repair_deterministic'
    assert 'completion aspect' in ' '.join(checks['deleted_sentences'][0]['reasons'])
    assert not checks['repaired_sentences'] and completed not in result['version']['content']
    assert model.text not in result['version']['content']
    # The degree-only check now sees the final text after early deletion.
    assert result['status'] == 'current' and result['version']['change_degree'] == 'small'


def test_game_scope_guard_overrides_permissive_initial_check(prepared):
    text = '在那次搭建中，我摆好了纸桥支架。'
    mid = add_self(prepared, text, kind='game')
    model = Permissive(prepared, mid, text, None)
    result = PersonaEngine(prepared, model).regenerate(expected_version=1)
    checks = result['version']['checks']
    assert checks['initial_model']['sentences'][-1]['supported']
    assert checks['deleted_sentences'] and text not in result['version']['content']
    assert 'game-only evidence' in ' '.join(checks['initial_model_errors'])
    assert '解释了规则' in result['version']['content']


def test_check_input_labels_all_sentences_and_recheck_has_local_indices(prepared):
    model = Model(prepared)
    PersonaEngine(prepared, model).regenerate(expected_version=1)
    checks = [p for purpose, p, _ in model.calls if purpose in ('persona_check', 'persona_recheck')]
    assert checks[0]['candidate'][0]['origin'] == 'initial_template'
    for payload in checks:
        assert [s.get('index') for s in payload['candidate']] == list(range(1, len(payload['candidate']) + 1))
    assert [s['index'] for s in checks[1]['candidate']] == [1]


def test_duplicate_output_indices_are_not_silently_renumbered(prepared):
    class Duplicates(Model):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            if purpose == 'persona_check':
                value = json.loads(reply.content)
                for row in value['sentences']:
                    row['index'] = 1
                reply.content = dumps(value)
            return reply
    result = PersonaEngine(prepared, Duplicates(prepared)).regenerate(expected_version=1)
    assert result['status'] == 'rejected' and current_persona(prepared)['id'] == 1


@pytest.mark.parametrize('original,allowed', [
    ('在现实里整理书房时，我核对了目录。', True),
    ('整理书架时，我核对了目录。', False),
])
def test_game_entry_does_not_override_an_explicit_real_world_source(original, allowed):
    row = sentence('在现实里整理书房时，我核对了目录。')
    assert bool(problems(row, memory(kind='game', original=original))) is not allowed


def test_still_invalid_extracted_repair_is_removed_before_model_recheck(prepared):
    original = '在那次共读里，我也把这一页读完。'
    completed = '在那次共读里，我读完了这一页。'
    mid = add_self(prepared, original)
    edit_memory(prepared, mid, 1, content=completed)
    model = Permissive(prepared, mid, '我在那次共读时读完了这一页。', completed)
    result = PersonaEngine(prepared, model).regenerate(expected_version=1)
    rechecks = [request for purpose, request, _ in model.calls if purpose == 'persona_recheck']
    assert len(rechecks) == 1 and rechecks[0]['candidate'] == []
    assert rechecks[0]['task'] == 'degree_only'
    assert completed not in rechecks[0]['candidate_text']
    deleted = result['version']['checks']['deleted_sentences']
    assert deleted[0]['stage'] == 'repair_deterministic'
    assert deleted[0]['replacement'] == completed
    assert 'completion aspect' in ' '.join(deleted[0]['reasons'])
