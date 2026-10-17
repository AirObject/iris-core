"""EF-21: retained excerpts are output; selection must preserve rule clauses."""
import json

import pytest

from iris import entry_profile as ep
from iris.db import dumps
from test_entry_profile_incremental import NOW, Gateway, accept, fixed_projection_clock, profiles, projection
from test_entry_profile_incremental_quality import seed, check_seed
from test_entry_profile_evidence_selection import frozen


def test_prefer_stable_bound_sources_over_occasion_details(store):
    mid, ids = seed(store, [
        (1, '甲', '昨晚气温12度，望远镜支架的木材受潮了。'),
        (3, '乙', '讨论望远镜支架的材料与重量。'),
        (5, '丙', '望远镜支架选材要比较重量和耐用性。'),
        (7, '丁', '这次望远镜支架运到后裂了，维修花了280元。'),
    ])
    g = check_seed(store)
    assert [s['message_id'] for s in g.calls[0]['payload']['memory']['sources']] == ids[1:3]
    row, = profiles(store)
    assert {s['message']['position'] for s in row['sources']} == set(ids[1:3])
    assert {s['message']['position'] for s in row['pending_sources']} == {ids[0], ids[3]}
    assert projection(store)['memory_ids'] == [mid]


@pytest.mark.parametrize('risky', [
    '2026年10月1日气温12度，比较望远镜支架的材料。',
    '这次望远镜支架维修花了280元，比较材料。',
    '我遇到望远镜支架断裂，后来换了材料。',
])
def test_excerpt_risk_is_a_preference_not_a_new_admission_rule(store, risky):
    mid, ids = seed(store, [(1, '甲', risky), (5, '乙', '比较望远镜支架的重量。')])
    task, work = frozen(store, mid)
    assert work is not None  # Necessary evidence still goes to semantic review.
    payload = json.loads(work['payload_json'])
    assert {s['message_id'] for s in payload['memory']['sources']} == set(ids)
    assert payload['memory']['sources'][0]['excerpt_hints']
    assert payload['memory']['sources'][1]['excerpt_hints'] == []


def test_stable_number_in_definition_is_not_an_occasion_penalty():
    assert ep._inc_excerpt_hints('小阶梯指四个相邻音上行再下行。') == []
    assert ep._inc_excerpt_hints('接口固定为12毫米。') == []
    assert ep._inc_excerpt_hints('维修价格涨到280元。') == ['transient_quantity']


def test_stable_preference_does_not_drop_specific_point_or_create_a_second_group(store):
    mid, ids = seed(store, [
        (1, '甲', '昨晚在苏州比较望远镜支架的材料。'),
        (4, '乙', '今天在苏州讨论望远镜支架的选材。'),
        (6, '丙', '比较望远镜支架的材料和重量。'),
        (8, '丁', '讨论望远镜支架的携带重量。'),
    ], body='苏州的望远镜支架选材。')
    _, work = frozen(store, mid)
    p = json.loads(work['payload_json'])
    assert {s['message_id'] for s in p['memory']['sources']} == set(ids[:2])
    assert all(v['independent_uses'] == 2 for v in p['memory']['topic_detail_review'])


def test_clean_followup_cannot_replace_required_independent_discussion(store):
    seed(store, [(1, '甲', '昨晚望远镜支架坏了，怎么选材，求建议？'),
                 (5, '甲', '望远镜支架按建议处理了，回来报告结果。')])
    g = check_seed(store)
    assert not g.calls and not projection(store)
    assert profiles(store)[0]['independent_uses'] == 1


def test_rule_selection_keeps_complete_announcement_not_just_question_and_agreement(store):
    mid, ids = seed(store, [
        (1, '甲', '大家能直接转载作者的照片吗？'),
        (2, '乙', '发布图片须先征得作者同意，不转载未经授权的照片。'),
        (3, '丙', '明白，转发前我会先征得许可。'),
    ], category='rule', body='发布图片须先征得作者同意，不转载未经授权的照片。')
    g = check_seed(store)
    assert ids[1] in [s['message_id'] for s in g.calls[0]['payload']['memory']['sources']]
    assert projection(store)['memory_ids'] == [mid]


def test_rule_selection_keeps_each_non_entity_clause(store):
    mid, ids = seed(store, [
        (1, '甲', '这里可以发布录音吗？'),
        (2, '乙', '以后发布录音须标注作者。'),
        (3, '乙', '作品不得转售，这条要求一直有效。'),
        (4, '丙', '好，我同意发布录音前先检查。'),
    ], category='rule', body='发布录音须标注作者，作品不得转售。')
    _, work = frozen(store, mid)
    assert {ids[1], ids[2]} <= {s['message_id'] for s in json.loads(work['payload_json'])['memory']['sources']}


def test_rule_announcement_kept_even_with_effective_date(store):
    mid, ids = seed(store, [
        (1, '甲', '能发布录音吗？'),
        (2, '乙', '从今天起，发布录音须标注作者，此要求长期有效。'),
        (3, '丙', '好的，我会检查。'),
    ], category='rule', body='发布录音须标注作者。')
    _, work = frozen(store, mid)
    assert ids[1] in [s['message_id'] for s in json.loads(work['payload_json'])['memory']['sources']]


def test_shortened_contiguous_excerpts_are_the_only_approved_sources(store):
    left = '比较望远镜支架的材料。'
    right = '望远镜支架选材要比较重量。'
    mid, ids = seed(store, [(1, '甲', '昨晚气温12度。'+left), (5, '乙', right+'这次维修花了280元。')])
    def shorten(p):
        v = accept(p)
        for s in v['safe_sources']:
            s['excerpt'] = left if s['message_id'] == ids[0] else right
        return v
    check_seed(store, Gateway(store, checks=shorten))
    row, = profiles(store)
    assert {s['excerpt'] for s in row['sources']} == {left, right}
    assert len(row['pending_sources']) == 2
    assert row['independent_days'] == 2 and projection(store)['memory_ids'] == [mid]


@pytest.mark.parametrize('replacement', ['比较望远镜材料', '比较望远镜支架的重量。'])
def test_shortening_cannot_splice_or_change_original_words(store, replacement):
    seed(store, [(1, '甲', '比较望远镜支架的材料。昨晚下雨了。'), (5, '乙', '望远镜支架材料可以比较。')])
    def bad(p):
        v = accept(p);v['safe_sources'][0]['excerpt'] = replacement;return v
    check_seed(store, Gateway(store, checks=bad))
    assert not projection(store)


def test_single_long_term_announcement_needs_no_role_metadata_or_second_approval(store):
    mid, ids = seed(store, [(1, '甲', '这里以后发布录音须标注作者，长期按这个要求执行。')],
                    category='rule', body='发布录音须标注作者。')
    def check(p):
        assert len(p['memory']['sources']) == 1
        assert '长期按这个要求执行' in p['memory']['sources'][0]['context']
        return accept(p)
    check_seed(store, Gateway(store, checks=check))
    assert projection(store)['memory_ids'] == [mid]


def test_queued_old_prompt_result_is_rechecked_before_retaining_excerpts(store):
    mid, _ = seed(store, [(1, '甲', '比较望远镜支架的材料。'), (5, '乙', '望远镜支架选材要比较重量。')])
    task, work = frozen(store, mid)
    p = json.loads(work['payload_json'])
    p['_snapshot']['admission_prompt_sha256'] = 'old-prompt-fingerprint'
    with store.write() as c:
        c.execute('UPDATE entry_profile_incremental_work SET work_key=?,payload_json=?,parsed_json=? WHERE id=?',
            ('old-evidence-policy', dumps(p), dumps(accept(p)), work['id']))
    task._run_one(work)
    with store.read() as c:
        state = c.execute('SELECT state,reason FROM entry_profile_incremental_work WHERE id=?',(work['id'],)).fetchone()
    assert tuple(state) == ('cancelled', 'stale_check')
    assert not task.gateway.calls and not projection(store)
    task.run('group')
    assert len(task.gateway.calls) == 1
    assert projection(store)['memory_ids'] == [mid]
