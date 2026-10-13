"""HTTP transition and frozen host field/type contracts."""
import pytest
from fastapi.testclient import TestClient
from iris.api import create_app
from iris import entry_profile as ep
from conftest import login_admin, authorize_host
from test_entry_profile_memory_live import message, RuleGateway, CLOCK


@pytest.fixture
def client(store):
    message(store)
    with TestClient(create_app(store=store, configs={}),base_url='http://127.0.0.1',client=('127.0.0.1',1234)) as c:
        c.app.state.scheduler.stop()
        login_admin(c); authorize_host(c)
        c.app.state.entry_profile_jobs.engine.gateway=RuleGateway(store)
        c.app.state.entry_profile_jobs.engine.clock=CLOCK
        yield c


def test_manual_creation_generic_memory_api_and_host_contract(client, store):
    response=client.post('/admin/api/memories',json={'kind':'入口画像','owner_entry_id':'group',
        'category':'rule','content':'不得刷屏。'})
    assert response.status_code==201,response.text
    memory=response.json(); mid=memory['id']
    assert memory['owner_entry_id']=='group' and memory['category']=='rule' and memory['manual_created'] is True
    snapshot=client.get('/admin/api/entries/group/profile').json()
    assert snapshot['mode']=='memory' and snapshot['pending_count']==0
    assert snapshot['projection']['memory_ids']==[mid]
    prepared=client.post('/api/v1/entries/group/prepare',json={'text':'','judge':False}).json()
    profile=prepared['entry_profile']
    assert set(profile)=={'text','version','generated_at','possibly_stale','memory_ids'}
    assert type(profile['version']) is int and type(profile['possibly_stale']) is bool
    assert isinstance(profile['text'],str) and isinstance(profile['generated_at'],str)
    assert profile['memory_ids']==[mid]
    assert not prepared['memories']
    filtered=client.get('/admin/api/memories',params={'kind':'入口画像','entry_id':'group'}).json()
    assert filtered['total']==1
    bad=client.post('/admin/api/memories',json={'kind':'入口画像','content':'bad','category':'rule'})
    assert bad.status_code==400


@pytest.mark.parametrize('method,path,payload',[
    ('put','',{'expected_version':0,'content':'旧的整版编辑。'}),
    ('post','/versions/1/confirm',{'expected_version':0}),
    ('post','/versions/1/reject',{'expected_version':0}),
    ('post','/versions/1/rollback',{'expected_version':0}),
    ('patch','/settings',{'expected_version':0,'publish_mode':'all_manual'}),
])
def test_retired_routes_return_410_machine_code(client,method,path,payload):
    response=getattr(client,method)('/admin/api/entries/group/profile'+path,json=payload)
    assert response.status_code==410,response.text
    assert response.json()['error']['code'] in ('entry_profile_legacy_write_retired','entry_profile_publish_mode_retired')


def test_enabled_setting_still_uses_projection_cas(client,store):
    ep.EntryProfileEngine(store,RuleGateway(store),clock=CLOCK).regenerate('group',expected_version=0)
    version=client.get('/admin/api/entries/group/profile').json()['projection']['version']
    response=client.patch('/admin/api/entries/group/profile/settings',json={'expected_version':version,'enabled':False})
    assert response.status_code==200,response.text
    assert client.get('/admin/api/entries/group/profile').json()['projection'] is None
    assert client.patch('/admin/api/entries/group/profile/settings',json={'expected_version':version,'enabled':True}).status_code==409
