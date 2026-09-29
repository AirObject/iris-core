import json
from concurrent.futures import TimeoutError as FutureTimeout

import httpx
import pytest

from iris.models import (Gateway, ModelConfig, ModelError, parse_json_object,
                         parse_json_object_with_status, repair_unescaped_value_quotes)


def configs():
    return {"chat": ModelConfig("https://example.invalid/api/v1", "placeholder", "stub-chat"),
            "embedding": ModelConfig("https://example.invalid/api/plan/v3", "placeholder", "stub-embed")}


def response(content='{"ok":true}', *, flags=None, finish_reason="stop", usage=None, base_status=0):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
            "usage": usage or {}, "base_resp": {"status_code": base_status}, **(flags or {})}


def test_lenient_json_removes_think_fence_and_trailing_comma():
    raw = '<think>private reasoning</think>\n```json\n{"memories": [1,],}\n``` extra'
    assert parse_json_object(raw) == {"memories": [1]}


def test_bare_quotes_in_string_value_are_fixed_without_changing_valid_json():
    valid = '{"memories":[{"content":"她说\\"好呀\\"，我记住了"}]}'
    assert repair_unescaped_value_quotes(valid) == valid
    assert parse_json_object_with_status(valid)[1] == "direct"
    malformed = '{"memories":[{"content":"她说"好呀"，我记住了"}]}'
    fixed, status = parse_json_object_with_status(malformed)
    assert status == "quote_repaired"
    assert fixed["memories"][0]["content"] == "她说“好呀”，我记住了"


def test_quote_repair_is_counted_as_first_response_success(store):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=response('{"content":"他说"收到"。"}'))
    gateway = Gateway(configs(), store, client=httpx.Client(transport=httpx.MockTransport(handler)))
    parsed, _, repair, status, _ = gateway.json_chat([{"role": "user", "content": "test"}], "learning")
    assert parsed == {"content": "他说“收到”。"}
    assert repair is None and status == "quote_repaired" and len(calls) == 1


def test_retry_after_then_backoff_and_reasoning_usage(store):
    sequence = [httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(503),
                httpx.Response(200, json=response(usage={"prompt_tokens": 10, "completion_tokens": 20,
                    "completion_tokens_details": {"reasoning_tokens": 7}}))]
    slept = []
    def handler(request):
        return sequence.pop(0)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    gateway = Gateway(configs(), store, client=client, sleeper=slept.append)
    assert gateway.chat([{"role": "user", "content": "test"}], "test").content == '{"ok":true}'
    assert slept == [3, 8]
    with store.read() as conn:
        calls = conn.execute("SELECT result_category,reasoning_tokens,error_summary FROM model_calls ORDER BY id").fetchall()
    assert [c[0] for c in calls] == ["retryable", "retryable", "success"]
    assert calls[-1][1] == 7
    assert all("placeholder" not in str(c[2]) for c in calls)


def test_explicit_safety_refusal_requires_unusable_output(store):
    items = [response("", flags={"input_sensitive": True}, base_status=1001),
             response('{"ok":true}', flags={"output_sensitive": True}, base_status=1001)]
    def handler(request):
        return httpx.Response(200, json=items.pop(0))
    gateway = Gateway(configs(), store, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ModelError) as caught:
        gateway.chat([{"role": "user", "content": "test"}], "test")
    assert caught.value.category == "content_rejection"
    assert gateway.chat([{"role": "user", "content": "test"}], "test").content == '{"ok":true}'
    with store.read() as conn:
        assert [r[0] for r in conn.execute("SELECT result_category FROM model_calls ORDER BY id")] == ["content_rejection", "success"]


@pytest.mark.parametrize(("status", "category"), [(401, "authentication"), (404, "configuration"), (402, "account")])
def test_http_error_categories_without_provider_body(store, status, category):
    gateway = Gateway(configs(), store, client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, text="secret-looking provider body"))))
    with pytest.raises(ModelError) as caught:
        gateway.chat([{"role": "user", "content": "test"}], "test")
    assert caught.value.category == category
    with store.read() as conn:
        assert "secret-looking" not in conn.execute("SELECT error_summary FROM model_calls").fetchone()[0]


def test_bad_json_or_length_gets_one_repair_call(store):
    items = [response('{"broken":', finish_reason="length"), response('{"ok":true}')]
    gateway = Gateway(configs(), store, client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=items.pop(0)))))
    parsed, raw, repair, status, reason = gateway.json_chat([{"role": "user", "content": "test"}], "learning")
    assert (parsed, status) == ({"ok": True}, "repaired")
    assert raw == '{"broken":' and repair == '{"ok":true}'
    assert reason == "finish_reason=length"


def test_finish_reason_and_output_usage_are_recorded_for_first_and_repair(store):
    from conftest import msg
    from iris.queue import form_batch
    msg(store, 1, "我喜欢茶")
    batch_id = form_batch(store, "A", "test").id
    items = [response('{"broken":', finish_reason="length", usage={"completion_tokens": 16000}),
             response('{"ok":true}', usage={"completion_tokens": 8})]
    gateway = Gateway(configs(), store, client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=items.pop(0)))))
    try:
        gateway.json_chat([{"role": "user", "content": "test"}], "learning", 16000, batch_id=batch_id)
        with store.read() as conn:
            rows = conn.execute("SELECT purpose,finish_reason,completion_tokens,batch_id FROM model_calls ORDER BY id").fetchall()
        assert [tuple(row) for row in rows] == [("learning", "length", 16000, batch_id),
                                              ("learning_repair", "stop", 8, batch_id)]
    finally:
        gateway.close()


def test_embedding_uses_configured_plan_path(store):
    paths = []
    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}], "usage": {}})
    gateway = Gateway(configs(), store, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert gateway.embedding("中文") == [0.1, 0.2]
    assert paths == ["/api/plan/v3/embeddings"]


@pytest.mark.parametrize(("purpose", "deadline"), [
    ("test", 120), ("learning", 120), ("learning_repair", 120),
    ("learning_judge", 240), ("learning_judge_repair", 240),
])
def test_generation_has_hard_total_timeout_and_two_retries(store, purpose, deadline):
    class Future:
        def cancel(self):
            return False

        def result(self, timeout):
            assert timeout == deadline
            raise FutureTimeout()
    class Pool:
        def submit(self, *args, **kwargs):
            assert kwargs["timeout"].read == deadline
            assert kwargs["timeout"].connect == 10
            return Future()
    sleeps = []
    gateway = Gateway(configs(), store, sleeper=sleeps.append)
    gateway._pool.shutdown(wait=False)
    gateway._pool = Pool()
    with pytest.raises(ModelError) as caught:
        gateway.chat([{"role": "user", "content": "test"}], purpose)
    assert caught.value.category == "retryable"
    assert sleeps == [2, 8]


def test_provider_account_status_is_classified_without_body_in_record(store):
    body = {"base_resp": {"status_code": 1201, "status_msg": "insufficient balance"}, "choices": []}
    gateway = Gateway(configs(), store, client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=body))))
    with pytest.raises(ModelError) as caught:
        gateway.chat([{"role": "user", "content": "test"}], "test")
    assert caught.value.category == "account"
    with store.read() as conn:
        call = conn.execute("SELECT result_category,error_summary FROM model_calls").fetchone()
    assert tuple(call) == ("account", "provider status 1201")
