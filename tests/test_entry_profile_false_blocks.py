"""EF-26: lexical false blocks only; no prompt or admission-policy changes."""
import pytest

from iris import entry_profile as ep
from test_entry_profile_incremental import (
    Gateway, add, fixed_projection_clock, profiles, projection, run, source,
)
from test_entry_profile_incremental_quality import seed, check_seed


@pytest.mark.parametrize('body', [
    '讨论几种安排、三类工具与两种方案。',
    '比较3种做法和第2版的结构。',
    '交流每周三次、每次两遍的反复练习。',
    '比较第一步与第二步，再把同一过程做一次。',
    '按多个维度讨论修正前后的差别。',
    '讨论一点差别、一些变化与一起尝试的过程。',
    '讨论米、厘米、毫米等单位及不同计量方法。',
    '比较吉他的不同演奏方法。',
    '每隔三天做一次，每两周交流一回。',
])
def test_generic_categories_counts_and_lexical_nouns_are_not_literal_names(body):
    assert ep._inc_missing_entities(body, ['交流做法与变化。']) == []


@pytest.mark.parametrize('body,evidence', [
    ('讨论《水岸长歌》的表达。', '讨论《远方来信》的表达。'),
    ('讨论南京的路线。', '讨论杭州的路线。'),
    ('比较华为的设备。', '比较不同设备。'),
    ('讨论李白的作品。', '讨论不同诗人的作品。'),
    ('选择品牌为“修正”的产品。', '讨论不同产品。'),
    ('选择型号 Q7。', '选择型号 Q70。'),
    ('比较35毫米与50毫米。', '比较35毫米。'),
    ('读数为23。', '读数为123。'),
    ('偏差为2.5%。', '偏差为12.5%。'),
    ('比较十公里的距离。', '比较三十公里的距离。'),
    ('比较十五毫米的尺寸。', '比较十五厘米的尺寸。'),
    ('时间为2026年12月3日。', '时间为2026年3月12日。'),
    ('时间为18:30。', '时间为18:03。'),
    ('姓名为修正。', '参与者有不同称呼。'),
    ('地名为“吉他”。', '普通演奏方法。'),
    ('数值为百分之五十。', '数值为百分之三十。'),
])
def test_actual_names_identifiers_and_values_still_require_exact_bound_anchors(body, evidence):
    assert ep._inc_missing_entities(body, [evidence])


@pytest.mark.parametrize('body', [
    '讨论《水岸长歌》的表达。',
    '讨论南京的路线。',
    '比较华为的设备。',
    '讨论李白的作品。',
    '选择品牌为“修正”的产品。',
    '选择型号 Q7。',
    '比较35毫米与50毫米。',
    '偏差为2.5%。',
    '比较十公里的距离。',
    '时间为2026年12月3日。',
])
def test_real_anchors_are_allowed_when_present_in_this_items_sources(body):
    assert ep._inc_missing_entities(body, [body]) == []


def test_generic_body_still_reaches_and_can_fail_the_unchanged_semantic_checker(store):
    seed(store, [(1, '甲', '先记录输入，再比较输出。'),
                 (4, '乙', '把步骤重做后逐项比较差别。')],
         body='按三个维度讨论修正前后的差别。')
    def reject(payload):
        return dict(decision='reject', violations=['无依据'],
                    reason='这些依据不能证明正文概括。', safe_sources=[])
    g = check_seed(store, Gateway(store, checks=reject))
    assert len(g.calls) == 1 and 'admit' in g.calls[0]['purpose']
    assert not projection(store)


def test_real_unbound_name_still_blocks_before_model_check(store):
    seed(store, [(1, '甲', '先记录输入，再比较输出。'),
                 (4, '乙', '把步骤重做后逐项比较差别。')],
         body='讨论《水岸长歌》的结构。')
    g = check_seed(store)
    assert not g.calls and not projection(store)
    assert profiles(store)[0]['admission_reason'].startswith('unbound_entities:')


@pytest.mark.parametrize('body', [
    '群内不公开他人的住址。',
    '讨论其他人的不同看法。',
    '讨论他者视角的表达。',
    '讨论利他选择与排他条件。',
])
def test_neutral_other_person_or_compound_word_is_not_gender_inference(store, body):
    add(store, '群规公告：' + body)
    def observe(payload):
        return {'observations': [dict(op='propose', category='rule', body=body,
            saying='', meaning='', definition_refs=[],
            source_refs=[source(m) for m in payload['messages']])]}
    g = Gateway(store, observe)
    run(store, g)
    assert any('admit' in c['purpose'] for c in g.calls)
    assert projection(store)


@pytest.mark.parametrize('body', [
    '他分享步骤。', '他们分享步骤。', '他俩分享步骤。',
    '她分享步骤。', '她们分享步骤。', '她俩分享步骤。',
    '妳分享步骤。', '祂分享步骤。', 'He shares the steps.',
    'Her method is used here.',
])
def test_actual_gendered_pronouns_remain_blocked(store, body):
    add(store, '群规公告：' + body)
    def observe(payload):
        return {'observations': [dict(op='propose', category='rule', body=body,
            saying='', meaning='', definition_refs=[],
            source_refs=[source(m) for m in payload['messages']])]}
    g = Gateway(store, observe)
    run(store, g)
    assert not any('admit' in c['purpose'] for c in g.calls)
    assert not profiles(store) and not projection(store)


MEANING = '先列出要点再填写细节'


def saying_observer(body, *, saying='落笔', meaning=MEANING, definitions=True):
    def observe(payload):
        refs = [source(m) for m in payload['messages']]
        if payload['profiles']:
            return {'observations': [dict(op='confirm',
                memory_id=payload['profiles'][0]['memory_id'], source_refs=refs)]}
        return {'observations': [dict(op='propose', category='saying', body=body,
            saying=saying, meaning=meaning,
            definition_refs=[m['message_id'] for m in payload['messages']] if definitions else [],
            source_refs=refs)]}
    return observe


def saying_messages(store):
    add(store, '这里说落笔，就是先列出要点再填写细节。', sender='甲')
    add(store, '我先落笔，列好要点之后补充细节。', 4, sender='乙')


@pytest.mark.parametrize('body', [
    '“落笔”：先列出要点再填写细节。',
    '「落笔」：先列出要点再填写细节。',
    '落笔:先列出要点再填写细节。',
    ' 落笔 ： 先列出要点再填写细节。 ',
])
def test_saying_structure_allows_only_equivalent_framing(store, body):
    saying_messages(store)
    g = Gateway(store, saying_observer(body))
    run(store, g)
    assert any('admit' in c['purpose'] for c in g.calls)
    assert projection(store)['text'] == '群内说法：落笔：' + MEANING
    assert profiles(store)[0]['body'] == '落笔：' + MEANING


@pytest.mark.parametrize('body,meaning,definitions', [
    ('落笔：不用整理，直接发出。', MEANING, True),
    ('落笔', MEANING, True),
    ('先列出要点再填写细节', MEANING, True),
    ('“落笔：先列出要点再填写细节。', MEANING, True),
    ('落笔：' + MEANING, MEANING, False),
    ('落笔', '', True),
])
def test_genuine_saying_structure_or_definition_errors_still_block(store, body, meaning, definitions):
    saying_messages(store)
    g = Gateway(store, saying_observer(body, meaning=meaning, definitions=definitions))
    run(store, g)
    assert not any('admit' in c['purpose'] for c in g.calls)
    assert not profiles(store) and not projection(store)


def test_account_privacy_guard_is_not_relaxed(store):
    add(store, '群规公告：公开账号13900139000。')
    g = Gateway(store, lambda p: {'observations': [dict(op='propose',category='rule',
        body='公开账号13900139000。', saying='',meaning='',definition_refs=[],
        source_refs=[source(m) for m in p['messages']])]})
    run(store, g)
    assert not profiles(store) and not projection(store)


def test_quantity_spacing_is_only_framing_not_a_different_value():
    assert ep._inc_missing_entities('尺寸为35 毫米。', ['尺寸为35毫米。']) == []
    assert ep._inc_missing_entities('尺寸为35毫米。', ['尺寸为35 毫米。']) == []


def test_existing_exact_saying_structure_is_not_reparsed_as_framing():
    assert ep._inc_saying_body('甲:乙', '甲:乙', '') == '甲:乙'


@pytest.mark.parametrize('body,evidence', [
    ('日期为三月二十日。', '日期为二十月三日。'),
    ('日期为二〇二六年十二月三日。', '日期为二〇二六年三月十二日。'),
    ('时间为晚上八点。', '时间为晚上九点。'),
    ('时间为八点。', '时间为九点。'),
    ('时间为八点三十分。', '时间为八点四十分。'),
    ('时间为八点半。', '时间为九点半。'),
])
def test_chinese_calendar_and_clock_values_are_not_generic_number_fragments(body, evidence):
    assert ep._inc_missing_entities(body, [evidence])
    assert ep._inc_missing_entities(body, [body]) == []


def test_generic_number_of_points_is_not_a_clock_time():
    assert ep._inc_missing_entities('提出三点意见，分四节交流。', ['讨论不同意见与安排。']) == []


@pytest.mark.parametrize('body', [
    '讨论500g的计量。', '比较48kHz的采样。', '讨论3D的表达。',
    '按60fps比较呈现。', '数值为1e-3。',
    '版本为2.4.1。', '版本为v2.4.1。', '版本为v2.4.1alpha。',
    '读数为23.', '数值为2.5.', 'At 18:30.', '读数为500kgx。',
])
def test_bound_numeric_and_identifier_forms_do_not_reject_their_own_source(body):
    assert ep._inc_missing_entities(body, [body]) == []


@pytest.mark.parametrize('body,evidence', [
    ('版本为2.4.1。', '版本为2.4.2。'),
    ('版本为v2.4.1。', '版本为v2.4.2。'),
    ('版本为2.4.1。', '版本为2.4.1.5。'),
    ('读数为23。', '读数为23.5。'),
])
def test_complete_version_identifier_cannot_borrow_numeric_fragments(body, evidence):
    assert ep._inc_missing_entities(body, [evidence])
