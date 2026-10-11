import pytest
from fastapi.testclient import TestClient

from conftest import login_admin
from iris.api import create_app


def test_native_limit_default_save_and_old_client_omission(store):
    with TestClient(create_app(store=store), base_url='http://127.0.0.1', client=('127.0.0.1', 12345)) as client:
        client.app.state.scheduler.stop()
        login_admin(client)
        assert client.get('/admin/api/settings').json()['learning_image_limit'] == 8
        response = client.patch('/admin/api/settings/limits', json={'learning_image_limit': 2})
        assert response.status_code == 200, response.text
        assert response.json()['learning_image_limit'] == 2
        response = client.patch('/admin/api/settings/limits', json={'learning_concurrency': 1})
        assert response.status_code == 200, response.text
        assert response.json()['learning_image_limit'] == 2
    assert store.setting('learning_image_limit') == 2
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_operations WHERE action='limits_saved'").fetchone()[0] == 2


@pytest.mark.parametrize('value', [-1, 33, True, None, '8'])
def test_native_limit_rejects_invalid_values(store, value):
    with TestClient(create_app(store=store), base_url='http://127.0.0.1', client=('127.0.0.1', 12345)) as client:
        client.app.state.scheduler.stop()
        login_admin(client)
        response = client.patch('/admin/api/settings/limits', json={'learning_image_limit': value})
        assert response.status_code == 400, response.text
        assert response.json()['error']['fields'][0]['field'] == 'body.learning_image_limit'
    assert store.setting('learning_image_limit') is None
