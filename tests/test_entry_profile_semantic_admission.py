"""EF-22 proof contracts; hand-written examples, no real model calls."""
import copy
import json

import pytest
from iris import entry_profile as ep
from iris.db import dumps
from test_entry_profile_incremental import NOW, Gateway, add, profiles, projection, propose, run, source
from test_entry_profile_incremental_quality import seed, check_seed, small_evidence_budget


def payload(category='topic', body='比较容器的密封性；比较容器的清洗难度。'):
    sources=[dict(source_id='s1',message_id=11,field='content',excerpt='容器密封性如何，清洗方便吗？',speaker='甲',date='2026-10-01',occurred_at='2026-10-01T12:00:00+08:00',context='容器密封性如何，清洗方便吗？',message_speaker='甲',quote=None,burst_id='a',group_id=11,count_eligible=True),
             dict(source_id='s2',message_id=12,field='content',excerpt='这一种密封更好，但拆开清洗较费事。',speaker='乙',date='2026-10-01',occurred_at='2026-10-01T12:01:00+08:00',context='这一种密封更好，但拆开清洗较费事。',message_speaker='乙',quote=None,burst_id='b',group_id=12,count_eligible=True),
             dict(source_id='s3',message_id=21,field='content',excerpt='另一款容器的密封和清洗应怎样取舍？',speaker='丙',date='2026-10-04',occurred_at='2026-10-04T12:00:00+08:00',context='另一款容器的密封和清洗应怎样取舍？',message_speaker='丙',quote=None,burst_id='c',group_id=21,count_eligible=True)]
    for s in sources:s['segments']=ep._inc_segments(s['excerpt'])
    m=dict(memory_id=1,revision=1,category=category,body=body,saying='',meaning='',definition_refs=[],sources=sources)
    m['proof_points']=ep._inc_proof_points(body,category)
    m['discussion_groups']=[dict(group_id=s['group_id'],date=s['date'],source_ids=[s['source_id']],eligible=True) for s in sources]
    return dict(review_contract='semantic_v8',mode='final',memory=m)


def vote(p):
    m=p['memory']
    return dict(decision='accept',violations=[],reason='每项核对。',safe_sources=[{k:s[k] for k in ('message_id','field','excerpt')} for s in m['sources']],
        point_support=[dict(point_id=x['point_id'],source_ids=[s['source_id'] for s in m['sources']]) for x in m['proof_points']],
        group_reviews=[dict(group_id=g['group_id'],discussion_id=g['group_id'],same_topic=True,independent=True,focus='同一主题',reason='重新发起') for g in m['discussion_groups']] if m['category']=='topic' else [],
        usage_reviews=[],rule_support={})


def test_topic_answer_supports_group_without_becoming_new_discussion():
    p=payload();v=vote(p)
    v['group_reviews'][1].update(discussion_id=11,independent=False,reason='回答第一组的问题')
    # Each point is proved by an answer + the later independent discussion.
    for point in v['point_support']:point['source_ids']=['s2','s3']
    assert ep.validate_incremental_admission(v,p)['decision']=='accept'


@pytest.mark.parametrize('failure',['missing_point','single_group','foreign_source','missing_group','followup','other_topic'])
def test_topic_rejects_incomplete_or_non_independent_proof(failure):
    p=payload();v=vote(p)
    if failure=='missing_point':v['point_support'].pop()
    if failure=='single_group':v['point_support'][-1]['source_ids']=['s1','s2']
    if failure=='foreign_source':v['point_support'][0]['source_ids']=['outside']
    if failure=='missing_group':v['group_reviews'].pop()
    if failure=='followup':v['group_reviews'][2].update(independent=False,discussion_id=11)
    if failure=='other_topic':v['group_reviews'][2]['same_topic']=False
    with pytest.raises(ValueError):ep.validate_incremental_admission(v,p)


def saying_payload():
    p=payload('saying','搭桥：把双方的意思连接起来')
    p['memory'].update(saying='搭桥',meaning='把双方的意思连接起来',definition_refs=[11],discussion_groups=[])
    for s,text in zip(p['memory']['sources'],['搭桥就是把双方的意思连接起来。','搭桥这个词是哪来的？','来搭桥吧']):
        s.update(excerpt=text,context=text,segments=ep._inc_segments(text))
    return p


def saying_vote(p):
    v=vote(p);v['usage_reviews']=[dict(source_id=s['source_id'],role=role,reason='按该来源实际用法判断') for s,role in zip(p['memory']['sources'],['use_and_definition','mention','use'])]
    for point in v['point_support']:point['source_ids']=['s1']
    return v


def test_saying_mention_cannot_replace_retained_second_use():
    p=saying_payload();v=saying_vote(p);v['safe_sources'].pop()
    with pytest.raises(ValueError,match='use'):ep.validate_incremental_admission(v,p)


def test_saying_meaning_cannot_borrow_removed_definition():
    p=saying_payload();v=saying_vote(p);v['safe_sources'].pop(0)
    with pytest.raises(ValueError):ep.validate_incremental_admission(v,p)


def test_saying_two_uses_and_bound_meaning_pass():
    p=saying_payload();assert ep.validate_incremental_admission(saying_vote(p),p)['decision']=='accept'


def test_rule_requires_positive_scope_persistence_and_announcement():
    p=payload('rule','公开文件必须征得作者同意。');p['memory']['discussion_groups']=[]
    v=vote(p)
    with pytest.raises(ValueError,match='rule'):ep.validate_incremental_admission(v,p)
    v['rule_support']=dict(scope=['s1'],persistent=['s1'],announcement=['s1'],endorsements=[])
    assert ep.validate_incremental_admission(v,p)['decision']=='accept'


def test_format_repair_cannot_flip_group_rejection():
    p=payload();v=vote(p);old=copy.deepcopy(v);old['group_reviews'][-1]['same_topic']=False
    with pytest.raises(ValueError,match='repair'):ep.validate_incremental_admission(v,p,old)


def test_map_page_may_lack_another_group_without_rejecting_whole_body():
    p=payload();p['mode']='map';p['memory']['sources']=p['memory']['sources'][:1];p['memory']['discussion_groups']=p['memory']['discussion_groups'][:1]
    v=vote(p);v['point_support'][-1]['source_ids']=[]
    assert ep.validate_incremental_admission(v,p)['decision']=='accept'
    p['mode']='final'
    with pytest.raises(ValueError):ep.validate_incremental_admission(v,p)


@pytest.mark.parametrize('text',['其他人的作品比较。','利他行为的讨论。','他山之石的含义。','吉他的保养。'])
def test_neutral_word_is_not_a_gendered_pronoun(text):
    assert not ep._inc_gendered(text)


@pytest.mark.parametrize('text',['她喜欢此类作品。','他总会先处理。','他们都使用该办法。','her preferred method'])
def test_real_gendered_pronoun_remains_blocked(text):
    assert ep._inc_gendered(text)


@pytest.mark.parametrize('body',['进一步比较不同材质。','一部分内容的衔接。','同一种设计的取舍。','一般线路的选择。'])
def test_grammatical_numeral_fragment_is_not_specific_quantity(body):
    assert not ep._inc_missing_entities(body,['讨论材料及设计的选择。'])


def test_real_numbers_and_titles_still_need_bound_evidence():
    assert ep._inc_missing_entities('35毫米与50毫米镜头，作品《云梯》。',['35毫米镜头。'])==['云梯','50']


def test_saying_body_is_constructed_from_fields_instead_of_independent_generation(store):
    add(store,'搭桥就是把双方的意思接起来。')
    def observe(p):
        v=propose(p,category='saying',body='模型把正文写成了另一句话')
        v['observations'][0].update(saying='搭桥',meaning='把双方的意思接起来',definition_refs=[p['messages'][0]['message_id']])
        return v
    run(store,Gateway(store,observe))
    assert profiles(store)[0]['body']=='搭桥：把双方的意思接起来'


def test_source_range_uses_original_contiguous_segments_and_author(store):
    add(store,'开场提醒。以后分享文件须先问作者。谢谢。')
    def observe(p):
        v=propose(p,category='rule',body='分享文件须先问作者。')
        m=p['messages'][0]
        v['observations'][0]['source_refs']=[dict(message_id=m['message_id'],field='content',start_segment=1,end_segment=2)]
        return v
    run(store,Gateway(store,observe))
    assert profiles(store)[0]['sources'][0]['excerpt']=='以后分享文件须先问作者。'


def test_all_category_rejections_and_removed_proposals_get_bounded_feedback(store):
    add(store,'说法小结。')
    def observe(p):
        v=propose(p,category='saying',body='搭桥');v['observations'][0].update(saying='搭桥')
        v['observations'][0]['source_refs'][0]['excerpt']='不存在的原话'
        return v
    run(store,Gateway(store,observe))
    add(store,'继续讨论。',3,sender='乙')
    g=Gateway(store,lambda p:{'observations':[]});run(store,g)
    assert g.calls[0]['payload']['feedback']
    assert g.calls[0]['payload']['feedback'][0]['evidence'] is False


def test_irrelevant_mapping_page_can_be_empty_without_publishing():
    p=payload();p['mode']='map';v=vote(p);v['safe_sources']=[]
    for point in v['point_support']:point['source_ids']=[]
    for group in v['group_reviews']:group.update(same_topic=False,independent=False)
    assert ep.validate_incremental_admission(v,p)['decision']=='accept'


def test_derived_saying_body_obeys_length_limit():
    value={'observations':[dict(op='propose',category='saying',body='',saying='搭桥',meaning='连接含义'*50,
        definition_refs=[1],source_refs=[dict(message_id=1,field='content',excerpt='解释')])]}
    with pytest.raises(ValueError):ep.validate_incremental_observations(value)


def test_saying_usage_role_invalid_type_is_a_format_error():
    p=saying_payload();v=saying_vote(p);v['usage_reviews'][0]['role']=[]
    with pytest.raises(ValueError):ep.validate_incremental_admission(v,p)


def test_proof_planner_preserves_all_conjunctive_definition_sources():
    p=saying_payload();m=p['memory'];v=saying_vote(p)
    # The definition needs two bound clauses, not either source by itself.
    m['definition_refs']=[11,12];v['usage_reviews'][1]['role']='definition'
    v['point_support'][0]['source_ids']=['s1','s2']
    selected,reason=ep._inc_plan_proof(m,[v],lambda ss:len(ss)<=3)
    assert reason is None and {s['source_id'] for s in selected}=={'s1','s2','s3'}
    selected,reason=ep._inc_plan_proof(m,[v],lambda ss:len(ss)<=2)
    assert selected is None and reason=='proof_gap:complete_evidence_budget'


def test_proof_planner_does_not_treat_two_partial_points_as_a_complete_theme():
    p=payload();v=vote(p)
    v['point_support'][0]['source_ids']=['s1']
    v['point_support'][1]['source_ids']=['s3']
    selected,reason=ep._inc_plan_proof(p['memory'],[v],lambda ss:True)
    assert selected is None and reason=='proof_gap:independent_point_support'


def test_question_and_reply_stay_in_same_proof_bundle_without_extra_count(store):
    from iris.memory_ops import entry_profile_source,write_entry_profile_memory
    a=add(store,'这种容器的密封和清洗怎样取舍？',1)
    b=add(store,'密封圈容易清洗，但要检查接口。',1,sender='乙',minute=2)
    c=add(store,'另一种容器的密封与清洗值得比较。',4,sender='丙')
    with store.write() as conn:
        original=conn.execute('SELECT sender_subject_id,content FROM messages WHERE id=?',(a,)).fetchone()
        conn.execute('UPDATE messages SET quote_author_subject_id=?,quote_content=? WHERE id=?',(*original,b))
        mid=write_entry_profile_memory(store,'group',op='new',content='容器的密封与清洗。',category='topic',importance=30,
            sources=[entry_profile_source(conn,x) for x in (a,b,c)],current=NOW,_conn=conn)['memory_id']
        conn.execute('UPDATE entry_profile_memory_state SET cursor=?,scan_cursor=?',(c,c))
    from test_entry_profile_incremental import accept
    def check(p):
        groups=p['memory']['discussion_groups']
        assert len(groups)==2
        first=next(g for g in groups if g['group_id']==a)
        assert len(first['source_ids'])==2
        assert not next(s for s in p['memory']['sources'] if s['message_id']==b)['count_eligible']
        v=accept(p)
        for point in v['point_support']:
            point['source_ids']=[s['source_id'] for s in p['memory']['sources'] if s['message_id'] in (b,c)]
        return v
    check_seed(store,Gateway(store,checks=check))
    assert projection(store)['memory_ids']==[mid]


def test_source_loss_cannot_replace_reviewed_use_with_a_mention(store):
    from iris.memory_ops import entry_profile_source,write_entry_profile_memory
    a=add(store,'搭桥就是把两边的意思连起来。',1)
    b=add(store,'搭桥这个词是哪来的？',4,sender='乙')
    c=add(store,'先来搭桥吧',4,sender='丙',minute=5)
    with store.write() as conn:
        mid=write_entry_profile_memory(store,'group',op='new',content='搭桥',category='saying',importance=30,
            sources=[entry_profile_source(conn,x) for x in (a,b,c)],current=NOW,_conn=conn)['memory_id']
        conn.execute('UPDATE entry_profile_memory_state SET cursor=?,scan_cursor=?',(c,c))
    from test_entry_profile_incremental import accept
    def check(p):
        v=accept(p)
        for role in v['usage_reviews']:
            sid=next(s for s in p['memory']['sources'] if s['source_id']==role['source_id'])
            role['role']='mention' if sid['message_id']==b else 'use'
        return v
    check_seed(store,Gateway(store,checks=check));assert projection(store)
    with store.write() as conn:
        conn.execute('UPDATE messages SET content=? WHERE id=?',('这条使用已失效。',c))
    assert projection(store) is None
    assert profiles(store)[0]['admission_state']=='candidate'


def test_mapping_and_final_requests_are_bounded_and_first_pass_cannot_publish(store, small_evidence_budget):
    from test_entry_profile_incremental import accept
    texts=[(1,'甲','讨论容器的密封性能。'),(2,'乙','另一个容器的密封值得比较。'),
           (3,'丙','补充容器壁厚。'),(4,'丁','补充材料颜色。'),(5,'戊','补充包装标记。'),
           (6,'己','补充尺寸。'),(7,'庚','补充收藏位置。')]
    mid,ids=seed(store,texts,body='容器的密封性能。')
    def check(p):
        v=accept(p)
        if p['mode']=='map':
            keep={s['source_id'] for s in p['memory']['sources'] if s['message_id'] in ids[:2]}
            v['safe_sources']=[r for r in v['safe_sources'] if r['message_id'] in ids[:2]]
            for point in v['point_support']:point['source_ids']=list(keep)
            for group in v['group_reviews']:
                if not keep.intersection(next(g['source_ids'] for g in p['memory']['discussion_groups'] if g['group_id']==group['group_id'])):
                    group.update(same_topic=False,independent=False)
        return v
    g=Gateway(store,checks=check);check_seed(store,g,one_round=True)
    assert not projection(store)
    check_seed(store,g)
    assert projection(store)['memory_ids']==[mid]
    assert [s['message']['position'] for s in profiles(store)[0]['sources']]==ids[:2]
    assert g.calls[-1]['payload']['mode']=='final'
    from iris.queue import estimate_tokens
    for call in g.calls:
        assert len(call['payload']['memory']['sources'])<=6
        assert estimate_tokens(dumps(call['payload']['memory']))<=2000
        assert estimate_tokens(dumps(call['payload']))+estimate_tokens(ep.incremental_prompts()['admit']['text'])<=4000


def test_old_protocol_cached_accept_is_cancelled_and_rechecked(store):
    add(store,'本群长期禁止广告。')
    run(store,Gateway(store,lambda p:propose(p,category='rule',body='禁止广告。')))
    with store.write() as conn:
        work=dict(conn.execute("SELECT * FROM entry_profile_incremental_work WHERE kind='admit'").fetchone())
        p=json.loads(work['payload_json']);p['_snapshot'].pop('admission_prompt_sha256')
        conn.execute("UPDATE entry_profile_incremental_work SET state='queued',payload_json=? WHERE id=?",(dumps(p),work['id']))
    g=Gateway(store);ep.IncrementalProfileTask(store,g,clock=lambda:NOW)._run_one(work)
    assert not g.calls
    with store.read() as conn:
        assert conn.execute('SELECT state FROM entry_profile_incremental_work WHERE id=?',(work['id'],)).fetchone()[0]=='cancelled'


def test_new_evidence_recheck_does_not_recharge_same_accepted_body(store):
    from test_entry_profile_incremental import accept
    mid,ids=seed(store,[(1,'甲','容器密封要比较接口。'),(4,'乙','另一次讨论容器密封接口。')],body='容器密封的比较。')
    check_seed(store);assert projection(store)
    with store.write() as conn:
        conn.execute('UPDATE memories SET retention=37,decay_visits=3 WHERE id=?',(mid,))
    add(store,'另一种容器也需比较密封接口。',6,sender='丙')
    g=Gateway(store,lambda p:{'observations':[dict(op='confirm',memory_id=mid,source_refs=[source(p['messages'][0])])]})
    run(store,g)
    with store.read() as conn:
        row=conn.execute('SELECT retention,decay_visits FROM memories WHERE id=?',(mid,)).fetchone()
        assert row['retention']==42 and row['decay_visits']==3
    before=len(g.calls);check_seed(store,g);assert len(g.calls)==before


def test_observation_budget_accounts_for_directory_segments_and_feedback(store):
    from iris.memory_ops import write_entry_profile_memory
    from iris.queue import estimate_tokens
    add(store,'关于材料和制作的完整讨论。'*85)
    add(store,'另一条关于材料和制作的完整讨论。'*80,sender='乙',minute=3)
    for i in range(24):
        write_entry_profile_memory(store,'group',op='new',manual=True,category=None,content=f'管理员说明{i}：'+('参考资料及交流约定。'*8))
    g=Gateway(store,lambda p:{'observations':[]});result=run(store,g)
    assert result['status']=='published' and result['reason'] is None and g.calls
    with store.read() as conn: assert ep.incremental_progress(conn,'group')['observation_gaps']==0
    for call in g.calls:
        assert estimate_tokens(ep.incremental_prompts()['observe']['text'])+estimate_tokens(dumps(call['payload']))<=6000


def test_rejection_feedback_keeps_purge_dependencies_but_hides_them_from_model(store):
    first=add(store,'只有这个消息的原话。')
    def bad(p):
        v=propose(p,category='rule',body='尚未证实的长期规则。')
        v['observations'][0]['source_refs'][0]['excerpt']='不存在的摘录'
        return v
    run(store,Gateway(store,bad))
    second=add(store,'下一天的新消息。',3,sender='乙')
    g=Gateway(store,lambda p:{'observations':[]});run(store,g)
    assert '_refs' not in dumps(g.calls[-1]['payload']['feedback'])
    with store.read() as conn:
        work=conn.execute("SELECT id,payload_json FROM entry_profile_incremental_work WHERE kind='observe' AND cursor_after=?",(second,)).fetchone()
        deps=list(conn.execute("SELECT ref_id FROM purge_record_refs WHERE table_name='entry_profile_incremental_work' AND row_id=? AND ref_kind='message'",(str(work['id']),)))
        assert str(first) in {row[0] for row in deps}


def test_complete_proof_can_need_four_dates_without_losing_a_body_point():
    p=payload();m=p['memory']
    m['sources'][1].update(date='2026-10-02',occurred_at='2026-10-02T12:00:00+08:00')
    fourth={**m['sources'][2],'source_id':'s4','message_id':31,'date':'2026-10-06','occurred_at':'2026-10-06T12:00:00+08:00',
        'group_id':31,'excerpt':'另一种容器拆洗更方便。','context':'另一种容器拆洗更方便。','burst_id':'d'}
    fourth['segments']=ep._inc_segments(fourth['excerpt']);m['sources'].append(fourth)
    v=vote(p);v['safe_sources'].append({k:fourth[k] for k in ('message_id','field','excerpt')}) if len(v['safe_sources'])==3 else None
    v['point_support'][0]['source_ids']=['s1','s2']
    v['point_support'][1]['source_ids']=['s3','s4']
    selected,reason=ep._inc_plan_proof(m,[v],lambda ss:len(ss)<=6)
    assert reason is None and {s['source_id'] for s in selected}=={'s1','s2','s3','s4'}


def test_dictionary_generic_name_tag_is_only_a_hint(monkeypatch):
    from jieba import posseg
    monkeypatch.setattr(posseg,'cut',lambda *a,**k:iter([('材料','nr'),('关系','nt')]))
    assert ep._inc_missing_entities('材料关系的比较。',['讨论不同材质怎样连接。'])==[]


def test_rule_retirement_still_needs_explicit_rule_basis():
    p=payload('rule','');p['memory'].update(action='retire',discussion_groups=[])
    v=vote(p)
    with pytest.raises(ValueError,match='rule'):ep.validate_incremental_admission(v,p)


def test_block_before_queue_gets_bounded_non_evidence_feedback(store):
    add(store,'今晚来讨论。')
    run(store,Gateway(store))
    mid=profiles(store)[0]['memory_id']
    with store.write() as conn:
        conn.execute('UPDATE entry_profile_admissions SET reason=? WHERE memory_id=?',('proof_gap:source_limit',mid))
    with store.read() as conn:
        feedback=ep._inc_feedback(conn,'group')
    blocked=next(item for item in feedback if item.get('memory_id')==mid)
    assert blocked['reason']=='proof_gap:source_limit' and blocked['evidence'] is False
    assert {'memory_id':mid} in blocked['_refs']


def test_oversegmented_body_is_a_candidate_gap_not_a_task_crash(store):
    add(store,'本入口的长期规则公告：分享前核对出处。')
    g=Gateway(store,lambda p:propose(p,category='rule',body='核对，'*25+'出处。'))
    result=run(store,g)
    assert result['status']=='published'
    with store.read() as conn:
        row=conn.execute('SELECT state,reason FROM entry_profile_admissions').fetchone()
    assert row['state']=='candidate' and row['reason']=='proof_gap:proof_point_limit'
    assert len(g.calls)==1


def test_bad_previous_proof_shape_does_not_crash_json_repair():
    p=payload();v=vote(p)
    previous={**v,'group_reviews':7,'usage_reviews':False}
    assert ep.validate_incremental_admission(v,p,previous=previous)['decision']=='accept'


@pytest.mark.parametrize('body,evidence',[
    ('比较十公里和五公里的路线。',['五公里的路线更短。']),
    ('比较一毫米的间隔。',['间隔是三毫米。']),
])
def test_quantified_units_are_not_generic_numeral_fragments(body,evidence):
    assert ep._inc_missing_entities(body,evidence)


def test_chinese_quantity_cannot_match_a_suffix_of_another_number():
    assert ep._inc_missing_entities('比较十公里路线。',['比较三十公里路线。'])
    assert ep._inc_missing_entities('一种材料和一部分结构。',['材料与结构的比较。'])==[]


@pytest.mark.parametrize('variant',['搭桥搭桥','先搭一会儿桥','搭桥一下下','搭一下桥吧'])
def test_saying_light_variants_preserve_lexical_core(variant):
    assert ep._inc_short_saying_use('搭桥',variant)


@pytest.mark.parametrize('variant',['搭桥梁','不搭桥','拆桥','搭桥收费'])
def test_saying_different_object_negation_or_extra_fact_is_not_bare_use(variant):
    assert not ep._inc_short_saying_use('搭桥',variant)


def test_identical_actual_uses_count_but_bursts_and_quotes_do_not():
    p=saying_payload();m=p['memory']
    for s in m['sources']:s.update(excerpt='搭桥',context='搭桥')
    a,b=[ep._inc_public_canonical(s) for s in (m['sources'][0],m['sources'][2])]
    assert ep._inc_enough([a,b],'saying','搭桥')
    b=copy.deepcopy(b);b['message']['occurred_at']=a['message']['occurred_at'];b['message']['date']=a['message']['date']
    assert not ep._inc_enough([a,b],'saying','搭桥')
    b=copy.deepcopy(a);b['evidence_identity']='message:21';b['message'].update(position=21,occurred_at='2026-10-04T12:00:00+08:00',date='2026-10-04')
    b['field']='quote'
    assert not ep._inc_enough([a,b],'saying','搭桥')


@pytest.mark.parametrize("semantic_link",[False,True])
def test_proof_selection_keeps_the_initiator_of_a_supporting_answer(semantic_link):
    p=payload();m=p['memory'];m['body']='比较容器的密封性。';m['proof_points']=ep._inc_proof_points(m['body'],'topic')
    if not semantic_link:m['sources'][1].update(group_id=11,count_eligible=False)
    # The answer proves the point, but its initiating question is needed to
    # establish the discussion. A shorter, unrelated question on that day
    # cannot replace that witness merely because the calendar date matches.
    stray={**m['sources'][0],'source_id':'stray','message_id':13,'group_id':13,'excerpt':'另一话题？','context':'另一话题？'}
    stray['segments']=ep._inc_segments(stray['excerpt']);m['sources'].append(stray)
    m=ep._inc_memory_page(m,m['sources']);p['memory']=m
    v=vote(p)
    v['point_support'][0]['source_ids']=['s2','s3']
    if semantic_link:v['group_reviews'][1].update(discussion_id=11,independent=False)
    selected,reason=ep._inc_plan_proof(m,[v],lambda ss:len(ss)<=3)
    assert reason is None
    assert {s['source_id'] for s in selected}=={'s1','s2','s3'}


def test_removed_initiator_cannot_keep_a_group_counting_via_its_answer():
    p=payload();m=p['memory'];m['sources'][1].update(group_id=11,count_eligible=False)
    p['memory']=ep._inc_memory_page(m,m['sources'])
    v=vote(p)
    v['safe_sources']=[r for r in v['safe_sources'] if r['message_id']!=11]
    for point in v['point_support']:point['source_ids']=['s2','s3']
    with pytest.raises(ValueError,match='group|support'):
        ep.validate_incremental_admission(v,p)
