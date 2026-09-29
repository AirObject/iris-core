import json
from pathlib import Path

from iris.evaluation import (_combine_judges, _duplicate_goals, _metrics,
                             _pace_for_entry, _parse_failure_records)


def test_learning_metrics_use_new_memories_and_required_facts_as_denominators():
    row = {
        "case": {"messages": [{}, {}, {}]},
        "actual": {
            "attempts": [{"parse_status": "direct", "duration_ms": 100, "error": None}],
            "batches": [{"state": "succeeded"}],
            "calls": [
                {"purpose": "learning", "prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": 2},
                {"purpose": "learning_judge", "prompt_tokens": 20, "completion_tokens": 4, "reasoning_tokens": 1},
            ],
        },
        "judge": {
            "memory_results": [
                {"id": 1, "correct_worth": True, "forbidden": False, "evidence_correct": True, "attribution_correct": True},
                {"id": 2, "correct_worth": False, "forbidden": True, "evidence_correct": False, "attribution_correct": False},
            ],
            "fact_covered": [True, False], "link_covered": [], "goal_covered": [],
        },
    }
    metrics = _metrics([row])
    assert metrics["precision"] == 0.5
    assert metrics["fact_recall"] == 0.5
    assert metrics["false_memory_rate"] == 0.5
    assert metrics["evidence_accuracy"] == 0.5
    assert metrics["attribution_accuracy"] == 0.5
    assert metrics["parse_direct"] == 1.0
    assert metrics["learning_tokens"] == {"prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": 2}
    assert metrics["judge_tokens"] == {"prompt_tokens": 20, "completion_tokens": 4, "reasoning_tokens": 1}


def test_frozen_learning_corpus_has_splits_and_required_annotations():
    source = Path(__file__).resolve().parents[1] / "evals" / "learning_v1.jsonl"
    cases = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
    assert len(cases) == 36
    assert all(case["split"] == "dev" for case in cases)
    assert next(case for case in cases if case["id"] == "L018")["must"][0]["stance"] == "转述"
    assert all(len(case["messages"]) >= 6 for case in cases)
    assert all(all(key in case for key in ("must", "forbidden", "links", "goals")) for case in cases)
    tags = {tag for case in cases for tag in case["tags"]}
    assert {"injection", "roleplay", "nickname", "sensitive", "smalltalk", "crowd", "attribution"} <= tags


def test_learning_v2_is_frozen_at_realistic_entry_lengths_with_new_holdout():
    source = Path(__file__).resolve().parents[1] / "evals" / "learning_v2.jsonl"
    cases = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
    assert len(cases) == 40
    assert len({case["id"] for case in cases}) == 40
    assert sum(case["split"] == "holdout" for case in cases) == 16
    assert sum(len(case["must"]) for case in cases if case["split"] == "holdout") >= 60
    assert sum(len(case["messages"]) for case in cases) == 1056
    assert all(len(case["must"]) >= 3 for case in cases)
    for case in cases:
        kind = case["entry_type"]
        count = len(case["messages"])
        assert 15 <= count <= 40 if kind == "private" else 30 <= count <= 80
        if kind in ("group", "live"):
            assert len({m["speaker"] for m in case["messages"] if m["speaker"] != "场景"}) >= 5
        assert all(set(("fact", "speaker", "about", "about_optional", "stance")) <= fact.keys()
                   for fact in case["must"])
        assert all(set(("at", "speaker", "type", "content")) <= message.keys()
                   for message in case["messages"])
        assert all(key in case for key in ("forbidden", "links", "goals"))
    tags = {tag for case in cases for tag in case["tags"]}
    assert {"injection", "roleplay", "nickname", "sensitive", "smalltalk", "crowd",
            "attribution", "quote_present", "event_action", "correction", "repeat",
            "cross_batch", "cross_midnight", "relative_time", "long_message"} <= tags
    assert _pace_for_entry("private") == _pace_for_entry("group") == "standard"
    assert _pace_for_entry("live") == _pace_for_entry("stream") == "realtime"


def test_two_judgments_list_every_disagreement_and_score_conservatively():
    first = {"memory_results": [{"id": 7, "correct_worth": True, "forbidden": False,
                                 "evidence_correct": True, "attribution_correct": True}],
             "fact_covered": [True], "link_covered": [True], "goal_covered": [True],
             "actual_link_correct": [True]}
    second = {"memory_results": [{"id": 7, "correct_worth": False, "forbidden": True,
                                  "evidence_correct": False, "attribution_correct": True}],
              "fact_covered": [False], "link_covered": [False], "goal_covered": [True],
              "actual_link_correct": [False]}
    scored, differences, decisions = _combine_judges(first, second, "V021")
    assert decisions == 8 and len(differences) == 6
    assert scored["memory_results"][0] == {"id": 7, "correct_worth": False, "forbidden": True,
                                            "evidence_correct": False, "attribution_correct": True}
    assert scored["fact_covered"] == [False]
    assert scored["actual_link_correct"] == [False]
    assert all(item["case"] == "V021" for item in differences)


def test_duplicate_goal_count_is_entry_scoped_and_parse_quote_fix_is_direct():
    goals = [{"kind": "normal", "content": "周五问小林面试结果", "entry_id": "A", "state": "open", "deadline": None},
             {"kind": "normal", "content": "周五问小林的面试结果", "entry_id": "A", "state": "open", "deadline": None},
             {"kind": "normal", "content": "周五问小林面试结果", "entry_id": "B", "state": "open", "deadline": None},
             {"kind": "question", "content": "小林喜欢猫吗？", "entry_id": "A", "state": "open", "deadline": None},
             {"kind": "question", "content": "小林喜欢猫吗", "entry_id": "A", "state": "open", "deadline": None}]
    assert _duplicate_goals(goals) == {"normal": 1, "question": 1}
    row = {"case": {"messages": [{}]},
           "actual": {"attempts": [{"parse_status": "quote_repaired", "duration_ms": 5}],
                      "batches": [{}], "calls": [], "goals": goals},
           "judge": {"memory_results": [], "fact_covered": [True], "link_covered": [],
                     "goal_covered": [], "actual_link_correct": [True, False]},
           "judge_inconsistencies": [{}], "judge_decisions": 8}
    metrics = _metrics([row])
    assert metrics["parse_direct"] == 1.0 and metrics["parse_quote_repaired"] == 1
    assert metrics["link_precision"] == 0.5
    assert metrics["goal_duplicates"] == 1 and metrics["question_duplicates"] == 1
    assert metrics["judge_inconsistency_rate"] == 0.125


def test_first_invalid_output_is_capped_and_known_key_is_redacted():
    row = {"case": {"id": "V021"}, "actual": {"attempts": [
        {"batch_id": 3, "number": 1, "parse_status": "repaired", "error": "parse error",
         "raw_output": "before-secret-value-" + "x" * 2200},
        {"batch_id": 4, "number": 1, "parse_status": "quote_repaired", "error": None,
         "raw_output": "ignored"}]}}
    records = _parse_failure_records([row], "secret-value")
    assert len(records) == 1
    assert records[0]["case"] == "V021" and records[0]["batch"] == 3
    assert len(records[0]["first_raw"]) == 2000
    assert "secret-value" not in records[0]["first_raw"]
