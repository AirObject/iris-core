"""Frozen, individually authored regression cases; no conversation generator."""

import json
from pathlib import Path


def test_handwritten_v3_dev_cases_cover_fixes_at_real_batch_boundaries():
    root = Path(__file__).resolve().parents[1]
    cases = [json.loads(line) for line in (root / "evals/learning_v3.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(cases) == 10
    assert len({case["id"] for case in cases}) == 10
    assert all(case["split"] == "dev" for case in cases)
    for case in cases:
        assert len(case["messages"]) >= 12
        assert all({"at", "speaker", "type", "content"} <= message.keys() for message in case["messages"])
        assert [m["at"] for m in case["messages"]] == sorted(m["at"] for m in case["messages"])
        assert all({"fact", "speaker", "about", "about_optional", "stance"} <= fact.keys() for fact in case["must"])
        assert {"must", "forbidden", "links", "goals"} <= case.keys()
    by_id = {case["id"]: case for case in cases}
    assert len(by_id["F008"]["messages"]) == 12
    assert len(by_id["F008"]["must"]) == 6
    # Correction falls beyond the first 12 target + 2 future messages.
    assert "作废" in by_id["F007"]["messages"][14]["content"]
    assert len({m["account_id"] for m in by_id["F001"]["messages"] if m["speaker"] == "米粒"}) == 2
    assert all("speaker_account_id" in fact and "about_account_ids" in fact for fact in by_id["F001"]["must"])
    assert {link["kind"] for case in cases for link in case["links"]} == {"alias", "same_as", "roleplay"}
    tags = {tag for case in cases for tag in case["tags"]}
    assert {"same_name", "alias", "promise", "crowd", "confidentiality", "cross_batch", "dense_12"} <= tags
