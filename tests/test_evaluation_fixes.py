import json
from pathlib import Path

from iris import evaluation
from iris.cli import main
from iris.evaluation import _case_data, _combine_judges, _judge, _metrics, run_learning_eval
from iris.models import ModelConfig

from conftest import FakeGateway, batch, msg
from test_batches import memory


def test_evaluation_collects_aliases_subject_ids_and_provenance(store):
    msg(store, 1, "叫我阿灯，我喜欢薄荷茶")
    batch(store, FakeGateway({"people": [{"name": "P1", "alias": "阿灯", "evidence": [1]}],
                              "memories": [memory("小林喜欢薄荷茶")]}))
    actual = _case_data(store)
    person = next(p for p in actual["identities"] if p["name"] == "小林")
    assert person["aliases"] == ["阿灯"]
    assert actual["memories"][0]["about_subject_ids"] == [person["id"]]
    assert actual["memories"][0]["evidence"][0]["sender_subject_id"] == person["id"]
    link = actual["links"][0]
    assert (link["kind"], link["a"], link["b"]) == ("alias", "小林", "阿灯")
    assert link["subject_a"] == person["id"] and link["source_message_id"] == 1
    assert link["evidence_content"] == "叫我阿灯，我喜欢薄荷茶"
    assert actual["aliases"][0]["alias"] == "阿灯"


def test_alias_judging_keeps_quoted_declaration_content(store):
    from iris.queue import add_message
    add_message(store, entry_id="A", entry_name="A", platform="test", entry_kind="group", kind="message",
                sender="小王", content="转发原话", occurred_at="2026-10-01T09:00:00+08:00", dedupe_key="quote",
                quote_author="小林", quote_content="叫我阿灯就行")
    batch(store, FakeGateway({"people": [{"name": "P2", "alias": "阿灯", "evidence": [1]}]}))
    actual = _case_data(store)
    assert actual["links"][0]["evidence_quote_content"] == "叫我阿灯就行"


def test_alias_metrics_and_truncated_batches_do_not_mix_with_other_links_or_judge():
    first = {"case": {"id": "A", "messages": [{}], "links": [{"kind": "alias"}, {"kind": "roleplay"}]},
             "actual": {"attempts": [{"parse_status": "repaired", "duration_ms": 400}], "batches": [{"id": 1}],
                        "links": [{"kind": "alias"}, {"kind": "alias"}, {"kind": "roleplay"}],
                        "calls": [
                            {"purpose": "learning", "batch_id": 1, "finish_reason": "length", "completion_tokens": 16000, "duration_ms": 90000},
                            {"purpose": "learning_repair", "batch_id": 1, "finish_reason": "length", "completion_tokens": 16000, "duration_ms": 95000},
                            {"purpose": "learning_judge", "batch_id": None, "finish_reason": "length", "completion_tokens": 6000, "duration_ms": 30000},
                            {"purpose": "learning_judge", "batch_id": None, "finish_reason": None, "duration_ms": 120000, "error_summary": "total timeout"},
                            {"purpose": "learning_judge_repair", "batch_id": None, "finish_reason": None, "duration_ms": 120000, "error_summary": "ReadTimeout"}]},
             "judge": {"memory_results": [], "fact_covered": [], "goal_covered": [],
                       "link_covered": [True, False], "actual_link_correct": [True, False, True]}}
    second = {"case": {"id": "B", "messages": [{}], "links": [{"kind": "alias"}]},
              "actual": {"attempts": [{"parse_status": "direct", "duration_ms": 200}], "batches": [{"id": 1}],
                         "links": [], "calls": [
                             {"purpose": "learning", "batch_id": 1, "finish_reason": "length", "completion_tokens": 16000, "duration_ms": 80000}]},
              "judge": {"memory_results": [], "fact_covered": [], "goal_covered": [], "link_covered": [False], "actual_link_correct": []}}
    metric = _metrics([first, second])
    assert metric["length_truncated_batches"] == 2
    assert metric["length_truncated_calls"] == 3
    assert metric["judge_length_truncated_calls"] == 1
    assert metric["judge_timeout_calls"] == 2
    assert metric["learning_timeout_calls"] == 0
    assert metric["alias_recall"] == metric["alias_precision"] == 0.5
    assert metric["required_aliases"] == metric["new_aliases"] == 2
    assert metric["learning_max_completion_tokens"] == 16000
    assert metric["learning_call_max_ms"] == 95000


def test_judge_v3_rejects_array_string_names_and_wrong_alias_relation(store):
    msg(store, 1, "叫我阿灯")
    actual = _case_data(store)
    actual["links"] = [{"kind": "same_as", "a": "小林", "b": "阿灯"},
                       {"kind": "roleplay", "a": "['小林']", "b": "阿灯"}]
    case = {"messages": [{"content": "叫我阿灯"}], "must": [], "forbidden": [],
            "links": [{"kind": "alias", "a": "小林", "b": "阿灯"}], "goals": []}
    fake = FakeGateway({"memory_results": [], "fact_covered": [], "goal_covered": [],
                        "link_covered": [True], "actual_link_correct": [True, True]})
    judged = _judge(fake, case, actual)
    assert judged["link_covered"] == [False]
    assert judged["actual_link_correct"] == [True, False]
    assert evaluation.SCORING_VERSION == "scoring_v3"
    assert "评分说明 v3" in fake.requests[0]["messages"][0]["content"]
    assert fake.requests[0]["max_tokens"] == 16000


def test_deterministic_link_rejection_does_not_hide_judge_disagreement():
    common = {"memory_results": [], "fact_covered": [], "goal_covered": [],
              "link_covered": [False], "actual_link_correct": [False]}
    first = {**common, "model_verdicts": {"link_covered": [True], "actual_link_correct": [True]}}
    second = {**common, "model_verdicts": {"link_covered": [False], "actual_link_correct": [False]}}
    scored, differences, count = _combine_judges(first, second, "test")
    assert count == len(differences) == 2
    assert scored["link_covered"] == scored["actual_link_correct"] == [False]
    assert all(d["first"] is True and d["second"] is False and d["scored"] is False for d in differences)


def test_fact_coverage_rejects_missing_people_and_wrong_same_name_account():
    identities = [{"id": "a", "name": "小米", "account_id": "first", "aliases": ["米糕"]},
                  {"id": "b", "name": "小米", "account_id": "second", "aliases": []},
                  {"id": "self", "name": "我", "account_id": None, "aliases": []}]
    actual = {"identities": identities, "batches": [], "goals": [], "links": [], "aliases": [],
              "memories": [{"id": 1, "speaker": "小米", "speaker_subject_id": "a", "about": ["小米"],
                            "about_subject_ids": ["a"], "stance": "亲历"}]}
    must = [{"fact": "小米的事", "speaker": "小米", "about": ["小米"], "stance": "亲历",
             "speaker_account_id": "second", "about_account_ids": ["second"]},
            {"fact": "小米和朋友的事", "speaker": "小米", "about": ["小米", "朋友"], "stance": "亲历"},
            {"fact": "米糕的事", "speaker": "米糕", "about": ["米糕"], "stance": "亲历"}]
    fake = FakeGateway({"fact_covered": [True, True, True], "memory_results": [],
                        "goal_covered": [], "link_covered": [], "actual_link_correct": []})
    result = _judge(fake, {"messages": [], "must": must, "forbidden": [], "goals": [], "links": []}, actual)
    assert result["fact_covered"] == [False, False, True]
    assert result["model_verdicts"]["fact_covered"] == [True, True, True]


class EvaluationGateway(FakeGateway):
    def __init__(self, configs, store):
        super().__init__()
        self.configs = configs

    def json_chat(self, messages, purpose, max_tokens=3500, **kwargs):
        if purpose == "learning":
            self.response = {"people": [{"name": "P1", "alias": "阿灯", "evidence": [1]}],
                             "memories": [memory("小林喜欢薄荷茶")]}
        else:
            payload = json.loads(messages[-1]["content"])
            assert payload["actual_aliases"][0]["alias"] == "阿灯"
            self.response = {"memory_results": [{"id": m["id"], "correct_worth": True, "forbidden": False,
                                                "evidence_correct": True, "attribution_correct": True}
                                               for m in payload["actual_memories"]],
                             "fact_covered": [True] * len(payload["must"]),
                             "link_covered": [True], "goal_covered": [], "actual_link_correct": [True]}
        return super().json_chat(messages, purpose, max_tokens, **kwargs)

    def close(self):
        pass


def test_external_corpus_and_out_use_production_path_and_respect_split(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, "Gateway", EvaluationGateway)
    root = tmp_path / "workspace"
    root.mkdir()
    corpus = tmp_path / "external.jsonl"
    out = tmp_path / "outside-reports"
    case = {"id": "external-dev", "split": "dev", "entry_type": "private", "tags": [],
            "messages": [{"at": "2026-10-01T09:00:00+08:00", "speaker": "小林", "type": "message",
                          "content": "叫我阿灯，我喜欢薄荷茶"}],
            "must": [], "forbidden": [], "goals": [], "links": [{"kind": "alias", "a": "小林", "b": "阿灯"}]}
    corpus.write_text(json.dumps(case, ensure_ascii=False) + "\n" +
                      json.dumps({**case, "id": "not-run", "split": "holdout"}, ensure_ascii=False), encoding="utf-8")
    configs = {"chat": ModelConfig("fake", "", "fake"), "embedding": ModelConfig("", "", "")}
    path, report = run_learning_eval(configs, root, "dev", corpus=corpus, out=out)
    assert path.parent == out and path.with_suffix(".json").exists()
    assert report["corpus"]["cases"] == 1 and report["cases"][0]["id"] == "external-dev"
    assert report["metrics"]["all"]["alias_recall"] == 1
    assert report["timeouts_seconds"] == {"learning": 120, "judge": 240}
    assert len(report["details"]) == 1
    assert report["details"][0]["actual"]["memories"]
    assert len(report["details"][0]["judges"]) == 2
    assert not (root / "evals").exists()
    assert "因长度截断的批次数" in path.read_text(encoding="utf-8")
    assert "隐藏" in path.read_text(encoding="utf-8")


def test_cli_forwards_corpus_out_and_preserves_default_arguments(tmp_path, monkeypatch):
    from iris import cli
    seen = []
    monkeypatch.setattr(cli, "load_test_models", lambda: {})
    def run(configs, root, split, *, corpus=None, out=None, judge_runs=2):
        seen.append((split, corpus, out, judge_runs))
        return tmp_path / "report.md", {"metrics": {}}
    monkeypatch.setattr(cli, "run_learning_eval", run)
    assert main(["eval", "learning", "--corpus", str(tmp_path / "外部.jsonl"), "--out", str(tmp_path / "报告")]) == 0
    assert main(["eval", "learning"]) == 0
    assert main(["eval", "learning", "--judge-runs", "1"]) == 0
    assert seen == [("all", tmp_path / "外部.jsonl", tmp_path / "报告", 2), ("all", None, None, 2), ("all", None, None, 1)]


def test_single_judge_has_no_invented_agreement(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, "Gateway", EvaluationGateway)
    case = {"id": "one", "split": "dev", "entry_type": "private", "messages": [
        {"at": "2026-10-01T09:00:00+08:00", "speaker": "小林", "type": "message", "content": "叫我阿灯，我喜欢薄荷茶"}],
        "must": [], "forbidden": [], "goals": [], "links": [{"kind": "alias", "a": "小林", "b": "阿灯"}]}
    row = evaluation._run_case({"chat": ModelConfig("fake", "", "fake"), "embedding": ModelConfig("", "", "")}, case, judge_runs=1)
    assert len(row["judges"]) == 1 and row["judge_decisions"] == 0
    assert _metrics([row])["judge_inconsistency_rate"] is None


def test_single_judge_report_does_not_claim_two_judges_agree(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, "Gateway", EvaluationGateway)
    case = {"id": "one", "split": "dev", "entry_type": "private", "messages": [
        {"at": "2026-10-01T09:00:00+08:00", "speaker": "小林", "type": "message", "content": "叫我阿灯，我喜欢薄荷茶"}],
        "must": [], "forbidden": [], "goals": [], "links": [{"kind": "alias", "a": "小林", "b": "阿灯"}]}
    corpus = tmp_path / "case.jsonl"
    corpus.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    configs = {"chat": ModelConfig("fake", "", "fake"), "embedding": ModelConfig("", "", "")}
    _, previous = run_learning_eval(configs, tmp_path, "dev", corpus=corpus, judge_runs=2)
    path, report = run_learning_eval(configs, tmp_path, "dev", corpus=corpus, judge_runs=1)
    prose = path.read_text(encoding="utf-8")
    assert "两次判分结论一致" not in prose
    assert "本次为单判" in prose
    assert "个百分点" not in prose
