"""M2 reporting is diagnostic only and never changes learning or judging input."""
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from iris import evaluation as ev
from iris.db import now
from iris.models import ModelConfig
from test_retrieval import put


def test_r13_duplicate_report_counts_all_pairs_and_excludes_inactive_and_setup(store):
    ids = [put(store, text) for text in (
        "我每周在文化馆练习大提琴", "我每周在文化馆练习大提琴。", "我每周在文化馆练习大提琴！")]
    for options in ({"lifecycle": "forgotten"}, {"lifecycle": "deleted"}, {"stance": "设定"}):
        put(store, "我每周在文化馆练习大提琴", **options)
    with store.read() as conn:
        before = [tuple(r) for r in conn.execute("SELECT * FROM memories ORDER BY id")]
    actual = ev._case_data(store)
    stats = actual["memory_duplicates"]
    assert stats["eligible_memories"] == 3
    assert stats["pairs"] == [[ids[0], ids[1]], [ids[0], ids[2]], [ids[1], ids[2]]]
    assert stats["memory_ids"] == ids
    assert [m["id"] for m in stats["memories"]] == ids
    assert stats["vector_memories"] == 0
    # Existing quality denominators/payload retain their exact selection rule.
    assert len(actual["memories"]) == 5
    case = {"messages": [], "must": [], "forbidden": [], "links": [], "goals": []}
    assert "memory_duplicates" not in ev._judge_payload(case, actual)
    with store.read() as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM memories ORDER BY id")] == before
        assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM recalls").fetchone()[0] == 0


@pytest.mark.parametrize("difference", ["speaker", "about", "stance", "world", "event_time", "number", "negation"])
def test_r13_duplicate_report_keeps_structural_numeric_and_negative_boundaries(store, difference):
    with store.write() as conn:
        conn.execute("INSERT INTO subjects(id,kind,name,parent_id,created_at) VALUES('other','person','我',NULL,?)", (now(),))
    text = "我每周练习大提琴3次"
    put(store, text, vector=[1, 0])
    options = {"vector": [1, 0]}
    if difference == "speaker": options["speaker"] = "other"
    if difference == "about": options["about"] = ("other",)
    if difference == "stance": options["stance"] = "观点"
    if difference == "event_time": options["event_time"] = "2026-10-09"
    if difference == "number": text = text.replace("3", "4")
    if difference == "negation": text = "我不每周练习大提琴3次"
    second = put(store, text, **options)
    if difference == "world":
        with store.write() as conn:
            conn.execute("UPDATE memories SET world='fiction' WHERE id=?", (second,))
    assert ev._case_data(store)["memory_duplicates"]["pairs"] == []


@pytest.mark.parametrize("cosine,expected", [(0.959, False), (0.961, True)])
def test_r13_duplicate_report_uses_stored_vectors_and_current_revisions(store, cosine, expected):
    a = put(store, "我习惯把旧明信片按照旅行路线分类收藏", vector=[1, 0])
    b = put(store, "那些寄回来的卡片被我按行程编入相册", vector=[cosine, np.sqrt(1-cosine*cosine)])
    with store.write() as conn:
        conn.execute("UPDATE memories SET revision=4 WHERE id=?", (b,))
    stats = ev._case_data(store)["memory_duplicates"]
    assert stats["vector_memories"] == 2
    assert stats["pairs"] == ([[a, b]] if expected else [])
    with store.write() as conn:
        conn.execute("UPDATE memories SET embedding_model='another-model' WHERE id=?", (b,))
    assert ev._case_data(store)["memory_duplicates"]["pairs"] == []


def minimal_row(case_id="case", group="learning_v4"):
    return {"case": {"id": case_id, "split": "dev", "messages": [{}], "must": [], "forbidden": [],
                     "links": [], "goals": []}, "corpus_group": group,
            "actual": {"memories": [], "links": [], "goals": [], "attempts": [], "calls": [],
                       "batches": [], "identities": [], "aliases": [],
                       "memory_duplicates": {"eligible_memories": 3, "vector_memories": 0,
                          "pairs": [[1, 2], [1, 3], [2, 3]], "memory_ids": [1, 2, 3], "memories": []}},
            "judge": {"memory_results": [], "fact_covered": [], "link_covered": [],
                      "goal_covered": [], "actual_link_correct": []},
            "judge_inconsistencies": [], "judge_decisions": 0}


def test_confirmation_counts_are_per_successful_batch_not_unique_memories_or_attempts():
    row = minimal_row()
    row["actual"]["batches"] = [
        {"id": 1, "state": "succeeded", "result_json": json.dumps({"confirmed": [7, 8]})},
        {"id": 2, "state": "succeeded", "result_json": json.dumps({"confirmed": [7]})},
        {"id": 3, "state": "abandoned", "result_json": None},
    ]
    stats = ev._confirmation_stats(row["actual"])
    assert stats == {"count": 3, "batches": [{"batch_id": 1, "memory_ids": [7, 8]},
                                            {"batch_id": 2, "memory_ids": [7]}]}
    metrics = ev._metrics([row, row])
    assert metrics["near_duplicate_pairs"] == 6
    assert metrics["near_duplicate_memories"] == 6  # IDs are local to each case.
    assert metrics["reconfirmations"] == 6
    assert metrics["precision"] is metrics["false_memory_rate"] is None
    del row["actual"]["memory_duplicates"]
    assert ev._metrics([row])["near_duplicate_pairs"] is None  # Old checkpoints are unknown, not zero.


def test_public_groups_are_micro_averaged_and_survive_export_without_leaking_into_judge():
    rows = [minimal_row(str(n), f"learning_v{n}") for n in range(1, 5)]
    for n, row in enumerate(rows, 1):
        row["judge"]["fact_covered"] = [True] + [False] * (n-1)
    grouped = ev._corpus_metrics(rows)
    assert list(grouped) == ["learning_v1", "learning_v2", "learning_v3", "learning_v4", "learning_v1+v3+v4"]
    assert grouped["learning_v1+v3+v4"]["cases"] == 3
    assert grouped["learning_v1+v3+v4"]["fact_recall"] == 3 / 8
    exported = ev._learning_rows(rows)
    assert [r["corpus_group"] for r in exported] == [r["corpus_group"] for r in rows]
    assert exported[0]["actual"]["memory_duplicates"] == rows[0]["actual"]["memory_duplicates"]
    assert "corpus_group" not in ev._judge_payload(rows[0]["case"], exported[0]["actual"])
    assert "judge" not in exported[0]


def test_default_corpus_load_includes_v4_and_reports_groups(tmp_path, monkeypatch):
    import shutil
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "repo"
    (root / "evals").mkdir(parents=True)
    for name in ev.PUBLIC_LEARNING_CORPORA:
        shutil.copyfile(repository / "evals" / (name + ".jsonl"), root / "evals" / (name + ".jsonl"))
    seen = []
    def fake_run(configs, case, *args):
        seen.append(case["id"])
        row = minimal_row(case["id"])
        row.pop("corpus_group")
        row["case"] = case
        row["judge"]["fact_covered"] = [False] * len(case["must"])
        row["judge"]["link_covered"] = [False] * len(case["links"])
        row["judge"]["goal_covered"] = [False] * len(case["goals"])
        return row
    monkeypatch.setattr(ev, "_run_case", fake_run)
    config = {"chat": ModelConfig("offline", "", "offline"), "embedding": ModelConfig("", "", "")}
    path, report = ev.run_learning_eval(config, root, out=tmp_path / "reports", judge_runs=1)
    assert len(seen) == 104
    assert report["corpus"]["messages"] == 1750
    assert report["corpus_metrics"]["learning_v4"]["cases"] == 18
    assert report["corpus_metrics"]["learning_v1+v3+v4"]["cases"] == 64
    assert report["cases"][-1]["corpus_group"] == "learning_v4"
    assert report["cases"][-1]["memory_duplicates"]["pairs"] == [[1, 2], [1, 3], [2, 3]]
    assert report["cases"][-1]["confirmations"]["count"] == 0
    prose = path.read_text(encoding="utf-8")
    assert "learning_v1+v3+v4" in prose and "近似重复对数" in prose and "再次确认" in prose


def test_learning_v4_frozen_bytes_and_batch_boundaries(monkeypatch):
    from conftest import FakeGateway
    source = Path(__file__).resolve().parents[1] / "evals" / "learning_v4.jsonl"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == "15516050d090f5ae9c5a96c3b90a412f36897713576c2db569124fa3e8638fe3"
    class EmptyGateway(FakeGateway):
        def __init__(self, configs, store): super().__init__()
        def close(self): pass
    monkeypatch.setattr(ev, "Gateway", EmptyGateway)
    configs = {"chat": ModelConfig("offline", "", "offline"), "embedding": ModelConfig("", "", "")}
    cases = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
    batches = 0
    for case in cases:
        assert case["split"] == "dev"
        row = ev._run_case(configs, case, judge_mode="external")
        targets = [json.loads(b["target_ids"]) for b in row["actual"]["batches"]]
        count = 4 if case["entry_type"] == "live" else 12
        assert targets == [list(range(start, min(start+count, len(case["messages"])+1)))
                           for start in range(1, len(case["messages"])+1, count)]
        assert len(targets) >= 2
        batches += len(targets)
    assert batches == 41


def test_request_comparison_rejects_missing_source_instead_of_importing_installed_candidate(tmp_path):
    import runpy
    script = Path(__file__).resolve().parents[1] / 'evals' / 'compare_learning_requests.py'
    worker = runpy.run_path(str(script))['worker']
    with pytest.raises(ValueError, match='source checkout'):
        worker(tmp_path / 'missing-main', tmp_path / 'requests.json', script.parents[1])
