"""EF-15: recover thematic coverage without bypassing semantic admission."""
import pytest

from iris import entry_profile as ep
from test_entry_profile_incremental import (
    Gateway, accept, add, fixed_projection_clock, profiles, projection, propose, run,
)
from test_entry_profile_incremental_quality import seed, check_seed


@pytest.mark.parametrize('body,evidence', [
    ('讨论线路与装备的选择。', ['北坡陡，河边平缓。', '轻量背包容易磨损。']),
    ('比较作品的结构与表达。', ['这张画先铺大色块。', '那篇小说从结尾开始写。']),
    ('讨论交流中用词的细节与画面感。', ['这里换成推门显得主动。']),
    ('讨论噪声来源和测试方法。', ['关掉风扇后嘶声变小。']),
])
def test_common_nouns_are_semantically_checked_not_literal_entities(body, evidence):
    assert ep._inc_missing_entities(body, evidence) == []


@pytest.mark.parametrize('body,evidence,needle', [
    ('讨论《远山纪事》的叙述。', ['讨论《近海纪事》的叙述。'], '远山纪事'),
    ('比较苏州的路线。', ['杭州的路线更平缓。'], '苏州'),
    ('比较型号 X4 与 RF-35。', ['型号 X40 与 RF-50。'], 'x4'),
    ('选择品牌为“佳能”的镜头。', ['尼康镜头。'], '佳能'),
    ('比较十公里线路。', ['五公里线路。'], '十'),
    ('比较35毫米与50毫米镜头。', ['35毫米镜头。'], '50'),
    ('日期为2026年12月3日。', ['2026年3月12日。'], '2026年12月3日'),
])
def test_specific_identifiers_still_need_bound_evidence(body, evidence, needle):
    assert any(needle in term for term in ep._inc_missing_entities(body, evidence))


@pytest.mark.parametrize('body', [
    '讨论《远山纪事》的叙述。',
    '比较苏州的路线。',
    '选择品牌为“佳能”的镜头。',
    '比较十公里线路。',
    '日期为2026年12月3日。',
])
def test_named_or_numeric_terms_pass_when_actually_bound(body):
    assert ep._inc_missing_entities(body, [body]) == []


def test_broad_topic_from_distinct_objects_still_requires_model_admission(store):
    mid, _ = seed(store, [
        (1, '甲', '北坡这条路上升很多，碎石路段走起来费力。'),
        (4, '乙', '河边小径几乎没有爬升，雨后泥泞比较难走。'),
    ], body='讨论周边徒步路线的选择与难度。')
    g = check_seed(store)
    assert len(g.calls) == 1 and 'admit' in g.calls[0]['purpose']
    assert projection(store)['memory_ids'] == [mid]
    row = profiles(store)[0]
    assert row['category'] == 'topic' and row['independent_days'] == 2


def reject(payload):
    return {'decision': 'reject', 'violations': ['无依据'],
            'reason': '逐条核对后不成立。', 'safe_sources': []}


def test_generic_body_is_not_a_bypass_of_semantic_check(store):
    seed(store, [
        (1, '甲', '北坡上升很多，走起来费力。'),
        (4, '乙', '河边小径雨后泥泞，走得很慢。'),
    ], body='讨论装备的选择。')
    g = check_seed(store, Gateway(store, checks=reject))
    assert len(g.calls) == 1 and not projection(store)
    assert profiles(store)[0]['importance'] == 30


@pytest.mark.parametrize('announcement', [
    '每次分享设计图前先删掉定位，避免别人顺着找到住处。',
    '讨论新章节先标注剧透并折叠，这个要求一直适用。',
    '提问不需要送礼，制作步骤有疑问就直接问。',
    '你上传照片时应去掉定位，这项要求对所有发帖者都适用。',
])
def test_rule_without_magic_scope_words_reaches_semantic_check(store, announcement):
    add(store, announcement)
    g = Gateway(store, lambda p: propose(p, category='rule', body='分享内容须遵守隐私要求。'))
    run(store, g)
    assert any('admit' in call['purpose'] for call in g.calls)
    assert projection(store)


def test_ambiguous_request_does_not_become_rule_if_checker_rejects(store):
    add(store, '谁愿意把说明书再发一下？')
    g = Gateway(store, lambda p: propose(p, category='rule', body='分享说明书。'), checks=reject)
    run(store, g)
    assert any('admit' in call['purpose'] for call in g.calls)
    assert not projection(store)


@pytest.mark.parametrize('text', [
    '小鹿，你把资料单独给我发一下。',
    '今晚八点之前别再发图片，活动结束再恢复。',
    '协会规定所有会员每年提交证明。',
    '这是我自己的安排，请不要把个人的事放进介绍。',
])
def test_obviously_non_group_rules_remain_blocked_before_model_check(store, text):
    add(store, text)
    g = Gateway(store, lambda p: propose(p, category='rule', body='分享内容前核对信息。'))
    run(store, g)
    assert not any('admit' in call['purpose'] for call in g.calls)
    assert not projection(store)


def test_repeated_short_phrase_does_not_connect_whole_discussion_days(store):
    seed(store, [
        (1, '甲', '把瓶子静置。'),
        (1, '乙', '盐水搅匀后测上层和下层，看看读数是否相同。'),
        (4, '丙', '换了窄口容器，再把瓶子静置。'),
        (4, '丁', '这次换成糖水，比较不同温度的读数。'),
        (7, '戊', '换个位置，把瓶子静置，再比较亮处和暗处的颜色。'),
    ], body='讨论溶液测试的条件与读数差异。')
    check_seed(store)
    row = profiles(store)[0]
    assert row['independent_days'] == 3 and row['independent_uses'] == 3
    assert projection(store)


def test_same_author_following_up_same_request_is_one_discussion(store):
    seed(store, [
        (1, '甲', '谁有望远镜支架的图纸？'),
        (4, '甲', '之前问的望远镜支架图纸有结果了吗？'),
        (7, '甲', '望远镜支架图纸找到了，谢谢。'),
    ])
    g = check_seed(store)
    assert profiles(store)[0]['independent_uses'] == 1
    assert not g.calls and not projection(store)


def test_another_author_followup_without_direct_link_is_left_to_semantic_check(store):
    seed(store, [
        (1, '甲', '谁有望远镜支架的图纸？'),
        (4, '乙', '望远镜支架图纸找到了。'),
    ])
    g = check_seed(store, Gateway(store, checks=reject))
    assert profiles(store)[0]['independent_days'] == 2
    assert len(g.calls) == 1 and not projection(store)


def test_same_author_followup_about_another_object_does_not_join(store):
    seed(store, [
        (1, '甲', '谁有望远镜支架的图纸？'),
        (4, '甲', '之前问的水泵外壳图纸找到了。'),
    ], body='讨论机械图纸。')
    check_seed(store)
    assert profiles(store)[0]['independent_days'] == 2


def test_bare_followup_without_object_or_reply_link_requires_semantic_judgment(store):
    seed(store, [(1, '甲', '谁有望远镜支架图纸？'), (4, '甲', '怎么样了？')])
    g = check_seed(store, Gateway(store, checks=reject))
    assert len(g.calls) == 1 and not projection(store)


def test_exact_reposted_body_cannot_add_an_independent_topic(store):
    ids = [add(store, '望远镜支架用木材，振动恢复更快。', day, sender=sender, key=str(day))
           for day, sender in [(1, '甲'), (4, '乙')]]
    from iris.memory_ops import entry_profile_source, write_entry_profile_memory
    from test_entry_profile_incremental import NOW
    with store.write() as c:
        write_entry_profile_memory(store, 'group', op='new', content='讨论望远镜支架。',
            category='topic', importance=30, sources=[entry_profile_source(c, mid) for mid in ids],
            current=NOW, _conn=c)
        c.execute('UPDATE entry_profile_memory_state SET cursor=?,scan_cursor=?', (ids[-1], ids[-1]))
    check_seed(store)
    assert profiles(store)[0]['independent_uses'] == 1 and not projection(store)

@pytest.mark.parametrize('text', [
    '我们以后统一先删定位再上传，这个安排一直适用。',
    '我提醒大家，个人信息不要放进公开的作品介绍。',
    '@全体成员 上传照片前先去掉定位。',
    '@everyone 分享资料先征求作者许可。',
])
def test_collective_or_broadcast_notice_is_not_a_single_person_request(store, text):
    add(store, text)
    g = Gateway(store, lambda p: propose(p, category='rule', body='分享内容前核对信息。'))
    run(store, g)
    assert any('admit' in call['purpose'] for call in g.calls)


def test_ambiguous_dictionary_name_is_sent_to_semantic_review(store):
    # Bare nr tags are not sufficient to distinguish a real brand from a
    # mislabeled common noun; the checker must still prove the body point.
    mid,_=seed(store,[(1,'甲','比较不同品牌的设备。'),(4,'乙','比较设备的构造。')],body='比较华为的设备。')
    gateway=check_seed(store,Gateway(store,checks=reject))
    check=next(c['payload'] for c in gateway.calls if 'admit' in c['purpose'])
    assert '华为' in check['memory']['entity_hints']
    assert not projection(store)
