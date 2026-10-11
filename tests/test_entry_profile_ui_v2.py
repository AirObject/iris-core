"""The management editor uses v2 budgets without changing generation or v1."""
import pytest
from fastapi.testclient import TestClient

from conftest import login_admin
from iris.api import create_app
from test_entry_profile import Clock
from test_entry_profile_v2 import setup, run, item

BASE = '/admin/api/entries/group/profile'


@pytest.fixture
def client(store):
    clock = Clock()
    setup(store, clock)
    run(store, clock, [item()])
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1', client=('127.0.0.1', 1000)) as c:
        c.app.state.scheduler.stop()
        login_admin(c)
        yield c


def original():
    return {'original_index': 0, 'kind': 'saying', 'label': '翻面', 'meaning': '',
            'quotes': [{'message_id': 1, 'text': '反看试印纸背面'}]}


def test_structured_short_edit_preserves_evidence_and_marks_changes(client, store):
    old = client.get(BASE + '/versions/1').json()
    response = client.put(BASE, json={'expected_version': 1, 'items': [original(),
        {'kind': 'rule', 'label': '不要发布私人地址'}]})
    assert response.status_code == 200, response.text
    value = response.json()
    assert len(value['content']) < 150 and value['checks']['format_version'] == 2
    assert old['checks']['quote_validation_items'] == [{'index': 1, 'passed': 1, 'total': 1, 'quotes': [{'index': 1, 'passed': True, 'reason': None}]}]
    assert value['sentences'][0] == old['sentences'][0]
    assert value['sentences'][1]['admin_written'] and value['sentences'][1]['basis'] == []
    changed = {**original(), 'label': '反面检查'}
    updated = client.put(BASE, json={'expected_version': 2, 'items': [changed]}).json()
    assert updated['sentences'][0]['admin_written'] and updated['sentences'][0]['basis'] == []
    assert updated['sentences'][0]['quotes'][0]['field'] == 'content'
    assert updated['sentences'][0]['quotes'][0]['speaker_name']
    stale = client.put(BASE, json={'expected_version': 2, 'items': [original()]})
    assert stale.status_code == 409
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_operations WHERE action='entry_profile_edit'").fetchone()[0] == 2
    assert client.post(BASE + '/versions/2/rollback', json={'expected_version': 3}).status_code == 200


def test_v2_rendered_text_compatibility_and_readonly_budgets(client, store):
    changes = store._writer.total_changes
    summary = client.get(BASE).json()
    assert summary['current']['format_version'] == 2
    assert summary['edit_limits'] == {'max_items': 6, 'max_chars': 500, 'max_tokens': 500}
    assert store._writer.total_changes == changes
    result = client.put(BASE, json={'expected_version': 1, 'content': summary['current']['content']})
    assert result.status_code == 200
    assert not result.json()['sentences'][0]['admin_written']


@pytest.mark.parametrize('items', [
    [{'kind': 'saying', 'label': '一'}, {'kind': 'saying', 'label': '二'}, {'kind': 'saying', 'label': '三'},
     {'kind': 'saying', 'label': '四'}, {'kind': 'saying', 'label': '五'}, {'kind': 'saying', 'label': '六'}, {'kind': 'saying', 'label': '七'}],
    [{'kind': 'saying', 'label': '新条目', 'quotes': [{'message_id': 99999, 'text': '不存在'}]}],
    [{'kind': 'saying', 'label': '新条目', 'quotes': [{'message_id': 1, 'text': '拼接原话'}]}],
    [{'original_index': 10, 'kind': 'rule', 'label': '没有该条'}],
    [original(), original()],
    [{'kind': 'topic', 'label': '话题', 'meaning': '不适用'}],
])
def test_bad_edits_rejected_without_new_version(client, items):
    assert client.put(BASE, json={'expected_version': 1, 'items': items}).status_code == 400
    assert client.get(BASE).json()['settings']['current_version'] == 1


def test_total_budget_and_csrf(client, store):
    with store.write() as conn:
        conn.execute("INSERT OR REPLACE INTO runtime_settings(key,value_json) VALUES('entry_profile',?)", ('{"max_chars":150}',))
    too_long = [{'kind': 'saying', 'label': '甲' * 40, 'meaning': '乙' * 80}, {'kind': 'rule', 'label': '丙' * 40}]
    assert client.put(BASE, json={'expected_version': 1, 'items': too_long}).status_code == 400
    client.headers.pop('X-Iris-CSRF')
    assert client.put(BASE, json={'expected_version': 1, 'items': [original()]}).status_code == 403
