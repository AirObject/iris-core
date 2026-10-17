"""Limits PATCH preserves omitted values, runtime settings and audit scope."""
import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin
from iris.api import create_app


PATH = '/admin/api/settings/limits'
ORIGINAL = {
    'daily_token_limit': 12000,
    'learning_concurrency': 5,
    'learning_image_limit': 4,
    'native_image_learning': True,
    'media_retention_days': 21,
}


@pytest.fixture
def client(store, monkeypatch):
    with TestClient(create_app(store=store, configs={}), base_url='http://127.0.0.1',
                    client=('127.0.0.1', 1000)) as client:
        client.app.state.scheduler.stop()
        login_admin(client)
        assert client.patch(PATH, json=ORIGINAL).status_code == 200
        health = client.app.state.health
        scheduler = client.app.state.scheduler
        monkeypatch.setattr(health, 'set_daily_token_limit', Mock(wraps=health.set_daily_token_limit))
        monkeypatch.setattr(scheduler, 'set_concurrency', Mock(wraps=scheduler.set_concurrency))
        yield client


@pytest.mark.parametrize('changes', [
    {'media_retention_days': 14},
    {'daily_token_limit': None},
    {'learning_concurrency': 3},
    {'learning_image_limit': 0},
    {'native_image_learning': False},
    {},
    {'daily_token_limit': 9000, 'learning_concurrency': 4, 'learning_image_limit': 2,
     'native_image_learning': False, 'media_retention_days': 30},
])
def test_partial_limits_preserve_omissions_and_sync_only_submitted_fields(client, store, changes):
    with store.read() as conn:
        before = conn.execute("SELECT COUNT(*) FROM admin_operations WHERE action='limits_saved'").fetchone()[0]
    response = client.patch(PATH, json=changes)
    assert response.status_code == 200
    expected = {**ORIGINAL, **changes}
    for key, value in expected.items():
        assert response.json()[key] == value
        assert store.setting(key) == value
    health = client.app.state.health
    scheduler = client.app.state.scheduler
    assert health.budget()['limit'] == expected['daily_token_limit']
    status = client.get('/admin/api/status').json()
    assert status['budget']['limit'] == expected['daily_token_limit']
    assert status['scheduler']['max_concurrent'] == expected['learning_concurrency']
    if 'daily_token_limit' in changes:
        health.set_daily_token_limit.assert_called_once_with(changes['daily_token_limit'])
    else:
        health.set_daily_token_limit.assert_not_called()
    if 'learning_concurrency' in changes:
        scheduler.set_concurrency.assert_called_once_with(changes['learning_concurrency'])
    else:
        scheduler.set_concurrency.assert_not_called()
    with store.read() as conn:
        rows = conn.execute("SELECT details_json FROM admin_operations WHERE action='limits_saved' ORDER BY id").fetchall()
    assert len(rows) == before + 1
    assert json.loads(rows[-1][0]) == changes
