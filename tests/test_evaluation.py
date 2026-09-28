import json
from pathlib import Path

from iris.evaluation import _metrics


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
    assert sum(case["split"] == "dev" for case in cases) == 24
    assert sum(case["split"] == "holdout" for case in cases) == 12
    assert all(len(case["messages"]) >= 6 for case in cases)
    assert all(all(key in case for key in ("must", "forbidden", "links", "goals")) for case in cases)
    tags = {tag for case in cases for tag in case["tags"]}
    assert {"injection", "roleplay", "nickname", "sensitive", "smalltalk", "crowd", "attribution"} <= tags
