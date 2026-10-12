"""Explicit learning prompt selection must not change the default request."""
from importlib.resources import files

import pytest

from conftest import FakeGateway, msg
from iris.learning import LEARNING_MAX_TOKENS, LearningEngine, PROMPT, PROMPT_VERSION
from iris.queue import form_batch


@pytest.mark.parametrize("version", ["learning_v7", "learning_v8", "learning_v9", "learning_v10"])
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
    assert PROMPT_VERSION == "learning_v10"
    assert PROMPT == files("iris").joinpath("prompts", "learning_v10.md").read_text(encoding="utf-8")


def test_default_learning_prompt_is_v10_with_16000_output_tokens(store):
    msg(store, 1, "收到")
    gateway = FakeGateway({})
    engine = LearningEngine(store, gateway)
    assert PROMPT_VERSION == engine.prompt_version == "learning_v10"
    formed = form_batch(store, "A", PROMPT_VERSION, target_count=1)
    engine.run_batch(formed.id, force=True)
    expected = files("iris").joinpath("prompts", "learning_v10.md").read_text(encoding="utf-8")
    assert engine.prompt == PROMPT == expected
    assert gateway.requests[0]["messages"][0] == {"role": "system", "content": expected}
    assert gateway.requests[0]["max_tokens"] == LEARNING_MAX_TOKENS == 16000


@pytest.mark.parametrize("versions", [
    ("learning_v7", "learning_v8"),
    ("learning_v8", "learning_v9"),
    ("learning_v9", "learning_v10"),
    ("learning_v10", "learning_v7"),
])
def test_prompt_selection_is_isolated_between_existing_and_new_engines(store, versions):
    gateways = [FakeGateway({}) for _ in range(4)]
    engines = [LearningEngine(store, gateways[0])]
    engines.extend(LearningEngine(store, gateway, prompt_version=version)
                   for gateway, version in zip(gateways[1:3], versions))
    engines.append(LearningEngine(store, gateways[3]))
    expected_versions = ("learning_v10", *versions, "learning_v10")
    for number, (engine, gateway, version) in enumerate(zip(engines, gateways, expected_versions), 1):
        assert engine.prompt_version == version
        msg(store, number, "收到")
        formed = form_batch(store, "A", version, target_count=1)
        engine.run_batch(formed.id, force=True)
        expected = files("iris").joinpath("prompts", version + ".md").read_text(encoding="utf-8")
        assert gateway.requests[0]["messages"][0] == {"role": "system", "content": expected}
        assert gateway.requests[0]["max_tokens"] == 16000
    assert PROMPT_VERSION == "learning_v10"


@pytest.mark.parametrize("version", ["", "learning_v6", "learning_v11", "../learning_v10", None])
def test_unknown_prompt_version_fails_before_learning(store, version):
    gateway = FakeGateway({})
    with pytest.raises(ValueError, match="unsupported learning prompt version"):
        LearningEngine(store, gateway, prompt_version=version)
    assert gateway.requests == []
