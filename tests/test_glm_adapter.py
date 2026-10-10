"""Provider behavior exercised through the local OpenAI-compatible HTTP server."""
import json
from dataclasses import replace
from datetime import timedelta
from email.utils import format_datetime

import pytest

from fake_openai import Clock, FakeOpenAI, completion
from iris.model_health import ModelHealth, fingerprint
from iris.models import Gateway, ModelConfig, ModelError, load_test_models


def ark_error(code="", message="", kind=""):
    return {"error": {"code": code, "message": message, "type": kind, "param": ""}}


def test_optional_effort_config_is_generic_and_chat_only(tmp_path):
    path = tmp_path / "fake-models.toml"
    path.write_text('[chat]\nmodel="fake"\nreasoning_effort="vendor-defined"\n'
                    '[embedding]\nreasoning_effort="ignored"\n', encoding="utf-8")
    configs = load_test_models(path)
    assert configs["chat"].reasoning_effort == "vendor-defined"
    assert configs["embedding"].reasoning_effort is None
    path.write_text('[chat]\nmodel="fake"\n', encoding="utf-8")
    assert load_test_models(path)["chat"].reasoning_effort is None


@pytest.mark.parametrize("value", ['42', 'true', '[]', '""', '"   "'])
def test_invalid_effort_fails_locally_without_echoing_config(tmp_path, value):
    path = tmp_path / "fake-models.toml"
    path.write_text(f'[chat]\napi_key="fake-sensitive-value"\nreasoning_effort={value}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="reasoning_effort") as caught:
        load_test_models(path)
    assert "fake-sensitive-value" not in str(caught.value)


def test_effort_sent_only_when_configured_and_recorded_per_attempt(store):
    with FakeOpenAI().serve() as server:
        gateway = Gateway(server.configs, store)
        try:
            messages = [{"role": "user", "content": "{}"}]
            gateway.chat(messages, "learning", 16)
            assert server.requests[-1][1] == {"messages": messages, "max_tokens": 16,
                "response_format": {"type": "json_object"}, "model": "stub-chat"}
            gateway.replace_config("chat", replace(server.configs["chat"], reasoning_effort="low"))
            server.enqueue(body=completion("invalid-json"))
            gateway.json_chat(messages, "learning", 16)
            assert [r[1]["reasoning_effort"] for r in server.requests[-2:]] == ["low", "low"]
            gateway.embedding("vector")
            assert "reasoning_effort" not in server.requests[-1][1]
            with store.read() as conn:
                rows = conn.execute("SELECT purpose,reasoning_effort FROM model_calls ORDER BY id").fetchall()
            assert [tuple(r) for r in rows] == [("learning", None), ("learning", "low"),
                                              ("learning_repair", "low"), ("embedding", None)]
        finally:
            gateway.close()


def test_ark_codes_precede_http_and_empty_error_fields_fall_back(store):
    cases = [
        (400, "SensitiveContentDetected", "content_rejection"),
        (400, "SensitiveContentDetected.SevereViolation", "content_rejection"),
        (400, "InputTextSensitiveContentDetected", "content_rejection"),
        (400, "OutputTextSensitiveContentDetected", "content_rejection"),
        (400, "InputTextRiskDetection", "content_rejection"),
        (400, "OutputTextRiskDetection", "content_rejection"),
        (500, "InputTextSensitiveContentDetected", "content_rejection"),
        (400, "InvalidParameter", "configuration"),
        (429, "InvalidParameter.UnsupportedValue", "configuration"),
        (400, "MissingParameter", "configuration"),
        (400, "InvalidEndpoint.ClosedEndpoint", "configuration"),
        (404, "InvalidEndpointOrModel.NotFound", "configuration"),
        (401, "AuthenticationError", "authentication"),
        (401, "InvalidAccountStatus", "account"),
        (400, "InvalidSubscription", "account"),
        (403, "AccountOverdueError", "account"),
        (403, "OperationDenied.ServiceOverdue", "account"),
        (403, "AccessDenied", "account"),
        (429, "QuotaExceeded", "account"),
        (429, "QuotaExceeded.AgentPlanQuotaExceeded", "account"),
        (429, "SetLimitExceeded", "account"),
        (429, "AccountRateLimitExceeded", "retryable"),
        (429, "RateLimitExceeded.EndpointRPMExceeded", "retryable"),
        (429, "RateLimitExceeded.EndpointTPMExceeded", "retryable"),
        (429, "InflightBatchsizeExceeded", "retryable"),
        (429, "ServerOverloaded", "retryable"),
        (429, "RequestBurstTooFast", "retryable"),
        (400, "ContentSecurityDetectionError", "retryable"),
        (500, "InternalServiceError", "retryable"),
        (400, "", "item_error"), (401, "", "authentication"),
        (403, "", "account"), (404, "", "configuration"),
        (429, "", "retryable"), (500, "", "retryable"),
        (400, "SensitiveContentDetectedUnknown", "item_error"),
    ]
    with FakeOpenAI().serve() as server:
        gateway = Gateway(server.configs, store, sleeper=lambda _: None)
        try:
            for status, code, expected in cases:
                start = len(server.requests)
                for _ in range(3 if expected == "retryable" else 1):
                    server.enqueue(status, ark_error(code))
                with pytest.raises(ModelError) as caught:
                    gateway.json_chat([], "learning")
                assert caught.value.category == expected, (status, code)
                assert len(server.requests) - start == (3 if expected == "retryable" else 1)
            # Non-Ark providers may supply only a known error.type.
            server.enqueue(429, ark_error(kind="insufficient_quota"))
            with pytest.raises(ModelError) as caught:
                gateway.chat([], "learning")
            assert caught.value.category == "account"
        finally:
            gateway.close()


@pytest.mark.parametrize("filtered", [False, True])
def test_content_rejection_is_terminal_without_repair_or_memory(store, filtered):
    from conftest import msg
    from iris.learning import LearningEngine, PROMPT_VERSION
    from iris.queue import form_batch
    msg(store, 1, "我喜欢茶")
    formed = form_batch(store, "A", PROMPT_VERSION)
    with FakeOpenAI().serve() as server:
        body = completion('{"memories": [')
        body["choices"][0]["finish_reason"] = "content_filter"
        server.enqueue(200 if filtered else 400, body if filtered else ark_error("InputTextRiskDetection"))
        configs = {"chat": server.configs["chat"]}
        health = ModelHealth(store, configs)
        gateway = Gateway(configs, store, health=health)
        try:
            LearningEngine(store, gateway).run_batch(formed.id, force=True)
            assert len(server.requests) == 1
            with store.read() as conn:
                assert conn.execute("SELECT state FROM batches").fetchone()[0] == "refused"
                assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
                assert conn.execute("SELECT reason FROM memory_gaps").fetchone()[0] == "content_rejection"
            assert health.snapshot()["chat"]["state"] == "normal"
        finally:
            gateway.close()


def test_reasoning_is_diagnostic_only_and_missing_usage_is_unknown(store):
    secret_reasoning = 'private reasoning {"wrong":true}'
    with FakeOpenAI().serve() as server:
        gateway = Gateway(server.configs, store)
        try:
            for field in ("reasoning_content", "reasoning"):
                body = completion('{"answer":true}')
                body["choices"][0]["message"][field] = secret_reasoning
                body["usage"] = {"prompt_tokens": 4, "completion_tokens": 9,
                                 "completion_tokens_details": {"reasoning_tokens": 6}}
                server.enqueue(body=body)
                result = gateway.json_chat([], "learning")
                assert result[0] == {"answer": True} and result[1] == '{"answer":true}'
            for message in ({"content": "{}", "reasoning_content": ""}, {"content": "{}"},
                            {"content": "{}", "reasoning_content": None}):
                server.enqueue(body={"choices": [{"message": message, "finish_reason": "stop"}]})
                gateway.json_chat([], "learning")
            with store.read() as conn:
                rows = [dict(r) for r in conn.execute("SELECT * FROM model_calls ORDER BY id")]
                assert secret_reasoning not in "\n".join(conn.iterdump())
            for row in rows[:2]:
                assert (row["reasoning_present"], row["reasoning_chars"], row["reasoning_tokens"]) == (1, len(secret_reasoning), 6)
            assert [(r["reasoning_present"], r["reasoning_chars"]) for r in rows[2:]] == [(1, 0), (0, 0), (1, None)]
            assert all(r["prompt_tokens"] is r["completion_tokens"] is r["reasoning_tokens"] is None for r in rows[2:])
        finally:
            gateway.close()


def test_effort_changes_health_identity_and_recovery_probe_payload(store):
    clock = Clock()
    with FakeOpenAI().serve() as server:
        low = replace(server.configs["chat"], reasoning_effort="low")
        high = replace(low, reasoning_effort="high")
        assert fingerprint(low) != fingerprint(high)
        configs = {"chat": low}
        health = ModelHealth(store, configs, clock=clock)
        gateway = Gateway(configs, store, health=health)
        try:
            token = health.check("chat", "learning")
            health.observe("chat", token, "configuration", "invalid parameter")
            assert gateway.replace_config("chat", high)
            assert health.snapshot()["chat"]["state"] == "normal"
            health.observe("chat", token, "authentication", "stale failure")
            assert health.snapshot()["chat"]["state"] == "normal"
            token = health.check("chat", "learning")
            health.observe("chat", token, "account", "HTTP 429")
            assert not health.due_probes()
            clock.advance(60)
            assert gateway.probe("chat")
            assert server.requests[-1][1]["reasoning_effort"] == "high"
        finally:
            gateway.close()


def test_retry_after_overrides_jitter_and_shared_budget_is_never_extended(store):
    clock = Clock()
    slept = []
    def sleep(seconds):
        slept.append(seconds)
        clock.advance(seconds)
    with FakeOpenAI().serve() as server:
        gateway = Gateway(server.configs, store, clock=clock, monotonic=clock.monotonic,
                          sleeper=sleep, jitter=lambda low, high: high)
        try:
            server.enqueue(429, ark_error("AccountRateLimitExceeded"))
            server.enqueue(500, ark_error("InternalServiceError"))
            assert gateway.json_chat([], "learning")[0] == {}
            assert slept == [4, 8]  # 2, 4 exponential base plus the selected jitter.
            slept.clear()
            server.enqueue(429, ark_error("AccountRateLimitExceeded"), {"Retry-After": "1"})
            date = format_datetime(clock() + timedelta(seconds=10), usegmt=True)
            server.enqueue(429, ark_error("AccountRateLimitExceeded"), {"Retry-After": date})
            gateway.json_chat([], "learning")
            assert slept == [1, 9]
            slept.clear()
            count = len(server.requests)
            server.enqueue(429, ark_error("AccountRateLimitExceeded"), {"Retry-After": "181"})
            with pytest.raises(ModelError):
                gateway.json_chat([], "learning")
            assert len(server.requests) == count + 1 and slept == []
        finally:
            gateway.close()


def test_403_and_quota_pause_as_account_without_short_retries(store):
    clock = Clock()
    with FakeOpenAI().serve() as server:
        health = ModelHealth(store, server.configs, clock=clock)
        gateway = Gateway(server.configs, store, health=health,
                          sleeper=lambda _: pytest.fail("account errors must not sleep/retry"))
        try:
            for status, code in [(403, ""), (429, "QuotaExceeded"), (400, "InvalidSubscription")]:
                gateway.retry_now("chat")
                count = len(server.requests)
                server.enqueue(status, ark_error(code))
                with pytest.raises(ModelError) as caught:
                    gateway.chat([], "learning")
                assert caught.value.category == "account" and caught.value.paused
                assert len(server.requests) == count + 1
                assert health.snapshot()["chat"]["state"] == "account_problem"
                assert not health.due_probes()
        finally:
            gateway.close()


def test_unset_effort_preserves_saved_health_fingerprint():
    import hashlib
    config = ModelConfig("https://unused.invalid", "fake-key", "chat")
    legacy = hashlib.sha256(json.dumps([config.base_url, config.model, config.api_key, config.dimensions]).encode()).hexdigest()
    assert fingerprint(config) == legacy


@pytest.mark.parametrize("header", ["invalid", "nan", "inf", "-inf"])
def test_invalid_retry_after_uses_finite_backoff(store, header):
    with FakeOpenAI().serve() as server:
        slept = []
        gateway = Gateway(server.configs, store, sleeper=slept.append, jitter=lambda low, high: 0)
        try:
            server.enqueue(429, ark_error("AccountRateLimitExceeded"), {"Retry-After": header})
            gateway.chat([], "learning")
            assert slept == [2]
        finally:
            gateway.close()


def test_reasoning_never_substitutes_for_content_or_enters_repair(store):
    with FakeOpenAI().serve() as server:
        body = completion("")
        body["choices"][0]["message"]["reasoning_content"] = '{"private_reasoning":true}'
        server.enqueue(body=body)
        gateway = Gateway(server.configs, store)
        try:
            result = gateway.json_chat([], "learning")
            assert result[0] == {} and result[1] == "" and result[3] == "repaired"
            assert server.requests[1][1]["messages"][0] == {"role": "assistant", "content": ""}
            assert "private_reasoning" not in json.dumps(server.requests)
        finally:
            gateway.close()
