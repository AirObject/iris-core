"""Authenticated PS2 projections and durable generation receipts."""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin
from iris.api import create_app
from test_person_summary import Clock, Model, seed

BASE='/admin/api/people/a/summary'


@pytest.fixture
def client(store):
    clock=Clock(); seed(store,clock)
    with TestClient(create_app(store=store,configs={}),base_url='http://127.0.0.1',client=('127.0.0.1',1234)) as client:
        client.app.state.scheduler.stop(); login_admin(client)
        engine=client.app.state.person_summary_jobs.engine
        engine.gateway,engine.clock=Model(store),clock
        yield client


def start(client,expected=0):
    response=client.post(BASE+'/regenerate',json={'expected_version':expected})
    assert response.status_code==202,response.text
    assert response.json()['accepted']
    return response.headers['location']


def finished(client,path):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        result=client.get(path).json()
        if result['state']!='running': return result
        time.sleep(.005)
    pytest.fail('summary did not finish')


def test_reads_generation_evidence_and_pagination(client,store):
    before=store._writer.total_changes
    view=client.get(BASE).json()
    assert view['current'] is None and view['pending_count']==0 and view['supported']
    assert view['edit_limits']=={'max_items':5,'max_chars':200,'max_tokens':250}
    assert client.get(BASE+'/versions').json()['total']==0
    assert before==store._writer.total_changes
    attempt=finished(client,start(client))
    assert attempt['state']=='published' and attempt['stage']=='finished'
    before=store._writer.total_changes
    value=client.get(BASE+'/versions/1').json()
    assert value['sentences'][0]['basis'][0]['evidence']['content']
    assert not value['sentences'][0]['admin_written']
    assert 'material' not in value and 'model_input' not in value['checks'] and 'memory_state' not in value
    assert 'outputs_json' not in attempt and 'material_json' not in attempt
    assert client.get(BASE+'/versions?limit=1').json()['total']==1
    assert client.get('/admin/api/people/b/summary/versions/1').status_code==404
    assert client.get('/admin/api/people/b/summary/attempts/'+str(attempt['id'])).status_code==404
    assert before==store._writer.total_changes


def test_edit_rollback_conflicts_and_audit(client,store):
    a=client.put(BASE,json={'expected_version':0,'content':'简短说明。'})
    assert a.status_code==200 and a.json()['version']==1
    assert a.json()['sentences'][0]['admin_written']
    assert client.put(BASE,json={'expected_version':1,'content':'更新说明。'}).json()['version']==2
    assert client.get(BASE+'/diff?before_version=1&after_version=2').json()['changed']
    assert client.post(BASE+'/versions/1/rollback',json={'expected_version':2}).json()['version']==3
    for method,path,body in [('PUT','',{'content':'旧请求。'}),('PATCH','/settings',{'enabled':False}),
                             ('POST','/versions/1/rollback',{}),('POST','/regenerate',{})]:
        assert client.request(method,BASE+path,json={'expected_version':0,**body}).status_code==409
        assert client.request(method,BASE+path,json=body).status_code==400
    with store.read() as conn:
        actions={r[0] for r in conn.execute("SELECT action FROM admin_operations WHERE object_type='person_summary'")}
    assert actions>={'person_summary_edit','person_summary_rollback'}


def test_candidate_settings_confirmation_and_rejection(client,store):
    assert client.patch(BASE+'/settings',json={'expected_version':0,'publish_mode':'all_manual'}).status_code==200
    assert finished(client,start(client))['state']=='candidate'
    assert client.get(BASE).json()['pending_count']==1
    assert client.post(BASE+'/versions/1/confirm',json={'expected_version':0}).json()['is_current']
    assert finished(client,start(client,1))['state']=='candidate'
    assert client.post(BASE+'/versions/2/reject',json={'expected_version':1,'reason':'暂不采用'}).status_code==200
    assert client.get(BASE+'/versions/2').json()['checks']['reason']=='暂不采用'
    assert client.patch(BASE+'/settings',json={'expected_version':1,'enabled':False}).json()['enabled'] is False
    assert client.get(BASE).json()['settings']['enabled'] is False


def test_async_busy_reservation_and_shutdown(client,store):
    entered,release=threading.Event(),threading.Event()
    model=client.app.state.person_summary_jobs.engine.gateway
    def hook(): entered.set(); assert release.wait(5)
    model.hook=hook
    try:
        path=start(client)
        assert entered.wait(5)
        assert client.get(path).json()['state']=='running'
        assert client.post(BASE+'/regenerate',json={'expected_version':0}).status_code==409
        assert not store._writer.in_transaction
    finally: release.set()
    assert finished(client,path)['state']=='published'


def test_auth_csrf_and_purge_projection(client,store):
    assert client.get(BASE,headers={'Cookie':''}).status_code==401
    assert client.put(BASE,json={'expected_version':0,'content':'说明。'},headers={'X-Iris-CSRF':''}).status_code==403
    finished(client,start(client))
    from iris.person_summary import purge_memory_references
    with store.write() as conn: purge_memory_references(conn,[1])
    value=client.get(BASE+'/versions/1').json()
    assert value['content']=='' and value['checks']['cleared_reason']=='因彻底清除而清空'
    assert all(not s['text'] and all(b.get('evidence') is None for b in s['basis']) for s in value['sentences'])
    assert client.post(BASE+'/versions/1/rollback',json={'expected_version':1}).status_code==400


def test_consolidation_setting_roundtrip_and_strict_type(client,store):
    assert client.get('/admin/api/settings').json()['consolidation']['person_summary_enabled'] is True
    response=client.patch('/admin/api/settings/consolidation',json={'person_summary_enabled':False})
    assert response.status_code==200 and response.json()['consolidation']['person_summary_enabled'] is False
    assert client.patch('/admin/api/settings/consolidation',json={'person_summary_enabled':1}).status_code==400
    assert client.get('/admin/api/settings').json()['consolidation']['max_calls']==300
    with store.read() as conn:
        assert conn.execute("SELECT 1 FROM admin_operations WHERE action='consolidation_settings_saved'").fetchone()
