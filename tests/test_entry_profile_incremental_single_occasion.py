"""EF-16: recurrence belongs to every topic detail, not just its broad subject."""
import json

import pytest

from iris import entry_profile as ep
from iris.db import dumps
from iris.memory_ops import edit_memory, purge_memory
from iris.queue import estimate_tokens
from test_entry_profile_incremental import (
    NOW, Gateway, accept, add, fixed_projection_clock, profiles, projection, propose, run, source,
)
from test_entry_profile_incremental_quality import seed, check_seed


def hints(payload):
    return {x['term']: x for x in payload['memory']['topic_detail_review']}


def single_occasion(payload):
    return {'decision': 'reject', 'violations': ['单条消息泛化'],
            'reason': '主题重复出现，但支架加软垫只在一次讨论中出现；可重新提出“讨论望远镜支架的选用”。',
            'safe_sources': []}


def test_entity_occurrence_counts_all_discussion_links_before_filtering(store):
    mid, ids = seed(store, [
        (1, '甲', '求一份苏州望远镜支架的图纸，有没有？'),
        (4, '甲', '之前问的苏州望远镜支架的图纸还有吗？'),
        (7, '乙', '重新比较望远镜支架的钢材和铝材。'),
    ], body='讨论苏州的望远镜支架。')
    g = check_seed(store, Gateway(store, checks=single_occasion))
    assert len(g.calls) == 1
    term = hints(g.calls[0]['payload'])['苏州']
    assert term['independent_uses'] == term['all_evidence_uses'] == 1
    assert term['message_ids'] == [ids[0]]
    assert not projection(store)


def test_repeated_entity_in_two_independent_days_is_preserved(store):
    mid, ids = seed(store, [
        (1, '甲', '在苏州比较望远镜支架的铝材和钢材。'),
        (4, '乙', '苏州的望远镜支架用木材比钢材轻。'),
    ], body='苏州的望远镜支架选材。')
    def check(p):
        item = hints(p)['苏州']
        assert item['independent_uses'] == item['all_evidence_uses'] == 2
        assert item['message_ids'] == ids
        return accept(p)
    check_seed(store, Gateway(store, checks=check))
    assert projection(store)['memory_ids'] == [mid]
    assert profiles(store)[0]['importance'] == 50


def test_same_day_and_numeric_substrings_do_not_inflate_detail_support(store):
    seed(store, [
        (1, '甲', '比较35毫米镜头的成像。'),
        (1, '乙', '35毫米镜头的边缘清晰度如何？'),
        (4, '丙', '135毫米镜头的透视表现可以再谈谈。'),
    ], body='讨论35毫米镜头的成像。')
    g = check_seed(store, Gateway(store, checks=single_occasion))
    assert hints(g.calls[0]['payload'])['35']['all_evidence_uses'] == 1
    assert not projection(store)


def test_quoted_detail_does_not_count_as_a_second_use(store):
    mid, ids = seed(store, [
        (1, '甲', '在苏州讨论望远镜支架的钢材。'),
        (4, '乙', '比较木质望远镜支架的重量。'),
        (7, '丙', '重新比较望远镜支架的携带重量。'),
    ], body='苏州的望远镜支架。')
    with store.write() as c:
        author = c.execute('SELECT sender_subject_id FROM messages WHERE id=?', (ids[0],)).fetchone()[0]
        c.execute('UPDATE messages SET quote_author_subject_id=?,quote_content=? WHERE id=?',
                  (author, '在苏州讨论望远镜支架的钢材。', ids[1]))
    g = check_seed(store, Gateway(store, checks=single_occasion))
    assert hints(g.calls[0]['payload'])['苏州']['all_evidence_uses'] == 1


def test_shared_specific_detail_witnesses_are_kept_in_each_bounded_page(store):
    # The first source on each day is not the only possible witness. Select
    # two actual entity witnesses without counting their same-day peers twice.
    seed(store, [
        (1, '甲', '望远镜支架的材料怎么选择？'),
        (1, '乙', '我在苏州比较了木材和钢材做的望远镜支架。'),
        (4, '丙', '望远镜支架还可以讨论携带重量。'),
        (4, '丁', '苏州的望远镜支架展品中铝材很轻。'),
        (7, '戊', '望远镜支架的木材需要防潮。'),
    ], body='苏州的望远镜支架选材。')
    def check(p):
        assert hints(p)['苏州']['independent_uses'] == 2
        assert len(p['memory']['sources']) <= 6
        assert estimate_tokens(dumps(p['memory'])) <= ep.IncrementalProfileLimits().evidence_tokens
        return accept(p)
    check_seed(store, Gateway(store, checks=check))
    assert projection(store)


def test_generic_single_occasion_detail_rejection_is_sent_to_next_observation(store):
    mid, _ = seed(store, [
        (1, '甲', '望远镜支架晃动，我垫了软垫才稳。'),
        (4, '乙', '望远镜支架换成木材后重量下降。'),
    ], body='讨论望远镜支架的选用，并用软垫避免晃动。')
    def check(p):
        # Lexical hints deliberately do not classify ordinary nouns as entities.
        assert '软垫' not in hints(p)
        return single_occasion(p)
    check_seed(store, Gateway(store, checks=check))
    assert not projection(store)
    assert profiles(store)[0]['body'].endswith('并用软垫避免晃动。')
    add(store, '望远镜支架换用铝材能否减轻重量？', 8, sender='丙')
    def observe(p):
        target = next(x for x in p['profiles'] if x['memory_id'] == mid)
        feedback = target['admission_feedback']
        assert feedback['violations'] == ['单条消息泛化']
        assert '软垫' in feedback['reason']
        assert 'sources' not in target
        assert len(feedback['reason']) <= 240
        return {'observations': [{'op': 'change', 'memory_id': mid, 'action': 'revise',
            'category': 'topic', 'body': '讨论望远镜支架的选用。', 'saying': '', 'meaning': '',
            'definition_refs': [], 'source_refs': [source(p['messages'][0])]}]}
    g = Gateway(store, observations=observe)
    run(store, g)
    assert projection(store)['memory_ids'] == [mid]
    assert profiles(store)[0]['body'] == '讨论望远镜支架的选用。'
    assert sum('admit' in x['purpose'] for x in g.calls) == 1


def test_repeated_nonentity_point_can_be_admitted_without_literal_entity_hints(store):
    mid, _ = seed(store, [
        (1, '甲', '望远镜支架换材时，木材轻但怕潮。'),
        (4, '乙', '比较望远镜支架的材料，钢材沉但更耐潮。'),
    ], body='讨论望远镜支架材料在重量和防潮上的取舍。')
    def check(p):
        assert not {'重量', '防潮'} & hints(p).keys()
        return accept(p)
    check_seed(store, Gateway(store, checks=check))
    assert projection(store)['memory_ids'] == [mid]


def test_rejected_feedback_is_bounded_and_disappears_after_manual_edit(store):
    mid, _ = seed(store, [(1, '甲', '望远镜支架需软垫。'), (4, '乙', '望远镜支架可换材。')],
                  body='讨论望远镜支架。')
    vote = {'decision': 'reject', 'violations': ['单条消息泛化'],
            'reason': '只出现一次。' * 100, 'safe_sources': []}
    check_seed(store, Gateway(store, checks=lambda p: vote))
    with store.read() as c:
        directory, _ = ep._inc_directory(c, 'group', [], ep.IncrementalProfileLimits())
    assert len(directory[0]['admission_feedback']['reason']) == 240
    revision = profiles(store)[0]['revision']
    edit_memory(store, mid, revision, content='讨论望远镜支架的材质。')
    with store.read() as c:
        directory, _ = ep._inc_directory(c, 'group', [], ep.IncrementalProfileLimits())
    assert 'admission_feedback' not in directory[0]


def test_purge_removes_feedback_from_frozen_observation_payloads(store):
    mid, _ = seed(store, [(1, '甲', '望远镜支架需软垫。'), (4, '乙', '望远镜支架可换材。')],
                  body='讨论望远镜支架。')
    check_seed(store, Gateway(store, checks=single_occasion))
    add(store, '继续谈器材重量。', 8, sender='丙')
    task = ep.IncrementalProfileTask(store, Gateway(store), clock=lambda: NOW)
    wid = task._freeze_chunk('group')
    with store.read() as c:
        payload = json.loads(c.execute('SELECT payload_json FROM entry_profile_incremental_work WHERE id=?', (wid,)).fetchone()[0])
    assert 'admission_feedback' in payload['profiles'][0]
    purge_memory(store, mid, profiles(store)[0]['revision'], confirm=True)
    with store.read() as c:
        work = c.execute('SELECT * FROM entry_profile_incremental_work WHERE id=?', (wid,)).fetchone()
    assert work['state'] == 'cancelled'
    assert '软垫' not in work['payload_json']


def test_non_topics_do_not_acquire_recurrence_detail_requirement(store):
    add(store, '公告：本群只允许在苏州的公共场所线下见面，此规范长期有效。')
    g = Gateway(store, lambda p: propose(p, category='rule', body='只在苏州公共场所线下见面。'))
    run(store, g)
    assert projection(store)
    assert 'topic_detail_review' not in g.calls[-1]['payload']['memory']


def test_new_prompts_load_v4_and_explicitly_check_every_detail():
    prompts = ep.incremental_prompts()
    assert [prompts[x]['name'] for x in ('observe', 'admit')] == [
        'entry_profile_observe_v4', 'entry_profile_admit_v4']
    assert '单次细节' in prompts['observe']['text']
    assert '每个细节' in prompts['admit']['text']
    assert 'admission_feedback' in prompts['observe']['text']
    assert 'topic_detail_review' in prompts['admit']['text']

def test_nonmatching_bridge_cannot_split_an_entity_discussion_on_a_page(store):
    mid, ids = seed(store, [
        (1, '甲', '苏州的望远镜支架图纸谁有？'),
        (3, '乙', '望远镜支架图纸我先查找一下。'),
        (5, '丙', '苏州那份我找到了。'),
        (7, '丁', '重新比较望远镜支架的钢材重量。'),
    ], body='苏州的望远镜支架。')
    with store.write() as c:
        for parent, child in ((ids[0], ids[1]), (ids[1], ids[2])):
            original = c.execute('SELECT sender_subject_id,content FROM messages WHERE id=?', (parent,)).fetchone()
            c.execute('UPDATE messages SET quote_author_subject_id=?,quote_content=? WHERE id=?', (*original, child))
    with store.read() as c:
        row = c.execute('SELECT * FROM memories WHERE id=?', (mid,)).fetchone()
        sources = ep._inc_all_sources(c, row, NOW.isoformat())
    page = [s for s in sources if s['message']['position'] != ids[1]]
    item = next(x for x in ep._inc_topic_details('苏州的望远镜支架。', sources, page) if x['term'] == '苏州')
    assert item['independent_uses'] == item['all_evidence_uses'] == 1
    assert item['message_ids'] == [ids[0]]


def test_repeated_details_cannot_expand_the_single_request_evidence_limit(store):
    seed(store, [
        (1, '甲', '讨论AX1的边缘修整。'), (2, '乙', 'AX1也可重新打磨边缘。'),
        (3, '丙', '讨论BX2的接缝修整。'), (4, '丁', 'BX2的接缝可压平。'),
        (5, '戊', '讨论CX3的底部修整。'), (6, '己', 'CX3底部能加垫层。'),
        (7, '庚', '讨论DX4的表面修整。'), (8, '辛', 'DX4可抛光表面。'),
    ], body='讨论AX1、BX2、CX3、DX4的修整。')
    g = check_seed(store)
    assert not g.calls and not projection(store)
    assert profiles(store)[0]['admission_reason'] == 'evidence_budget_exceeded'
