import json
from pathlib import Path

import pytest

from iris.db import Store
from iris.learning import LearningEngine, PROMPT_VERSION
from iris.models import ModelConfig, ModelError
from iris.queue import add_message, form_batch


def learning_output(sections):
    """Complete the protocol envelope for tests focused on individual items."""
    return {**{key: [] for key in ("memories", "updates", "people", "goals", "questions")}, **sections}


class FakeGateway:
    def __init__(self, response=None, hook=None):
        self.response = response if response is not None else {}
        self.hook = hook
        self.materials = []
        self.requests = []
        self.configs = {"chat": ModelConfig("fake", "", "fake"), "embedding": ModelConfig("", "", "")}

    def json_chat(self, messages, purpose, max_tokens=3500, *, batch_id=None):
        self.materials.append(messages[-1]["content"])
        self.requests.append({"messages": messages, "purpose": purpose, "max_tokens": max_tokens, "batch_id": batch_id})
        if self.hook:
            self.hook(messages)
        if isinstance(self.response, Exception):
            raise self.response
        result = self.response.pop(0) if isinstance(self.response, list) else self.response
        if isinstance(result, Exception):
            raise result
        # Item-focused tests supply only the sections under test. Emit a complete
        # protocol envelope; malformed envelopes use the real Gateway transport.
        if purpose == "learning" and isinstance(result, dict):
            result = learning_output(result)
        return result, json.dumps(result, ensure_ascii=False), None, "direct"

    def embedding(self, text, purpose="embedding"):
        return [1.0, 0.0]


@pytest.fixture(autouse=True)
def isolate_deployment_environment(monkeypatch):
    """Deployment settings must come from each test, not the caller's shell."""
    for name in ("IRIS_DATA_DIR", "IRIS_HOST", "IRIS_PORT"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "iris.db")
    try:
        yield value
    finally:
        value.close()


def msg(store, index, text, *, entry="A", sender="小林", kind="message", platform="test",
        quote_author=None, at=None):
    return add_message(store, entry_id=entry, entry_name=entry, platform=platform, entry_kind="group",
                       kind=kind, sender=sender, content=text,
                       occurred_at=at or f"2026-09-28T09:{index:02}:00+08:00", dedupe_key=f"{entry}-{index}",
                       account_id=f"{entry}:{sender}", quote_author=quote_author)


def batch(store, gateway, *, entry="A", count=2):
    formed = form_batch(store, entry, PROMPT_VERSION, target_count=count, future_count=2, history_count=2)
    assert formed is not None
    return formed, LearningEngine(store, gateway).run_batch(formed.id, force=True)


def login_admin(client):
    """Enter the real setup/login flow for tests that exercise protected UI routes."""
    client.headers["Content-Type"] = "application/json"
    state = client.get('/admin/api/session').json()
    client.headers['X-Iris-CSRF'] = state['csrf_token']
    path = '/admin/api/login' if state['admin_exists'] else '/admin/api/setup/password'
    response = client.post(path, json={'password': 'deterministic-test-admin'})
    assert response.status_code == 200, response.text
    client.headers['X-Iris-CSRF'] = client.get('/admin/api/session').json()['csrf_token']
    if not state['configured']:
        role = client.get('/admin/api/settings').json()['role']
        role['timezone'] = role['timezone'] or 'Asia/Shanghai'
        response = client.post('/admin/api/setup/complete', json=role)
        assert response.status_code == 200, response.text


def authorize_host(client, host='test-host'):
    """Issue a normal credential using the same core as offline provisioning."""
    value = client.app.state.tokens.create(host=host, scope={'kind': 'all'}, actor='local_cli')
    client.headers['Authorization'] = 'Bearer ' + value['token']
    return value
