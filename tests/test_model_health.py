import json
from dataclasses import replace

import httpx
import pytest

from fake_openai import Clock, FakeOpenAI, completion
from iris.model_health import ModelHealth
from iris.models import Gateway, ModelError


@pytest.mark.parametrize(("status", "body", "state"), [
    (401, {}, "invalid_key"), (403, {}, "invalid_key"), (404, {}, "configuration_error"),
    (402, {}, "account_problem"), (429, {"error": {"code": "insufficient_quota"}}, "account_problem"),
    (400, {"error": {"code": "content_filter"}}, "normal"),
])
def test_classification_and_configuration_recovery(store, status, body, state):
    with FakeOpenAI().serve() as server:
        health = ModelHealth(store, server.configs)
        gateway = Gateway(server.configs, store, health=health)
        try:
            server.enqueue(status, body)
            with pytest.raises(ModelError):
                gateway.json_chat([], "learning")
            assert health.snapshot()["chat"]["state"] == state
            if state != "normal":
                with pytest.raises(ModelError) as caught:
                    gateway.chat([], "learning")
                assert caught.value.paused and len(server.requests) == 1
                gateway.replace_config("chat", replace(server.configs["chat"], api_key="corrected-fake"))
                assert health.snapshot()["chat"]["state"] == "normal"
                assert gateway.json_chat([], "learning")[0] == {}
            with store.read() as conn:
                assert "fake-only" not in json.dumps([dict(r) for r in conn.execute("SELECT * FROM model_calls")])
        finally:
            gateway.close()


def test_three_transport_errors_pause_probe_backoff_restart_and_recover(store):
    clock = Clock()
    with FakeOpenAI().serve() as server:
        health = ModelHealth(store, server.configs, clock=clock)
        gateway = Gateway(server.configs, store, health=health, sleeper=clock.advance)
        try:
            for status in (429, 503, 500):
                server.enqueue(status, headers={"Retry-After": "3"})
            with pytest.raises(ModelError) as caught:
                gateway.chat([], "learning")
            assert caught.value.paused
            state = health.snapshot()["chat"]
            assert state["state"] == "temporarily_unavailable" and state["consecutive_errors"] == 3
            assert clock.elapsed == 11
            for _ in range(5):
                with pytest.raises(ModelError):
                    gateway.chat([], "learning")
            assert len(server.requests) == 3
            # Reopening health with the same config cannot silently unpause it.
            restored = ModelHealth(store, server.configs, clock=clock)
            assert restored.snapshot()["chat"]["next_probe_at"] == state["next_probe_at"]
            server.enqueue(503)
            clock.advance(60)
            assert not gateway.probe("chat")
            assert health.snapshot()["chat"]["probe_delay_seconds"] == 120
            for delay in (120, 240, 480, 600):
                clock.advance(delay)
                server.enqueue(503)
                assert not gateway.probe("chat")
            assert health.snapshot()["chat"]["probe_delay_seconds"] == 600
            clock.advance(600)
            assert gateway.probe("chat")
            assert health.snapshot()["chat"]["state"] == "normal"
            assert server.requests[-1][1]["messages"][0]["content"] == '只输出 JSON：{"ok":true}'
        finally:
            gateway.close()


def test_account_retry_now_but_auth_requires_changed_config(store):
    with FakeOpenAI().serve() as server:
        health = ModelHealth(store, server.configs)
        gateway = Gateway(server.configs, store, health=health)
        try:
            server.enqueue(402)
            with pytest.raises(ModelError):
                gateway.chat([], "learning")
            gateway.retry_now("chat")
            assert health.snapshot()["chat"]["state"] == "normal"
            server.enqueue(401)
            with pytest.raises(ModelError):
                gateway.chat([], "learning")
            gateway.retry_now("chat")
            assert health.snapshot()["chat"]["state"] == "invalid_key"
        finally:
            gateway.close()


def test_daily_limit_only_blocks_learning_until_role_midnight_or_raise(store):
    clock = Clock()
    store.set_setting("timezone", "Asia/Shanghai")
    with FakeOpenAI().serve() as server:
        health = ModelHealth(store, server.configs, clock=clock)
        gateway = Gateway(server.configs, store, health=health, clock=clock)
        try:
            health.set_daily_token_limit(15)
            gateway.chat([], "learning")
            assert health.snapshot()["chat"]["state"] == "usage_limit"
            with pytest.raises(ModelError) as caught:
                gateway.chat([], "learning")
            assert caught.value.paused
            assert gateway.embedding("查询", "retrieval_query") == [1.0, 0.0]
            health.set_daily_token_limit(100)
            assert health.learning_allowed()
            health.set_daily_token_limit(15)
            clock.advance(60)
            assert health.learning_allowed()
            gateway.chat([], "learning")
            assert not health.learning_allowed()
        finally:
            gateway.close()


def test_shared_learning_deadline_includes_repair_and_retry_delays(store):
    clock = Clock()
    deadlines = []
    def handler(request):
        deadlines.append(request.extensions["timeout"]["read"])
        clock.advance(110 if len(deadlines) == 1 else 1)
        return httpx.Response(200, json=completion("not-json" if len(deadlines) == 1 else "{}"))
    gateway = Gateway({}, store, client=httpx.Client(transport=httpx.MockTransport(handler)),
                      monotonic=clock.monotonic, sleeper=clock.advance)
    from iris.models import ModelConfig
    gateway.configs["chat"] = ModelConfig("http://fake", "", "fake")
    try:
        assert gateway.json_chat([], "learning")[0] == {}
        assert deadlines == [180, 70]
    finally:
        gateway.close()


def test_real_http_timeout_is_recorded_and_late_reply_is_discarded(store, monkeypatch):
    import iris.models as models
    monkeypatch.setattr(models, "LEARNING_TOTAL_TIMEOUT", 0.05)
    with FakeOpenAI().serve() as server:
        server.enqueue(delay=0.2)
        gateway = Gateway(server.configs, store)
        try:
            with pytest.raises(ModelError):
                gateway.json_chat([], "learning")
            with store.read() as conn:
                rows = conn.execute("SELECT * FROM model_calls").fetchall()
            assert len(rows) == 1 and rows[0]["timed_out"] == 1
        finally:
            gateway.close()


def test_late_probe_cannot_clear_new_authentication_failure(store):
    from iris.models import ModelConfig
    clock = Clock()
    health = ModelHealth(store, {"chat": ModelConfig("http://fake", "", "chat")}, clock=clock)
    token = health.check("chat", "learning")
    health.observe("chat", token, "authentication", "HTTP 401")
    health.observe("chat", token, "success", probe=True)
    assert health.snapshot()["chat"]["state"] == "invalid_key"
