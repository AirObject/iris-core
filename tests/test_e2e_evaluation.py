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


def test_U05_real_serve_subprocess_http_learning_kill_restart_prepare(tmp_path):
    corpus = tmp_path / "case.jsonl"
    script = {"id": "HTTP-restart", "split": "dev", "entries": [{"id": "a", "kind": "private", "pace": "realtime", "platform": "test"}],
              "messages": [
                  {"entry_id": "a", "dedupe_key": "one", "sender": "小林", "account_id": "lin", "kind": "message", "content": "我喜欢天文摄影", "occurred_at": "2026-10-04T09:00:00+08:00"},
                  {"entry_id": "a", "dedupe_key": "two", "sender": "Iris", "account_id": "iris", "kind": "self_output", "content": "收到。", "occurred_at": "2026-10-04T09:00:02+08:00"}],
              "checkpoints": [{"id": "ask", "entry_id": "a", "question": {"sender": "小林", "account_id": "lin", "kind": "message", "content": "我喜欢什么摄影？", "occurred_at": "2026-10-05T10:00:00+08:00", "dedupe_key": "question"},
                               "prepare": {"text": "天文摄影", "participants": [], "recent_limit": 1},
                               "expected": [{"fact": "小林喜欢天文摄影", "source_keys": ["one"]}], "forbidden": []}]}
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
    assert path.exists() and report["scripts"][0]["passed"], report["scripts"]
    row = report["details"][0]
    assert row["before_restart"]["batches"][0]["state"] == "succeeded"
    assert row["after_restart"]["batches"][0]["state"] == "succeeded"
    assert row["checkpoints"][0]["response"]["memories"][0]["content"] == "小林喜欢天文摄影"
    assert report["scripts"][0]["u04_seconds"] < 60
    assert report["judge_runs"] == 2
    assert "fake-only" not in path.read_text(encoding="utf-8")
