"""Learning, reply partition and scheduler integration for shared goals."""
from datetime import timedelta

import pytest

from conftest import FakeGateway, msg
from fake_openai import Clock
from iris.goals import Goals
from iris.learning import LearningEngine, PROMPT_VERSION
from iris.queue import form_batch
from iris.retrieval import Retrieval
from iris.scheduler import Scheduler
from test_batches import memory


def learn(store, clock, output, *, entry="A", count=1):
    formed = form_batch(store, entry, PROMPT_VERSION, target_count=count)
    assert formed is not None
    gateway = FakeGateway(output)
    result = LearningEngine(store, gateway, clock=clock).run_batch(formed.id, force=True)
    return gateway, result


def test_S16_learning_commit_records_message_and_promise_memory_evidence(store):
    clock = Clock()
    person_message = msg(store, 1, "请帮我预约牙医", sender="小林")
    promise_message = msg(store, 2, "我答应为小林预约牙医", sender="我", kind="self_output")
    promise = {**memory("我答应为小林预约牙医", [2], "我", ["我", "小林"], "亲历"), "type": "计划"}
    gateway, result = learn(store, clock, {
        "memories": [promise],
        "goals": [{"content": "为小林预约牙医", "deadline": (clock() + timedelta(days=2)).isoformat(),
                   "evidence": [1, 2]}],
    }, count=2)
    assert len(result["created"]) == 1
    with store.read() as conn:
        goal_id = conn.execute("SELECT id FROM goals WHERE merged_into IS NULL").fetchone()[0]
        person_id = conn.execute("SELECT sender_subject_id FROM messages WHERE id=?", (person_message,)).fetchone()[0]
        sources = {r[0] for r in conn.execute("SELECT message_id FROM goal_sources WHERE goal_id=?", (goal_id,))}
        related_memories = [tuple(r) for r in conn.execute(
            "SELECT memory_id,memory_revision FROM goal_memories WHERE goal_id=?", (goal_id,))]
    goal = Goals(store, clock=clock).get(goal_id)
    assert goal["origin"] == "internal" and goal["state"] == "open"
    assert goal["entry_id"] == "A" and goal["people"] == [person_id]
    assert sources == {person_message, promise_message}
    assert related_memories == [(result["created"][0], 1)]
    assert [m["id"] for m in goal["promise_memories"]] == result["created"]
    assert len(gateway.requests) == 1 and gateway.requests[0]["purpose"] == "learning"


def test_learning_delegates_inside_existing_write_transaction_with_injected_clock(store, monkeypatch):
    import iris.learning as learning

    clock = Clock()
    source = msg(store, 1, "我答应整理采访记录", sender="我", kind="self_output")
    original = learning.write_learning_goal
    calls = []

    def capture(conn, **values):
        assert conn is store._writer and conn.in_transaction
        calls.append(values)
        return original(conn, **values)

    monkeypatch.setattr(learning, "write_learning_goal", capture)
    learn(store, clock, {"goals": [{"content": "整理采访记录", "evidence": [1]}],
                         "questions": ["采访记录采用什么格式？"]})
    assert [call["kind"] for call in calls] == ["normal", "question"]
    assert all(call["current"] == clock() and call["evidence"] == [source] for call in calls)
    with store.read() as conn:
        assert conn.execute("SELECT deadline FROM goals WHERE kind='question'").fetchone()[0] is None


def test_learning_infers_known_alias_but_keeps_same_named_people_distinct(store):
    clock = Clock()
    person_message = msg(store, 1, "你好", entry="A", sender="小林")
    another = msg(store, 1, "你好", entry="B", sender="小林")
    with store.write() as conn:
        person_id = conn.execute("SELECT sender_subject_id FROM messages WHERE id=?", (person_message,)).fetchone()[0]
        other_id = conn.execute("SELECT sender_subject_id FROM messages WHERE id=?", (another,)).fetchone()[0]
        conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES(?,?)", (person_id, "林老师"))
        conn.execute("UPDATE messages SET learning_state='learned' WHERE id IN (?,?)", (person_message, another))
    msg(store, 2, "我会将诗集交给林老师", sender="我", kind="self_output")
    learn(store, clock, {"goals": [{"content": "将诗集交给林老师", "evidence": [1]}]})
    with store.read() as conn:
        goal_id = conn.execute("SELECT id FROM goals WHERE merged_into IS NULL").fetchone()[0]
    assert person_id != other_id and Goals(store, clock=clock).get(goal_id)["people"] == [person_id]


def test_S10_prepare_shares_multiple_goals_and_questions_across_entries(store):
    clock = Clock()
    msg(store, 1, "你好", entry="A")
    msg(store, 1, "你好", entry="B")
    goals = Goals(store, clock=clock)
    normal = goals.create(content="参观植物园", entry_id="B")["goal"]
    question = goals.create(content="展馆周末几点开放？", kind="question", entry_id="B")["goal"]
    closed = goals.create(content="整理旧照片", entry_id="A")["goal"]
    with store.write() as conn:
        conn.execute("UPDATE goals SET state='completed' WHERE id=?", (closed["id"],))
    retrieval = Retrieval(store, clock=clock)
    prepared = retrieval.prepare("A", text="", participants=[], judge=False)
    assert {g["id"] for g in prepared["goals"]} == {normal["id"], question["id"]}
    assert {g["kind"] for g in prepared["goals"]} == {"normal", "question"}
    assert all(g["entry_id"] == "B" for g in prepared["goals"])
    assert "goals" not in retrieval.search(text="")
    assert {g["id"] for g in retrieval.search(text="", include_goals=True)["goals"]} == {
        normal["id"], question["id"]}
    assert len(retrieval.prepare("A", text="", goal_limit=1, judge=False)["goals"]) == 1
    assert retrieval.prepare("A", text="", goal_limit=0, judge=False)["goals"] == []


def test_goal_partition_receives_current_people_and_time_for_prepare_and_search(store, monkeypatch):
    import iris.retrieval as retrieval_module

    clock = Clock()
    source = msg(store, 1, "你好", sender="小林")
    with store.read() as conn:
        person_id = conn.execute("SELECT sender_subject_id FROM messages WHERE id=?", (source,)).fetchone()[0]
    original = retrieval_module.goal_partition
    calls = []

    def capture(conn, **values):
        calls.append(values)
        return original(conn, **values)

    monkeypatch.setattr(retrieval_module, "goal_partition", capture)
    retrieval = Retrieval(store, clock=clock)
    retrieval.prepare("A", text="", participants=[person_id], judge=False)
    retrieval.search(text="", people=["小林"], include_goals=True)
    assert [call["entry_id"] for call in calls] == ["A", None]
    assert all(call["participants"] == [person_id] and call["current"] == clock() for call in calls)
    with store.read() as conn:
        assert Retrieval._goals(conn, "A", 10) == []  # Existing admin snapshot caller.


@pytest.mark.parametrize("blocked_by", ["learning_paused", "learning_capacity"])
def test_scheduler_publishes_goal_reminders_even_when_learning_cannot_run(store, blocked_by):
    class PausedHealth:
        def due_probes(self):
            return ()

        def allowed(self, kind):
            return False

        def learning_allowed(self):
            return False

    clock = Clock()
    gateway = FakeGateway()
    if blocked_by == "learning_paused":
        gateway.health = PausedHealth()
    else:
        store.set_setting("learning_concurrency", 0)
    goals = Goals(store, clock=clock)
    goal = goals.create(content="发送展览报名表", deadline=(clock() + timedelta(hours=2)).isoformat(),
                        reminder_minutes=60)["goal"]
    scheduler = Scheduler(store, gateway, clock=clock)
    try:
        assert scheduler.goals.clock is clock
        scheduler.tick()
        assert goals.get(goal["id"])["notifications"] == []
        clock.advance(3600)
        scheduler.tick()
        assert len(goals.get(goal["id"])["notifications"]) == 1
        scheduler.tick()
        assert len(goals.get(goal["id"])["notifications"]) == 1
        assert goals.get(goal["id"])["state"] == "open"
    finally:
        scheduler.stop()


def test_search_unknown_person_still_returns_shared_goal_partition(store):
    clock = Clock()
    goal = Goals(store, clock=clock).create(content="整理展览目录")["goal"]
    result = Retrieval(store, clock=clock).search(text="", people=["从未出现的人"], include_goals=True)
    assert result["memories"] == []
    assert [item["id"] for item in result["goals"]] == [goal["id"]]


@pytest.mark.parametrize("boundary", ["long_content", "many_people"])
def test_learning_goal_external_input_limits_do_not_roll_back_valid_memories(store, boundary):
    clock = Clock()
    people = []
    content = "整理" + "资料" * 1999 + "。"
    if boundary == "many_people":
        people = [(f"person-{i:03}", f"访客{i:03}") for i in range(101)]
        with store.write() as conn:
            conn.executemany("INSERT INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)",
                             [(sid, name, clock().isoformat()) for sid, name in people])
        content = "逐一联系" + "、".join(name for _, name in people)
    msg(store, 1, "我答应整理采访记录", sender="我", kind="self_output")
    promise = {**memory("我答应整理采访记录", [1], "我", ["我"], "亲历"), "type": "计划"}
    _, result = learn(store, clock, {"memories": [promise],
                                   "goals": [{"content": content, "evidence": [1]}]})
    assert len(result["created"]) == 1
    with store.read() as conn:
        assert conn.execute("SELECT state FROM batches").fetchone()[0] == "succeeded"
        goal_id = conn.execute("SELECT id FROM goals WHERE merged_into IS NULL").fetchone()[0]
    goal = Goals(store, clock=clock).get(goal_id)
    assert goal["content"] == content and goal["people"] == [sid for sid, _ in people]
    assert [item["id"] for item in goal["promise_memories"]] == result["created"]
