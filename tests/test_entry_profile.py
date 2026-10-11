"""Entry profiles: deterministic evidence, bounded checks and publication, no network."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from iris.db import dumps
from iris.models import ModelError, ModelReply
from iris.entry_profile import (
    EntryProfileEngine, EntryProfileConflict, current_profile, select_material,
    profile_due, profile_settings, set_settings, admin_edit, list_versions,
    get_version, version_diff, rollback, confirm_candidate, reject_candidate,
)


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    def __call__(self):
        return self.value
    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


@pytest.fixture
def clock():
    return Clock()


def messages(store, clock, count=50, *, entry='group', kind='group', days_ago=0, text='测试消息', message_kind='message'):
    ids=[]
    with store.write() as conn:
        conn.execute('INSERT OR IGNORE INTO entries(id,name,platform,kind) VALUES(?,?,?,?)', (entry,entry,'test',kind))
        for i in range(count):
            stamp=(clock()-timedelta(days=days_ago,minutes=count-i)).isoformat()
            ids.append(conn.execute('''INSERT INTO messages(entry_id,kind,sender_subject_id,content,occurred_at,received_at,dedupe_key)
                VALUES(?,?,'self',?,?,?,?)''',(entry,message_kind,f'{text}{i}',stamp,stamp,f'{stamp}:{message_kind}:{i}')).lastrowid)
    return ids


def memory(store, clock, mids, *, importance=50, text='本群聊到过照片整理', lifecycle='active'):
    stamp=clock().isoformat()
    with store.write() as conn:
        mid=conn.execute('''INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,retention,lifecycle,
            created_at,updated_at,first_confirmed_at,last_confirmed_at) VALUES(?,'事实','self','亲历',90,?,80,?,?,?,?,?)''',
            (text,importance,lifecycle,stamp,stamp,stamp,stamp)).lastrowid
        for msg in mids:
            conn.execute("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,?)",(mid,msg,stamp))
    return mid


TEXTS = [
    '这次聊天里大家围绕照片的整理方式交换了意见，并分别描述了自己当时的具体做法。',
    '这次聊天里出现了询问拍摄日期的互动，发言者给出了日期后，话题才继续往下展开。',
    '这次聊天里有人用不同相册区分照片，相关表达仅描述当次对话，没有说明长期习惯。',
    '这次聊天里交流中保留了尚未确定的信息，参与者在接话时说明了自己掌握的范围。',
    '这次聊天里可见的发言主要围绕照片展开，具体含义需要结合当时的前后文来理解。',
]


class Model:
    def __init__(self, store, *, texts=None, bad=(), violations=(), degree='small', hook=None, refs=None):
        self.store=store; self.texts=TEXTS if texts is None else texts
        self.bad=set(bad); self.violations=violations; self.degree=degree
        self.hook=hook; self.calls=[]; self.refs=refs
    def chat(self, messages, purpose, max_tokens=16000, **kwargs):
        assert not self.store._writer.in_transaction
        payload=json.loads(messages[-1]['content'])
        self.calls.append((purpose,payload,kwargs))
        if self.hook:
            hook,self.hook=self.hook,None
            hook()
        if purpose.endswith('generate'):
            value={'sentences':[{'text':t,'basis':self.refs or ['S1']} for t in self.texts]}
        else:
            value={'sentences':[{'index':i+1,'supported':i not in self.bad,
                'scene_qualified':i not in self.bad,'violations':list(self.violations) if i in self.bad else [],
                'reason':'假模型检查结果'} for i,_ in enumerate(payload['candidate'])],
                'change_degree':self.degree,'reason':'假模型变化判断'}
        return ModelReply(dumps(value),'stop',{'prompt_tokens':10,'completion_tokens':10})


def generate(store, clock, **kwargs):
    messages(store,clock)
    model=Model(store,**kwargs)
    result=EntryProfileEngine(store,model,clock=clock).regenerate('group',expected_version=0)
    return result,model


def test_new_defaults_and_no_read_side_effects(store, clock):
    messages(store,clock,1)
    assert store.setting('entry_profile')['publish_mode']=='check_auto'
    assert profile_settings(store,'group')['enabled'] is True
    assert current_profile(store,'group') is None
    assert list_versions(store,'group')==[]
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM entry_profile_versions').fetchone()[0]==0


def test_material_is_local_visible_active_and_bounded(store, clock):
    own=messages(store,clock,days_ago=1)
    other=messages(store,clock,entry='other')
    old=messages(store,clock,2,days_ago=15)
    messages(store,clock,2,message_kind='event')
    messages(store,clock,2,message_kind='action_result')
    high=memory(store,clock,own[:1],importance=90)
    low=memory(store,clock,old,importance=20)
    memory(store,clock,other,importance=100)
    memory(store,clock,own[1:2],lifecycle='deleted')
    restricted=memory(store,clock,[own[2],other[0]],importance=99)
    with store.write() as conn:
        conn.execute("UPDATE entries SET visibility='entry_only' WHERE id='other'")
    material=select_material(store,'group',clock=clock)
    assert material==select_material(store,'group',clock=clock)
    assert {s['message_id'] for s in material['messages']}<=set(own)
    assert [m['memory_id'] for m in material['memories']]==[high,low]
    assert restricted not in [m['memory_id'] for m in material['memories']]
    assert material['message_tokens']<=6000 and material['memory_tokens']<=2000
    assert material['window_message_count']==50


def test_daily_sampling_includes_quiet_days_and_truncates_without_rewriting(store,clock):
    for day in range(14):
        messages(store,clock,60 if day==0 else 1,days_ago=day,text='很长的原始消息'*300)
    material=select_material(store,'group',clock=clock)
    assert len({x['date'] for x in material['messages']})==14
    assert material['message_tokens']<=6000
    assert all('很长的原始消息' in x['content'] for x in material['messages'])


def test_due_thresholds_daily_limit_and_generation_time(store,clock):
    messages(store,clock,49)
    assert profile_due(store,'group',clock=clock)['reason']=='insufficient_messages'
    messages(store,clock,1,text='额外消息')
    assert profile_due(store,'group',clock=clock)['due']
    model=Model(store)
    engine=EntryProfileEngine(store,model,clock=clock)
    first=engine.update('group',expected_version=0)['version']
    assert first['status']=='published'
    assert profile_due(store,'group',clock=clock)['reason']=='daily_limit'
    clock.advance(hours=167)
    messages(store,clock,1,text='七天前一小时')
    assert not profile_due(store,'group',clock=clock)['due']
    clock.advance(hours=1)
    assert profile_due(store,'group',clock=clock)['due']
    assert current_profile(store,'group')['generated_at']==first['generated_at']


def test_private_entry_never_generates_or_accepts_admin_profile(store,clock):
    messages(store,clock,kind='private')
    model=Model(store)
    result=EntryProfileEngine(store,model,clock=clock).regenerate('group',expected_version=0)
    assert result['status']=='skipped' and result['reason']=='entry_kind'
    assert not model.calls
    with pytest.raises(ValueError):
        admin_edit(store,'group',''.join(TEXTS),expected_version=0,clock=clock)


def test_generate_check_publish_records_evidence_and_budget(store,clock):
    result,model=generate(store,clock)
    version=result['version']
    assert version['status']=='published' and version['author']=='model'
    assert version['checks']['passed']
    assert len(version['sentences'])==5
    assert version['sentences'][0]['basis'][0]['message_id']
    assert version['sentences'][0]['basis'][0]['sha256']
    assert all('S1' not in s['text'] for s in version['sentences'])
    assert [p for p,_,_ in model.calls]==['consolidation_entry_profile_generate','consolidation_entry_profile_check']
    assert all(k['_deadline'] for _,_,k in model.calls)
    assert len(list_versions(store,'group'))==1
    assert get_version(store,'group',version['version'])==version


@pytest.mark.parametrize('bad,status,removed',[((), 'published',0),((0,), 'published',1),((0,1),'rejected',2)])
def test_sentence_deletion_threshold(store,clock,bad,status,removed):
    result,_=generate(store,clock,bad=bad)
    version=result['version']
    assert version['status']==status
    assert len(version['checks']['deleted_sentences'])==removed
    assert (current_profile(store,'group') is not None)==(status=='published')
    assert all(TEXTS[i] not in version['content'] for i in bad)
    if status=='rejected':
        assert 'too_many_deleted' in version['checks']['errors']


def test_unknown_references_and_short_output_reject(store,clock):
    result,model=generate(store,clock,refs=['S999'])
    assert result['version']['status']=='rejected'
    assert len(model.calls)==1
    clock.advance(days=1)
    result=EntryProfileEngine(store,Model(store,texts=['当次聊天谈了照片。']),clock=clock).regenerate('group',expected_version=0)
    assert result['version']['status']=='rejected'
    assert 'too_short' in result['version']['checks']['errors']


def test_all_manual_confirmation_rejection_and_current_cas(store,clock):
    messages(store,clock)
    set_settings(store,'group',expected_version=0,publish_mode='all_manual',clock=clock)
    engine=EntryProfileEngine(store,Model(store),clock=clock)
    result=engine.regenerate('group',expected_version=0)
    version=result['version']['version']
    assert result['status']=='candidate' and current_profile(store,'group') is None
    published=confirm_candidate(store,'group',version,expected_version=0,clock=clock)
    assert published['generated_at']==result['version']['generated_at']
    with pytest.raises(EntryProfileConflict):
        admin_edit(store,'group',''.join(TEXTS),expected_version=0,clock=clock)
    clock.advance(days=1)
    pending=engine.regenerate('group',expected_version=version)['version']
    reject_candidate(store,'group',pending['version'],expected_version=version,clock=clock)
    assert current_profile(store,'group')['version']==version


def test_admin_sentences_preserved_by_code_rollback_and_diff(store,clock):
    result,_=generate(store,clock)
    first=result['version']['version']
    edited_text=TEXTS[0].replace('照片','相册')+''.join(TEXTS[1:])
    edited=admin_edit(store,'group',edited_text,expected_version=first,clock=clock)
    assert edited['sentences'][0]['author']=='admin'
    assert all(s['author']=='model' for s in edited['sentences'][1:])
    clock.advance(days=1)
    result=EntryProfileEngine(store,Model(store),clock=clock).regenerate('group',expected_version=edited['version'])
    assert edited['sentences'][0]['text'] in result['version']['content']
    diff=version_diff(store,'group',first,edited['version'])
    assert diff['changed']
    restored=rollback(store,'group',first,expected_version=result['version']['version'],clock=clock)
    assert restored['content']==get_version(store,'group',first)['content']
    assert restored['version']>result['version']['version']
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_operations WHERE object_type='entry_profile'").fetchone()[0]>=4


@pytest.mark.parametrize('mutation',['message','memory','visibility','settings','current'])
def test_concurrent_changes_never_publish_stale_generation(store,clock,mutation):
    own=messages(store,clock)
    mid=memory(store,clock,own[:1])
    def change():
        if mutation=='settings':
            set_settings(store,'group',expected_version=0,enabled=False,clock=clock)
        elif mutation=='current':
            admin_edit(store,'group',''.join(TEXTS),expected_version=0,clock=clock)
        else:
            with store.write() as conn:
                if mutation=='message': conn.execute('UPDATE messages SET content=? WHERE id=?',('改过的原话',own[0]))
                if mutation=='memory': conn.execute('UPDATE memories SET revision=revision+1 WHERE id=?',(mid,))
                if mutation=='visibility': conn.execute("DELETE FROM sources WHERE memory_id=?",(mid,))
    refs=['M1'] if mutation in ('memory','visibility') else ['S1']
    model=Model(store,hook=change,refs=refs)
    with pytest.raises(EntryProfileConflict):
        EntryProfileEngine(store,model,clock=clock).regenerate('group',expected_version=0)
    assert current_profile(store,'group') is None or mutation=='current'


@pytest.mark.parametrize('change',['message_cleanup','memory_delete','memory_forget','memory_edit'])
def test_possibly_stale_computed_without_rewriting(store,clock,change):
    own=messages(store,clock)
    mid=memory(store,clock,own[:1])
    version=EntryProfileEngine(store,Model(store,refs=['M1']),clock=clock).regenerate('group',expected_version=0)['version']
    with store.write() as conn:
        if change=='message_cleanup':
            conn.execute('DELETE FROM sources WHERE message_id=?',(own[0],))
            conn.execute('DELETE FROM messages WHERE id=?',(own[0],))
        elif change=='memory_edit': conn.execute('UPDATE memories SET revision=revision+1 WHERE id=?',(mid,))
        else: conn.execute('UPDATE memories SET lifecycle=? WHERE id=?',('deleted' if change=='memory_delete' else 'forgotten',mid))
    current=current_profile(store,'group')
    assert current['possibly_stale'] and current['stale_basis']
    assert current['content']==version['content']
    assert len(list_versions(store,'group'))==1


@pytest.mark.parametrize('reason',['paused','usage_limit'])
def test_health_gate_skips_without_model(store,clock,reason):
    messages(store,clock)
    model=Model(store)
    def blocked(*a,**kw):
        raise ModelError('paused','not available',paused=True,reason=reason)
    model.health=SimpleNamespace(check=blocked)
    result=EntryProfileEngine(store,model,clock=clock).regenerate('group',expected_version=0)
    assert result['status']=='skipped' and result['reason']==reason and not model.calls


def test_checker_timeout_records_rejected_candidate_with_old_current_intact(store,clock):
    messages(store,clock)
    model=Model(store)
    original=model.chat
    def chat(messages,purpose,*args,**kwargs):
        if purpose.endswith('check'):
            raise ModelError('network','deadline exceeded',reason='timeout')
        return original(messages,purpose,*args,**kwargs)
    model.chat=chat
    result=EntryProfileEngine(store,model,clock=clock).regenerate('group',expected_version=0)
    assert result['version']['status']=='rejected'
    assert result['version']['checks']['reason']=='timeout'
    assert current_profile(store,'group') is None


@pytest.mark.parametrize('bad,expected',[(range(3),'published'),(range(4),'rejected')])
def test_exact_thirty_percent_boundary(store,clock,bad,expected):
    texts=[f'本次第{i+1}段讨论中，发言者围绕照片的整理和展示办法进行了具体交流。' for i in range(10)]
    result,_=generate(store,clock,texts=texts,bad=bad)
    assert result['status']==expected
    assert len(result['version']['checks']['deleted_sentences'])==len(bad)


def test_admin_warning_retains_sentence_and_requires_confirmation(store,clock):
    messages(store,clock)
    admin=admin_edit(store,'group',''.join(TEXTS),expected_version=0,clock=clock)
    model=Model(store,texts=[],bad=range(5),violations=['个人评价'])
    result=EntryProfileEngine(store,model,clock=clock).regenerate('group',expected_version=admin['version'])
    assert result['status']=='candidate'
    assert result['version']['content']==admin['content']
    assert not result['version']['checks']['deleted_sentences']
    assert result['version']['checks']['admin_warnings']
    assert current_profile(store,'group')['version']==admin['version']


def test_rejected_candidate_is_not_a_rollback_target(store,clock):
    result,_=generate(store,clock,bad=(0,1))
    with pytest.raises(ValueError):
        rollback(store,'group',result['version']['version'],expected_version=0,clock=clock)


def test_confirm_rejects_changed_evidence(store,clock):
    own=messages(store,clock)
    set_settings(store,'group',expected_version=0,publish_mode='all_manual',clock=clock)
    pending=EntryProfileEngine(store,Model(store),clock=clock).regenerate('group',expected_version=0)['version']
    with store.write() as conn:conn.execute('DELETE FROM messages WHERE id=?',(own[0],))
    with pytest.raises(EntryProfileConflict):
        confirm_candidate(store,'group',pending['version'],expected_version=0,clock=clock)


def test_current_context_is_entry_local_disabled_and_readonly(store,clock):
    from iris.entry_profile import profile_context
    generate(store,clock);messages(store,clock,entry='other')
    with store.read() as conn:
        assert set(profile_context(conn,'group'))=={'text','version','generated_at','possibly_stale'}
        assert profile_context(conn,'other') is None
    set_settings(store,'group',expected_version=1,enabled=False,clock=clock)
    with store.read() as conn:assert profile_context(conn,'group') is None
    assert current_profile(store,'group') is not None


def test_second_generation_after_fifty_new_messages_and_manual_daily_cap(store,clock):
    first,_=generate(store,clock)
    same=EntryProfileEngine(store,Model(store),clock=clock).regenerate('group',expected_version=first['version']['version'])
    assert same['reason']=='daily_limit'
    clock.advance(days=1);messages(store,clock,50,text='新一日消息')
    assert profile_due(store,'group',clock=clock)['due']


def test_concurrent_generation_reservation_prevents_second_call(store,clock):
    from iris.entry_profile import EntryProfileBusy
    messages(store,clock)
    second=Model(store); engine=EntryProfileEngine(store,second,clock=clock)
    def overlap():
        result=engine.regenerate('group',expected_version=0)
        assert result['reason']=='daily_limit'
    model=Model(store,hook=overlap)
    result=EntryProfileEngine(store,model,clock=clock).regenerate('group',expected_version=0)
    assert result['status']=='published' and not second.calls


def test_gateway_retries_share_budget_and_do_not_hold_transaction(store,clock):
    import httpx
    from iris.models import Gateway
    from test_models import configs, response
    messages(store,clock)
    ticks=[0.0]; deadlines=[]; requests=[]; fake=Model(store)
    def handle(request):
        assert not store._writer.in_transaction
        requests.append(request)
        deadlines.append(request.extensions['timeout']['read'])
        if len(requests)==1:
            ticks[0]+=100
            return httpx.Response(503)
        ticks[0]+=3
        body=json.loads(request.content)
        kind='generate' if len(requests)==2 else 'check'
        reply=fake.chat(body['messages'],'consolidation_entry_profile_'+kind)
        return httpx.Response(200,json=response(reply.content,usage=reply.usage))
    gateway=Gateway(configs(),store,client=httpx.Client(transport=httpx.MockTransport(handle)),
        monotonic=lambda:ticks[0],sleeper=lambda seconds:ticks.__setitem__(0,ticks[0]+seconds),jitter=lambda *args:0)
    try:
        result=EntryProfileEngine(store,gateway,clock=clock).regenerate('group',expected_version=0)
        assert result['status']=='published'
        assert deadlines[0]<=120 and deadlines[1]<=20 and deadlines[2]<=120
        with store.read() as conn:
            rows=conn.execute('SELECT purpose,finish_reason,completion_tokens FROM model_calls ORDER BY id').fetchall()
        assert len(rows)==3 and rows[-1]['purpose']=='consolidation_entry_profile_check'
        assert rows[-1]['finish_reason']=='stop' and rows[-1]['completion_tokens']==10
    finally:
        gateway.close()


def test_gateway_daily_limit_rechecked_before_checker(store,clock):
    import httpx
    from iris.models import Gateway
    from iris.model_health import ModelHealth
    from test_models import configs, response
    messages(store,clock);fake=Model(store);seen=[]
    health=ModelHealth(store,configs(),clock=clock);health.set_daily_token_limit(1)
    def handle(request):
        seen.append(request)
        reply=fake.chat(json.loads(request.content)['messages'],'consolidation_entry_profile_generate')
        return httpx.Response(200,json=response(reply.content,usage=reply.usage))
    gateway=Gateway(configs(),store,health=health,clock=clock,client=httpx.Client(transport=httpx.MockTransport(handle)))
    try:
        result=EntryProfileEngine(store,gateway,clock=clock).regenerate('group',expected_version=0)
        assert result['status']=='rejected' and result['reason']=='usage_limit'
        assert len(seen)==1 and current_profile(store,'group') is None
    finally:
        gateway.close()


@pytest.mark.parametrize('size',[149,150,500,501])
def test_admin_length_boundaries(store,clock,size):
    messages(store,clock,1)
    text='字'*(size-1)+'。'
    if size in (150,500):
        assert admin_edit(store,'group',text,expected_version=0,clock=clock)['content']==text
    else:
        with pytest.raises(ValueError):admin_edit(store,'group',text,expected_version=0,clock=clock)


def test_stale_evidence_is_absorbed_next_dream_without_new_chat(store,clock):
    own=messages(store,clock)
    first=EntryProfileEngine(store,Model(store),clock=clock).regenerate('group',expected_version=0)['version']
    linked=first['sentences'][0]['basis'][0]['message_id']
    with store.write() as conn:conn.execute('DELETE FROM messages WHERE id=?',(linked,))
    assert profile_due(store,'group',clock=clock)['reason']=='daily_limit'
    clock.advance(days=1)
    assert profile_due(store,'group',clock=clock)['due']
    next_version=EntryProfileEngine(store,Model(store),clock=clock).update('group',expected_version=first['version'])['version']
    assert next_version['status']=='published' and not next_version['possibly_stale']
    assert all(b.get('message_id')!=linked for sentence in next_version['sentences'] for b in sentence['basis'])


def test_update_counts_all_new_messages_since_version_not_only_window(store,clock):
    first,_=generate(store,clock)
    clock.advance(days=1);messages(store,clock,50,text='上一版之后的新消息')
    clock.advance(days=15)
    due=profile_due(store,'group',clock=clock)
    assert due['new_messages']==50 and due['due']
    result=EntryProfileEngine(store,Model(store),clock=clock).update('group',expected_version=first['version']['version'])
    assert result['status']=='skipped' and result['reason']=='no_evidence'


def test_check_input_groups_only_current_bound_evidence_per_sentence(store,clock):
    own=messages(store,clock)
    memory(store,clock,own[2:4])
    model=Model(store)
    original=model.chat
    def chat(messages,purpose,*args,**kwargs):
        reply=original(messages,purpose,*args,**kwargs)
        if purpose.endswith('generate'):
            value=json.loads(reply.content)
            for row,refs in zip(value['sentences'],[['S1'],['M1'],['S2'],['S1','M1'],['S2']],strict=True):
                row['basis']=refs
            return ModelReply(dumps(value),'stop',{})
        return reply
    model.chat=chat
    result=EntryProfileEngine(store,model,clock=clock).regenerate('group',expected_version=0)
    payload=model.calls[1][1]
    assert 'evidence' not in payload
    assert result['version']['checks']['model_input']==payload
    assert [s['index'] for s in payload['candidate']]==[1,2,3,4,5]
    assert [s['ref'] for s in payload['candidate'][0]['evidence']['messages']]==['S1']
    assert payload['candidate'][0]['evidence']['memories']==[]
    assert payload['candidate'][1]['evidence']['messages']==[]
    sources=payload['candidate'][1]['evidence']['memories'][0]['source_messages']
    assert [s['message_id'] for s in sources]==own[2:4]
    assert sources[0]['speaker']=='self' and sources[0]['content']
    assert 'sha256' not in dumps(payload)
    assert all(s['ref'] in sentence['refs'] for sentence in payload['candidate']
               for kind in ('messages','memories') for s in sentence['evidence'][kind])


def test_unqualified_scene_is_deleted_even_with_multiple_message_references(store,clock):
    messages(store,clock)
    model=Model(store,refs=['S1','S2'])
    original=model.chat
    def chat(messages,purpose,*args,**kwargs):
        reply=original(messages,purpose,*args,**kwargs)
        if purpose.endswith('check'):
            value=json.loads(reply.content)
            value['sentences'][0]['scene_qualified']=False
            return ModelReply(dumps(value),'stop',{})
        return reply
    model.chat=chat
    result=EntryProfileEngine(store,model,clock=clock).regenerate('group',expected_version=0)
    deleted=result['version']['checks']['deleted_sentences']
    assert len(deleted)==1 and deleted[0]['sentence']['text']==TEXTS[0]


def test_check_input_keeps_admin_sentences_without_borrowing_evidence(store,clock):
    messages(store,clock)
    first=admin_edit(store,'group',''.join(TEXTS),expected_version=0,clock=clock)
    model=Model(store,texts=[])
    result=EntryProfileEngine(store,model,clock=clock).regenerate('group',expected_version=first['version'])
    payload=model.calls[1][1]
    assert [s['text'] for s in payload['candidate']]==TEXTS
    assert all(s['author']=='admin' and s['evidence']=={'messages':[],'memories':[]} for s in payload['candidate'])
    assert result['version']['content']==first['content']
