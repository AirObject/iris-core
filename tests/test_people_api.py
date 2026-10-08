"""People management uses the real local admin session and CSRF middleware."""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin
from iris.api import create_app
from iris import people
from test_people import person, link, say, revision
from test_retrieval import put


@pytest.fixture
def client(store):
    with TestClient(create_app(store=store), base_url='http://127.0.0.1', client=('127.0.0.1', 1000)) as c:
        login_admin(c)
        c.app.state.scheduler.stop()
        yield c


@pytest.fixture
def identities(store):
    person(store, 'a', '小林')
    person(store, 'b', '林同学')
    person(store, 'c', '旁观者')
    return link(store, 'a', 'b')


def test_people_list_search_pending_pagination_and_detail_are_read_only(client, store, identities):
    source = say(store, 'a', content='我可能认识林同学')
    with store.write() as conn:
        conn.execute('UPDATE subject_links SET source_message_id=? WHERE id=?', (source, identities))
    alias = people.add_alias(store, 'a', '阿林', expected_revision=revision(store, 'a'))
    mid = put(store, '小林每周练习摄影', about=['a'], speaker='a')
    put(store, '小林过去爱滑冰', about=['a'], speaker='a', lifecycle='forgotten')
    rows = client.get('/admin/api/people', params={'pending_only': True, 'limit': 1}).json()
    assert rows['total'] == 2 and len(rows['items']) == 1 and rows['items'][0]['pending_links'] == 1
    assert [r['id'] for r in client.get('/admin/api/people?text=阿林').json()['items']] == ['a']
    detail = client.get('/admin/api/people/a').json()
    assert detail['memory_count'] == 2
    assert detail['aliases'][0]['id'] == alias['id']
    assert detail['platform_identities'][0]['account_id'] == 'a'
    assert detail['same_as'][0]['belief'] == 70
    assert detail['same_as'][0]['evidence_messages'][0]['content'] == '我可能认识林同学'
    memories = client.get(detail['memories_url']).json()
    assert memories['total'] == 2 and mid in [m['id'] for m in memories['items']]
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM recalls').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0] == 0
        assert {r[0] for r in conn.execute('SELECT retention FROM memories')} == {50}


def test_admin_confirm_merges_and_audits_then_rejects_stale_request(client, store, identities):
    payload = {'target_id': 'a', 'expected_revision': 1,
               'expected_source_revision': revision(store, 'b'), 'expected_target_revision': revision(store, 'a')}
    url = f'/admin/api/people/links/{identities}/confirm'
    response = client.post(url, json=payload)
    assert response.status_code == 200 and response.json()['source_id'] == 'b'
    assert client.post(url, json=payload).status_code == 409
    assert 'b' not in [r['id'] for r in client.get('/admin/api/people').json()['items']]
    assert client.get('/admin/api/people/b').json()['canonical_id'] == 'a'
    assert 'b' in [r['id'] for r in client.get('/admin/api/people?include_merged=true').json()['items']]
    assert 'b' not in [r['id'] for r in client.get('/admin/api/catalog').json()['people']]
    operations = client.get('/admin/api/operations?action=subjects_merged').json()
    assert operations['total'] == 1


def test_alias_add_delete_and_denial_use_revisions_and_audit(client, store, identities):
    payload = {'alias': '小林子', 'expected_revision': revision(store, 'a')}
    response = client.post('/admin/api/people/a/aliases', json=payload)
    assert response.status_code == 201
    alias = response.json()
    assert client.post('/admin/api/people/a/aliases', json=payload).status_code == 409
    assert client.request('DELETE', f"/admin/api/people/a/aliases/{alias['id']}",
                          json={'expected_revision': revision(store, 'a')}).status_code == 200
    assert client.post(f'/admin/api/people/links/{identities}/deny', json={'expected_revision': 1}).status_code == 200
    assert client.get('/admin/api/people?pending_only=true').json()['total'] == 0
    with store.read() as conn:
        records = [dict(r) for r in conn.execute("SELECT * FROM admin_operations WHERE action LIKE 'subject_%'")]
    assert {r['action'] for r in records} >= {'subject_alias_added', 'subject_alias_removed', 'subject_link_denied'}
    assert '小林子' not in json.dumps(records, ensure_ascii=False)


@pytest.mark.parametrize('body', [{'alias': ' ', 'expected_revision': 1}, {'alias': 'x'},
                                   {'alias': 'x', 'expected_revision': True}, {'alias': 'x', 'expected_revision': 1, 'extra': 1}])
def test_people_input_validation(client, store, identities, body):
    assert client.post('/admin/api/people/a/aliases', json=body).status_code == 400


@pytest.mark.parametrize('protection', ['csrf', 'origin', 'host', 'content_type', 'session'])
def test_people_writes_keep_host_session_and_csrf_protection(client, store, identities, protection):
    headers = {}
    if protection == 'csrf': headers['X-Iris-CSRF'] = 'wrong'
    if protection == 'origin': headers['Origin'] = 'https://evil.example'
    if protection == 'host': headers['Host'] = 'evil.example'
    if protection == 'content_type': headers['Content-Type'] = 'text/plain'
    if protection == 'session': client.cookies.clear()
    response = client.post(f'/admin/api/people/links/{identities}/deny', json={'expected_revision': 1}, headers=headers)
    assert response.status_code in (400, 401, 403, 415)
    with store.read() as conn:
        assert conn.execute('SELECT status FROM subject_links WHERE id=?', (identities,)).fetchone()[0] == 'possible'


@pytest.mark.parametrize('pinned,expected', [(None, 2), ('true', 1), ('false', 1)])
def test_memory_pinned_filter_includes_total_and_pagination(client, store, pinned, expected):
    first = put(store, '第一条记忆')
    second = put(store, '第二条记忆')
    with store.write() as conn:
        conn.execute('UPDATE memories SET pinned=1 WHERE id=?', (first,))
    params = {'limit': 1}
    if pinned is not None:
        params['pinned'] = pinned
    response = client.get('/admin/api/memories', params=params)
    assert response.status_code == 200 and response.json()['total'] == expected
    if pinned is not None:
        assert response.json()['items'][0]['id'] == (first if pinned == 'true' else second)
        params['offset'] = 1
        assert client.get('/admin/api/memories', params=params).json()['items'] == []
    assert client.get('/admin/api/memories?pinned=nonsense').status_code == 400


def test_host_prepare_and_search_keep_annotations(client, store, identities):
    say(store, 'a')
    put(store, '小林喜欢天文摄影', speaker='a', about=['a'])
    paths = [('/api/v1/memories/search', {'text': '天文摄影'}),
             ('/api/v1/entries/chat/prepare', {'text': '天文摄影', 'participants': [], 'judge': False})]
    for path, body in paths:
        response = client.post(path, json=body)
        assert response.status_code == 200
        assert response.json()['memories'][0]['subject_annotations']['possible_same_as'][0]['link_id'] == identities
