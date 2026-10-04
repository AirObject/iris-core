import json
from pathlib import Path

import pytest

from fake_openai import FakeOpenAI, completion
from iris.e2e_evaluation import combine_judges, load_corpus, run_e2e_eval


def test_fixed_corpus_is_handwritten_and_covers_required_scenarios():
    scripts = load_corpus(Path(__file__).resolve().parents[1] / "evals/e2e_v1.json")
    assert len(scripts) == 10
    assert len({s["id"] for s in scripts}) == 10
    assert {e["kind"] for s in scripts for e in s["entries"]} >= {"private", "group", "live"}
    assert sum(any("text" not in c.get("prepare", {}) and "participants" not in c.get("prepare", {})
                   for c in s["checkpoints"]) for s in scripts) >= 2
    assert any(m["kind"] == "self_output" for s in scripts for m in s["messages"])


def test_public_prepare_text_is_omitted_or_verbatim_question():
    scripts = load_corpus(Path(__file__).resolve().parents[1] / "evals/e2e_v1.json")
    for script in scripts:
        for checkpoint in script["checkpoints"]:
            text = checkpoint.get("prepare", {}).get("text")
            assert text is None or text == checkpoint["question"]["content"], (script["id"], checkpoint["id"])


def test_double_judge_disagreements_are_adverse_and_single_has_no_denominator():
    a = {"facts": [{"covered": True, "memory_ids": [1], "reason": "支持"}],
         "forbidden": [{"present": False, "memory_ids": [], "reason": "没有"}]}
    b = {"facts": [{"covered": False, "memory_ids": [], "reason": "没有"}],
         "forbidden": [{"present": True, "memory_ids": [2], "reason": "出现"}]}
    result = combine_judges([a, b])
    assert not result["passed"] and result["disagreements"] == 2 and result["decisions"] == 2
    assert combine_judges([a])["decisions"] == 0


@pytest.mark.parametrize("cross_entry,leak", [(False, False), (True, False), (True, True)])
def test_U05_real_serve_subprocess_http_learning_kill_restart_prepare(tmp_path, monkeypatch, cross_entry, leak):
    corpus = tmp_path / "case.jsonl"
    script = {"id": "HTTP-restart", "split": "dev", "entries": [{"id": "a", "kind": "private", "pace": "realtime", "platform": "test"}],
              "messages": [
                  {"entry_id": "a", "dedupe_key": "one", "sender": "小林", "account_id": "lin", "kind": "message", "content": "我喜欢天文摄影", "occurred_at": "2026-10-04T09:00:00+08:00"},
                  {"entry_id": "a", "dedupe_key": "two", "sender": "Iris", "account_id": "iris", "kind": "self_output", "content": "收到。", "occurred_at": "2026-10-04T09:00:02+08:00"}],
              "checkpoints": [{"id": "ask", "entry_id": "a", "question": {"sender": "小林", "account_id": "lin", "kind": "message", "content": "我喜欢什么摄影？", "occurred_at": "2026-10-05T10:00:00+08:00", "dedupe_key": "question"},
                               "prepare": {"text": "天文摄影", "participants": [], "recent_limit": 1},
                               "expected": [{"fact": "小林喜欢天文摄影", "source_keys": ["one"]}], "forbidden": []}]}
    if cross_entry:
        script["entries"][0]["kind"] = "group"
        script["entries"].append({"id": "dm", "kind": "private", "pace": "standard", "platform": "test"})
        checkpoint = script["checkpoints"][0]
        checkpoint["entry_id"] = "dm"
        checkpoint["prepare"] = {"recent_limit": 1}
        checkpoint["expected"][0].pop("source_keys")
        checkpoint["expected"][0]["sources"] = [{"entry_id": "a", "key": "one"}]
    if leak:
        from iris.e2e_evaluation import ServeProcess
        original_post = ServeProcess.post
        def leaked_post(service, path, payload):
            response = original_post(service, path, payload)
            if path.endswith("/prepare"):
                response["recent_messages"].append({"id": 1, **script["messages"][0]})
            return response
        monkeypatch.setattr(ServeProcess, "post", leaked_post)
    corpus.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    with FakeOpenAI().serve() as server:
        def handler(path, body):
            if path.endswith("embeddings"):
                return 200, {"data": [{"embedding": [1, 0]}]}, {}, 0
            if "端到端评分" in body["messages"][0]["content"]:
                material = json.loads(body["messages"][1]["content"])
                assert material["messages"][0]["message_id"] == 1
                result = {"facts": [{"covered": True, "memory_ids": [1], "reason": "覆盖"}], "forbidden": []}
            else:
                result = {"memories": [{"content": "小林喜欢天文摄影", "type": "偏好", "about": ["P1"], "speaker": "P1", "stance": "亲历", "evidence": [1]}]}
            return 200, completion(json.dumps(result, ensure_ascii=False)), {}, 0
        server.handler = handler
        path, report = run_e2e_eval({"chat": server.configs["chat"]}, tmp_path / "repo", "dev", corpus=corpus,
                                    out=tmp_path / "outside", judge_runs=2, wait_timeout=20)
    assert path.exists() and report["scripts"][0]["passed"] is not leak, report["scripts"]
    row = report["details"][0]
    assert row["before_restart"]["batches"][0]["state"] == "succeeded"
    assert row["after_restart"]["batches"][0]["state"] == "succeeded"
    assert row["checkpoints"][0]["response"]["memories"][0]["content"] == "小林喜欢天文摄影"
    assert report["scripts"][0]["u04_seconds"] < 60
    assert row["checkpoints"][0]["recent_message_isolation"]["passed"] is not leak
    assert row["checkpoints"][0]["combined"]["passed"]
    if cross_entry and not leak:
        assert {m["entry_id"] for m in row["checkpoints"][0]["response"]["recent_messages"]} == {"dm"}
        assert len(row["checkpoints"][0]["response"]["recent_messages"]) == 1
    assert report["judge_runs"] == 2
    assert "fake-only" not in path.read_text(encoding="utf-8")


def source_script():
    return {"id": "sources", "entries": [{"id": "group", "kind": "group", "pace": "realtime"},
              {"id": "dm", "kind": "private", "pace": "standard"}],
            "messages": [{"entry_id": "group", "dedupe_key": "g:1", "sender": "Lin", "kind": "message",
                          "content": "I like astronomy", "occurred_at": "2026-10-04T09:00:00+08:00"}],
            "checkpoints": [{"id": "ask", "entry_id": "dm", "question": {"sender": "Lin", "kind": "message",
                "dedupe_key": "q", "content": "What do I like?", "occurred_at": "2026-10-05T09:00:00+08:00"},
                "expected": [{"fact": "Lin likes astronomy", "sources": [{"entry_id": "group", "key": "g:1"}]}]}]}


def test_cross_entry_sources_and_latency_use_evidence_entry(tmp_path):
    from iris.e2e_evaluation import fact_latency
    script = source_script()
    path = tmp_path / "sources.json"
    path.write_text(json.dumps([script]), encoding="utf-8")
    assert load_corpus(path) == [script]
    c = script["checkpoints"][0]
    timing = fact_latency(c["expected"][0], {"covered": True, "memory_ids": [9]}, "dm",
                          {e["id"]: e for e in script["entries"]}, {("group", "g:1"): 10}, {9: 35})
    assert timing["seconds"] == 25 and timing["u04_applicable"] and timing["within_60_seconds"]


@pytest.mark.parametrize("source", [
    {"sources": [{"entry_id": "missing", "key": "g:1"}]},
    {"sources": [{"entry_id": "dm", "key": "g:1"}]},
    {"sources": [{"entry_id": "group", "key": "missing"}]},
    {"sources": []}, {"sources": "group:g:1"},
    {"sources": [{"entry_id": "group"}]},
    {"sources": [{"entry_id": "group", "key": "g:1"}], "source_keys": ["g:1"]},
])
def test_invalid_cross_entry_sources_fail_before_starting_service(tmp_path, source):
    script = source_script()
    script["checkpoints"][0]["expected"] = [{"fact": "fact", **source}]
    path = tmp_path / "sources.json"
    path.write_text(json.dumps([script]), encoding="utf-8")
    with pytest.raises(ValueError, match="source"):
        load_corpus(path)


@pytest.mark.parametrize("recent,passed", [
    ([{"id": 2, "entry_id": "dm"}, {"id": 3, "entry_id": "dm"}], True),
    ([{"id": 1, "entry_id": "group"}, {"id": 3, "entry_id": "dm"}], False),
    ([{"id": 1, "entry_id": "dm"}], False),
    ([{"id": 99, "entry_id": "dm"}], False),
    ([{"id": 3, "entry_id": "dm", "content": "foreign content"}], False),
])
def test_recent_message_isolation_checks_receipts_not_only_claimed_entry(recent, passed):
    from iris.e2e_evaluation import check_recent_messages
    sent = {1: {"entry_id": "group", "content": "group text"},
            2: {"entry_id": "dm", "content": "dm text"},
            3: {"entry_id": "dm", "content": "question"}}
    response = {"recent_messages": [{**sent.get(m["id"], {}), **m} for m in recent]}
    check = check_recent_messages(response, "dm", sent)
    assert check["passed"] is passed
