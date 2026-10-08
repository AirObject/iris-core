"""External judging is strict at the boundary and shares legacy scoring."""

import json

import pytest

from iris import cli, evaluation as ev
from iris.models import ModelConfig
from conftest import FakeGateway
from test_evaluation_fixes import EvaluationGateway


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def learned(tmp_path, monkeypatch):
    class LearningOnly(EvaluationGateway):
        def json_chat(self, messages, purpose, *args, **kwargs):
            assert purpose in ("learning", "learning_repair"), "external mode must never judge"
            return super().json_chat(messages, purpose, *args, **kwargs)
    monkeypatch.setattr(ev, "Gateway", LearningOnly)
    case = {"id": "dev/中文", "split": "dev", "entry_type": "private", "tags": [],
            "messages": [{"at": "2026-10-01T09:00:00+08:00", "speaker": "小林", "type": "message",
                          "content": "叫我阿灯，我喜欢薄荷茶"}],
            "must": [{"fact": "小林喜欢薄荷茶", "speaker": "小林", "about": ["小林"], "stance": "亲历"}],
            "forbidden": [], "goals": [], "links": [{"kind": "alias", "a": "小林", "b": "阿灯"}]}
    corpus = tmp_path / "input.jsonl"
    write_json(corpus, case)
    root = tmp_path / "repo"
    configs = {"chat": ModelConfig("fake", "fake-secret", "fake-learning"),
               "embedding": ModelConfig("", "", "")}
    path, manifest = ev.run_learning_eval(configs, root, "dev", corpus=corpus, out=tmp_path / "run",
                                          judge_mode="external")
    return path, manifest, configs, root, corpus


def verdict(payload):
    return {"memory_results": [{"id": m["id"], "correct_worth": True, "forbidden": False,
                                "evidence_correct": True, "attribution_correct": True}
                               for m in payload["actual_memories"]],
            "fact_covered": [True] * len(payload["must"]), "link_covered": [True] * len(payload["links"]),
            "goal_covered": [True] * len(payload["goals"]),
            "actual_link_correct": [True] * len(payload["actual_links"])}


def round_files(learned, directory, mutate=None):
    path, manifest, *_ = learned
    write_json(directory / "manifest.json", {"materials_sha256": manifest["materials_sha256"]})
    for entry in manifest["cases"]:
        payload = read_json(path.parent / entry["file"])["input"]
        result = verdict(payload)
        if mutate:
            mutate(result)
        write_json(directory / entry["judgment_file"], result)
    return directory


def test_material_payload_is_exactly_legacy_input_and_resumes_learning(learned, monkeypatch):
    path, manifest, configs, root, corpus = learned
    document = read_json(path.parent / manifest["cases"][0]["file"])
    row = read_json(path.parent / "run.json")["rows"][0]
    fake = FakeGateway(verdict(document["input"]))
    ev._judge(fake, row["case"], row["actual"])
    assert document["input"] == json.loads(fake.requests[0]["messages"][1]["content"])
    assert document["case_id"] == row["case"]["id"]
    assert document["corpus_sha256"] == manifest["run"]["corpus"]["sha256"]
    assert document["source_sha256"] == manifest["run"]["source_sha256"]
    assert document["scoring_version"] == "scoring_v3"
    assert document["input"]["role"] == row["actual"]["role"] == {"name": "Iris", "self_subject_id": "self"}
    assert set(document["input"]) == {"messages", "must", "forbidden", "links", "goals", "actual_memories",
                                       "actual_links", "actual_goals", "actual_subjects", "actual_aliases", "target_segments", "role"}
    monkeypatch.setattr(ev, "_run_case", lambda *a, **kw: pytest.fail("must reuse completed learning"))
    _, resumed = ev.run_learning_eval(configs, root, "dev", corpus=corpus, out=path.parent.parent,
                                      judge_mode="external", judge_runs=1)
    assert resumed["run"]["resumed_cases"] == 1


@pytest.mark.parametrize("has_role", [False, True])
def test_export_legacy_checkpoint_removes_all_previous_scores(learned, tmp_path, monkeypatch, has_role):
    path, manifest, *_ = learned
    run = read_json(path.parent / "run.json")
    metadata = {**manifest["run"], "cases": [{"id": row["case"]["id"]} for row in run["rows"]]}
    checkpoint = tmp_path / metadata["checkpoint_signature"]
    row = run["rows"][0]
    if not has_role:
        row["actual"].pop("role")
    row.update(judge={"bias": "old-score"}, judges=[{"bias": "old-score"}],
               judge_decisions=1, judge_inconsistencies=[{"bias": "old-score"}])
    row["actual"]["calls"].append({"purpose": "learning_judge", "completion_tokens": 900,
                                      "error_summary": "old-score"})
    write_json(checkpoint / "0000.json", row)
    report = tmp_path / "old-report.json"
    write_json(report, metadata)
    monkeypatch.setattr(ev, "_run_case", lambda *a, **kw: pytest.fail("export cannot run learning"))
    exported, result = ev.export_learning_judgments(checkpoint, tmp_path / "export", checkpoint_report=report)
    assert result["run"]["source_sha256"] == metadata["source_sha256"]
    assert result["run"]["chat_model"] == "fake-learning"
    expected = read_json(path.parent / manifest["cases"][0]["file"])["input"]
    if not has_role:
        expected.pop("role")
    assert read_json(exported.parent / result["cases"][0]["file"])["input"] == expected
    for file in exported.parent.rglob("*.json"):
        text = file.read_text(encoding="utf-8")
        assert "old-score" not in text and '"judges"' not in text
    assert set(read_json(exported.parent / "run.json")["rows"][0]) == {"case", "actual"}
    # Old checkpoints require provenance rather than attributing them to today's source/model.
    with pytest.raises(ValueError, match="metadata"):
        ev.export_learning_judgments(checkpoint, tmp_path / "no-provenance")
    (checkpoint / "0000.json").unlink()
    with pytest.raises(ValueError, match="0000.json"):
        ev.export_learning_judgments(checkpoint, tmp_path / "missing", checkpoint_report=report)


@pytest.mark.parametrize("rounds", [1, 2])
def test_external_scores_equal_legacy_judge_combine_and_metrics(learned, tmp_path, rounds):
    path, manifest, _, root, _ = learned
    first = round_files(learned, tmp_path / "round1")
    def change(result):
        result["memory_results"][0].update(correct_worth=False, forbidden=True)
        result["fact_covered"][0] = False
        result["reasons"] = {"fact_covered": ["逐项理由不参与计分"]}
    directories = [first]
    if rounds == 2:
        directories.append(round_files(learned, tmp_path / "round2", change))
    report_path, report = ev.score_learning_judgments(path.parent, directories, root,
                                                     judge_model="executor-test", out=tmp_path / "reports")
    row = read_json(path.parent / "run.json")["rows"][0]
    judgments = [ev._judge(FakeGateway(read_json(directory / manifest["cases"][0]["judgment_file"])),
                           row["case"], row["actual"]) for directory in directories]
    combined, differences, decisions = (ev._combine_judges(*judgments, row["case"]["id"])
                                        if rounds == 2 else (judgments[0], [], 0))
    row.update(judge=combined, judge_inconsistencies=differences, judge_decisions=decisions)
    assert report["metrics"]["all"] == ev._metrics([row])
    assert report["details"][0]["judge"] == combined
    assert report["details"][0]["judges"] == judgments
    assert report["judge_inconsistencies"] == differences
    assert report["judge_mode"] == "external" and report["judge_model"] == "executor-test"
    assert report["chat_model"] == "fake-learning" and report["judge_runs"] == rounds
    assert report["materials_sha256"] == manifest["materials_sha256"]
    assert report["spot_check"][0]["judge"] == combined
    prose = report_path.read_text(encoding="utf-8")
    assert "外部" in prose and "executor-test" in prose and manifest["materials_sha256"] in prose
    assert "判分与学习使用同一模型" not in prose
    if rounds == 1:
        assert report["metrics"]["all"]["judge_inconsistency_rate"] is None
    else:
        assert report["metrics"]["all"]["false_memory_rate"] == 1
        assert len(differences) == 3


@pytest.mark.parametrize("mutation, problem", [
    (lambda r: r.pop("fact_covered"), "fact_covered"),
    (lambda r: r.update(memory_results=[]), "memory_results"),
    (lambda r: r.update(link_covered=[True, False]), "link_covered"),
    (lambda r: r.update(goal_covered=None), "goal_covered"),
    (lambda r: r.update(actual_link_correct=["true"]), "actual_link_correct"),
    (lambda r: r["memory_results"][0].update(correct_worth=1), "correct_worth"),
    (lambda r: r["memory_results"][0].update(id=True), "id"),
    (lambda r: r["memory_results"][0].update(id=999), "id"),
])
def test_invalid_scores_are_errors_not_false(learned, tmp_path, mutation, problem):
    path, _, _, root, _ = learned
    directory = round_files(learned, tmp_path / "round", mutation)
    with pytest.raises(ValueError, match=problem):
        ev.score_learning_judgments(path.parent, [directory], root, judge_model="executor")
    assert not (root / "evals" / "reports").exists()


def test_validation_collects_missing_cases_and_errors_across_rounds(learned, tmp_path):
    path, manifest, _, root, _ = learned
    first = round_files(learned, tmp_path / "round1")
    (first / manifest["cases"][0]["judgment_file"]).unlink()
    second = round_files(learned, tmp_path / "round2", lambda r: r.update(fact_covered=[0], goal_covered=False))
    with pytest.raises(ValueError) as caught:
        ev.score_learning_judgments(path.parent, [first, second], root, judge_model="executor")
    assert all(problem in str(caught.value) for problem in ("round1", "round2", "dev/中文", "fact_covered", "goal_covered"))


def test_fingerprint_mismatch_and_duplicate_round_rejected(learned, tmp_path):
    path, manifest, _, root, _ = learned
    directory = round_files(learned, tmp_path / "round")
    with pytest.raises(ValueError, match="independent"):
        ev.score_learning_judgments(path.parent, [directory, directory], root, judge_model="executor")
    write_json(directory / "manifest.json", {"materials_sha256": "stale"})
    with pytest.raises(ValueError, match="fingerprint"):
        ev.score_learning_judgments(path.parent, [directory], root, judge_model="executor")
    write_json(directory / "manifest.json", {"materials_sha256": manifest["materials_sha256"]})
    material = path.parent / manifest["cases"][0]["file"]
    document = read_json(material)
    document["input"]["must"] = []
    write_json(material, document)
    with pytest.raises(ValueError, match="fingerprint"):
        ev.score_learning_judgments(path.parent, [directory], root, judge_model="executor")


@pytest.mark.parametrize("field, value", [("chat_model", "other-learner"), ("judge_mode", "model"),
                                          ("judge_runs", 2), ("scoring_version", "scoring_other"),
                                          ("corpus", {"sha256": "other"})])
def test_comparison_requires_matching_model_mode_and_existing_conditions(learned, tmp_path, field, value):
    path, _, _, root, _ = learned
    directory = round_files(learned, tmp_path / "round")
    report_path, report = ev.score_learning_judgments(path.parent, [directory], root,
                                                     judge_model="executor", out=tmp_path / "reports")
    _, repeated = ev.score_learning_judgments(path.parent, [directory], root,
                                             judge_model="executor", out=tmp_path / "reports")
    assert "个百分点" in ev._report_markdown(repeated)
    repeated["previous"][field] = value
    assert "个百分点" not in ev._report_markdown(repeated)
    # An old report without explicit judge_mode represents the legacy model preview only.
    if field == "judge_mode":
        repeated["previous"].pop("judge_mode")
        assert "个百分点" not in ev._report_markdown(repeated)


def test_offline_cli_export_and_score_do_not_load_model_config(learned, tmp_path, monkeypatch):
    path, manifest, _, _, _ = learned
    monkeypatch.setattr(cli, "load_test_models", lambda: pytest.fail("offline command read model config"))
    directory = round_files(learned, tmp_path / "round")
    assert cli.main(["eval", "learning-score", "--materials", str(path.parent), "--judgments", str(directory),
                     "--judge-model", "executor", "--out", str(tmp_path / "reports")]) == 0
    checkpoint = path.parent.parent / ".lc" / manifest["run"]["checkpoint_signature"][:16]
    assert cli.main(["eval", "learning-export", "--checkpoints", str(checkpoint),
                     "--out", str(tmp_path / "reexport")]) == 0
    assert cli.main(["eval", "learning-score", "--materials", str(path.parent),
                     "--judgments", str(directory)]) == 1


def test_external_deterministic_vetoes_preserve_raw_disagreements(learned, tmp_path):
    path, manifest, _, root, _ = learned
    rows = read_json(path.parent / "run.json")["rows"]
    row = rows[0]
    row["case"]["must"][0]["speaker_account_id"] = "wrong-account"
    # Each positive vote is vetoed for a different existing structural reason.
    row["actual"]["links"] = [
        {"kind": "same_as", "a": "小林", "b": "阿灯"},
        {"kind": "roleplay", "a": "['小林']", "b": "阿灯"},
        {"kind": "alias", "a": "小林", "b": "wrong-alias", "subject_a": "missing"}]
    metadata = {**manifest["run"], "corpus": ev._corpus_info([row["case"]]), "cases": [{"id": row["case"]["id"]}]}
    checkpoint = tmp_path / metadata["checkpoint_signature"]
    write_json(checkpoint / "metadata.json", metadata)
    write_json(checkpoint / "0000.json", row)
    new_path, new_manifest = ev.export_learning_judgments(checkpoint, tmp_path / "veto-materials")
    modified = (new_path, new_manifest, None, root, None)
    first = round_files(modified, tmp_path / "positive")
    def negative(result):
        result["fact_covered"] = [False]
        result["link_covered"] = [False]
        result["actual_link_correct"] = [False, False, False]
    second = round_files(modified, tmp_path / "negative", negative)
    _, report = ev.score_learning_judgments(new_path.parent, [first, second], root,
                                            judge_model="executor", out=tmp_path / "reports")
    scored = report["details"][0]
    assert scored["judges"][0]["actual_link_correct"] == [True, False, False]
    assert scored["judges"][0]["fact_covered"] == scored["judges"][0]["link_covered"] == [False]
    assert len(report["judge_inconsistencies"]) == 5
    assert all(d["first"] and not d["second"] and not d["scored"] for d in report["judge_inconsistencies"])
    votes = [ev._judge(FakeGateway(read_json(directory / "0000.json")), row["case"], row["actual"])
             for directory in (first, second)]
    assert (scored["judge"], scored["judge_inconsistencies"], scored["judge_decisions"]) == ev._combine_judges(
        *votes, row["case"]["id"])


@pytest.mark.parametrize("content, problem", [
    ('[]', "JSON object"), ('{', "Expecting"),
    ('{"fact_covered": [], "fact_covered": [true]}', "duplicate JSON key"),
])
def test_malformed_result_documents_are_reported(learned, tmp_path, content, problem):
    path, manifest, _, root, _ = learned
    directory = round_files(learned, tmp_path / "round")
    (directory / manifest["cases"][0]["judgment_file"]).write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=problem):
        ev.score_learning_judgments(path.parent, [directory], root, judge_model="executor")


def test_duplicate_and_out_of_order_memory_ids_rejected():
    case = {"must": [], "links": [], "goals": []}
    actual = {"memories": [{"id": 1}, {"id": 2}], "links": []}
    output = verdict({"actual_memories": actual["memories"], "must": [], "links": [], "goals": [], "actual_links": []})
    output["memory_results"].reverse()
    assert len(ev._judgment_errors(output, case, actual)) == 2
    output["memory_results"][0]["id"] = 1
    assert len(ev._judgment_errors(output, case, actual)) == 1


@pytest.mark.parametrize("arguments", [
    ["learning-score", "--corpus", "unused.jsonl"],
    ["learning-export", "--calibrate"],
    ["learning-export", "--compare-embeddings"],
    ["learning-score", "--compare-embeddings"],
    ["learning", "--compare-embeddings"],
    ["learning", "--materials", "unused"],
    ["recall", "--judge-mode", "external"],
])
def test_cli_rejects_inapplicable_options_before_loading_models(monkeypatch, arguments):
    monkeypatch.setattr(cli, "load_test_models", lambda: pytest.fail("invalid command loaded model config"))
    assert cli.main(["eval", *arguments]) == 1


def test_incomplete_and_mismatched_checkpoint_cannot_be_exported(learned, tmp_path):
    path, manifest, *_ = learned
    checkpoint = path.parent.parent / ".lc" / manifest["run"]["checkpoint_signature"][:16]
    row_path = checkpoint / "0000.json"
    row = read_json(row_path)
    row["actual"]["batches"][0]["state"] = "waiting"
    write_json(row_path, row)
    with pytest.raises(ValueError, match="incomplete"):
        ev.export_learning_judgments(checkpoint, tmp_path / "export")
    row["actual"]["batches"][0]["state"] = "succeeded"
    row["case"]["messages"][0]["content"] = "modified corpus"
    write_json(row_path, row)
    with pytest.raises(ValueError, match="corpus fingerprint"):
        ev.export_learning_judgments(checkpoint, tmp_path / "export")


def test_report_redacts_full_secret_before_parse_failure_excerpt(learned, tmp_path):
    path, manifest, _, root, _ = learned
    row = read_json(path.parent / "run.json")["rows"][0]
    row.update(judge=ev._score_judgment(verdict(ev._judge_payload(row["case"], row["actual"])), row["case"], row["actual"]),
               judge_inconsistencies=[], judge_decisions=0)
    row["actual"]["attempts"][0].update(parse_status="repaired", raw_output="x" * 1998 + "fake-secret")
    report_path, report = ev._write_learning_report([row], root, tmp_path / "reports", manifest["run"],
                                                    judge_runs=1, judge_mode="model", judge_model="fake-learning",
                                                    secrets=["fake-secret"])
    assert report["parse_failures"][0]["first_raw"].endswith("[R")
    assert "fake-secret" not in report_path.with_suffix(".json").read_text(encoding="utf-8")


@pytest.mark.parametrize("tamper", ["meta.json", "metadata.json", "missing"])
def test_short_checkpoint_export_checks_full_fingerprint(learned, tmp_path, tamper):
    path, manifest, *_ = learned
    signature = manifest["run"]["checkpoint_signature"]
    checkpoint = path.parent.parent / ".lc" / signature[:16]
    assert len(checkpoint.name) == 16
    exported, _ = ev.export_learning_judgments(checkpoint, tmp_path / "valid")
    assert exported.exists()
    if tamper == "missing":
        (checkpoint / "meta.json").unlink()
    else:
        target = checkpoint / tamper
        data = read_json(target)
        field = "signature" if tamper == "meta.json" else "checkpoint_signature"
        # Same short prefix, different full identity must still be rejected.
        data[field] = signature[:16] + ("0" if signature[-1] != "0" else "1") * 48
        write_json(target, data)
    with pytest.raises(ValueError, match="fingerprint"):
        ev.export_learning_judgments(checkpoint, tmp_path / "invalid")


@pytest.mark.parametrize("has_role", [False, True])
def test_learning_score_accepts_both_material_versions(learned, tmp_path, monkeypatch, has_role):
    path, manifest, _, root, _ = learned
    if not has_role:
        run = read_json(path.parent / "run.json")
        for row in run["rows"]:
            row["actual"].pop("role", None)
        manifest["run_sha256"] = ev._write_json(path.parent / "run.json", run)
        for entry in manifest["cases"]:
            document = read_json(path.parent / entry["file"])
            document["input"].pop("role", None)
            entry["sha256"] = ev._write_json(path.parent / entry["file"], document)
        manifest["materials_sha256"] = ev._json_sha256({k: v for k, v in manifest.items() if k != "materials_sha256"})
        write_json(path, manifest)
    document = read_json(path.parent / manifest["cases"][0]["file"])
    assert ("role" in document["input"]) == has_role
    directory = round_files(learned, tmp_path / "round")
    monkeypatch.setattr(ev, "Gateway", lambda *a, **kw: pytest.fail("scoring must not call models"))
    monkeypatch.setattr(cli, "load_test_models", lambda: pytest.fail("scoring must not load model config"))
    assert cli.main(["eval", "learning-score", "--materials", str(path.parent), "--judgments", str(directory),
                     "--judge-model", "executor-test", "--out", str(tmp_path / "reports")]) == 0
    report = read_json(next((tmp_path / "reports").glob("learning-*.json")))
    assert report["metrics"]["all"]["precision"] == 1


def test_role_information_is_bound_to_learning_data(learned, tmp_path):
    path, manifest, _, root, _ = learned
    entry = manifest["cases"][0]
    document = read_json(path.parent / entry["file"])
    document["input"]["role"] = {"name": "wrong role", "self_subject_id": "self"}
    entry["sha256"] = ev._write_json(path.parent / entry["file"], document)
    manifest["materials_sha256"] = ev._json_sha256({k: v for k, v in manifest.items() if k != "materials_sha256"})
    write_json(path, manifest)
    directory = round_files(learned, tmp_path / "round")
    with pytest.raises(ValueError, match="input differs"):
        ev.score_learning_judgments(path.parent, [directory], root, judge_model="executor-test")
