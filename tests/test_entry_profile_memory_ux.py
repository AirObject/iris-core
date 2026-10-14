"""EF-10 backend additions requested by UX; preserved content and read-only version."""
from datetime import datetime, timezone

from iris.memory_ops import confirm_retention
from test_entry_profile_memory_api import client


def create(client, body, category=None):
    response = client.post('/admin/api/memories', json={'kind': '入口画像', 'owner_entry_id': 'group',
        'category': category, 'content': body})
    assert response.status_code == 201, response.text
    return response.json()['id']


def test_admin_profile_exposes_current_confirmation_time_without_version_bump(client, store):
    mid = create(client, '长期禁止广告。', 'rule')
    before = client.get('/admin/api/entries/group/profile').json()
    with store.read() as conn:
        stamp = conn.execute('SELECT last_confirmed_at FROM memories WHERE id=?', (mid,)).fetchone()[0]
    assert before['memories'][0]['last_confirmed_at'] == stamp
    later = datetime(2026, 10, 16, tzinfo=timezone.utc)
    confirm_retention(store, mid, current=later)
    after = client.get('/admin/api/entries/group/profile').json()
    assert after['memories'][0]['last_confirmed_at'] == later.isoformat()
    assert after['projection']['version'] == before['projection']['version']


def test_multiline_manual_note_is_one_projected_item_and_stored_body_is_lossless(client, store):
    body = '管理员留下的说明。\n第二行说明。\r\n\n最后一行。'
    mid = create(client, body)
    other = create(client, '禁止广告。', 'rule')
    profile = client.post('/api/v1/entries/group/prepare', json={'text': '', 'judge': False}).json()['entry_profile']
    assert profile['memory_ids'] == [other, mid]
    assert profile['text'].splitlines() == ['明确群规／禁忌：禁止广告。', '管理员说明：管理员留下的说明。 第二行说明。 最后一行。']
    row = next(r for r in client.get('/admin/api/entries/group/profile').json()['memories'] if r['memory_id'] == mid)
    assert row['body'] == body
    filtered = client.post('/api/v1/entries/group/prepare', json={'text': '', 'judge': False, 'known_memory_ids': [mid]}).json()['entry_profile']
    assert filtered['memory_ids'] == [other] and '第二行' not in filtered['text']
