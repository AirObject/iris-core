"""Design 10.2: the role's own speech alone does not strengthen someone else's memory."""
import json

import pytest

from conftest import FakeGateway, batch, msg
from iris.learning import LearningEngine, PROMPT_VERSION, source_message_ids
from iris.memory_ops import adjust_retention
from iris.models import ModelConfig
from iris.queue import form_batch
from test_batches import memory
from test_refusal_split_queue import split

REASON = "confirmation only from self output"
CONTENT = "小林喜欢在安静的地方画水彩"


def row(store, memory_id):
    with store.read() as conn:
        return dict(conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone())


def seed(store, *, speaker="小林"):
    if speaker == "我":
        msg(store, 1, CONTENT, sender="我", kind="self_output")
        proposed = memory(CONTENT, speaker="我", about=["我"])
    else:
        msg(store, 1, CONTENT)
        proposed = memory(CONTENT)
    _, result = batch(store, FakeGateway({"memories": [proposed]}), count=1)
    return result["created"][0]


def reasons(result):
    return [item["reason"] for item in result["dropped"]]


def assert_not_confirmed(store, memory_id, before, result, new_ids):
    after = row(store, memory_id)
    assert result["confirmed"] == [] and reasons(result).count(REASON) == 1
    assert after["retention"] == before["retention"]
    assert after["last_confirmed_at"] == before["last_confirmed_at"]
    assert after["revision"] == before["revision"]
    assert set(new_ids) <= source_message_ids(store, memory_id)


def assert_confirmed(store, memory_id, before, result, new_ids):
    after = row(store, memory_id)
    assert result["confirmed"] == [memory_id] and REASON not in reasons(result)
    assert after["retention"] == before["retention"]+5
    assert after["last_confirmed_at"] != before["last_confirmed_at"]
    assert after["revision"] == before["revision"]
    assert set(new_ids) <= source_message_ids(store, memory_id)


def explicit(evidence):
    return {"updates": [{"ref": "M1", "action": "确认", "evidence": evidence}]}


def duplicate(evidence, **kwargs):
    return {"memories": [memory(CONTENT, evidence, **kwargs)]}


def role_says(store, number):
    # A quote makes 小林 a legitimate speaker for the validated new-memory path.
    return msg(store, number, CONTENT, sender="我", kind="self_output", quote_author="小林")


# Each case: speaker of the memory, target-segment senders, expected confirmation.
CASES = [
    pytest.param("小林", ["role"], False, id="all_self_output"),
    pytest.param("小林", ["role", "other"], True, id="mixed"),
    pytest.param("小林", ["other", "other"], True, id="all_others"),
    pytest.param("我", ["role"], True, id="speaker_is_self"),
]


def add_targets(store, senders):
    ids = []
    for offset, sender in enumerate(senders, start=2):
        if sender == "role":
            ids.append(role_says(store, offset))
        else:
            ids.append(msg(store, offset, CONTENT))
    return ids


@pytest.mark.parametrize("speaker,senders,confirmed", CASES)
def test_explicit_confirmation(store, speaker, senders, confirmed):
    memory_id = seed(store, speaker=speaker)
    before = row(store, memory_id)
    ids = add_targets(store, senders)
    evidence = [number+2 for number in range(len(senders))]  # P1 is the history segment.
    _, result = batch(store, FakeGateway(explicit(evidence)), count=len(senders))
    (assert_confirmed if confirmed else assert_not_confirmed)(store, memory_id, before, result, ids)
    if not confirmed:
        assert result["dropped"][0]["section"] == "updates"


@pytest.mark.parametrize("speaker,senders,confirmed", CASES)
def test_duplicate_confirmation(store, speaker, senders, confirmed):
    memory_id = seed(store, speaker=speaker)
    before = row(store, memory_id)
    ids = add_targets(store, senders)
    evidence = [number+2 for number in range(len(senders))]
    extra = {"speaker": "我", "about": ["我"]} if speaker == "我" else {}
    _, result = batch(store, FakeGateway(duplicate(evidence, **extra)), count=len(senders))
    assert not result["created"]
    (assert_confirmed if confirmed else assert_not_confirmed)(store, memory_id, before, result, ids)
    if not confirmed:
        assert result["dropped"][0]["section"] == "memories"


@pytest.mark.parametrize("kind", ["action_result", "event"])
@pytest.mark.parametrize("output", [explicit, duplicate], ids=["explicit", "duplicate"])
def test_action_results_and_events_are_not_role_speech(store, kind, output):
    memory_id = seed(store)
    before = row(store, memory_id)
    new = msg(store, 2, CONTENT, sender="我", kind=kind, quote_author="小林")
    _, result = batch(store, FakeGateway(output([2])), count=1)
    assert_confirmed(store, memory_id, before, result, [new])


@pytest.mark.parametrize("output", [explicit, duplicate], ids=["explicit", "duplicate"])
def test_self_output_with_an_action_result_still_confirms(store, output):
    memory_id = seed(store)
    before = row(store, memory_id)
    ids = [role_says(store, 2), msg(store, 3, CONTENT, sender="我", kind="action_result")]
    _, result = batch(store, FakeGateway(output([2, 3])), count=2)
    assert_confirmed(store, memory_id, before, result, ids)


def test_only_evidence_inside_the_target_segment_counts(store):
    memory_id = seed(store)  # message 1, from 小林, is the history segment of the next batch
    before = row(store, memory_id)
    new = role_says(store, 2)
    _, result = batch(store, FakeGateway(explicit([1, 2])), count=1)
    assert_not_confirmed(store, memory_id, before, result, [new])


def test_forgotten_memory_is_not_restored_by_the_roles_own_speech(store):
    memory_id = seed(store)
    adjust_retention(store, memory_id, value=19)
    adjust_retention(store, memory_id, value=34)
    before = row(store, memory_id)
    assert before["lifecycle"] == "forgotten"
    role_says(store, 2)
    _, result = batch(store, FakeGateway(explicit([2])), count=1)
    assert result["confirmed"] == [] and row(store, memory_id)["lifecycle"] == "forgotten"
    assert row(store, memory_id)["retention"] == 34


def test_dropped_entry_is_stored_with_the_batch_result_without_vectors(store):
    memory_id = seed(store)
    before = row(store, memory_id)
    role_says(store, 2)
    formed = form_batch(store, "A", PROMPT_VERSION, target_count=1, future_count=2, history_count=2)
    gateway = FakeGateway(duplicate([2]))
    gateway.configs["embedding"] = ModelConfig("fake", "", "fake-embedding")
    result = LearningEngine(store, gateway).run_batch(formed.id, force=True)
    assert result["confirmed"] == [] and reasons(result) == [REASON]
    with store.read() as conn:
        stored = json.loads(conn.execute("SELECT result_json FROM batches WHERE id=?", (formed.id,)).fetchone()[0])
    assert stored["dropped"][0]["reason"] == REASON and "_embedding" not in stored["dropped"][0]["item"]
    assert row(store, memory_id)["retention"] == before["retention"]


@pytest.mark.parametrize("first,second", [("self", "other"), ("other", "self")])
def test_one_increment_per_batch_when_items_disagree(store, first, second):
    memory_id = seed(store)
    before = row(store, memory_id)
    numbers = {"self": 2, "other": 3}
    role_says(store, 2)
    msg(store, 3, CONTENT)
    output = {"updates": [{"ref": "M1", "action": "确认", "evidence": [numbers[first]]}],
              "memories": [memory(CONTENT, [numbers[second]])]}
    _, result = batch(store, FakeGateway(output), count=2)
    assert result["confirmed"] == [memory_id] and reasons(result) == [REASON]
    assert row(store, memory_id)["retention"] == before["retention"]+5


def test_repeated_confirmations_from_others_still_count_once(store):
    memory_id = seed(store)
    before = row(store, memory_id)
    msg(store, 2, CONTENT)
    output = {**explicit([2]), "memories": [memory(CONTENT, [2])]*2}
    _, result = batch(store, FakeGateway(output), count=1)
    assert result["confirmed"] == [memory_id] and not result["dropped"]
    assert row(store, memory_id)["retention"] == before["retention"]+5


def test_split_leaf_with_only_role_speech_leaves_the_tree_increment_available(store):
    memory_id = seed(store)
    before = row(store, memory_id)
    role_says(store, 2)
    msg(store, 3, CONTENT)
    root = form_batch(store, "A", PROMPT_VERSION, target_count=2, future_count=0, history_count=1)
    left, right = split(store, root.id)
    first = LearningEngine(store, FakeGateway(explicit([2]))).run_batch(left, force=True)
    assert first["confirmed"] == [] and reasons(first) == [REASON]
    assert row(store, memory_id)["retention"] == before["retention"]
    # The left target is now history, so the right target is numbered after it.
    second = LearningEngine(store, FakeGateway(explicit([3]))).run_batch(right, force=True)
    assert second["confirmed"] == [memory_id]
    assert row(store, memory_id)["retention"] == before["retention"]+5


@pytest.mark.parametrize("action", ["修正", "反驳"])
def test_corrections_and_rebuttals_are_unchanged(store, action):
    memory_id = seed(store)
    before = row(store, memory_id)
    role_says(store, 2)
    output = {"updates": [{"ref": "M1", "action": action, "content": "小林改为画油画", "evidence": [2]}]}
    _, result = batch(store, FakeGateway(output), count=1)
    assert result["updated"] == [memory_id] and not result["dropped"]
    assert row(store, memory_id)["content"] == "小林改为画油画"
    assert row(store, memory_id)["retention"] == before["retention"]
