"""EF-14: general evidence boundaries, variant use, and bounded complete checks."""
import json

import pytest

from iris import entry_profile as ep
from iris.db import dumps
from iris.memory_ops import entry_profile_source, write_entry_profile_memory, purge_memory
from iris.models import ModelError
from iris.queue import estimate_tokens
from test_entry_profile_incremental import (
    NOW, Gateway, accept, add, fixed_projection_clock, profiles, projection, propose, run, source,
)


@pytest.mark.parametrize('text', [
    '我预约了检查，先别把我的个人事情写进作品说明。',
    '今天收工前大家不要在群里发链接，等投票结束再发。',
    '小舟，你这次别发广告了。',
    '公司规定员工不得把工程文件发给外部。',
])
def test_personal_temporary_targeted_or_external_request_cannot_become_rule(store,text):
    add(store,text)
    g=Gateway(store,lambda p:propose(p,category='rule',body='禁止发布个人事项。'))
    run(store,g)
    assert not projection(store)
    assert not any('admit' in c['purpose'] for c in g.calls)


@pytest.mark.parametrize('text', [
    '群规公告：全体成员不得公开别人的住址，此要求长期有效。',
    '本群规定每周五只能发原创照片，其他日期照常。',
])
def test_explicit_group_rule_is_not_confused_with_personal_or_recurring_time(store,text):
    add(store,text)
    g=Gateway(store,lambda p:propose(p,category='rule',body=text))
    run(store,g)
    assert projection(store)


def test_multiple_people_can_agree_on_a_long_term_rule(store):
    add(store,'以后分享文件都先问作者是否允许转发。',sender='甲')
    add(store,'同意这条规则，分享文件先征求作者许可。',sender='乙',minute=10)
    g=Gateway(store,lambda p:propose(p,category='rule',body='分享文件须获作者许可。'))
    run(store,g)
    assert projection(store)


def test_model_cannot_hide_personal_context_by_shortening_the_rule_quote(store):
    add(store,'这是我自己的安排，请不要把个人的事放进介绍。')
    def observe(p):
        v=propose(p,category='rule',body='个人的事不放进介绍。')
        v['observations'][0]['source_refs'][0]['excerpt']='请不要把个人的事放进介绍。'
        return v
    g=Gateway(store,observe)
    run(store,g)
    assert not projection(store)


def seed(store, texts, *, body='讨论望远镜的支架。',category='topic'):
    ids=[]
    for day,sender,text in texts:
        ids.append(add(store,text,day,sender=sender))
    with store.write() as conn:
        refs=[entry_profile_source(conn,mid) for mid in ids]
        mid=write_entry_profile_memory(store,'group',op='new',content=body,category=category,
            importance=30,sources=refs,current=NOW,_conn=conn)['memory_id']
        conn.execute('UPDATE entry_profile_memory_state SET cursor=?,scan_cursor=? WHERE entry_id=?',(max(ids),max(ids),'group'))
    return mid,ids


@pytest.fixture
def small_evidence_budget(monkeypatch):
    # Exercise real token-based pages, independent of the nomination's six-source cap.
    original = ep.IncrementalProfileLimits
    monkeypatch.setattr(ep, 'IncrementalProfileLimits',
        lambda **kwargs: original(**{'evidence_tokens': 1000, **kwargs}))


def check_seed(store,gateway=None,*,one_round=False):
    g=gateway or Gateway(store,lambda p:{'observations':[]})
    task=ep.IncrementalProfileTask(store,g,clock=lambda:NOW)
    task.one_round=one_round
    task.run('group')
    return g


@pytest.mark.parametrize('followup', [
    '之前问的望远镜支架还有吗？',
    '望远镜支架找到了，已经寄出给你。',
    '怎么样了？',
])
def test_unlinked_followup_is_rejected_by_semantic_admission(store,followup):
    seed(store,[(1,'甲','求一份望远镜支架的图纸，谁还有？'),(4,'乙',followup)])
    # EF-15: without a reply link or the same initiator and object, code must
    # not invent the event link. The semantic check still rejects this history.
    def reject_same_request(payload):
        return {'decision':'insufficient','violations':['无依据'],
                'reason':'同一请求的跟进，不是独立讨论。','safe_sources':[]}
    g=check_seed(store,Gateway(store,checks=reject_same_request))
    assert not projection(store)
    assert profiles(store)[0]['independent_uses']==2
    assert len(g.calls)==1


def test_new_discussion_of_same_object_can_be_independent(store):
    seed(store,[(1,'甲','望远镜支架的铝材和钢材在重量上有什么差别？'),
                (4,'乙','重新比较望远镜支架，实测木材阻尼比铝材好。')])
    check_seed(store)
    assert projection(store)


def test_reply_chain_cannot_make_a_second_discussion(store):
    first=add(store,'望远镜支架的图纸谁还有？',1)
    second=add(store,'我把望远镜支架的图纸发过去了。',4,sender='乙')
    with store.write() as c:
        original=c.execute('SELECT sender_subject_id,content FROM messages WHERE id=?',(first,)).fetchone()
        c.execute('UPDATE messages SET quote_author_subject_id=?,quote_content=? WHERE id=?',(*original,second))
        refs=[entry_profile_source(c,x) for x in (first,second)]
        write_entry_profile_memory(store,'group',op='new',content='讨论望远镜支架的图纸。',category='topic',
            importance=30,sources=refs,current=NOW,_conn=c)
        c.execute('UPDATE entry_profile_memory_state SET cursor=?,scan_cursor=?',(second,second))
    check_seed(store)
    assert not projection(store)


@pytest.mark.parametrize('saying,usage', [
    ('搭桥','我来搭个桥吧'),
    ('搭桥','搭的桥还在'),
    ('盖章','先盖盖章呀'),
    ('翻页','我们翻一翻页'),
    ('留灯','这盏灯还留着吗'),
    ('这盏灯还留着吗','留盏灯，我稍后接着谈'),
])
def test_conservative_grammatical_saying_variants(saying,usage):
    assert ep._inc_saying_used(saying,usage)


@pytest.mark.parametrize('saying,usage', [
    ('搭桥','这回拆桥'),('盖章','盖上盖子'),('留灯','把灯关掉'),
    ('不翻页','翻页'),('翻页','翻过很多别的内容后再看页码'),
    ('留灯','留个东西照亮房间'),('AB','CABINET'),
])
def test_distinct_sayings_or_unbounded_gaps_are_not_normalized(saying,usage):
    assert not ep._inc_saying_used(saying,usage)


@pytest.mark.parametrize('body,evidence,missing', [
    ('讨论《深海航线》的叙述。',['讨论叙述顺序。'],'深海航线'),
    ('苏州的纸灯塔制作。',['在杭州制作纸桥。'],'苏州'),
    ('比较35毫米与50毫米镜头。',['比较35毫米镜头。'],'50'),
])
def test_specific_object_cannot_be_borrowed_from_unbound_context(body,evidence,missing):
    assert any(missing in x for x in ep._inc_missing_entities(body,evidence))


def test_common_object_mismatch_is_still_rejected_by_semantic_check(store):
    seed(store,[(1,'甲','纸桥的折痕压反了，需要重折。'),
                (4,'乙','比较纸桥的宽度和插接位置。')],body='纸灯塔制作的折痕。')
    def reject_mismatch(payload):
        assert payload['memory']['body']=='纸灯塔制作的折痕。'
        return {'decision':'reject','violations':['无依据'],
                'reason':'依据讨论纸桥，不能写成纸灯塔。','safe_sources':[]}
    g=check_seed(store,Gateway(store,checks=reject_mismatch))
    assert len(g.calls)==1 and not projection(store)


def test_objects_must_survive_checker_source_shortening(store):
    seed(store,[(1,'甲','在苏州比较望远镜支架的铝材和钢材。'),
                (4,'乙','苏州展会上试用了木制望远镜支架。')],body='苏州的望远镜支架比较。')
    def check(p):
        v=accept(p)
        v['safe_sources'][0]['excerpt']='比较望远镜支架的铝材和钢材。'
        v['safe_sources'][1]['excerpt']='试用了木制望远镜支架。'
        return v
    check_seed(store,Gateway(store,checks=check))
    assert not projection(store)


def test_wrong_quote_field_is_rebound_only_to_exact_content_and_logged(store):
    add(store,'群规公告：本群禁止广告，长期有效。')
    def observe(p):
        v=propose(p,category='rule',body='禁止广告。')
        v['observations'][0]['source_refs'][0]['field']='quote'
        return v
    g=Gateway(store,observe)
    run(store,g)
    assert projection(store)
    assert profiles(store)[0]['sources'][0]['field']=='content'
    with store.read() as c:
        records=[json.loads(r[0]) for r in c.execute("SELECT result_json FROM entry_profile_incremental_work WHERE kind='observe'")]
    assert any(r.get('normalizations') for r in records)


def test_quoted_words_are_not_counted_as_the_relaying_author_use(store):
    first=add(store,'我们搭桥，把双方的句子接起来。',1)
    second=add(store,'借来看看。',4,sender='乙')
    with store.write() as c:
        original=c.execute('SELECT sender_subject_id,content FROM messages WHERE id=?',(first,)).fetchone()
        c.execute('UPDATE messages SET quote_author_subject_id=?,quote_content=? WHERE id=?',(*original,second))
        refs=[entry_profile_source(c,first),entry_profile_source(c,second,field='quote')]
        write_entry_profile_memory(store,'group',op='new',content='搭桥',category='saying',importance=30,sources=refs,current=NOW,_conn=c)
        c.execute('UPDATE entry_profile_memory_state SET cursor=?,scan_cursor=?',(second,second))
    check_seed(store)
    assert not projection(store)
    assert profiles(store)[0]['independent_uses']==1


EVIDENCE=[
    (1,'甲','望远镜支架比较铝材和钢材，重量差异明显。'),
    (2,'乙','望远镜支架改用木材后，振动恢复更快。'),
    (3,'丙','望远镜支架更宽的底座在风中更稳。'),
    (4,'丁','望远镜支架的接头松动要检查螺丝。'),
    (5,'戊','望远镜支架换成三脚结构，携带更方便。'),
    (6,'己','望远镜支架的重心放低，调节不再晃动。'),
    (7,'庚','望远镜支架新添配重，转动阻力增大。'),
    (8,'辛','望远镜支架连接软垫后，地面振动传入更少。'),
]


def test_over_token_budget_sources_are_checked_in_bounded_pages_before_admission(store, small_evidence_budget):
    mid,ids=seed(store,EVIDENCE)
    g=Gateway(store)
    check_seed(store,g,one_round=True)
    assert len(g.calls)==1
    assert not projection(store), 'A passed partial page must not publish the memory.'
    check_seed(store,g)
    assert projection(store)['memory_ids']==[mid]
    assert len(g.calls)>=2
    checked=set()
    for c in g.calls:
        payload=c['payload'];refs=payload['memory']['sources']
        assert len(refs)<=6
        assert estimate_tokens(dumps(payload['memory']))<=2000
        checked.update(x['message_id'] for x in refs)
    assert checked==set(ids), 'No old or apparently adverse evidence may be silently omitted.'


def test_later_page_rejection_does_not_publish_a_partially_approved_memory(store, small_evidence_budget):
    seed(store,EVIDENCE)
    def check(p):
        if any('软垫' in x['excerpt'] for x in p['memory']['sources']):
            return {'decision':'reject','violations':['无依据'],'reason':'后续依据改变限定。','safe_sources':[]}
        return accept(p)
    g=Gateway(store,checks=check)
    check_seed(store,g)
    assert len(g.calls)>=2
    assert not projection(store)
    assert profiles(store)[0]['importance']==30


def test_restart_reuses_accepted_pages_and_cas_still_protects_the_bundle(store, small_evidence_budget):
    mid,ids=seed(store,EVIDENCE)
    g=Gateway(store)
    check_seed(store,g,one_round=True)
    with store.write() as c:
        c.execute('UPDATE messages SET content=? WHERE id=?',('来源已改变。',ids[-1]))
    check_seed(store,g,one_round=True)
    assert len(g.calls)==1
    assert not projection(store)
    with store.read() as c:
        assert c.execute('SELECT importance FROM memories WHERE id=?',(mid,)).fetchone()[0]==30


def test_clear_between_pages_erases_every_page_and_never_calls_again(store, small_evidence_budget):
    mid,_=seed(store,EVIDENCE)
    g=Gateway(store)
    check_seed(store,g,one_round=True)
    assert len(g.calls)==1
    with store.read() as c:
        revision=c.execute('SELECT revision FROM memories WHERE id=?',(mid,)).fetchone()[0]
    purge_memory(store,mid,revision,confirm=True)
    check_seed(store,g)
    assert len(g.calls)==1
    with store.read() as c:
        for row in c.execute("SELECT payload_json,outputs_json,parsed_json FROM entry_profile_incremental_work WHERE memory_id=?",(mid,)):
            assert '望远镜' not in str(tuple(row))


def test_particle_normalization_does_not_remove_a_lexical_word_ending():
    assert not ep._inc_saying_used('网吧','网盘')
    assert ep._inc_saying_used('搭桥呢','搭个桥')


def test_effective_date_of_permanent_rule_is_not_a_one_day_arrangement(store):
    add(store,'群规公告：从今天起，本群长期禁止公开住址。')
    run(store,Gateway(store,lambda p:propose(p,category='rule',body='禁止公开住址。')))
    assert projection(store)


def test_later_page_timeout_cannot_promote_partial_acceptance(store, small_evidence_budget):
    seed(store,EVIDENCE)
    g=Gateway(store)
    check_seed(store,g,one_round=True)
    def late(g,purpose,payload): g.tick+=181
    g.hook=late
    check_seed(store,g)
    assert len(g.calls)==2 and not projection(store)
    assert profiles(store)[0]['importance']==30


def test_passed_page_is_not_recalled_after_restart(store, small_evidence_budget):
    seed(store,EVIDENCE)
    g=Gateway(store)
    check_seed(store,g,one_round=True)
    first=dumps(g.calls[0]['payload']['memory']['sources'])
    check_seed(store,g)
    assert projection(store)
    assert sum(dumps(c['payload']['memory']['sources'])==first for c in g.calls)==1


def test_saying_terminal_punctuation_does_not_drop_a_valid_candidate(store):
    add(store,'搭桥是什么意思？就是把双方的句子接起来。')
    def observe(p):
        return {'observations':[{'op':'propose','category':'saying','body':'搭桥：把双方的句子接起来。',
            'saying':'搭桥','meaning':'把双方的句子接起来','definition_refs':[p['messages'][0]['message_id']],
            'source_refs':[source(p['messages'][0])]}]}
    run(store,Gateway(store,observe))
    assert len(profiles(store))==1
    assert profiles(store)[0]['body']=='搭桥：把双方的句子接起来'


def test_eval_checked_stage_never_reports_a_partial_page_as_admitted(store, small_evidence_budget):
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from evals.entry_profile_eval.memory_runner import _incremental_stages
    seed(store,EVIDENCE)
    check_seed(store,one_round=True)
    with store.read() as c:
        work=[dict(r) for r in c.execute('SELECT * FROM entry_profile_incremental_work ORDER BY id')]
    stages=_incremental_stages(work,[])
    assert stages['deterministic']
    assert stages['checked']==[]


def test_alphanumeric_object_names_require_exact_bound_identifier():
    assert ep._inc_missing_entities('讨论 X4 相机。',['X5 相机很轻。']) == ['x4']
    assert ep._inc_missing_entities('讨论 X4 相机。',['X40 相机很轻。']) == ['x4']
    assert ep._inc_missing_entities('讨论 X4 相机。',['X4 相机很轻。']) == []


@pytest.mark.parametrize('saying,excerpt',[('搭桥','搭个桥墩'),('盖章','封盖章鱼缸')])
def test_saying_does_not_match_a_fragment_of_a_different_noun(saying,excerpt):
    assert not ep._inc_saying_used(saying,excerpt)


def test_admin_progress_identifies_bounded_check_pages(store, small_evidence_budget):
    from iris.admin_data import entry_profile_snapshot
    seed(store,EVIDENCE)
    run(store,Gateway(store,lambda p:{'observations':[]}))
    work=entry_profile_snapshot(store,'group')['latest_attempt']['work']
    checks=[w for w in work if w['kind']=='admit']
    assert len(checks)>=2
    # Mapping pages and final complete-proof review are distinct checks.
    assert [w['check_page'] for w in checks]==[1,2,1]
    assert [w['check_pages'] for w in checks]==[2,2,1]
