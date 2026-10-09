"""Source context and history are admin reads; clearing basis marks is audited."""
import pytest
from conftest import login_admin, msg
from iris import goals as module
from test_goal_review import backed
from test_goals_api import client
from iris.memory_ops import edit_memory


def test_sources_context_respects_entry_order_and_read_permissions(client, store):
    goal=client.app.state.goals
    ids=[]
    for i in range(5):
        ids.append(msg(store,i,'原始消息'+str(i),entry='A',at='2026-10-10T00:00:00Z'))
        msg(store,i,'另一个入口',entry='B')
    item=goal.create(content='追溯目标',origin='internal',entry_id='A',evidence=[ids[2]])['goal']
    path=f"/admin/api/goals/{item['id']}/sources"
    assert client.get(path).status_code==409
    login_admin(client)
    page=client.get(path+'?limit=1').json()
    assert page['total']==1
    source=page['items'][0]
    assert source['message_id']==ids[2] and not source['missing']
    assert [m['id'] for m in source['context']]==ids
    assert source['message']['entry_name']=='A'
    detail=client.get(f"/admin/api/goals/{item['id']}").json()
    assert detail['sources'][0]['context']==source['context']
    for query in ('limit=0','offset=-1','extra=1'):
        assert client.get(path+'?'+query).status_code==400
    assert client.get('/admin/api/goals/99999/sources').status_code==404
    with store.read() as conn: assert conn.execute('SELECT COUNT(*) FROM recalls').fetchone()[0]==0


def test_missing_source_keeps_id_and_explicit_notice(client,store):
    mid=msg(store,0,'旧来源')
    goal=client.app.state.goals.create(content='旧来源的目标',evidence=[mid])['goal']
    store._writer.execute('PRAGMA foreign_keys=OFF')
    with store.write() as conn: conn.execute('DELETE FROM messages WHERE id=?',(mid,))
    store._writer.execute('PRAGMA foreign_keys=ON')
    login_admin(client)
    source=client.get(f"/admin/api/goals/{goal['id']}/sources").json()['items'][0]
    assert source['message_id']==mid and source['missing']
    assert source['message'] is None and source['context']==[] and '清理' in source['notice']


def test_history_pagination_and_basis_clear_revision_csrf_audit(client,store):
    goals=client.app.state.goals
    goal,mid=backed(store,goals)
    assert edit_memory(store,mid,1,content='依据已修改')
    with store.write() as conn: module.review_goal_basis(conn,goals.clock())
    login_admin(client)
    detail=client.get(f"/admin/api/goals/{goal['id']}").json()
    assert detail['basis_needs_review']
    page=client.get(f"/admin/api/goals/{goal['id']}/revisions?limit=1&offset=1").json()
    assert page['total']==2 and len(page['items'])==1 and page['items'][0]['action']=='create'
    path=f"/admin/api/goals/{goal['id']}/basis-annotations/{detail['basis_annotations'][0]['id']}"
    payload={'expected_revision':detail['revision'],'reason':'已核实仍需办理'}
    assert client.request('DELETE',path,json=payload,headers={'X-Iris-CSRF':'wrong'}).status_code==403
    assert client.request('DELETE',path,json={**payload,'expected_revision':1}).status_code==409
    assert client.request('DELETE',path,json={**payload,'reason':' '}).status_code==400
    cleared=client.request('DELETE',path,json=payload)
    assert cleared.status_code==200 and not cleared.json()['basis_needs_review']
    updated=client.patch(f"/admin/api/goals/{goal['id']}",json={'expected_revision':cleared.json()['revision'],
        'content':'更新后的安排','reason':'与对方确认'} )
    assert updated.status_code==200
    latest=client.get(f"/admin/api/goals/{goal['id']}/revisions?limit=1").json()['items'][0]
    assert latest['reason']=='与对方确认' and latest['after']=={'content':'更新后的安排'}
    for query in ('limit=0','offset=-1','extra=1'):
        assert client.get(f"/admin/api/goals/{goal['id']}/revisions?"+query).status_code==400
    assert client.get('/admin/api/goals/99999/revisions').status_code==404


@pytest.mark.parametrize('protection',['session','origin','host','content_type'])
def test_clear_basis_keeps_existing_admin_protections(client,store,protection):
    goals=client.app.state.goals
    goal,mid=backed(store,goals)
    edit_memory(store,mid,1,content='修改后的承诺')
    with store.write() as conn: module.review_goal_basis(conn,goals.clock())
    current=goals.get(goal['id'])
    login_admin(client)
    headers={}
    if protection=='session': client.cookies.clear()
    elif protection=='origin': headers['Origin']='https://evil.example'
    elif protection=='host': headers['Host']='evil.example'
    else: headers['Content-Type']='text/plain'
    response=client.request('DELETE',f"/admin/api/goals/{goal['id']}/basis-annotations/{current['basis_annotations'][0]['id']}",
        json={'expected_revision':current['revision']},headers=headers)
    assert response.status_code in (400,401,403)
    assert goals.get(goal['id'])['basis_needs_review']


def test_goal_reason_validation_and_no_reason_only_edit(client):
    login_admin(client)
    goal=client.app.state.goals.create(content='管理修改理由')['goal']
    for fields in ({'reason':'说明但不修改'}, {'content':'新正文','reason':'x'*501},
                   {'content':'新正文','reason':123}, {'content':'新正文','extra':True}):
        assert client.patch(f"/admin/api/goals/{goal['id']}",json={'expected_revision':goal['revision'],**fields}).status_code==400
