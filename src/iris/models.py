"""OpenAI-compatible model gateway and safe call accounting."""

from __future__ import annotations

import json
import os
import re
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from .db import Store, now


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    api_key: str
    model: str


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
                 first_raw: str | None = None):
        super().__init__(summary)
        self.category = category
        self.summary = summary
        self.raw_output = raw_output
        self.first_raw = first_raw


def load_test_models(path: str | Path | None = None) -> dict[str, ModelConfig]:
    chosen = Path(path or os.environ.get("IRIS_TEST_MODELS", "test-models.toml"))
    data = tomllib.loads(chosen.read_text(encoding="utf-8"))
    result = {}
    for name in ("chat", "embedding"):
        group = data.get(name, {})
        result[name] = ModelConfig(
            str(group.get("base_url", "")).rstrip("/"),
            str(group.get("api_key", "")),
            str(group.get("model", "")),
        )
    return result


def _summary(message: str, key: str) -> str:
    # Provider messages sometimes include request headers; never persist them verbatim.
    clean = message.replace(key, "[REDACTED]") if key else message
    clean = re.sub(r"(?i)(authorization|api[_-]?key|bearer)\s*[:=]\s*\S+", r"\1=[REDACTED]", clean)
    return clean[:300]


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def parse_json_object(raw: str) -> dict[str, Any]:
    stripped = re.sub(r"<think\b[^>]*>.*?</think>", "", raw, flags=re.I | re.S)
    stripped = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", stripped.strip(), flags=re.I)
    start = stripped.find("{")
    if start < 0:
        raise ValueError("no JSON object")
    candidate = re.sub(r",\s*([}\]])", r"\1", stripped[start:])
    value, _ = json.JSONDecoder().raw_decode(candidate)
    if not isinstance(value, dict):
        raise ValueError("top-level value is not an object")
    return value


class Gateway:
    def __init__(self, configs: dict[str, ModelConfig], store: Store | None = None, client: httpx.Client | None = None, sleeper=time.sleep):
        self.configs = configs
        self.store = store
        self.client = client or httpx.Client()
        self._own_client = client is None
        self.sleeper = sleeper
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="iris-model")

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self._own_client:
            self.client.close()

    def _record(self, purpose: str, model: str, duration_ms: int, category: str, error: str | None,
                usage: dict[str, Any] | None = None, flags: dict[str, Any] | None = None, status_code: int | None = None) -> None:
        if not self.store:
            return
        usage = usage or {}
        flags = flags or {}
        details = usage.get("completion_tokens_details") or {}
        with self.store.write() as conn:
            conn.execute("""INSERT INTO model_calls
                (purpose,model,duration_ms,prompt_tokens,completion_tokens,reasoning_tokens,result_category,
                 error_summary,input_sensitive,output_sensitive,status_code,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (purpose, model, duration_ms, usage.get("prompt_tokens"), usage.get("completion_tokens"),
                 details.get("reasoning_tokens"), category, error, flags.get("input_sensitive"),
                 flags.get("output_sensitive"), status_code, now()))

    def _call(self, kind: str, purpose: str, payload: dict[str, Any]) -> dict[str, Any]:
        config = self.configs[kind]
        if not config.base_url or not config.model:
            raise ModelError("configuration", f"{kind} model is not configured")
        url = config.base_url + ("/chat/completions" if kind == "chat" else "/embeddings")
        headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}
        timeout = httpx.Timeout(120 if kind == "chat" else 30, connect=10)
        for attempt in range(3):
            started = time.monotonic()
            response: httpx.Response | None = None
            try:
                total_timeout = 120 if kind == "chat" else 30
                request = self._pool.submit(self.client.post, url, headers=headers, json=payload, timeout=timeout)
                response = request.result(timeout=total_timeout)
                duration = round((time.monotonic() - started) * 1000)
                status = response.status_code
                if status >= 400:
                    if status in (401, 403):
                        category = "authentication"
                    elif status in (402, 423):
                        category = "account"
                    elif status == 429 or status >= 500:
                        category = "retryable"
                    elif status in (400, 404, 422):
                        category = "configuration"
                    else:
                        category = "configuration"
                    # Do not store provider response bodies; they may echo credentials or user data.
                    summary = f"HTTP {status}"
                    self._record(purpose, config.model, duration, category, summary, status_code=status)
                    if category == "retryable" and attempt < 2:
                        self.sleeper(max((2, 8)[attempt], _retry_after(response.headers.get("Retry-After")) or 0))
                        continue
                    raise ModelError(category, summary)
                data = response.json()
                flags = {
                    "input_sensitive": data.get("input_sensitive"),
                    "output_sensitive": data.get("output_sensitive"),
                }
                body = data.get("base_resp") or {}
                provider_status = body.get("status_code")
                content = None
                if kind == "chat":
                    choices = data.get("choices") or []
                    content = (choices[0].get("message") or {}).get("content") if choices else None
                    if any(flags.values()) and not content:
                        self._record(purpose, config.model, duration, "content_rejection", "provider content safety refusal", data.get("usage"), flags, provider_status or status)
                        raise ModelError("content_rejection", "provider content safety refusal")
                if provider_status not in (None, 0, 200) and not content:
                    summary = f"provider status {provider_status}"
                    provider_message = str(body.get("status_msg") or (data.get("error") or {}).get("message") or "").casefold()
                    category = "account" if any(term in provider_message for term in
                                                ("balance", "quota", "credit", "billing", "余额", "欠费", "额度")) else "configuration"
                    self._record(purpose, config.model, duration, category, summary, flags=flags, status_code=provider_status)
                    raise ModelError(category, summary)
                self._record(purpose, config.model, duration, "success", None, data.get("usage"), flags, provider_status or status)
                return data
            except (httpx.TransportError, FutureTimeout) as exc:
                duration = round((time.monotonic() - started) * 1000)
                summary = "total timeout" if isinstance(exc, FutureTimeout) else _summary(type(exc).__name__, config.api_key)
                self._record(purpose, config.model, duration, "retryable", summary)
                if attempt < 2:
                    self.sleeper((2, 8)[attempt])
                    continue
                raise ModelError("retryable", summary) from exc
            except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
                duration = round((time.monotonic() - started) * 1000)
                summary = _summary(f"invalid provider response: {type(exc).__name__}", config.api_key)
                self._record(purpose, config.model, duration, "configuration", summary, status_code=response.status_code if response else None)
                raise ModelError("configuration", summary) from exc
        raise AssertionError("unreachable")

    def chat(self, messages: list[dict[str, str]], purpose: str, max_tokens: int = 3500) -> ModelReply:
        data = self._call("chat", purpose, {"model": self.configs["chat"].model, "messages": messages,
                                           "response_format": {"type": "json_object"}, "max_tokens": max_tokens})
        choice = (data.get("choices") or [{}])[0]
        return ModelReply(str((choice.get("message") or {}).get("content") or ""), choice.get("finish_reason"),
                          data.get("usage") or {}, data.get("input_sensitive"), data.get("output_sensitive"),
                          (data.get("base_resp") or {}).get("status_code"))

    def embedding(self, text: str, purpose: str = "embedding") -> list[float]:
        data = self._call("embedding", purpose, {"model": self.configs["embedding"].model, "input": text})
        try:
            return [float(v) for v in data["data"][0]["embedding"]]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelError("configuration", "embedding response has no vector") from exc

    def json_chat(self, messages: list[dict[str, str]], purpose: str, max_tokens: int = 3500) -> tuple[dict[str, Any], str, str | None, str, str | None]:
        reply = self.chat(messages, purpose, max_tokens)
        first_raw = reply.content
        try:
            if reply.finish_reason == "length":
                raise ValueError("finish_reason=length")
            return parse_json_object(first_raw), first_raw, None, "direct", None
        except ValueError as first_error:
            repair_messages = messages + [
                {"role": "assistant", "content": first_raw},
                {"role": "user", "content": f"上一个回答无法解析：{first_error}。只输出修正后的完整 JSON 对象，不要解释。"},
            ]
            try:
                second = self.chat(repair_messages, purpose + "_repair", max_tokens)
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
