"""Person summary mechanisms; fabricated unit fixtures, no provider or dev corpus."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from iris.db import dumps
from iris.models import ModelReply
from iris.memory_ops import purge_memory
from iris.person_summary import (
    PersonSummaryEngine, PersonSummaryConflict, current_summary, select_material,
    summary_context, summary_due, summary_settings, set_settings, admin_edit,
    get_version, list_versions, rollback, confirm_candidate,
)


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
    def __call__(self):
        return self.value
    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


@pytest.fixture
def clock():
    return Clock()


def setup_people(store, clock):
    with store.write() as conn:
        for eid in ('shared', 'restricted'):
            conn.execute("INSERT OR IGNORE INTO entries(id,name,platform,kind,visibility) VALUES(?,?, 'test','group',?)",
                         (eid,eid,'entry_only' if eid=='restricted' else 'shared'))
        for sid,name in (('a','甲同学'),('b','乙同学')):
            conn.execute("INSERT OR IGNORE INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)",
                         (sid,name,clock().isoformat()))


def memory(store, clock, text, *, speaker='a', about=(), importance=50, retention=80,
           lifecycle='active', restricted=False, days_ago=0, event_time=None):
    stamp=(clock()-timedelta(days=days_ago)).isoformat()
    with store.write() as conn:
        mid=conn.execute('''INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,retention,
            lifecycle,event_time,created_at,updated_at,first_confirmed_at,last_confirmed_at)
            VALUES(?,'事实',?,'亲历',90,?,?,?,?,?,?,?,?)''',
            (text,speaker,importance,retention,lifecycle,event_time,stamp,stamp,stamp,stamp)).lastrowid
        for sid in about:
            conn.execute('INSERT INTO memory_subjects(memory_id,subject_id) VALUES(?,?)',(mid,sid))
        if restricted:
            conn.execute("INSERT INTO memory_visibility_roots(memory_id,entry_id) VALUES(?,'restricted')",(mid,))
        return mid


def seed(store, clock, count=8):
    setup_people(store,clock)
    return [memory(store,clock,f'机制测试记忆{i}：甲同学学习木工。',days_ago=i) for i in range(count)]


class Model:
    def __init__(self, store, *, items=None, bad=(), hook=None, violations=()):
        self.store=store; self.items=items; self.bad=set(bad); self.hook=hook
        self.violations=violations; self.calls=[]
    def chat(self, messages, purpose, max_tokens=16000, **kwargs):
        assert not self.store._writer.in_transaction
        payload=json.loads(messages[-1]['content']); self.calls.append((purpose,payload,kwargs))
        if self.hook:
            hook,self.hook=self.hook,None
            hook()
        if purpose.endswith('generate'):
            value={'items':self.items if self.items is not None else [
                {'kind':'fact','text':'正在学习木工。','memory_refs':['M1']} ]}
        else:
            value={'items':[{'index':i+1,'supported':i not in self.bad,
                'scene_qualified':i not in self.bad,'violations':list(self.violations) if i in self.bad else [],
                'reason':'假模型检查'} for i in range(len(payload['candidate']))],
                'change_degree':'small','reason':'假模型变化判断'}
        return ModelReply(dumps(value),'stop',{'prompt_tokens':10,'completion_tokens':10})


def generate(store,clock,**kwargs):
    model=Model(store,**kwargs)
    return PersonSummaryEngine(store,model,clock=clock).regenerate('a',expected_version=0),model


def test_defaults_and_reads_do_not_generate(store,clock):
    seed(store,clock)
    assert store.setting('person_summary')['publish_mode']=='check_auto'
    assert store.setting('person_summary')['max_subjects_per_run']==10
    assert summary_settings(store,'a')['enabled'] is True
    assert current_summary(store,'a') is None
    assert list_versions(store,'a')==[]
    with store.read() as conn:
        assert summary_context(conn,'a','shared') is None
        assert conn.execute('SELECT COUNT(*) FROM person_summary_attempts').fetchone()[0]==0


def test_material_global_active_related_deterministic_and_bounded(store,clock):
    mids=seed(store,clock)
    hidden=memory(store,clock,'绝不能进入材料的入口限定事项',restricted=True,importance=100)
    forgotten=memory(store,clock,'遗忘内容',lifecycle='forgotten',importance=100)
    foreign=memory(store,clock,'乙同学的事项',speaker='b',importance=100)
    about=memory(store,clock,'某人的公开职业',speaker='self',about=['a'],importance=90)
    material=select_material(store,'a',clock=clock)
    assert material==select_material(store,'a',clock=clock)
    selected=[m['memory_id'] for m in material['memories']]
    assert selected[0]==about
    assert set(selected)=={*mids,about}
    assert not {hidden,forgotten,foreign}&set(selected)
    assert material['memory_tokens']<=3000
    assert '绝不能进入材料' not in dumps(material)
    assert 'messages' not in material and 'source_messages' not in dumps(material)


def test_name_alias_boundaries_and_same_name_ambiguity(store,clock):
    setup_people(store,clock)
    with store.write() as conn:
        conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES('a','阿甲')")
    named=memory(store,clock,'阿甲在学木工',speaker='self')
    assert [m['memory_id'] for m in select_material(store,'a',clock=clock)['memories']]==[named]
    with store.write() as conn:
        conn.execute("UPDATE subjects SET name='甲同学' WHERE id='b'")
    ambiguous=memory(store,clock,'甲同学住在某个城市',speaker='self')
    assert ambiguous not in [m['memory_id'] for m in select_material(store,'a',clock=clock)['memories']]


def test_minimum_and_excluded_subject_types(store,clock):
    seed(store,clock,7)
    assert summary_due(store,'a',clock=clock)['reason']=='insufficient_memories'
    assert generate(store,clock)[0]['status']=='skipped'
    for sid in ('self','scene'):
        assert summary_due(store,sid,clock=clock)['reason']=='subject_kind'


def test_generate_publish_no_message_ids_and_read_only_context(store,clock):
    seed(store,clock)
    result,model=generate(store,clock)
    assert result['status']=='published' and len(model.calls)==2
    value=current_summary(store,'a')
    assert value['version']==1 and value['sentences'][0]['basis'][0]['revision']==1
    assert len(value['content'])<=200
    before=store._writer.total_changes
    with store.read() as conn:
        context=summary_context(conn,'a','shared')
    assert set(context)=={'subject_id','name','text','version','generated_at','possibly_stale'}
    assert context['subject_id']=='a' and context['possibly_stale'] is False
    assert 'M1' not in context['text'] and 'memory_id' not in context['text']
    assert store._writer.total_changes==before


def test_visibility_change_hides_context_marks_stale_and_drops_previous_from_prompt(store,clock):
    mids=seed(store,clock)
    result,_=generate(store,clock)
    with store.write() as conn:
        conn.execute("INSERT INTO memory_visibility_roots(memory_id,entry_id) VALUES(?,'restricted')",(mids[0],))
    assert current_summary(store,'a')['possibly_stale'] is True
    assert current_summary(store,'a',entry_id='shared') is None
    with store.read() as conn:
        assert summary_context(conn,'a','shared') is None
        assert summary_context(conn,'a','restricted') is None
    memory(store,clock,'新增全局材料')
    model=Model(store)
    PersonSummaryEngine(store,model,clock=clock).regenerate('a',expected_version=1)
    assert result['version']['content'].strip() not in dumps(model.calls[0][1])


def test_all_manual_edit_preservation_rollback_and_expected_version(store,clock):
    seed(store,clock)
    set_settings(store,'a',expected_version=0,publish_mode='all_manual',clock=clock)
    result,_=generate(store,clock)
    assert result['status']=='candidate'
    assert current_summary(store,'a') is None
    confirm_candidate(store,'a',result['version']['version'],expected_version=0,clock=clock)
    edited=admin_edit(store,'a','管理员明确填写的简短说明。',expected_version=1,clock=clock)
    assert edited['sentences'][0]['author']=='admin'
    model=Model(store)
    result=PersonSummaryEngine(store,model,clock=clock).regenerate('a',expected_version=2)
    assert '管理员明确填写的简短说明。' in result['version']['content']
    assert any(s['author']=='admin' for s in result['version']['sentences'])
    rolled=rollback(store,'a',1,expected_version=2,clock=clock)
    assert rolled['version']>2 and rolled['content']==get_version(store,'a',1)['content']
    with pytest.raises(PersonSummaryConflict):
        set_settings(store,'a',expected_version=1,enabled=False,clock=clock)


def test_deletion_threshold_uses_model_items_only(store,clock):
    seed(store,clock)
    items=[{'kind':'fact','text':'学习木工。','memory_refs':['M1']},
           {'kind':'fact','text':'无效条目。','memory_refs':['M999']}]
    result,model=generate(store,clock,items=items)
    assert result['status']=='published' and len(model.calls)==2
    model=Model(store,items=items,bad=[0])
    rejected=PersonSummaryEngine(store,model,clock=clock).regenerate('a',expected_version=1)
    assert rejected['status']=='rejected'
    assert current_summary(store,'a')['version']==1


def test_evidence_revision_and_settings_cas_outside_model_transactions(store,clock):
    mids=seed(store,clock)
    def edit():
        with store.write() as conn:
            conn.execute('UPDATE memories SET revision=revision+1 WHERE id=?',(mids[0],))
    with pytest.raises(PersonSummaryConflict):
        generate(store,clock,hook=edit)
    assert current_summary(store,'a') is None
    model=Model(store,hook=lambda:set_settings(store,'a',expected_version=0,enabled=False,clock=clock))
    with pytest.raises(PersonSummaryConflict):
        PersonSummaryEngine(store,model,clock=clock).regenerate('a',expected_version=0)


def test_update_counts_changed_memories_not_retention_decay(store,clock):
    mids=seed(store,clock)
    generate(store,clock)
    assert summary_due(store,'a',clock=clock)['reason']=='not_due'
    with store.write() as conn:
        conn.execute('UPDATE memories SET retention=retention-1')
    assert summary_due(store,'a',clock=clock)['changed_memories']==0
    with store.write() as conn:
        conn.execute('UPDATE memories SET revision=revision+1 WHERE id IN (?,?,?,?)',mids[:4])
    assert summary_due(store,'a',clock=clock)['changed_memories']==4
    # A changed cited memory is already stale and may regenerate before the count threshold.
    assert summary_due(store,'a',clock=clock)['due']


def test_forgetting_basis_marks_stale_without_read_writes(store,clock):
    mids=seed(store,clock)
    generate(store,clock)
    with store.write() as conn:
        conn.execute("UPDATE memories SET lifecycle='forgotten' WHERE id=?",(mids[0],))
    value=current_summary(store,'a')
    assert value['possibly_stale']
    assert any(x['reason']=='memory_unavailable' for x in value['stale_basis'])


def attach_source(store, clock, mid, entry='shared', *, ancestor=None):
    with store.write() as conn:
        if ancestor is not None:
            conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,created_at) VALUES(?,'memory',?,?)",
                         (mid,ancestor,clock().isoformat()))
        else:
            msg=conn.execute('''INSERT INTO messages(entry_id,kind,sender_subject_id,content,occurred_at,received_at,dedupe_key)
                VALUES(?,'message','a','原始消息不得读取',?,?,?)''',
                (entry,clock().isoformat(),clock().isoformat(),str(mid))).lastrowid
            conn.execute("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,?)",
                         (mid,msg,clock().isoformat()))


@pytest.mark.parametrize('mode',['entry_only','entries'])
@pytest.mark.parametrize('source',['message','root','ancestor'])
def test_declared_private_excluded_even_when_effective_scope_is_all(store,clock,mode,source):
    from iris.memory_ops import Visibility
    mids=seed(store,clock)
    with store.write() as conn:
        conn.execute("DELETE FROM entries WHERE id='restricted'")
        conn.execute("UPDATE entries SET visibility=? WHERE id='shared'",(mode,))
    if source=='message':
        attach_source(store,clock,mids[0])
    elif source=='root':
        with store.write() as conn:
            conn.execute("INSERT INTO memory_visibility_roots VALUES(?,'shared')",(mids[0],))
    else:
        attach_source(store,clock,mids[1]); attach_source(store,clock,mids[0],ancestor=mids[1])
        # A cycle must terminate, and the restriction reaches the entire cycle.
        attach_source(store,clock,mids[1],ancestor=mids[0])
    with store.read() as conn:
        assert Visibility(conn).memory(mids[0]) is None
    assert mids[0] not in {m['memory_id'] for m in select_material(store,'a',clock=clock)['memories']}


def test_changed_source_declaration_hides_even_in_single_entry(store,clock):
    mids=seed(store,clock); attach_source(store,clock,mids[0])
    with store.write() as conn:
        conn.execute("DELETE FROM entries WHERE id='restricted'")
    generate(store,clock)
    with store.write() as conn:
        conn.execute("UPDATE entries SET visibility='entry_only' WHERE id='shared'")
    assert current_summary(store,'a')['possibly_stale']
    with store.read() as conn:
        assert summary_context(conn,'a','shared') is None


def test_purge_clears_all_version_states_and_attempt_copies_atomically(store,clock):
    from iris.person_summary import purge_memory_references
    mids=seed(store,clock)
    generate(store,clock)
    set_settings(store,'a',expected_version=1,publish_mode='all_manual',clock=clock)
    engine=PersonSummaryEngine(store,Model(store),clock=clock)
    engine.regenerate('a',expected_version=1)  # superseded candidate becomes rejected
    engine.regenerate('a',expected_version=1)
    assert {v['status'] for v in list_versions(store,'a')}=={'published','candidate','rejected'}
    with pytest.raises(RuntimeError):
        with store.write() as conn:
            purge_memory_references(conn,[mids[0]])
            raise RuntimeError('rollback transaction')
    assert current_summary(store,'a')['content']
    with store.write() as conn:
        result=purge_memory(store,mids[0],1,confirm=True,_conn=conn)
    assert result['redaction_counts']['person_summary_versions']==3
    assert result['redaction_counts']['person_summary_attempts']==3
    for value in list_versions(store,'a'):
        assert value['content']=='' and value['possibly_stale']
        assert value['checks']['cleared_reason']=='因彻底清除而清空'
        assert value['material']=={}
        assert value['sentences'][0]['kind']=='fact'
        assert value['sentences'][0]['basis'][0]['memory_id']==mids[0]
        assert '正在学习木工' not in dumps(value)
    with store.read() as conn:
        assert summary_context(conn,'a','shared') is None
        for row in conn.execute('SELECT material_json,outputs_json,cleared_at FROM person_summary_attempts'):
            assert row['material_json']==row['outputs_json']=='{}' and row['cleared_at']
    with pytest.raises(ValueError):
        rollback(store,'a',1,expected_version=1,clock=clock)
    with store.write() as conn:
        assert purge_memory_references(conn,[mids[0]])=={'versions':0,'attempts':0}


@pytest.mark.parametrize('during',['generate','check'])
def test_purge_inflight_never_restores_prose(store,clock,during):
    mids=seed(store,clock)
    class Purging(Model):
        def chat(self,messages,purpose,**kwargs):
            if purpose.endswith(during):
                with store.write() as conn:
                    purge_memory(store,mids[0],1,confirm=True,_conn=conn)
            return super().chat(messages,purpose,**kwargs)
    with pytest.raises(PersonSummaryConflict):
        PersonSummaryEngine(store,Purging(store),clock=clock).regenerate('a',expected_version=0)
    assert list_versions(store,'a')==[]
    with store.read() as conn:
        row=conn.execute('SELECT * FROM person_summary_attempts').fetchone()
        assert row['state']=='conflict' and row['outputs_json']==row['material_json']=='{}'


def test_purge_detected_on_failed_generation(store,clock):
    mids=seed(store,clock)
    class Broken(Model):
        def chat(self,*args,**kwargs):
            purge_memory(store,mids[0],1,confirm=True)
            return ModelReply('不合法且包含旧正文','stop',{})
    result=PersonSummaryEngine(store,Broken(store),clock=clock).regenerate('a',expected_version=0)
    assert result['status']=='conflict'
    with store.read() as conn:
        row=conn.execute('SELECT * FROM person_summary_attempts').fetchone()
        assert row['outputs_json']==row['material_json']=='{}' and row['cleared_at']


def test_merge_invalidates_source_and_regenerates_survivor(store,clock):
    from iris.people import confirm_link
    seed(store,clock); generate(store,clock)
    for i in range(8):
        memory(store,clock,f'乙同学的公开测试事实{i}',speaker='b')
    PersonSummaryEngine(store,Model(store),clock=clock).regenerate('b',expected_version=0)
    with store.write() as conn:
        lid=conn.execute("INSERT INTO subject_links(subject_a,subject_b,kind,belief,created_at) VALUES('a','b','same_as',90,'2026-10-11T12:00:00+00:00')").lastrowid
        revisions={r['id']:r['revision'] for r in conn.execute("SELECT id,revision FROM subjects WHERE id IN ('a','b')")}
    confirm_link(store,lid,target_id='b',expected_revision=1,
                 expected_source_revision=revisions['a'],expected_target_revision=revisions['b'])
    assert current_summary(store,'a')['invalidated_reason']=='subject_merged'
    with store.read() as conn:
        assert summary_context(conn,'a','shared') is None
    assert summary_due(store,'b',clock=clock)['due']
    assert summary_due(store,'a',clock=clock)['reason']=='subject_merged'


def test_update_thresholds_for_uncited_memories(store,clock):
    mids=seed(store,clock); generate(store,clock)
    with store.write() as conn:
        conn.execute('UPDATE memories SET revision=revision+1 WHERE id IN (?,?,?,?)',mids[1:5])
    assert summary_due(store,'a',clock=clock)['reason']=='not_due'
    clock.advance(days=14)
    assert summary_due(store,'a',clock=clock)['due']
    clock.advance(days=-14)
    memory(store,clock,'第五条变化')
    assert summary_due(store,'a',clock=clock)['due']


def test_recent_item_expiry_and_output_budget(store,clock):
    seed(store,clock)
    items=[{'kind':'recent','text':'正在学木工。','memory_refs':['M1'],
            'time_range':'截至2026年10月12日','valid_until':'2026-10-12'}]
    result,_=generate(store,clock,items=items)
    assert result['status']=='published'
    clock.advance(days=2)
    with store.read() as conn:
        assert summary_context(conn,'a','shared',clock=clock) is None
    assert current_summary(store,'a',clock=clock)['possibly_stale']
    rejected=PersonSummaryEngine(store,Model(store,items=[{'kind':'fact','text':'长'*201,'memory_refs':['M1']}]),clock=clock).regenerate('a',expected_version=1)
    assert rejected['status']=='rejected'
    assert current_summary(store,'a')['version']==1


def test_purge_tracks_previous_text_even_when_old_basis_not_selected(store,clock):
    from iris.person_summary import purge_memory_references
    mids=seed(store,clock); generate(store,clock)
    with store.write() as conn:
        conn.execute('UPDATE memories SET importance=0 WHERE id=?',(mids[0],))
    for i in range(18):
        memory(store,clock,'新的公开材料'+str(i)+'。'*30,importance=100)
    result=PersonSummaryEngine(store,Model(store),clock=clock).regenerate('a',expected_version=1)
    assert mids[0] not in {m['memory_id'] for m in result['version']['material']['evidence']['memories']}
    assert result['version']['material']['previous']
    with store.write() as conn:
        purge_memory_references(conn,[mids[0]],clock=clock)
    assert get_version(store,'a',2)['content']==''


def test_source_change_during_generation_cannot_publish(store,clock):
    mids=seed(store,clock); attach_source(store,clock,mids[0])
    def restrict():
        with store.write() as conn:
            conn.execute("UPDATE entries SET visibility='entry_only' WHERE id='shared'")
    with pytest.raises(PersonSummaryConflict):
        generate(store,clock,hook=restrict)
    assert not list_versions(store,'a')


def test_manual_warning_needs_confirmation_and_admin_text_is_not_deleted(store,clock):
    seed(store,clock)
    admin_edit(store,'a','管理员手写说明。',expected_version=0,clock=clock)
    model=Model(store,bad=[1],violations=['敏感个人信息'])
    result=PersonSummaryEngine(store,model,clock=clock).regenerate('a',expected_version=1)
    assert result['status']=='candidate'
    assert result['version']['checks']['admin_warnings']
    assert '管理员手写说明。' in result['version']['content']
    assert current_summary(store,'a')['version']==1


@pytest.mark.parametrize('reply', ['{"items":[],"items":[]}','{"items":[{"kind":[]}]}'])
def test_malformed_outputs_preserve_previous_and_are_recorded(store,clock,reply):
    seed(store,clock); generate(store,clock)
    class Malformed(Model):
        def chat(self,*args,**kwargs): return ModelReply(reply,'stop',{})
    result=PersonSummaryEngine(store,Malformed(store),clock=clock).regenerate('a',expected_version=1)
    assert result['status'] in ('failed','rejected')
    assert current_summary(store,'a')['version']==1


def test_one_live_generation_and_interrupt_recovery(store,clock):
    from iris.person_summary import PersonSummaryBusy
    seed(store,clock)
    def concurrent():
        with pytest.raises(PersonSummaryBusy): generate(store,clock)
    result,model=generate(store,clock,hook=concurrent)
    assert result['status']=='published' and len(model.calls)==2
    class Interrupted(Model):
        def chat(self,*args,**kwargs): raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        PersonSummaryEngine(store,Interrupted(store),clock=clock).regenerate('a',expected_version=1)
    PersonSummaryEngine(store,Model(store),clock=clock).regenerate('a',expected_version=1)
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM person_summary_attempts WHERE state='running'").fetchone()[0]==0
        assert conn.execute("SELECT COUNT(*) FROM person_summary_attempts WHERE reason='interrupted'").fetchone()[0]==1


def test_real_gateway_transport_is_mocked_and_daily_budget_rechecked(store,clock):
    import httpx
    from iris.models import Gateway
    from iris.model_health import ModelHealth
    from test_models import configs, response
    seed(store,clock); fake=Model(store); seen=[]
    health=ModelHealth(store,configs(),clock=clock); health.set_daily_token_limit(1)
    def handle(request):
        assert not store._writer.in_transaction
        seen.append(request)
        reply=fake.chat(json.loads(request.content)['messages'],'consolidation_person_summary_generate')
        return httpx.Response(200,json=response(reply.content,usage=reply.usage))
    gateway=Gateway(configs(),store,health=health,clock=clock,client=httpx.Client(transport=httpx.MockTransport(handle)))
    try:
        result=PersonSummaryEngine(store,gateway,clock=clock).regenerate('a',expected_version=0)
        assert result['status']=='rejected' and result['reason']=='usage_limit' and len(seen)==1
        assert current_summary(store,'a') is None
    finally:
        gateway.close()


def test_unbound_context_cannot_be_borrowed_by_checker(store,clock):
    seed(store,clock)
    result,model=generate(store,clock)
    checked=model.calls[1][1]['candidate'][0]
    assert len(checked['evidence'])==1
    assert checked['evidence'][0]['memory_id']==result['version']['sentences'][0]['basis'][0]['memory_id']
    assert 'memories' not in model.calls[1][1]


def test_entry_read_never_exposes_admin_material_snapshots(store,clock):
    mids=seed(store,clock); generate(store,clock)
    with store.write() as conn:
        conn.execute("INSERT INTO memory_visibility_roots(memory_id,entry_id) VALUES(?,'restricted')",(mids[1],))
    value=current_summary(store,'a',entry_id='shared',clock=clock)
    assert set(value)=={'subject_id','name','text','version','generated_at','possibly_stale'}
    assert value['text']=='稳定事实：正在学习木工。\n'
    assert current_summary(store,'a')['material']  # admin-only evidence remains inspectable


@pytest.mark.parametrize('bulk', [False, True])
def test_memory_purge_integrates_person_summary_cleanup(store, clock, bulk):
    from iris import admin_data, memory_ops
    mids=seed(store,clock); generate(store,clock)
    if bulk:
        preview=admin_data.bulk_memory_preview(store,subject_id='a',action='purge')
        result=memory_ops.apply_bulk_memories(store,snapshot_token=preview['snapshot_token'],action='purge',confirm=True)
    else:
        result=purge_memory(store,mids[0],1,confirm=True)
    assert result['redaction_counts']['person_summary_versions']==1
    assert result['redaction_counts']['person_summary_attempts']==1
    assert current_summary(store,'a')['content']==''
    with store.read() as conn:
        assert summary_context(conn,'a','shared',clock=clock) is None
        row=conn.execute('SELECT * FROM person_summary_attempts').fetchone()
        assert row['material_json']==row['outputs_json']=='{}' and row['cleared_at']
        assert conn.execute("SELECT needs_regeneration FROM person_summary_settings WHERE subject_id='a'").fetchone()[0]==1


def test_memory_and_person_summary_purge_roll_back_together(store, clock):
    mids=seed(store,clock); generate(store,clock)
    with pytest.raises(RuntimeError,match='rollback purge'):
        with store.write() as conn:
            purge_memory(store,mids[0],1,confirm=True,_conn=conn)
            assert conn.execute('SELECT content FROM person_summary_versions').fetchone()[0]==''
            assert conn.execute('SELECT cleared_at FROM person_summary_attempts').fetchone()[0]
            raise RuntimeError('rollback purge')
    with store.read() as conn:
        assert conn.execute('SELECT purged_at FROM memories WHERE id=?',(mids[0],)).fetchone()[0] is None
        assert conn.execute('SELECT content FROM person_summary_versions').fetchone()[0]
        assert conn.execute('SELECT cleared_at FROM person_summary_attempts').fetchone()[0] is None
