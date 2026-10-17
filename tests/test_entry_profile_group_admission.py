"""EF-20: semantic group receipts, bound meaning, and evidence/display separation."""
import copy
import json

import pytest

from iris import entry_profile as ep
from iris.db import dumps
from test_entry_profile_incremental import NOW, Gateway, accept, fixed_projection_clock, profiles, projection
from test_entry_profile_incremental_quality import seed, check_seed


def reviewed(payload):
    vote = accept(payload)
    vote['group_reviews'] = [dict(group_id=g['group_id'], focus='支架的材料取舍。', same_topic=True,
        independent=g['eligible'], reason='该组独立发起相同的讨论维度。')
        for g in payload['memory'].get('discussion_groups', [])]
    vote['meaning_support'] = []
    return vote


@pytest.mark.parametrize('followup', [
    '我按大家的建议做了，望远镜支架现在已经稳了，谢谢。',
    '前几天求助的望远镜支架，我照着建议处理了，回来报告结果。',
    '望远镜支架的问题解决了，回来反馈一下。',
])
def test_requesters_result_report_is_not_a_second_discussion(store, followup):
    seed(store, [(1, '甲', '望远镜支架怎么选材，求建议？'), (6, '甲', followup)])
    g = check_seed(store)
    assert profiles(store)[0]['independent_uses'] == 1
    assert not g.calls and not projection(store)


@pytest.mark.parametrize('text', [
    '上回讲的支架选材，我回去试了，确实稳了。',
    '关于前几天那次讨论，回来反馈一下望远镜支架的结果。',
])
def test_explicit_reference_to_prior_discussion_cannot_be_a_new_group(store, text):
    seed(store, [(1, '甲', '望远镜支架可比较钢材和木材。'), (6, '乙', text)])
    g = check_seed(store)
    assert profiles(store)[0]['independent_uses'] == 1
    assert not g.calls and not projection(store)


def test_same_author_can_start_a_new_discussion_without_retrospective_report(store):
    mid, _ = seed(store, [(1, '甲', '望远镜支架怎么选材？'),
        (6, '甲', '新做一副望远镜支架，铝材和钢材的重量如何取舍？')])
    check_seed(store, Gateway(store, checks=reviewed))
    assert projection(store)['memory_ids'] == [mid]


def test_followup_does_not_hide_a_later_genuinely_independent_discussion(store):
    mid, ids = seed(store, [(1, '甲', '望远镜支架怎么选材，求建议？'),
        (4, '甲', '我按建议处理了望远镜支架，回来报告结果。'),
        (8, '乙', '新的望远镜支架想比较木材和铝材。')])
    def check(p):
        groups = p['memory']['discussion_groups']
        assert len([g for g in groups if g['eligible']]) == 2
        assert ids[1] not in [g['group_id'] for g in groups if g['eligible']]
        return reviewed(p)
    check_seed(store, Gateway(store, checks=check))
    assert projection(store)['memory_ids'] == [mid]


def test_different_topic_group_cannot_be_accepted_by_only_top_level_decision(store):
    seed(store, [(1, '甲', '讨论广播剧的配乐。'),
                 (5, '乙', '我在制作广播剧，求录音软件帮助。')], body='讨论广播剧的配乐。')
    def check(p):
        value = reviewed(p)
        value['group_reviews'][1].update(same_topic=False, reason='这一组是制作软件求助，不是配乐讨论。')
        return value
    g = check_seed(store, Gateway(store, checks=check))
    assert not projection(store)
    assert len(g.calls) == 2  # One bounded JSON repair, no semantic promotion.
    assert g.calls[0]['deadline'] == g.calls[1]['deadline']


def test_each_group_is_required_and_has_exact_id_and_real_boolean(store):
    seed(store, [(1, '甲', '望远镜支架怎么选材？'), (5, '乙', '比较望远镜支架材料的重量。')])
    g = check_seed(store, Gateway(store, checks=reviewed))
    payload = g.calls[0]['payload']
    good = reviewed(payload)
    ep.validate_incremental_admission(good, payload)
    bads = []
    for mutation in ('missing', 'duplicate', 'unknown', 'nonbool'):
        bad = copy.deepcopy(good)
        if mutation == 'missing': bad['group_reviews'].pop()
        if mutation == 'duplicate': bad['group_reviews'][1] = bad['group_reviews'][0]
        if mutation == 'unknown': bad['group_reviews'][1]['group_id'] = 999999
        if mutation == 'nonbool': bad['group_reviews'][1]['independent'] = 'true'
        bads.append(bad)
    for bad in bads:
        with pytest.raises(ValueError): ep.validate_incremental_admission(bad, payload)
    rejected = copy.deepcopy(good)
    rejected['group_reviews'][1]['independent'] = False
    with pytest.raises(ValueError, match='repair_changed'):
        ep.validate_incremental_admission(good, payload, rejected)


def meaning_payload():
    return {'review_contract':'groups_and_meaning_v1','memory':{
        'category':'saying','body':'收线：暂时停止追问，保存问题。',
        'meaning':'暂时停止追问，保存问题。','definition_refs':[1],
        'sources':[{'message_id':1,'field':'content','excerpt':'收线就是暂时停止追问。',
                    'context':'收线就是暂时停止追问。另一个说法才是保存问题。'},
                   {'message_id':2,'field':'content','excerpt':'我们先收线吧。'}]}}


def meaning_vote(payload):
    return {'decision':'accept','violations':[],'reason':'逐项核对。','group_reviews':[],
        'safe_sources':[{k:s[k] for k in ('message_id','field','excerpt')} for s in payload['memory']['sources']],
        'meaning_support':[{'point':'暂时停止追问，保存问题。','source_refs':[
            {'message_id':1,'field':'content','excerpt':'收线就是暂时停止追问。'}]}]}


@pytest.mark.parametrize('mutation', ['unbound_message','context_only','missing_point','not_retained'])
def test_meaning_cannot_borrow_an_unbound_or_removed_explanation(mutation):
    payload=meaning_payload(); vote=meaning_vote(payload)
    ref=vote['meaning_support'][0]['source_refs'][0]
    if mutation=='unbound_message': ref['message_id']=3
    if mutation=='context_only': ref['excerpt']='另一个说法才是保存问题。'
    if mutation=='missing_point': vote['meaning_support'][0]['point']='暂时停止追问'
    if mutation=='not_retained': vote['safe_sources']=vote['safe_sources'][1:]
    with pytest.raises(ValueError): ep.validate_incremental_admission(vote,payload)


def test_meaning_all_points_have_bound_retained_explanations():
    payload=meaning_payload()
    payload['memory']['sources'][0]['excerpt']='收线就是暂时停止追问，保存问题。'
    vote=meaning_vote(payload)
    vote['meaning_support'][0]['source_refs'][0]['excerpt']=payload['memory']['sources'][0]['excerpt']
    ep.validate_incremental_admission(vote,payload)


@pytest.mark.parametrize('detail', [
    '昨晚天气降到12度。', '这部作品今天换了主演。', '同事的家人住院了。',
])
def test_host_never_appends_raw_evidence_as_a_quote(store, detail):
    mid, _ = seed(store, [(1, '甲', '望远镜支架比较钢材和木材。'+detail),
                           (5, '乙', '望远镜支架选材比较携带重量。')])
    # A fake acceptance isolates rendering from semantic quote/privacy rejection.
    check_seed(store, Gateway(store, checks=reviewed))
    value=projection(store)
    assert value['memory_ids']==[mid]
    assert detail not in value['text']
    assert value['text']=='常聊话题：讨论望远镜的支架。'
    assert any(detail in s['message']['content'] for s in profiles(store)[0]['sources'])
    assert projection(store)==value  # Read does not increment the version.


def test_hypothetical_use_of_advice_is_not_a_completed_result_report(store):
    mid, _ = seed(store, [(1,'甲','望远镜支架怎么选材，求建议？'),
        (7,'甲','新做一副望远镜支架，想按建议比较木材和钢材，重量如何取舍？')])
    check_seed(store, Gateway(store, checks=reviewed))
    assert projection(store)['memory_ids']==[mid]


def test_accepted_check_without_per_group_results_is_not_enough(store):
    seed(store, [(1,'甲','望远镜支架选材有什么区别？'), (5,'乙','比较木材望远镜支架的重量。')])
    def old_vote(p):
        value=accept(p);value.pop('group_reviews',None);return value
    g=check_seed(store,Gateway(store,checks=old_vote))
    assert not projection(store)
    assert len(g.calls)==2 and g.calls[0]['deadline']==g.calls[1]['deadline']


def test_safe_sources_must_still_include_the_two_reviewed_groups(store):
    seed(store, [(1,'甲','望远镜支架选材有什么区别？'), (5,'乙','比较木材望远镜支架的重量。')])
    def check(p):
        value=reviewed(p);value['safe_sources']=value['safe_sources'][:1];return value
    check_seed(store,Gateway(store,checks=check))
    assert not projection(store)


def test_old_queued_acceptance_cannot_bypass_new_group_review(store):
    from test_entry_profile_evidence_selection import frozen
    mid, _=seed(store,[(1,'甲','望远镜支架选材有什么区别？'),(5,'乙','比较木材望远镜支架的重量。')])
    task, work=frozen(store,mid)
    payload=json.loads(work['payload_json'])
    old=accept(payload);old.pop('group_reviews');old.pop('meaning_support')
    payload.pop('review_contract');payload['memory'].pop('discussion_groups')
    with store.write() as c:
        c.execute('UPDATE entry_profile_incremental_work SET work_key=?,payload_json=?,parsed_json=? WHERE id=?',
                  ('pre-v6-work',dumps(payload),dumps(old),work['id']))
    task.run('group')
    with store.read() as c:
        old_work=c.execute('SELECT state,reason FROM entry_profile_incremental_work WHERE id=?',(work['id'],)).fetchone()
    assert tuple(old_work)==('cancelled','stale_check')
    assert len(task.gateway.calls)==1
    assert task.gateway.calls[0]['payload']['review_contract']=='groups_and_meaning_v1'
    assert projection(store)['memory_ids']==[mid]


def test_bound_meaning_point_removal_does_not_publish_after_restart(store):
    mid, ids=seed(store,[(1,'甲','收线就是暂时停止追问。'),(5,'乙','我们先收线吧。')],
                  category='saying',body='收线：暂时停止追问')
    with store.write() as c:
        c.execute('UPDATE entry_profile_admissions SET definition_refs_json=? WHERE memory_id=?',(dumps([ids[0]]),mid))
    def borrow(p):
        value=accept(p)
        value['meaning_support'][0]['source_refs'][0]['excerpt']='把未决事项放进备忘。'
        return value
    g=check_seed(store,Gateway(store,checks=borrow))
    assert len(g.calls)==2 and not projection(store)
    check_seed(store,Gateway(store,checks=borrow))
    assert not projection(store)
    assert profiles(store)[0]['importance']==30


def test_json_repair_can_replace_a_null_review_array_without_crashing(store):
    seed(store,[(1,'甲','望远镜支架选材有什么区别？'),(5,'乙','比较木材望远镜支架的重量。')])
    attempts=[]
    def check(p):
        value=reviewed(p)
        if not attempts: value['group_reviews']=None
        attempts.append(True)
        return value
    g=check_seed(store,Gateway(store,checks=check))
    assert len(g.calls)==2 and projection(store)
    assert g.calls[0]['deadline']==g.calls[1]['deadline']
