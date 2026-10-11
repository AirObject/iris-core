"""Explicit learning prompt selection must not change the default request."""
from importlib.resources import files

import pytest

from conftest import FakeGateway, msg
from iris.learning import LearningEngine, PROMPT, PROMPT_VERSION
from iris.queue import form_batch


@pytest.mark.parametrize("version", ["learning_v7", "learning_v8", "learning_v9"])
def test_explicit_prompt_version_is_used_for_learning(store, version):
    msg(store, 1, "收到")
    formed = form_batch(store, "A", version, target_count=1)
    gateway = FakeGateway({})
    engine = LearningEngine(store, gateway, prompt_version=version)
    result = engine.run_batch(formed.id, force=True)
    expected = files("iris").joinpath("prompts", version + ".md").read_text(encoding="utf-8")
    assert not result["dropped"]
    assert gateway.requests[0]["messages"][0] == {"role": "system", "content": expected}
    assert gateway.requests[0]["max_tokens"] == 16000
    with store.read() as conn:
        assert conn.execute("SELECT prompt_version FROM batches WHERE id=?", (formed.id,)).fetchone()[0] == version
    assert PROMPT_VERSION == "learning_v7"
    assert PROMPT == files("iris").joinpath("prompts", "learning_v7.md").read_text(encoding="utf-8")


def test_explicit_selection_does_not_change_other_engines_default(store):
    LearningEngine(store, FakeGateway({}), prompt_version="learning_v8")
    msg(store, 1, "收到")
    formed = form_batch(store, "A", PROMPT_VERSION, target_count=1)
    gateway = FakeGateway({})
    LearningEngine(store, gateway).run_batch(formed.id, force=True)
    assert gateway.requests[0]["messages"][0] == {"role": "system", "content": PROMPT}
    assert PROMPT_VERSION == "learning_v7"


@pytest.mark.parametrize("version", ["", "learning_v6", "learning_v10", "../learning_v9", None])
def test_unknown_prompt_version_fails_before_learning(store, version):
    gateway = FakeGateway({})
    with pytest.raises(ValueError, match="unsupported learning prompt version"):
        LearningEngine(store, gateway, prompt_version=version)
    assert gateway.requests == []
