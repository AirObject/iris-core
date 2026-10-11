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
    def forbidden(*args,**kwargs): raise AssertionError('unnecessary evidence check')
    monkeypatch.setattr(summaries,'summary_context',forbidden)
    r=Retrieval(store)
    assert r.prepare('shared',participants=['a'],judge=False)['people']==[]
    assert r.prepare('shared',participants=['a'],judge=False,people_limit=0)['people']==[]


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
