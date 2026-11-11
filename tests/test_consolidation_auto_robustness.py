"""#139 public run: reasons never block execution, ID coercion, bounded repairs, auto prompt, conflicts kept."""
import pytest

from iris.memory_ops import delete_memory
from test_consolidation_auto import (GOOD, T1, AutoModel, auto, change, conflict_of, dep_model, dependent, item_of, link,
                                     mem, merge_answer, message, mutual, pair_answer, row, run, subject, supersedes)

__all__ = ['change', 'mutual', 'dependent']

BAD = '记忆 999 与 4 是一回事，主体[nobody|谁]说过“材料里没有的话”'


def pair_steps(model):
    return model.steps().count('pair')


def sequence(*answers):
    """Answer each call with the next value, repeating the last."""
    state = {'n': 0}
    def answer(payload):
        value = answers[min(state['n'], len(answers)-1)]
        state['n'] += 1
        return value(payload) if callable(value) else value
    return answer


# ---- 1. a reason never blocks a checked structural conclusion ---------------------

def test_bad_reasons_do_not_block_a_mutual_merge(store, mutual):
    model = AutoModel(store, pair={**pair_answer(mutual), 'reason': BAD}, merge={'content': GOOD, 'reason': BAD})
    _, report = run(store, model)
    details = item_of(report, 'merged')['details']
    assert row(store, details['result_id'])['content'] == GOOD
    assert details['reason_source'] == 'generated' and details['archived_reason'] == BAD
    assert '999' not in details['report'] and f"记忆 {details['absorbed_ids'][0]}" in details['report']


def test_bad_reason_does_not_block_an_ordinary_merge(store):
    auto(store)
    m1, m2 = message(store, 1, '周三去游泳馆', at=T1), message(store, 2, '周三去游泳馆呀', at=T1)
    who = subject(store, m1)
    a, b = mem(store, '小林周三去游泳馆', [m1], who), mem(store, '小林周三去游泳馆。', [m2], who)
    _, report = run(store, AutoModel(store, pair=lambda p: {**merge_answer(p), 'reason': BAD}))
    details = item_of(report, 'merged')['details']
    assert details['reason_source'] == 'generated' and '999' not in details['report']


def test_bad_reason_does_not_block_a_dependency_decision(store, dependent):
    _, report = run(store, dep_model(store, [], reason=BAD))
    details = item_of(report, 'auto_dependency')['details']
    assert details['verdict'] == 'forget' and details['archived_reason'] == BAD and '999' not in details['report']
    assert row(store, dependent['target'])['lifecycle'] == 'forgotten'


def test_report_only_still_requires_a_grounded_reason(store, mutual):
    auto(store, 'report_only_v1')
    _, report = run(store, AutoModel(store, pair={**pair_answer(mutual), 'reason': BAD}))
    assert any(i['outcome'] == 'failed' for i in report['items'] if i['phase'] == 'consolidation')


# ---- 2. IDs written as strings, missing fields ------------------------------------

@pytest.mark.parametrize('evidence', [lambda c: [str(c['m1']), f"来源消息{c['m2']}"], lambda c: ['A-1', 'A-2'],
                                      lambda c: [f"消息 {c['m1']}", f"#{c['m2']}"]])
def test_strings_naming_exactly_one_visible_id_are_accepted(store, mutual, evidence):
    answer = {**pair_answer(mutual), 'evidence': evidence(mutual), 'keep_id': f"记忆{mutual['a']}"}
    model = AutoModel(store, pair=answer, merge={'content': GOOD, 'reason': '合并'})
    _, report = run(store, model)
    assert pair_steps(model) == 1 and item_of(report, 'merged')


def test_unmappable_id_gets_one_repair_and_is_never_guessed(store, mutual):
    answer = {**pair_answer(mutual), 'evidence': ['来源消息999']}
    model = AutoModel(store, pair=answer, merge={'content': GOOD, 'reason': '合并'})
    _, report = run(store, model)
    assert pair_steps(model) == 2
    failed = next(i for i in report['items'] if i['outcome'] == 'failed')
    assert failed['reason'] == 'unsafe_write' and failed['details']['rejection'] == 'invalid_evidence'
    assert row(store, mutual['a'])['merged_into'] is None and row(store, mutual['b'])['merged_into'] is None


def test_repaired_ids_are_checked_again_and_accepted(store, mutual):
    good = pair_answer(mutual)
    model = AutoModel(store, pair=sequence({**good, 'evidence': [999]}, good), merge={'content': GOOD, 'reason': '合并'})
    _, report = run(store, model)
    assert pair_steps(model) == 2 and item_of(report, 'merged')


def test_missing_keep_id_gets_one_repair(store, mutual):
    good = pair_answer(mutual)
    missing = {k: v for k, v in good.items() if k != 'keep_id'}
    model = AutoModel(store, pair=sequence(missing, good), merge={'content': GOOD, 'reason': '合并'})
    _, report = run(store, model)
    assert pair_steps(model) == 2 and item_of(report, 'merged')


def test_extra_field_gets_one_repair(store, mutual):
    good = pair_answer(mutual)
    model = AutoModel(store, pair=sequence({**good, 'preferred_keep_id': mutual['a']}, good), merge={'content': GOOD, 'reason': '合并'})
    _, report = run(store, model)
    assert pair_steps(model) == 2 and item_of(report, 'merged')


def test_relation_ids_as_strings(store, change):
    answer = supersedes(change, old_id=f"记忆{change['a']}", new_id=str(change['b']), evidence=[f"来源消息{change['m2']}"])
    run(store, AutoModel(store, pair=conflict_of([change['m1'], change['m2']]), conflict=answer))
    assert row(store, change['a'])['lifecycle'] == 'forgotten'


def test_dependency_ids_as_strings(store, dependent):
    _, report = run(store, dep_model(store, [f"记忆{dependent['support']}"]))
    assert item_of(report, 'auto_dependency')['details']['verdict'] == 'keep'


# ---- 3. auto_v1 uses its own pair prompt --------------------------------------------

@pytest.mark.parametrize('resolution,marker', [('auto_v1', '自动处理模式'), ('report_only_v1', '改为管理员建议')])
def test_pair_prompt_matches_the_resolution(store, mutual, resolution, marker):
    auto(store, resolution)
    model = AutoModel(store, pair=pair_answer(mutual), merge={'content': GOOD, 'reason': '合并'})
    run(store, model)
    assert marker in model.messages[0][0]['content']


# ---- 4. unique_details get one correction; conflicts are not swallowed ------------

def test_invalid_fragment_gets_one_correction(store, mutual):
    bad = {**pair_answer(mutual), 'unique_details': {'a': ['带着纸袋'], 'b': ['在北门等']}}
    model = AutoModel(store, pair=sequence(bad, pair_answer(mutual)), merge={'content': GOOD, 'reason': '合并'})
    _, report = run(store, model)
    assert pair_steps(model) == 2 and item_of(report, 'merged')
    note = model.messages[1][-1]['content']
    assert '带着纸袋' in note and '小林计划周三领钥匙，带纸袋' in note and '小林计划周三领钥匙，在北门等' in note


def test_fragment_still_invalid_after_correction_keeps_both(store, mutual):
    bad = {**pair_answer(mutual), 'unique_details': {'a': ['带着纸袋'], 'b': ['在北门等']}}
    model = AutoModel(store, pair=bad, merge={'content': GOOD, 'reason': '合并'})
    run(store, model)
    assert model.steps() == ['pair', 'pair']
    assert row(store, mutual['a'])['merged_into'] is None and row(store, mutual['b'])['merged_into'] is None


def test_conflict_with_invalid_fragments_still_reaches_relation(store, change):
    classification = {**conflict_of([change['m1'], change['m2']]), 'unique_details': {'a': ['正文里没有'], 'b': []}}
    model = AutoModel(store, pair=classification, conflict=supersedes(change))
    run(store, model)
    assert model.steps() == ['pair', 'conflict']
    assert row(store, change['a'])['lifecycle'] == 'forgotten'


def test_report_only_conflict_with_invalid_fragments_is_unchanged(store, change):
    auto(store, 'report_only_v1')
    classification = {**conflict_of([change['m1'], change['m2']]), 'unique_details': {'a': ['正文里没有'], 'b': []}}
    model = AutoModel(store, pair=classification)
    run(store, model)
    assert model.steps() == ['pair']


# ---- 5. a scope exclusion is recorded ------------------------------------------------

def test_visibility_exclusion_is_recorded(store, change):
    from conftest import msg
    from iris.memory_ops import set_entry_visibility
    from test_consolidation_auto import T3
    m3 = msg(store, 3, '私聊里说改成周四去游泳', entry='B', sender='小林', at=T3)
    c = mem(store, '小林这次改成周四去游泳', [m3], change['who'])
    set_entry_visibility(store, 'B', 'entry_only')
    _, report = run(store, AutoModel(store))
    item = next(i for i in report['items'] if i['item_key'] == 'skip:visibility_differs')
    assert item['reason'] == 'visibility_differs' and any(c in pair for pair in item['details']['pairs'])
