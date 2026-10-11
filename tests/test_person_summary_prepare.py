"""PS2 host additions are independent of existing retrieval partitions."""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import authorize_host
from iris.api import create_app
from iris.db import dumps
from iris.person_summary import admin_edit, PersonSummaryEngine
from iris.queue import estimate_tokens
from iris.retrieval import Retrieval
from test_person_summary import Clock, Model, seed, setup_people
from test_host_api_contract import assert_schema


def participants(store, clock):
    setup_people(store, clock)
    with store.write() as conn:
        for sid in ('c', 'd', 'empty'):
            conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)", (sid,sid,clock().isoformat()))
    for sid in ('a', 'b', 'c', 'd'):
        admin_edit(store,sid,'手工摘要'+sid+'。',expected_version=0,clock=clock)


def said(store, sid, number, entry='shared'):
    with store.write() as conn:
        return conn.execute("""INSERT INTO messages(entry_id,kind,sender_subject_id,content,occurred_at,received_at,dedupe_key)
            VALUES(?,'message',?,'机制消息','2026-10-11T12:00:00Z','2026-10-11T12:00:00Z',?)""",(entry,sid,str(number))).lastrowid


@pytest.fixture
def client(store):
    with TestClient(create_app(store=store,configs={}),base_url='http://127.0.0.1',client=('127.0.0.1',1234)) as client:
        client.app.state.scheduler.stop(); authorize_host(client)
        yield client


def test_people_order_display_names_budget_and_old_partitions(store):
    clock=Clock(); participants(store,clock)
    for n,sid in enumerate(('a','b','c','b','self','scene')): said(store,sid,n)
    retrieval=Retrieval(store,clock=clock)
    value=retrieval.prepare('shared',text='',judge=False)
    assert [p['subject_id'] for p in value['people']]==['b','c','a']
    other=retrieval.prepare('shared',text='',judge=False,people_limit=0)
    for response in (value,other):
        response['judgment'].pop('duration_ms')
        for hint in response['hints']:
            if hint['code']=='recall_judgment': hint.pop('duration_ms',None)
    assert {k:v for k,v in value.items() if k not in ('people','recall_id')}=={k:v for k,v in other.items() if k not in ('people','recall_id')}
    value=retrieval.prepare('shared',text='',judge=False,participants=['c','a','c','empty','d','b'],
                            participant_names={'a':'本次甲名','d':'本次丁名'},people_limit=3)
    assert [(p['subject_id'],p['name']) for p in value['people']]==[('c','c'),('a','本次甲名'),('d','本次丁名')]
    assert estimate_tokens(dumps(value['people']))<=1000
    assert retrieval.prepare('shared',participants=[],judge=False)['people']==[]
    assert 'people' not in retrieval.search(text='')
    with store.read() as conn:
        assert conn.execute("SELECT name FROM subjects WHERE id='a'").fetchone()[0]=='甲同学'


def test_disabled_and_no_published_summary_do_not_load_evidence(store,monkeypatch):
    from iris import person_summary as summaries
    setup_people(store,Clock())
    calls=[]
    def forbidden(*args,**kwargs):
        calls.append(True)
        raise AssertionError('unnecessary evidence check')
    monkeypatch.setattr(summaries,'summary_context',forbidden)
    r=Retrieval(store)
    assert r.prepare('shared',participants=['a'],judge=False)['people']==[]
    assert r.prepare('shared',participants=['a'],judge=False,people_limit=0)['people']==[]
    assert not calls


def test_host_contract_scope_limits_names_and_search(client,store):
    clock=Clock(); participants(store,clock)
    token=client.app.state.tokens.create(host='scoped',scope={'kind':'entries','entries':['shared']},actor='local_cli')['token']
    headers={'Authorization':'Bearer '+token}
    response=client.post('/api/v1/entries/shared/prepare',headers=headers,json={
        'participants':['a'],'participant_names':{'a':'宿主甲'},'judge':False})
    assert response.status_code==200,response.text
    item=response.json()['people'][0]
    assert set(item)=={'subject_id','name','text','version','generated_at','possibly_stale'}
    assert item['name']=='宿主甲' and item['version']==1 and type(item['possibly_stale']) is bool
    schema=client.app.openapi()['paths']['/api/v1/entries/{entry_id}/prepare']['post']['responses']['200']['content']['application/json']['schema']
    assert_schema(response.json(),schema,client.app.openapi())
    assert client.post('/api/v1/entries/restricted/prepare',headers=headers,json={}).status_code==403
    assert client.post('/api/v1/entries/shared/prepare',headers=headers,json={'people_limit':0}).json()['people']==[]
    assert 'people' not in client.post('/api/v1/memories/search',json={'text':''}).json()


@pytest.mark.parametrize('value',[-1,4,True,'2',1.5,None])
def test_invalid_people_limit(client,store,value):
    setup_people(store,Clock())
    assert client.post('/api/v1/entries/shared/prepare',json={'people_limit':value}).status_code==400


@pytest.mark.parametrize('value',[{'a':''},{'a':'x'*201},{'a':3},[],{'a':'   '}])
def test_invalid_display_names(client,store,value):
    setup_people(store,Clock())
    assert client.post('/api/v1/entries/shared/prepare',json={'participant_names':value}).status_code==400


@pytest.mark.parametrize('change',['restricted','forgotten','deleted','purged','disabled'])
def test_final_snapshot_drops_unavailable_summary_after_judgment(store,monkeypatch,change):
    from iris import recall_judge
    from iris.person_summary import set_settings, purge_memory_references
    clock=Clock(); seed(store,clock)
    result=PersonSummaryEngine(store,Model(store),clock=clock).regenerate('a',expected_version=0)
    mid=result['version']['sentences'][0]['basis'][0]['memory_id']
    original=recall_judge.judge
    def mutate(*args,**kwargs):
        value=original(*args,**kwargs)
        if change=='disabled': set_settings(store,'a',expected_version=1,enabled=False)
        else:
            with store.write() as conn:
                if change=='restricted': conn.execute("INSERT INTO memory_visibility_roots VALUES(?,'restricted')",(mid,))
                elif change=='purged': purge_memory_references(conn,[mid])
                else: conn.execute('UPDATE memories SET lifecycle=? WHERE id=?',(change,mid))
        return value
    monkeypatch.setattr(recall_judge,'judge',mutate)
    assert Retrieval(store,clock=clock).prepare('shared',text='',participants=['a'],judge=False)['people']==[]


def test_private_alias_cannot_select_people_partition(store):
    clock=Clock(); participants(store,clock)
    source=said(store,'a',1,'restricted')
    with store.write() as conn:
        conn.execute("INSERT INTO subject_aliases(subject_id,alias,source_message_id) VALUES('a','私有别称',?)",(source,))
        conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES('b','公开别称')")
    r=Retrieval(store,clock=clock)
    assert r.prepare('shared',participants=['私有别称'],judge=False)['people']==[]
    assert r.prepare('shared',participants=['公开别称'],judge=False)['people'][0]['name']=='公开别称'


def test_combined_budget_skips_whole_items_and_closed_person(store):
    clock=Clock(); participants(store,clock)
    for sid in ('a','b','c'):
        admin_edit(store,sid,'汉'*199,expected_version=1,clock=clock)
    values=Retrieval(store,clock=clock).prepare('shared',participants=['a','b','c'],
        participant_names={sid:'名'*200 for sid in ('a','b','c')},judge=False)['people']
    assert 0<len(values)<3 and estimate_tokens(dumps(values))<=1000
    assert all(p['text']=='汉'*199 for p in values)


def test_limit_one_missing_summaries_and_merged_subject(store):
    clock=Clock(); participants(store,clock)
    from iris.person_summary import set_settings
    set_settings(store,'a',expected_version=1,enabled=False)
    r=Retrieval(store,clock=clock)
    assert [p['subject_id'] for p in r.prepare('shared',participants=['a','b','c'],people_limit=1,judge=False)['people']]==['b']
    with store.write() as conn:
        conn.execute("UPDATE subjects SET merged_into='b',revision=revision+1 WHERE id='a'")
    assert [p['subject_id'] for p in r.prepare('shared',participants=['a','b'],judge=False)['people']]==['b']


def test_people_failure_isolated_with_fixed_hint_and_runtime_record(client, store, monkeypatch):
    from iris import retrieval
    participants(store, Clock())
    baseline = client.post('/api/v1/entries/shared/prepare', json={'participants':['a'],'people_limit':0,'judge':False}).json()
    def broken(*args, **kwargs): raise RuntimeError('private implementation detail')
    monkeypatch.setattr(retrieval, 'people_context', broken)
    response = client.post('/api/v1/entries/shared/prepare', json={'participants':['a'],'judge':False})
    assert response.status_code == 200
    value = response.json()
    assert value['people'] == []
    assert {'code':'people_unavailable','message':'人物摘要暂不可用。'} in value['hints']
    assert value['memories'] == baseline['memories'] and value['entry_profile'] == baseline['entry_profile']
    with store.read() as conn:
        events = [dict(r) for r in conn.execute("SELECT event,entry_id,reason,result FROM runtime_events WHERE event='prepare' ORDER BY id")]
    assert len(events) == 2  # One baseline request and one degraded request; no duplicate diagnostics.
    assert events[-1] == {'event':'prepare','entry_id':'shared','reason':None,'result':'error'}
    assert 'private implementation detail' not in json.dumps([value,events])


def test_name_keyed_display_map_reuses_participant_resolution(store, monkeypatch):
    from contextlib import contextmanager
    clock = Clock(); participants(store, clock)
    statements = []; read = store.read
    @contextmanager
    def traced():
        with read() as conn:
            conn.set_trace_callback(statements.append)
            yield conn
    monkeypatch.setattr(store, 'read', traced)
    value = Retrieval(store, clock=clock).prepare('shared', participants=['甲同学','乙同学'],
        participant_names={'甲同学':'本次甲','乙同学':'本次乙'}, judge=False)
    assert [(p['subject_id'],p['name']) for p in value['people']] == [('a','本次甲'),('b','本次乙')]
    scans = [s for s in statements if 'SELECT DISTINCT s.id FROM subjects s' in s]
    assert len(scans) == 2


def test_reused_name_resolution_still_rechecks_alias_scope_after_judgment(store, monkeypatch):
    from iris import recall_judge
    clock=Clock(); participants(store,clock)
    source=said(store,'a',9)
    with store.write() as conn:
        conn.execute("INSERT INTO subject_aliases(subject_id,alias,source_message_id) VALUES('a','临时别称',?)",(source,))
    original=recall_judge.judge
    def restrict(*args,**kwargs):
        result=original(*args,**kwargs)
        with store.write() as conn: conn.execute("UPDATE entries SET visibility='entry_only' WHERE id='shared'")
        return result
    monkeypatch.setattr(recall_judge,'judge',restrict)
    assert Retrieval(store,clock=clock).prepare('shared',participants=['临时别称'],judge=False)['people']==[]


def test_name_only_evidence_shares_names_across_summaries(store, monkeypatch):
    from iris import person_summary as ps
    from test_person_summary import memory
    clock=Clock(); setup_people(store,clock)
    for sid,name in [('a','甲同学'),('b','乙同学')]:
        for i in range(8): memory(store,clock,f'{name}的具体记载{i}。',speaker='self')
        assert PersonSummaryEngine(store,Model(store),clock=clock).regenerate(sid,expected_version=0)['status']=='published'
    original=ps._SummaryNames; built=[]
    def names(conn):
        result=original(conn); built.append(result); return result
    monkeypatch.setattr(ps,'_SummaryNames',names)
    value=Retrieval(store,clock=clock).prepare('shared',participants=['a','b'],judge=False)
    assert len(value['people'])==2 and len(built)==1


@pytest.mark.parametrize('change',['forgotten','deleted','recent_expired'])
def test_prepare_keeps_other_items_and_people_when_one_item_unavailable(store,change):
    clock=Clock(); seed(store,clock)
    admin_edit(store,'b','乙仍有效。',expected_version=0,clock=clock)
    items=[{'kind':'recent' if change=='recent_expired' else 'fact','text':'甲会被移除的事项。','memory_refs':['M1'],
            **({'time_range':'近期'} if change=='recent_expired' else {})},
           {'kind':'fact','text':'甲仍有效的事项。','memory_refs':['M2']}]
    value=PersonSummaryEngine(store,Model(store,items=items),clock=clock).regenerate('a',expected_version=0)
    assert value['status']=='published'
    if change=='recent_expired': clock.advance(days=30)
    else:
        mid=value['version']['sentences'][0]['basis'][0]['memory_id']
        with store.write() as conn: conn.execute('UPDATE memories SET lifecycle=? WHERE id=?',(change,mid))
    result=Retrieval(store,clock=clock).prepare('shared',participants=['a','b'],judge=False)['people']
    assert [p['subject_id'] for p in result]==['a','b']
    assert '被移除' not in result[0]['text'] and '仍有效' in result[0]['text']
    assert result[0]['possibly_stale'] and not result[1]['possibly_stale']


def test_summary_name_matching_keeps_longest_label_and_word_boundaries(store):
    import re
    from iris import person_summary as ps
    clock=Clock(); setup_people(store,clock)
    with store.write() as conn:
        for sid,name in [('parent','甲同学妈妈'),('suffix','同学'),('ann','Ann'),('anna','Anna'),
                         ('prefixed','A甲'),('quoted','甲(同学)'),('single','甲'),('sharp','Straße')]:
            conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)",(sid,name,clock().isoformat()))
        conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES('a','小甲')")
        conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES('b','小甲')")
    with store.read() as conn:
        names=ps._SummaryNames(conn)
        labels=sorted(names.labels,key=lambda value:(-len(value),value))
        reference=re.compile('|'.join(re.escape(label) if re.search('[\u4e00-\u9fff]',label)
            else r'(?<!\w)'+re.escape(label)+r'(?!\w)' for label in labels),re.I)
        for text in ['甲同学妈妈与甲同学、小甲见面。','A甲、甲(同学)；同学与甲。',
                     'annual Joanne Ann ANNA Anna strasse Straße Iris', '未提及任何姓名。']:
            actual=[(m.group(),m.span()) for m in names.pattern.finditer(text)]
            assert actual==[(m.group(),m.span()) for m in reference.finditer(text)]
        assert [m.group() for m in names.pattern.finditer('甲同学妈妈')] == ['甲同学妈妈']
        assert not list(names.pattern.finditer('annual Joanne'))
        assert '小甲' not in names.unique_labels['a']
