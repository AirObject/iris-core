"""Effort provenance survives collection, export, scoring, and config reloads."""
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from iris import cli, evaluation as ev, e2e_evaluation as e2e
from iris.db import Store
from iris.model_health import ModelHealth
from iris.models import Gateway, ModelConfig
from iris.service_status import service_status
from fake_openai import FakeOpenAI
from test_external_judging import learned, round_files, read_json
from test_e2e_external_judging import collected, round_files as e2e_round_files


def test_learning_effort_changes_checkpoint_and_survives_external_scoring(learned, tmp_path):
    _, _, configs, root, corpus = learned
    results = []
    for effort in ("low", "high", "low"):
        current = {**configs, "chat": replace(configs["chat"], reasoning_effort=effort)}
        path, manifest = ev.run_learning_eval(current, root, corpus=corpus, out=tmp_path / "effort",
                                              judge_mode="external")
        assert manifest["run"]["chat_reasoning_effort"] == effort
        results.append((path, manifest, current, root, corpus))
    assert results[0][1]["run"]["checkpoint_signature"] != results[1][1]["run"]["checkpoint_signature"]
    assert [r[1]["run"]["resumed_cases"] for r in results] == [0, 0, 1]
    judgments = round_files(results[0], tmp_path / "round")
    path, report = ev.score_learning_judgments(results[0][0].parent, [judgments], root,
                                              judge_model="fake-executor", out=tmp_path / "score")
    assert report["chat_reasoning_effort"] == "low"
    assert "low" in path.read_text(encoding="utf-8")
    assert "fake-secret" not in path.with_suffix(".json").read_text(encoding="utf-8")


def test_e2e_effort_bound_to_signature_and_external_report(collected, tmp_path):
    _, _, root, corpus = collected
    runs = []
    for effort in (None, "low", "high"):
        configs = {"chat": ModelConfig("https://unused.invalid", "fake-secret", "fake-learning",
                                      reasoning_effort=effort)}
        path, manifest = e2e.run_e2e_eval(configs, root, corpus=corpus, out=tmp_path / "effort",
                                         judge_mode="external")
        assert manifest["run"]["chat_reasoning_effort"] == effort
        assert len(manifest["run"]["checkpoint_signature"]) == 64
        runs.append((path.parent, manifest, root, corpus))
    assert len({r[1]["run"]["checkpoint_signature"] for r in runs}) == 3
    judgments = e2e_round_files(runs[1], tmp_path / "round")
    _, report = e2e.score_e2e_judgments(runs[1][0], [judgments], root,
                                      judge_model="fake-executor", out=tmp_path / "score")
    assert report["chat_reasoning_effort"] == "low"
    assert report["checkpoint_signature"] == runs[1][1]["run"]["checkpoint_signature"]


def test_diagnostics_reach_learning_and_service_status(store):
    with FakeOpenAI().serve() as server:
        configs = {**server.configs, "chat": replace(server.configs["chat"], reasoning_effort="low")}
        health = ModelHealth(store, configs)
        gateway = Gateway(configs, store, health=health)
        try:
            server.enqueue(body={"choices": [{"message": {"content": "{}", "reasoning_content": "abc"},
                                               "finish_reason": "stop"}]})
            gateway.chat([], "learning")
            actual = ev._case_data(store)
            status = service_status(store, SimpleNamespace(running=True, last_error=None), health)
            assert status["chat_reasoning_effort"] == "low"
            for calls in (actual["calls"], status["learning_calls_24h"]):
                assert calls[0]["reasoning_effort"] == "low"
                assert calls[0]["reasoning_present"] == 1 and calls[0]["reasoning_chars"] == 3
                assert calls[0]["reasoning_tokens"] is None
            assert status["usage"]["today"]["calls_without_usage"] == 1
        finally:
            gateway.close()


def test_models_check_displays_effort_even_when_connection_fails(tmp_path, monkeypatch, capsys):
    with FakeOpenAI().serve() as server:
        configs = {**server.configs, "chat": replace(server.configs["chat"], reasoning_effort="low")}
        monkeypatch.setattr(cli, "load_test_models", lambda: configs)
        assert cli.main(["--db", str(tmp_path / "check.db"), "models", "check"]) == 0
        assert "reasoning_effort=low" in capsys.readouterr().out
        server.enqueue(401, {"error": {"code": "AuthenticationError"}})
        assert cli.main(["--db", str(tmp_path / "check.db"), "models", "check"]) == 1
        assert "reasoning_effort=low" in capsys.readouterr().out
        configs["chat"] = replace(configs["chat"], reasoning_effort=None)
        assert cli.main(["--db", str(tmp_path / "check.db"), "models", "check"]) == 0
        assert "reasoning_effort=未配置（不发送）" in capsys.readouterr().out


def test_migration_006_preserves_old_calls_with_unknown_diagnostics(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.create_function("iris_terms", 1, lambda text: text, deterministic=True)
        conn.execute("CREATE TABLE schema_migrations(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        scripts = Path(ev.__file__).parent / "migrations"
        for script in sorted(scripts.glob("*.sql")):
            if script.name >= "006":
                continue
            conn.executescript(script.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations VALUES(?,?)", (script.name, "2026-10-06"))
        conn.execute("INSERT INTO model_calls(purpose,model,duration_ms,result_category,created_at) "
                     "VALUES('learning','legacy',10,'success','2026-10-06')")
    store = Store(path)
    try:
        with store.read() as conn:
            row = dict(conn.execute("SELECT * FROM model_calls").fetchone())
        assert row["model"] == "legacy"
        assert row["reasoning_effort"] is row["reasoning_present"] is row["reasoning_chars"] is None
        assert list(tmp_path.glob("old.db.*.bak"))
    finally:
        store.close()


def test_legacy_learning_materials_do_not_invent_an_effort(learned, tmp_path):
    path, manifest, configs, root, corpus = learned
    checkpoints = path.parent.parent / ".lc" / manifest["run"]["checkpoint_signature"][:16]
    metadata = read_json(checkpoints / "metadata.json")
    metadata.pop("chat_reasoning_effort", None)
    (checkpoints / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    new_path, legacy = ev.export_learning_judgments(checkpoints, tmp_path / "legacy-materials")
    assert "chat_reasoning_effort" not in legacy["run"]
    old = (new_path, legacy, configs, root, corpus)
    judgments = round_files(old, tmp_path / "legacy-round")
    report_path, report = ev.score_learning_judgments(new_path.parent, [judgments], root,
        judge_model="fake-executor", out=tmp_path / "legacy-score")
    assert "chat_reasoning_effort" not in report
    assert "未知（旧记录）" in report_path.read_text(encoding="utf-8")


def test_legacy_e2e_materials_remain_scoreable(collected, tmp_path):
    materials, manifest, root, corpus = collected
    metadata = dict(manifest["run"])
    metadata.pop("chat_reasoning_effort", None)
    metadata.pop("checkpoint_signature", None)
    path, legacy = e2e.export_e2e_judgments(read_json(materials / "run.json")["rows"], metadata,
                                          tmp_path / "legacy-materials")
    assert "chat_reasoning_effort" not in legacy["run"]
    old = (path.parent, legacy, root, corpus)
    judgments = e2e_round_files(old, tmp_path / "legacy-round")
    _, report = e2e.score_e2e_judgments(path.parent, [judgments], root,
        judge_model="fake-executor", out=tmp_path / "legacy-score")
    assert "chat_reasoning_effort" not in report
