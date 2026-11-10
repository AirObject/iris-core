"""Dropped items are saved in the batch result, so runtime vectors must stay out of them."""
import json

from conftest import FakeGateway, msg
from iris.learning import LearningEngine, PROMPT_VERSION
from iris.models import ModelConfig
from iris.queue import form_batch
from test_batches import memory


def run(store, output, hook=None):
    formed = form_batch(store, "A", PROMPT_VERSION, target_count=1, future_count=2, history_count=2)
    gateway = FakeGateway(output, hook=hook)
    gateway.configs["embedding"] = ModelConfig("fake", "", "fake-embedding")
    return formed, LearningEngine(store, gateway).run_batch(formed.id, force=True)


def stored_result(store, batch_id):
    with store.read() as conn:
        row = conn.execute("SELECT state,result_json FROM batches WHERE id=?", (batch_id,)).fetchone()
    return row["state"], json.loads(row["result_json"])


def assert_no_runtime_fields(result):
    assert all("_embedding" not in item["item"] for item in result["dropped"] if isinstance(item["item"], dict))


def test_stale_derived_memory_drop_does_not_fail_the_batch(store):
    msg(store, 1, "小林养了一只猫")
    _, seeded = run(store, {"memories": [memory()]})
    source = seeded["created"][0]
    msg(store, 2, "小林最近常去宠物店")

    def forget_source(_):
        with store.write() as conn:
            conn.execute("UPDATE memories SET lifecycle='forgotten' WHERE id=?", (source,))

    output = {"memories": [{**memory("小林常去宠物店", [2]), "derived_from": ["M1"]}]}
    # The material must offer M1 for the derived reference to pass validation.
    formed, result = run(store, output, hook=forget_source)

    assert result["created"] == []
    assert [item["reason"] for item in result["dropped"]] == ["derived memory changed or invisible"]
    assert_no_runtime_fields(result)
    state, stored = stored_result(store, formed.id)
    assert state == "succeeded"
    assert stored["dropped"][0]["item"]["content"] == "小林常去宠物店"
    assert_no_runtime_fields(stored)


def test_batch_limit_drops_stay_serializable_with_embeddings(store):
    msg(store, 1, "小林列了很多爱好")
    output = {"memories": [memory(f"小林喜欢第{i}种爱好") for i in range(22)]}

    formed, result = run(store, output)

    assert len(result["created"]) == 20
    assert [item["reason"] for item in result["dropped"]] == ["batch memory limit 20"] * 2
    assert_no_runtime_fields(result)
    state, stored = stored_result(store, formed.id)
    assert state == "succeeded"
    assert_no_runtime_fields(stored)
