"""OpenAI-compatible model gateway and safe call accounting."""

from __future__ import annotations

import json
import math
import os
import random
import re
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from .db import Store, now


CHAT_TOTAL_TIMEOUT = 120
LEARNING_TOTAL_TIMEOUT = 180
EMBEDDING_TOTAL_TIMEOUT = 30
JUDGE_TOTAL_TIMEOUT = 240


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    dimensions: int | None = None
    reasoning_effort: str | None = None

    def __post_init__(self):
        # Values are provider-defined; do not impose Ark's enum on other providers.
        if self.reasoning_effort is not None and (
                not isinstance(self.reasoning_effort, str) or not self.reasoning_effort.strip()):
            raise ValueError("reasoning_effort must be a nonempty string when configured")


@dataclass
class ModelReply:
    content: str
    finish_reason: str | None
    usage: dict[str, Any]
    input_sensitive: bool | None = None
    output_sensitive: bool | None = None
    status_code: int | None = None


class ModelError(Exception):
    def __init__(self, category: str, summary: str, raw_output: str | None = None,
                 first_raw: str | None = None, *, paused: bool = False):
        super().__init__(summary)
        self.category = category
        self.summary = summary
        self.raw_output = raw_output
        self.first_raw = first_raw
        self.paused = paused


def load_test_models(path: str | Path | None = None) -> dict[str, ModelConfig]:
    chosen = Path(path or os.environ.get("IRIS_TEST_MODELS", "test-models.toml"))
    data = tomllib.loads(chosen.read_text(encoding="utf-8"))
    result = {}
    for name in ("chat", "embedding"):
        group = data.get(name, {})
        api_key = str(group.get("api_key", ""))
        if "api_key_env" in group:
            env_name = group["api_key_env"]
            if "api_key" in group or not isinstance(env_name, str) or not env_name or env_name not in os.environ:
                raise ValueError("api_key_env needs an existing environment variable and no api_key field")
            api_key = os.environ[env_name]
        result[name] = ModelConfig(
            str(group.get("base_url", "")).rstrip("/"),
            api_key,
            str(group.get("model", "")),
            group.get("dimensions"),
            group.get("reasoning_effort") if name == "chat" else None,
        )
    return result


def _summary(message: str, key: str) -> str:
    # Provider messages sometimes include request headers; never persist them verbatim.
    clean = message.replace(key, "[REDACTED]") if key else message
    clean = re.sub(r"(?i)(authorization|api[_-]?key|bearer)\s*[:=]\s*\S+", r"\1=[REDACTED]", clean)
    return clean[:300]


def _retry_after(value: str | None, current: datetime | None = None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value) - (current or datetime.now(timezone.utc))).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


# Ark error codes (including dotted subcodes), checked 2026-10-07:
# https://docs.volcengine.com/docs/ark/error-codes?lang=zh
# A code takes precedence over HTTP status: e.g. quota exhaustion is also 429,
# and ContentSecurityDetectionError is a retryable moderation service failure.
# QuotaExceeded is overloaded for queued jobs in Ark's other APIs; this gateway
# calls synchronous chat/embeddings and conservatively treats it as account quota.
_ERROR_CODES = {
    "content_rejection": (
        "SensitiveContentDetected", "InputTextSensitiveContentDetected", "OutputTextSensitiveContentDetected",
        "InputTextRiskDetection", "OutputTextRiskDetection",
        "content_filter", "content_policy_violation", "content_safety", "sensitive_content"),
    "configuration": ("InvalidParameter", "MissingParameter", "InvalidEndpoint", "InvalidEndpointOrModel"),
    "authentication": ("AuthenticationError", "authentication_error", "invalid_api_key"),
    "account": (
        "InvalidSubscription", "InvalidAccountStatus", "AccountOverdueError", "OperationDenied", "AccessDenied",
        "QuotaExceeded", "SetLimitExceeded", "ModelNotOpen", "insufficient_quota", "insufficient_balance",
        "account_suspended", "account_deactivated", "billing_hard_limit_reached", "balance_not_enough", "arrearage"),
    "retryable": (
        "AccountRateLimitExceeded", "RateLimitExceeded", "ModelAccountRpmRateLimitExceeded",
        "ModelAccountTpmRateLimitExceeded", "ModelAccountFlexTpmRateLimitExceeded", "APIAccountRpmRateLimitExceeded",
        "ModelAccountIpmRateLimitExceeded", "InflightBatchsizeExceeded", "ServerOverloaded", "RequestBurstTooFast",
        "ContentSecurityDetectionError", "InternalServiceError"),
}


def _http_error_category(status: int, data: Any) -> str:
    error = data.get("error") if isinstance(data, dict) else None
    error = error if isinstance(error, dict) else {}
    # Some OpenAI-compatible providers use only type; Ark may leave it empty.
    for field in ("code", "type"):
        code = error.get(field)
        if not isinstance(code, str):
            continue
        code = code.casefold()
        for category, names in _ERROR_CODES.items():
            if any(code == name.casefold() or code.startswith(name.casefold() + ".") for name in names):
                return category
    if status == 401:
        return "authentication"
    if status in (402, 403, 423):
        return "account"
    if status in (408, 429) or status >= 500:
        return "retryable"
    return "configuration"


def _reasoning_diagnostics(message: dict[str, Any]) -> tuple[bool, int | None]:
    # Ark returns message.reasoning_content; some compatible providers use reasoning.
    # Count Unicode characters only. Never retain the reasoning text in call records.
    values = [message[key] for key in ("reasoning_content", "reasoning") if key in message]
    chars = sum(len(value) for value in values) if all(isinstance(value, str) for value in values) else None
    return bool(values), chars


def repair_unescaped_value_quotes(candidate: str) -> str:
    """Replace likely bare quotes inside JSON string values, leaving valid JSON intact."""
    try:
        json.JSONDecoder().raw_decode(candidate)
        return candidate
    except json.JSONDecodeError:
        pass
    output: list[str] = []
    stack: list[str] = []
    inside = False
    value_string = False
    escaped = False
    inner_quotes = 0
    last_nonspace = ""
    for index, char in enumerate(candidate):
        if inside:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                following = candidate[index + 1:].lstrip()
                next_char = following[:1]
                terminal = next_char in (",", "}", "]") if value_string else next_char == ":"
                if value_string and not terminal:
                    char = "“" if inner_quotes % 2 == 0 else "”"
                    inner_quotes += 1
                else:
                    inside = False
        else:
            if char == '"':
                value_string = last_nonspace == ":" or bool(stack and stack[-1] == "[")
                inside = True
                inner_quotes = 0
            elif char in "{[":
                stack.append(char)
            elif char in "}]" and stack:
                stack.pop()
        output.append(char)
        if not inside and not char.isspace():
            last_nonspace = char
    return "".join(output)


def parse_json_object_with_status(raw: str) -> tuple[dict[str, Any], str]:
    stripped = re.sub(r"<think\b[^>]*>.*?</think>", "", raw, flags=re.I | re.S)
    stripped = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", stripped.strip(), flags=re.I)
    start = stripped.find("{")
    if start < 0:
        raise ValueError("no JSON object")
    candidate = re.sub(r",\s*([}\]])", r"\1", stripped[start:])
    status = "direct"
    try:
        value, _ = json.JSONDecoder().raw_decode(candidate)
    except json.JSONDecodeError:
        repaired = repair_unescaped_value_quotes(candidate)
        if repaired == candidate:
            raise
        value, _ = json.JSONDecoder().raw_decode(repaired)
        status = "quote_repaired"
    if not isinstance(value, dict):
        raise ValueError("top-level value is not an object")
    return value, status


def parse_json_object(raw: str) -> dict[str, Any]:
    return parse_json_object_with_status(raw)[0]


class Gateway:
    def __init__(self, configs: dict[str, ModelConfig], store: Store | None = None, client: httpx.Client | None = None,
                 sleeper=time.sleep, *, health=None, clock=None, monotonic=time.monotonic, jitter=random.uniform):
        self.health = health
        self.configs = health.configs if health else dict(configs)
        self.store = store
        self.client = client or httpx.Client()
        self._own_client = client is None
        self.sleeper = sleeper
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic
        self.jitter = jitter
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="iris-model")

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self._own_client:
            self.client.close()

    def _record(self, purpose: str, model: str, duration_ms: int, category: str, error: str | None,
                usage: dict[str, Any] | None = None, flags: dict[str, Any] | None = None, status_code: int | None = None,
                *, finish_reason: str | None = None, batch_id: int | None = None,
                kind: str | None = None, timed_out: bool = False, reasoning_effort: str | None = None,
                reasoning_present: bool | None = None, reasoning_chars: int | None = None) -> None:
        if not self.store:
            return
        usage = usage or {}
        flags = flags or {}
        details = usage.get("completion_tokens_details") or {}
        with self.store.write() as conn:
            conn.execute("""INSERT INTO model_calls
                (purpose,model,duration_ms,prompt_tokens,completion_tokens,reasoning_tokens,result_category,
                 error_summary,input_sensitive,output_sensitive,status_code,created_at,finish_reason,batch_id,model_kind,timed_out,
                 reasoning_effort,reasoning_present,reasoning_chars)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (purpose, model, duration_ms, usage.get("prompt_tokens"), usage.get("completion_tokens"),
                 details.get("reasoning_tokens"), category, error, flags.get("input_sensitive"),
                 flags.get("output_sensitive"), status_code, self.clock().isoformat(), finish_reason, batch_id, kind, int(timed_out),
                 reasoning_effort, reasoning_present, reasoning_chars))

    def replace_config(self, kind: str, config: ModelConfig) -> bool:
        if self.health:
            return self.health.replace_config(kind, config)
        changed = self.configs.get(kind) != config
        self.configs[kind] = config
        return changed

    def retry_now(self, kind: str) -> None:
        if self.health:
            self.health.retry_now(kind)

    def probe(self, kind: str) -> bool:
        if not self.health or kind not in self.health.due_probes():
            return False
        payload = ({"messages": [{"role": "user", "content": '只输出 JSON：{"ok":true}'}],
                    "max_tokens": 64, "response_format": {"type": "json_object"}}
                   if kind == "chat" else self._embedding_payload("Iris 测试连接"))
        try:
            self._call(kind, "health_probe", payload, probe=True)
            return True
        except ModelError:
            return False

    @staticmethod
    def timeout_for(kind, purpose):
        if kind == "embedding":
            return 2 if purpose == "retrieval_query" else EMBEDDING_TOTAL_TIMEOUT
        if purpose in ("learning", "learning_repair"):
            return LEARNING_TOTAL_TIMEOUT
        if purpose in ("learning_judge", "learning_judge_repair", "e2e_judge", "e2e_judge_repair"):
            return JUDGE_TOTAL_TIMEOUT
        return CHAT_TOTAL_TIMEOUT

    def _call(self, kind: str, purpose: str, payload: dict[str, Any], *, batch_id: int | None = None,
              deadline: float | None = None, probe: bool = False) -> dict[str, Any]:
        deadline = deadline if deadline is not None else self.monotonic() + self.timeout_for(kind, purpose)
        attempts = 1 if probe or purpose == "retrieval_query" else 3
        for attempt in range(attempts):
            token = self.health.check(kind, purpose, probe=probe) if self.health else None
            config = self.configs.get(kind)
            if not config or not config.base_url or not config.model:
                raise ModelError("configuration", f"{kind} model is not configured", paused=bool(self.health))
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise ModelError("retryable", "total timeout")
            url = config.base_url + ("/chat/completions" if kind == "chat" else "/embeddings")
            headers = {"Content-Type": "application/json"}
            if config.api_key:
                headers["Authorization"] = f"Bearer {config.api_key}"
            timeout = httpx.Timeout(remaining, connect=min(10, remaining))
            started = self.monotonic()
            response = None
            flags, usage = {}, {}
            finish_reason, status_code = None, None
            category, summary, timed_out = "success", None, False
            retry_after = None
            reasoning_present, reasoning_chars = None, None
            reasoning_effort = config.reasoning_effort if kind == "chat" else None
            request_payload = {**payload, "model": config.model}
            if reasoning_effort is not None:
                request_payload["reasoning_effort"] = reasoning_effort
            try:
                request = self._pool.submit(self.client.post, url, headers=headers,
                                            json=request_payload, timeout=timeout)
                response = request.result(timeout=remaining)
                status_code = response.status_code
                if status_code >= 400:
                    try:
                        error_data = response.json()
                    except ValueError:
                        error_data = {}
                    category = _http_error_category(status_code, error_data)
                    # Provider prose can echo credentials or input; do not persist it.
                    summary = f"HTTP {status_code}"
                    retry_after = _retry_after(response.headers.get("Retry-After"), self.clock())
                else:
                    data = response.json()
                    flags = {"input_sensitive": data.get("input_sensitive"), "output_sensitive": data.get("output_sensitive")}
                    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
                    body = data.get("base_resp") or {}
                    provider_status = body.get("status_code")
                    choices = data.get("choices") or []
                    choice = choices[0] if choices else {}
                    message = choice.get("message") or {}
                    content = message.get("content")
                    if kind == "chat":
                        reasoning_present, reasoning_chars = _reasoning_diagnostics(message)
                    finish_reason = choice.get("finish_reason")
                    if kind == "chat" and (finish_reason == "content_filter" or
                            (choice.get("message") or {}).get("refusal") or (any(flags.values()) and not content)):
                        category, summary = "content_rejection", "provider content safety refusal"
                    elif provider_status not in (None, 0, 200) and not content:
                        provider_message = str(body.get("status_msg") or (data.get("error") or {}).get("message") or "").casefold()
                        category = "account" if any(term in provider_message for term in
                            ("balance", "quota", "credit", "billing", "余额", "欠费", "额度")) else "configuration"
                        summary = f"provider status {provider_status}"
                    elif kind == "embedding":
                        vector = data["data"][0]["embedding"]
                        if not isinstance(vector, list) or not vector or any(not math.isfinite(float(v)) for v in vector) or not any(float(v) for v in vector):
                            raise ValueError("invalid embedding vector")
                        if payload.get('dimensions') is not None and len(vector) != payload['dimensions']:
                            raise ValueError('embedding dimensions do not match request')
                    elif not choices or not isinstance(content, str):
                        raise ValueError("missing chat choice")
                    status_code = provider_status or status_code
            except (httpx.TransportError, FutureTimeout) as exc:
                request.cancel()
                timed_out = isinstance(exc, (FutureTimeout, httpx.TimeoutException))
                category = "retryable"
                summary = "total timeout" if isinstance(exc, FutureTimeout) else type(exc).__name__
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                category, summary = "configuration", f"invalid provider response: {type(exc).__name__}"
            duration = round((self.monotonic() - started) * 1000)
            # Even an HTTP client that returns just after the deadline cannot commit a late result.
            if category == "success" and self.monotonic() > deadline:
                category, summary, timed_out = "retryable", "total timeout", True
            self._record(purpose, config.model, duration, category, summary, usage, flags, status_code,
                         finish_reason=finish_reason, batch_id=batch_id, kind=kind, timed_out=timed_out,
                         reasoning_effort=reasoning_effort, reasoning_present=reasoning_present, reasoning_chars=reasoning_chars)
            paused = self.health.observe(kind, token, category, summary, probe=probe) if self.health else False
            if category == "content_rejection":
                paused = False  # Explicit safety refusal is a terminal batch result even during another outage.
            if category == "success":
                if self.configs.get(kind) != config:
                    raise ModelError("paused", "model configuration changed during request", paused=True)
                return data
            if category != "retryable" or paused or attempt == attempts - 1:
                raise ModelError(category, summary, paused=paused)
            base_delay = 2 * (2 ** attempt)
            retry_delay = retry_after if retry_after is not None else base_delay + self.jitter(0, base_delay)
            if deadline - self.monotonic() <= retry_delay:
                raise ModelError(category, summary, paused=paused)
            self.sleeper(retry_delay)
        raise AssertionError("unreachable")

    def chat(self, messages: list[dict[str, str]], purpose: str, max_tokens: int = 3500, *, batch_id: int | None = None,
             _deadline: float | None = None) -> ModelReply:
        data = self._call("chat", purpose, {"messages": messages,
                         "response_format": {"type": "json_object"}, "max_tokens": max_tokens}, batch_id=batch_id, deadline=_deadline)
        choice = (data.get("choices") or [{}])[0]
        return ModelReply(str((choice.get("message") or {}).get("content") or ""), choice.get("finish_reason"),
                          data.get("usage") or {}, data.get("input_sensitive"), data.get("output_sensitive"),
                          (data.get("base_resp") or {}).get("status_code"))

    def _embedding_payload(self, text: str) -> dict[str, Any]:
        config = self.configs['embedding']
        payload = {"model": config.model, "input": text}
        settings = self.store.setting('retrieval', {}) if self.store else {}
        dimensions = config.dimensions
        if dimensions is None and settings.get('embedding_model') == config.model:
            dimensions = settings.get('embedding_dimensions', 2048)
        if dimensions is not None:
            payload['dimensions'] = dimensions
        return payload

    def embedding(self, text: str, purpose: str = "embedding") -> list[float]:
        data = self._call("embedding", purpose, self._embedding_payload(text))
        try:
            return [float(v) for v in data["data"][0]["embedding"]]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelError("configuration", "embedding response has no vector") from exc

    def json_chat(self, messages: list[dict[str, str]], purpose: str, max_tokens: int = 3500, *,
                  batch_id: int | None = None) -> tuple[dict[str, Any], str, str | None, str, str | None]:
        deadline = self.monotonic() + self.timeout_for("chat", purpose)
        reply = self.chat(messages, purpose, max_tokens, batch_id=batch_id, _deadline=deadline)
        first_raw = reply.content
        try:
            if reply.finish_reason == "length":
                raise ValueError("finish_reason=length")
            parsed, status = parse_json_object_with_status(first_raw)
            return parsed, first_raw, None, status, None
        except ValueError as first_error:
            repair_messages = messages + [
                {"role": "assistant", "content": first_raw},
                {"role": "user", "content": f"上一个回答无法解析：{first_error}。只输出修正后的完整 JSON 对象，不要解释。"},
            ]
            try:
                second = self.chat(repair_messages, purpose + "_repair", max_tokens, batch_id=batch_id, _deadline=deadline)
            except ModelError as error:
                error.first_raw = first_raw
                raise
            try:
                if second.finish_reason == "length":
                    raise ValueError("finish_reason=length")
                return parse_json_object(second.content), first_raw, second.content, "repaired", str(first_error)
            except ValueError as second_error:
                raise ModelError("retryable", f"JSON parse failed after repair (first: {first_error}; second: {second_error})",
                                 second.content, first_raw=first_raw) from second_error
