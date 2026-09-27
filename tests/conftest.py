import json
from pathlib import Path

import pytest

from iris.db import Store
from iris.learning import LearningEngine, PROMPT_VERSION
from iris.models import ModelConfig, ModelError
from iris.queue import add_message, form_batch


class FakeGateway:
    def __init__(self, response=None, hook=None):
        self.response = response if response is not None else {}
        self.hook = hook
        self.materials = []
        self.configs = {"chat": ModelConfig("fake", "", "fake"), "embedding": ModelConfig("", "", "")}

    def json_chat(self, messages, purpose, max_tokens=3500):
        self.materials.append(messages[-1]["content"])
        if self.hook:
            self.hook(messages)
        if isinstance(self.response, Exception):
            raise self.response
        result = self.response.pop(0) if isinstance(self.response, list) else self.response
        if isinstance(result, Exception):
            raise result
        return result, json.dumps(result, ensure_ascii=False), None, "direct"

    def embedding(self, text, purpose="embedding"):
        return [1.0, 0.0]


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
