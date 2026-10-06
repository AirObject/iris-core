"""External E2E judgments share the frozen preview input and scoring rules."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from iris import cli, e2e_evaluation as ev
from iris.models import ModelConfig
from conftest import FakeGateway
from test_e2e_evaluation import source_script


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def verdict(covered=True, forbidden=False):
    return {"facts": [{"covered": covered, "memory_ids": [9] if covered else [], "reason": "证据核对"}],
            "forbidden": [{"present": forbidden, "memory_ids": [9] if forbidden else [], "reason": "禁止项核对"}]}


@pytest.fixture
def collected(tmp_path, monkeypatch):
    script = source_script()
    script["id"] = "script/中文"
    script["checkpoints"][0]["forbidden"] = ["Lin dislikes astronomy"]
    second = copy.deepcopy(script["checkpoints"][0])
    second["id"] = "second:question"
    second["question"]["dedupe_key"] = "q2"
    script["checkpoints"].append(second)
    corpus = tmp_path / "input.json"
    write_json(corpus, [script])

    class HTTPService:
        def __init__(self, *args):
            self.messages = []
        def start(self):
            return self.status()
        def stop(self):
            pass
        def post(self, path, payload):
            entry = path.split("/")[-2]
            if path.endswith("/messages"):
                self.messages.append({**payload, "id": len(self.messages) + 1, "entry_id": entry})
                return {"message_ids": [len(self.messages)]}
            assert path.endswith("/prepare")
            return {"memories": [{"id": 9, "content": "Lin likes astronomy"}],
                    "recent_messages": [m for m in self.messages if m["entry_id"] == entry]}
        def status(self):
            return {"entries": [{"pending_count": 0}],
                    "batches": [{"id": 1, "state": "succeeded", "result": {"created": [9]}}],
                    "learning_calls_24h": [{"duration_ms": 180000, "timed_out": True},
                                           {"duration_ms": 1200, "timed_out": False}]}

    monkeypatch.setattr(ev, "ServeProcess", HTTPService)
    monkeypatch.setattr(ev, "Gateway", lambda *a, **kw: pytest.fail("external collection must not construct a judge gateway"))
    configs = {"chat": ModelConfig("https://unused.invalid", "fake-secret", "fake-learning")}
    root = tmp_path / "repo"
    path, manifest = ev.run_e2e_eval(configs, root, corpus=corpus, out=tmp_path / "run", judge_mode="external")
    return path.parent, manifest, root, corpus


def round_files(collected, directory, votes=None):
    materials, manifest, *_ = collected
    write_json(directory / "manifest.json", read_json(materials / "round-template.json"))
    for i, entry in enumerate(manifest["cases"]):
        write_json(directory / entry["judgment_file"], votes[i] if votes is not None else verdict())
    return directory


def test_materials_match_exact_preview_payload_and_contain_provenance(collected):
    materials, manifest, _, corpus = collected
    assert manifest["format_version"] == 1 and manifest["evaluation"] == "e2e"
    assert len(manifest["cases"]) == 2
    assert manifest["run"]["corpus_sha256"] == hashlib.sha256(corpus.read_bytes()).hexdigest()
    assert len(manifest["run"]["source_sha256"]) == 64
    assert (materials / "scoring.md").read_text(encoding="utf-8") == ev.SCORING
    row = read_json(materials / "run.json")["rows"][0]
    for entry, checkpoint, observed in zip(manifest["cases"], row["script"]["checkpoints"], row["checkpoints"], strict=True):
        document = read_json(materials / entry["file"])
        fake = FakeGateway(verdict())
        ev._judge(fake, {**row["script"], "messages": row["judged_messages"]}, checkpoint, observed["response"])
        assert document["input"] == json.loads(fake.requests[0]["messages"][1]["content"])
        assert document["script_id"] == entry["script_id"] == row["id"]
        assert document["checkpoint_id"] == entry["checkpoint_id"] == checkpoint["id"]
        assert document["input"]["messages"][0]["message_id"] == 1
        assert set(document["input"]) == {"messages", "question", "timezone", "expected", "forbidden", "memories"}
        assert document["source_sha256"] == manifest["run"]["source_sha256"]
        assert document["corpus_sha256"] == manifest["run"]["corpus_sha256"]
        assert "judges" not in observed and "combined" not in observed and "latencies" not in observed
    assert not list(materials.parent.glob("e2e-*.json"))
    assert "fake-secret" not in (materials / "run.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("rounds", [1, 2])
def test_external_scoring_equals_preview_judge_and_combine(collected, tmp_path, rounds):
    materials, manifest, root, _ = collected
    votes = [verdict(), verdict(False, True)]
    dirs = [round_files(collected, tmp_path / f"round{i}", [votes[i]] * 2) for i in range(rounds)]
    path, report = ev.score_e2e_judgments(materials, dirs, root, judge_model="executor", out=tmp_path / "reports")
    row = read_json(materials / "run.json")["rows"][0]
    for spec, observed, scored in zip(row["script"]["checkpoints"], row["checkpoints"], report["scripts"][0]["checkpoints"], strict=True):
        legacy = [ev._judge(FakeGateway(v), row["script"], spec, observed["response"]) for v in votes[:rounds]]
        combined = ev.combine_judges(legacy)
        assert {key: scored[key] for key in combined} == combined
        assert scored["recent_message_isolation"]["passed"]
        assert scored["latencies"][0]["u04_applicable"]
    assert report["passed"] == (1 if rounds == 1 else 0)
    assert report["judge_mode"] == "external" and report["judge_model"] == "executor"
    assert report["judge_runs"] == rounds and report["models"]["chat"] == "fake-learning"
    assert report["chat_model"] == "fake-learning"
    assert report["materials_sha256"] == manifest["materials_sha256"]
    assert len(report["judgment_rounds"]) == rounds
    assert report["timeouts_seconds"]["learning"] == 180
    assert report["learning_latency"]["timeouts"] == 1
    assert report["learning_latency"]["max_ms"] == 180000
    assert report["judge_disagreement_rate"] == (None if rounds == 1 else 1.0)
    assert len(report["judge_inconsistencies"]) == (0 if rounds == 1 else 4)
    assert report["scripts"][0]["u04_passed"] is (rounds == 1)
    assert read_json(path) == report


@pytest.mark.parametrize("mutate,match", [
    (lambda v: [], "object"),
    (lambda v: {**v, "facts": []}, "length"),
    (lambda v: {**v, "forbidden": None}, "length"),
    (lambda v: {**v, "facts": [None]}, "decision"),
    (lambda v: {**v, "facts": [{**v["facts"][0], "covered": "true"}]}, "decision"),
    (lambda v: {**v, "facts": [{**v["facts"][0], "covered": 1}]}, "decision"),
    (lambda v: {**v, "facts": [{**v["facts"][0], "memory_ids": [123]}]}, "not returned"),
    (lambda v: {**v, "facts": [{**v["facts"][0], "memory_ids": [True]}]}, "not returned"),
    (lambda v: {**v, "facts": [{**v["facts"][0], "memory_ids": [9.0]}]}, "not returned"),
    (lambda v: {**v, "facts": [{**v["facts"][0], "memory_ids": []}]}, "supporting"),
    (lambda v: {**v, "facts": [{**v["facts"][0], "reason": None}]}, "decision"),
])
def test_strict_validation_shared_with_preview(collected, tmp_path, mutate, match):
    materials, _, root, _ = collected
    invalid = mutate(verdict())
    directory = round_files(collected, tmp_path / "round", [invalid] * 2)
    with pytest.raises(ValueError, match=match):
        ev.score_e2e_judgments(materials, [directory], root, judge_model="executor", out=tmp_path / "reports")
    row = read_json(materials / "run.json")["rows"][0]
    with pytest.raises(ValueError, match=match):
        ev._judge(FakeGateway([invalid]), row["script"], row["script"]["checkpoints"][0], row["checkpoints"][0]["response"])
    assert not (tmp_path / "reports").exists()


def test_collects_missing_and_invalid_files_before_reporting(collected, tmp_path):
    materials, manifest, root, _ = collected
    first = round_files(collected, tmp_path / "first")
    second = round_files(collected, tmp_path / "second")
    (first / manifest["cases"][0]["judgment_file"]).unlink()
    write_json(second / manifest["cases"][1]["judgment_file"], verdict(True, True) | {"facts": []})
    with pytest.raises(ValueError) as error:
        ev.score_e2e_judgments(materials, [first, second], root, judge_model="executor")
    assert "round 1" in str(error.value) and "round 2" in str(error.value) and "second:question" in str(error.value)


@pytest.mark.parametrize("fault", ["round", "duplicate", "material", "scoring", "manifest", "extra", "json_keys"])
def test_material_binding_and_round_integrity(collected, tmp_path, fault):
    materials, manifest, root, _ = collected
    directory = round_files(collected, tmp_path / "round")
    dirs = [directory]
    if fault == "round":
        write_json(directory / "manifest.json", {"materials_sha256": "wrong"})
    elif fault == "duplicate":
        dirs.append(directory)
    elif fault in ("material", "scoring", "manifest"):
        target = {"material": manifest["cases"][0]["file"], "scoring": "scoring.md", "manifest": "manifest.json"}[fault]
        (materials / target).write_text("{}", encoding="utf-8")
    elif fault == "extra":
        write_json(directory / "unexpected.json", verdict())
    else:
        (directory / manifest["cases"][0]["judgment_file"]).write_text('{"facts": [], "facts": []}', encoding="utf-8")
    with pytest.raises(ValueError):
        ev.score_e2e_judgments(materials, dirs, root, judge_model="executor")


@pytest.mark.parametrize("failure", ["isolation", "drain", "unreached"])
def test_deterministic_failures_cannot_be_overruled_by_positive_judges(collected, tmp_path, failure):
    materials, manifest, root, _ = collected
    rows = read_json(materials / "run.json")["rows"]
    row = rows[0]
    if failure == "isolation":
        row["checkpoints"][0]["response"]["recent_messages"].append({"id": 1, **row["script"]["messages"][0]})
    elif failure == "drain":
        row["failure"] = "background learning did not drain before wait_timeout"
    else:
        row["checkpoints"] = []
        row["failure"] = "evaluation infrastructure: service unavailable"
    exported, new_manifest = ev.export_e2e_judgments(rows, manifest["run"], tmp_path / "changed")
    changed = (exported.parent, new_manifest, root, None)
    directory = round_files(changed, tmp_path / "round")
    _, report = ev.score_e2e_judgments(exported.parent, [directory], root, judge_model="executor", out=tmp_path / "reports")
    assert report["passed"] == 0 and report["total"] == 1
    if failure == "isolation":
        checkpoint = report["scripts"][0]["checkpoints"][0]
        assert checkpoint["passed"] and not checkpoint["recent_message_isolation"]["passed"]
    assert report["scripts"][0]["failure"]


def test_offline_score_cli_never_loads_config(collected, tmp_path, monkeypatch, capsys):
    materials, _, _, _ = collected
    directory = round_files(collected, tmp_path / "round")
    monkeypatch.setattr(cli, "load_test_models", lambda: pytest.fail("offline scorer must not load keys"))
    assert cli.main(["eval", "e2e-score", "--materials", str(materials), "--judgments", str(directory),
                     "--judge-model", "executor", "--out", str(tmp_path / "reports")]) == 0
    assert "Report:" in capsys.readouterr().out


@pytest.mark.parametrize("args", [
    ["e2e", "--judge-mode", "external", "--judge-runs", "1"],
    ["e2e-score", "--judge-model", "executor"],
    ["e2e-score", "--split", "dev"],
    ["learning", "--script", "E001"],
    ["learning", "--wait-timeout", "1"],
])
def test_cli_rejects_invalid_options_without_loading_config(monkeypatch, args):
    monkeypatch.setattr(cli, "load_test_models", lambda: pytest.fail("reject invalid options before model configuration"))
    assert cli.main(["eval", *args]) == 1


def test_script_selection_preserves_frozen_corpus_and_rejects_unknown(collected, tmp_path):
    _, _, root, corpus = collected
    configs = {"chat": ModelConfig("unused", "", "fake-learning")}
    path, manifest = ev.run_e2e_eval(configs, root, corpus=corpus, out=tmp_path / "selected",
                                    judge_mode="external", script_ids=["script/中文"])
    assert manifest["run"]["script_ids"] == ["script/中文"]
    assert manifest["run"]["corpus_sha256"] == hashlib.sha256(corpus.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="script"):
        ev.run_e2e_eval(configs, root, corpus=corpus, judge_mode="external", script_ids=["missing"])


def test_export_discards_previous_votes_and_requires_fresh_directory(collected, tmp_path):
    materials, manifest, _, _ = collected
    rows = read_json(materials / "run.json")["rows"]
    row = rows[0]
    row.update(passed=True, judges=["old-verdict"], judge_inconsistencies=["old-verdict"])
    row["checkpoints"][0].update(judges=["old-verdict"], combined={"passed": True}, latencies=["old-verdict"])
    exported, _ = ev.export_e2e_judgments(rows, manifest["run"], tmp_path / "export")
    assert "old-verdict" not in (exported.parent / "run.json").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        ev.export_e2e_judgments(rows, manifest["run"], exported.parent)


@pytest.mark.parametrize("fault", ["payload", "scoring", "mapping", "kind"])
def test_rehashed_but_inconsistent_materials_rejected(collected, tmp_path, fault):
    materials, manifest, root, _ = collected
    directory = round_files(collected, tmp_path / "round")
    if fault == "payload":
        entry = manifest["cases"][0]
        document = read_json(materials / entry["file"])
        document["input"]["question"]["content"] = "different question"
        write_json(materials / entry["file"], document)
        entry["sha256"] = hashlib.sha256((materials / entry["file"]).read_bytes()).hexdigest()
    elif fault == "scoring":
        (materials / "scoring.md").write_text("changed scoring", encoding="utf-8")
        manifest["scoring_sha256"] = hashlib.sha256((materials / "scoring.md").read_bytes()).hexdigest()
    elif fault == "mapping":
        manifest["cases"][0]["checkpoint_id"] = "wrong"
    else:
        manifest["evaluation"] = "learning"
    manifest["materials_sha256"] = ev._json_sha256({k: v for k, v in manifest.items() if k != "materials_sha256"})
    write_json(materials / "manifest.json", manifest)
    write_json(directory / "manifest.json", {"materials_sha256": manifest["materials_sha256"]})
    with pytest.raises(ValueError):
        ev.score_e2e_judgments(materials, [directory], root, judge_model="executor")
