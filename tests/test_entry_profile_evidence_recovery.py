"""EF-24 hand-written proof/format regressions; no quality-corpus fixtures."""
import copy
import json
from dataclasses import replace

import pytest
from iris import entry_profile as ep
from iris.db import dumps
from test_entry_profile_incremental import NOW, Gateway, accept, add, profiles, projection, propose, run
from test_entry_profile_incremental_quality import seed, check_seed
from test_entry_profile_semantic_admission import payload, vote, saying_payload, saying_vote


SEVEN = [(1, '甲', '盒盖的密封性能值得比较。'),
         (2, '乙', '另一种盒盖的密封性怎样？'),
         (3, '丙', '拆卸盒盖时要怎样看密封接口？'),
         (4, '丁', '还有一种盒盖密封的取舍。'),
         (5, '戊', '不同盒盖的密封方式可以对比。'),
         (6, '己', '再讨论盒盖的密封设计。'),
         (7, '庚', '比较另一类盒盖的密封接口。')]


def test_seven_sources_within_token_budget_get_one_complete_final(store):
    mid, ids = seed(store, SEVEN, body='盒盖的密封性。')
    g = Gateway(store)
    check_seed(store, g)
    assert [c['payload']['mode'] for c in g.calls] == ['final']
    assert {s['message_id'] for s in g.calls[0]['payload']['memory']['sources']} == set(ids)
    assert projection(store)['memory_ids'] == [mid]
    for c in g.calls:
        assert ep.estimate_tokens(dumps(c['payload']['memory'])) <= 2000
        assert ep.estimate_tokens(ep.incremental_prompts()['admit']['text']) + ep.estimate_tokens(dumps(c['payload'])) <= 4000


def test_proof_fit_is_tokens_and_bounded_admission_sources_not_nomination_six(store):
    task = ep.IncrementalProfileTask(store, Gateway(store))
    m = payload()['memory']
    m['sources'] = m['sources'] + [dict(m['sources'][0], source_id='extra'+str(i), message_id=100+i) for i in range(4)]
    assert len(m['sources']) == 7
    assert task._proof_fits(m, {'entry_id':'group'}, 'final', NOW.isoformat(), 'Asia/Shanghai')
    task.limits = replace(task.limits, evidence_tokens=100)
    assert not task._proof_fits(m, {'entry_id':'group'}, 'final', NOW.isoformat(), 'Asia/Shanghai')
    task.limits = replace(task.limits, evidence_tokens=100000, admit_request_tokens=100000)
    m['sources'] = m['sources'] * 5
    assert not task._proof_fits(m, {'entry_id':'group'}, 'final', NOW.isoformat(), 'Asia/Shanghai')
    assert task.limits.evidence == 6  # Observation nominations still bounded to six.


def test_proof_validator_accepts_complete_seven_source_review():
    p = payload(); m = p['memory']
    for i in range(4):
        s = dict(m['sources'][0], source_id='extra'+str(i), message_id=100+i, group_id=100+i)
        m['sources'].append(s)
        m['discussion_groups'].append(dict(group_id=100+i,date=s['date'],source_ids=[s['source_id']],eligible=True))
    assert ep.validate_incremental_admission(vote(p),p)['decision']=='accept'


def test_full_source_id_keeps_use_and_definition_sentences():
    p=saying_payload();s=p['memory']['sources'][0]
    s.update(excerpt='先搭桥。搭桥就是把双方的意思连接起来。',context='先搭桥。搭桥就是把双方的意思连接起来。')
    s['segments']=ep._inc_segments(s['excerpt'])
    v=saying_vote(p);v['safe_sources'][0]={'source_id':s['source_id']}
    result=ep.validate_incremental_admission(v,p)
    assert result['safe_sources'][0]['excerpt']==s['excerpt']


@pytest.mark.parametrize('ref', [
    {'source_id':'s1','start_segment':1,'end_segment':2},
    {'message_id':11,'field':'content','excerpt':'搭桥就是把双方的意思连接起来。'},
])
def test_saying_cannot_trim_definition_out_of_same_complete_use_sentence(ref):
    p=saying_payload();s=p['memory']['sources'][0]
    s.update(excerpt='先搭桥，搭桥就是把双方的意思连接起来。',context='先搭桥，搭桥就是把双方的意思连接起来。')
    s['segments']=ep._inc_segments(s['excerpt'])
    v=saying_vote(p);v['safe_sources'][0]=ref
    with pytest.raises(ValueError,match='incomplete_saying_sentence'):
        ep.validate_incremental_admission(v,p)


def test_saying_can_remove_unrelated_whole_sentence_but_keep_full_use_and_definition():
    p=saying_payload();s=p['memory']['sources'][0]
    s.update(excerpt='昨晚车票涨到45元。先搭桥，搭桥就是把双方的意思连接起来。',context='昨晚车票涨到45元。先搭桥，搭桥就是把双方的意思连接起来。')
    s['segments']=ep._inc_segments(s['excerpt'])
    v=saying_vote(p);v['safe_sources'][0]={'source_id':'s1','start_segment':1,'end_segment':3}
    result=ep.validate_incremental_admission(v,p)
    assert result['safe_sources'][0]['excerpt']=='先搭桥，搭桥就是把双方的意思连接起来。'


def test_full_source_reference_does_not_read_unbound_context():
    p=saying_payload();p['memory']['sources'][0]['context']+='另有不属于绑定片段的说明。'
    v=saying_vote(p);v['safe_sources'][0]={'source_id':'s1'}
    result=ep.validate_incremental_admission(v,p)
    assert '另有' not in result['safe_sources'][0]['excerpt']


@pytest.mark.parametrize('category', ['topic','rule'])
def test_category_specific_format_repair_retains_points_and_shared_deadline(store, category):
    mid,ids=seed(store, SEVEN[:2], body='盒盖的密封性。' if category=='topic' else '本群分享文件先征得作者同意。',category=category)
    class RepairGateway(Gateway):
        def chat(self,messages,purpose,**kwargs):
            if purpose.endswith('_repair'):
                repair=messages[-1]['content']
                assert 'usage_reviews=[]' in repair
                assert 'point_support' in repair and 'point_id' in repair
                assert '八个' in repair
                if category=='rule': assert 'group_reviews=[]' in repair
            return super().chat(messages,purpose,**kwargs)
    def bad(p):
        v=accept(p);v['usage_reviews']=[dict(source_id=p['memory']['sources'][0]['source_id'],role='other',reason='不适用类别的冗余字段')]
        return v
    g=RepairGateway(store)
    calls=[0]
    def check(p):
        calls[0]+=1
        return bad(p) if calls[0]==1 else accept(p)
    g.checks=check
    check_seed(store,g)
    assert projection(store)['memory_ids']==[mid]
    assert len(g.calls)==2 and g.calls[0]['deadline']==g.calls[1]['deadline']


def test_observation_invalid_interval_is_repaired_before_losing_whole_rule(store):
    add(store,'以后本群公开文件前先征得作者同意。谢谢。')
    def invalid(p):
        v=propose(p,category='rule',body='公开文件须先征得作者同意。')
        m=p['messages'][0]
        v['observations'][0]['source_refs']=[dict(message_id=m['message_id'],field='content',start_segment=0,end_segment=99)]
        return v
    def corrected(p):return propose(p,category='rule',body='公开文件须先征得作者同意。')
    class RangeGateway(Gateway):
        def chat(self,messages,purpose,**kwargs):
            if purpose.endswith('observe_repair'):
                assert 'end_segment' in messages[-1]['content']
                assert 'message_id' in messages[-1]['content']
            return super().chat(messages,purpose,**kwargs)
    g=RangeGateway(store)
    calls=[0]
    def observe(p):
        calls[0]+=1
        return invalid(p) if calls[0]==1 else corrected(p)
    g.observations=observe
    run(store,g)
    assert projection(store)
    assert any(c['purpose'].endswith('observe_repair') for c in g.calls)
    assert g.calls[0]['deadline']==g.calls[1]['deadline']


def test_invalid_interval_repair_still_bad_never_writes(store):
    add(store,'本群长期不准张贴商业广告。')
    def bad(p):
        v=propose(p,category='rule',body='本群不准商业广告。')
        v['observations'][0]['source_refs']=[dict(message_id=p['messages'][0]['message_id'],field='content',start_segment=0,end_segment=99)]
        return v
    g=Gateway(store,observations=bad);run(store,g)
    assert not profiles(store) and not projection(store)
    assert len(g.calls)==2


def test_rule_in_reply_is_checked_with_full_announcement_not_blocked_as_personal(store):
    q=add(store,'文件要私信问许可吗？',1)
    announcement=add(store,'不用私信，群里统一在公开文件前征得作者同意，今后都一样。',1,sender='乙',minute=2)
    with store.write() as conn:
        r=conn.execute('SELECT sender_subject_id,content FROM messages WHERE id=?',(q,)).fetchone()
        conn.execute('UPDATE messages SET quote_author_subject_id=?,quote_content=? WHERE id=?',(*r,announcement))
    def observe(p):
        v=propose(p,category='rule',body='本群公开文件前先征得作者同意。')
        v['observations'][0]['source_refs']=[dict(message_id=announcement,field='content',excerpt='不用私信，群里统一在公开文件前征得作者同意，今后都一样。')]
        return v
    g=Gateway(store,observe);run(store,g)
    assert projection(store)
    assert profiles(store)[0]['sources'][0]['excerpt'].endswith('今后都一样。')


def test_personal_reply_still_cannot_pass_without_positive_rule_basis(store):
    add(store,'能帮我看看文件吗？',1)
    add(store,'你先给我看看就好。',1,sender='乙',minute=2)
    def observe(p):return propose(p,category='rule',body='本群文件都先给乙查看。')
    def check(p):
        v=accept(p);v['rule_support']=dict(scope=[],persistent=[],announcement=[],endorsements=[])
        return v
    run(store,Gateway(store,observe,check))
    assert not projection(store)



def test_removing_whole_bare_use_sentence_cannot_still_claim_use_and_definition():
    p=saying_payload();s=p['memory']['sources'][0]
    s.update(excerpt='先搭桥。搭桥就是把双方的意思连接起来。',context='先搭桥。搭桥就是把双方的意思连接起来。')
    s['segments']=ep._inc_segments(s['excerpt'])
    v=saying_vote(p)
    v['safe_sources'][0]={'source_id':'s1','start_segment':1,'end_segment':2}
    with pytest.raises(ValueError,match='use_sentence_removed'):
        ep.validate_incremental_admission(v,p)


def test_definition_only_can_drop_bare_use_if_two_other_real_uses_remain():
    p=saying_payload();s=p['memory']['sources'][0]
    s.update(excerpt='先搭桥。搭桥就是把双方的意思连接起来。',context='先搭桥。搭桥就是把双方的意思连接起来。')
    s['segments']=ep._inc_segments(s['excerpt'])
    second=p['memory']['sources'][1]
    second.update(excerpt='搭桥一下。',context='搭桥一下。',segments=['搭桥一下。'])
    v=saying_vote(p);v['safe_sources'][0]={'source_id':'s1','start_segment':1,'end_segment':2}
    v['usage_reviews'][0]['role']='definition';v['usage_reviews'][1]['role']='use'
    assert ep.validate_incremental_admission(v,p)['decision']=='accept'
