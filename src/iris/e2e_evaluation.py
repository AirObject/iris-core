"""HTTP-only end-to-end evaluation against killed/restarted real serve processes."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path

import httpx

from .models import Gateway, ModelError, LEARNING_TOTAL_TIMEOUT, JUDGE_TOTAL_TIMEOUT
from .service_status import percentile
# Reuse PR #7's transport primitives; E2E keeps its own payload and validator.
from .evaluation import (MATERIAL_FORMAT_VERSION, _json_sha256, _read_json,
                         _write_json, _verified_material_file)


SCORING_VERSION = "e2e_scoring_v1"
SCORING = files("iris").joinpath("prompts", SCORING_VERSION + ".md").read_text(encoding="utf-8")


def load_corpus(path):
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        scripts = [json.loads(line) for line in raw.splitlines() if line.strip()]
    else:
        value = json.loads(raw)
        scripts = value["scripts"] if isinstance(value, dict) else value
    if not isinstance(scripts, list) or not scripts:
        raise ValueError("e2e corpus must contain scripts")
    ids = set()
    from .api import Message, Prepare
    for script in scripts:
        if not isinstance(script.get("id"), str) or script["id"] in ids:
            raise ValueError("missing or duplicate e2e script ID")
        ids.add(script["id"])
        if script.get("split", "dev") not in ("dev", "holdout"):
            raise ValueError("invalid split")
        entries = {e["id"]: e for e in script["entries"]}
        if not entries or len(entries) != len(script["entries"]):
            raise ValueError("missing or duplicate entry ID")
        keys = set()
        for message in script["messages"]:
            if message["entry_id"] not in entries:
                raise ValueError("unknown message entry")
            key = (message["entry_id"], message["dedupe_key"])
            if key in keys:
                raise ValueError("duplicate message key")
            keys.add(key)
            Message.model_validate(_message(entries[message["entry_id"]], message))
            if not 0 <= message.get("delay_seconds", 0) <= 3600:
                raise ValueError("invalid message delay")
        if not script["messages"] or not script["checkpoints"]:
            raise ValueError("messages and checkpoints must be nonempty")
        checkpoints = set()
        for checkpoint in script["checkpoints"]:
            if checkpoint["id"] in checkpoints:
                raise ValueError("duplicate checkpoint ID")
            checkpoints.add(checkpoint["id"])
            entry = entries[checkpoint["entry_id"]]
            Message.model_validate(_message(entry, checkpoint["question"]))
            Prepare.model_validate(checkpoint.get("prepare", {}))
            if not checkpoint["expected"] or not all(isinstance(e, dict) and isinstance(e.get("fact"), str)
                                                     and e["fact"] for e in checkpoint["expected"]):
                raise ValueError("expected must be a nonempty list of facts")
            for fact in checkpoint["expected"]:
                if any(source not in keys for source in fact_sources(fact, checkpoint["entry_id"])):
                    raise ValueError("expected facts need sources present in this script")
            if not all(isinstance(f, str) for f in checkpoint.get("forbidden", [])):
                raise ValueError("forbidden must contain strings")
    return scripts


def fact_sources(fact, default_entry):
    """Legacy local keys or explicit entry/key objects; never parse delimiters in IDs."""
    if ("source_keys" in fact) == ("sources" in fact):
        raise ValueError("expected facts need exactly one of source_keys or sources")
    if "source_keys" in fact:
        keys = fact["source_keys"]
        if not isinstance(keys, list) or not keys or any(not isinstance(k, str) or not k for k in keys):
            raise ValueError("source_keys must be a nonempty string list")
        return [(default_entry, key) for key in keys]
    sources = fact["sources"]
    if not isinstance(sources, list) or not sources or any(
            not isinstance(s, dict) or set(s) != {"entry_id", "key"}
            or any(not isinstance(s[k], str) or not s[k] for k in ("entry_id", "key")) for s in sources):
        raise ValueError("sources must be nonempty entry_id/key objects")
    return [(source["entry_id"], source["key"]) for source in sources]


def fact_latency(expected, fact, entry_id, entries, sent, first_seen):
    sources = fact_sources(expected, entry_id)
    started = min(sent[source] for source in sources)
    refs = fact["memory_ids"]
    measured = fact["covered"] and refs and all(mid in first_seen for mid in refs)
    latency = round(max(first_seen[mid] for mid in refs) - started, 3) if measured else None
    return {"fact": expected["fact"], "seconds": latency,
            "sources": [{"entry_id": eid, "key": key} for eid, key in sources],
            "u04_applicable": all(entries[eid].get("pace", "standard") == "realtime" for eid, _ in sources),
            "within_60_seconds": latency is not None and 0 <= latency <= 60}


def check_recent_messages(response, entry_id, sent_messages):
    """Check HTTP receipts and original payloads, independently of semantic judging."""
    recent = response.get("recent_messages")
    if not isinstance(recent, list):
        return {"passed": False, "checked": 0, "reason": "recent_messages is not a list"}
    invalid = []
    for message in recent:
        mid = message.get("id") if isinstance(message, dict) else None
        original = sent_messages.get(mid) if type(mid) is int else None
        if not original or original["entry_id"] != entry_id or message.get("entry_id") != entry_id or any(
                message.get(key) != original[key] for key in ("content", "kind", "quote_content") if key in original):
            invalid.append(mid)
    return {"passed": not invalid, "checked": len(recent), "invalid_message_ids": invalid}


def _message(entry, message):
    metadata = {"entry_name": entry.get("name", entry["id"]), "entry_kind": entry["kind"],
                "platform": entry.get("platform", "e2e"), "pace": entry.get("pace", "standard")}
    return {**metadata, **{k: v for k, v in message.items() if k not in ("entry_id", "delay_seconds")}}


class ServeProcess:
    """Only communicates with iris through its public HTTP API."""
    def __init__(self, directory, configs):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        config = self.directory / "test-models.toml"
        self.env = {**os.environ, "IRIS_TEST_MODELS": str(config.resolve()), "PYTHONIOENCODING": "utf-8"}
        sections = []
        for kind, value in configs.items():
            fields = asdict(value)
            env_name = f"IRIS_E2E_{kind.upper()}_API_KEY"
            self.env[env_name] = fields.pop("api_key")
            fields["api_key_env"] = env_name
            sections.append(f"[{kind}]\n" + "\n".join(
                f"{key} = {json.dumps(item, ensure_ascii=False)}" for key, item in fields.items() if item is not None))
        # Even if the evaluator is killed before cleanup, this file has no API keys.
        config.write_text("\n".join(sections), encoding="utf-8")
        # Works in editable installs and from a deep-path source checkout without installing it again.
        self.env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        self.process = None
        self.log = None
        self.client = None

    def start(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.log = (self.directory / "serve-output.log").open("ab")
        self.process = subprocess.Popen([sys.executable, "-m", "iris.cli", "--db", str(self.directory / "iris.db"),
            "serve", "--port", str(port)], env=self.env, cwd=self.directory, stdin=subprocess.DEVNULL,
            stdout=self.log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"serve exited during startup (code {self.process.returncode})")
            try:
                response = self.client.get("/api/v1/status")
                if response.status_code == 200 and response.json()["scheduler"]["running"]:
                    return response.json()
            except httpx.TransportError:
                pass
            time.sleep(.1)
        raise RuntimeError("serve startup timed out")

    def stop(self):
        if self.process:
            if self.process.poll() is None:
                if os.name == "nt":
                    # A venv's Windows python.exe can be a redirector with a real
                    # interpreter child. Kill the owned tree, then wait for the launcher.
                    subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=10, check=False)
                else:
                    self.process.kill()
            self.process.wait(timeout=10)
            self.process = None
        if self.client:
            self.client.close()
        if self.log:
            self.log.close()

    def post(self, path, payload):
        response = self.client.post(path, json=payload)
        response.raise_for_status()
        return response.json()

    def status(self):
        response = self.client.get("/api/v1/status")
        response.raise_for_status()
        return response.json()


def _judge_payload(script, checkpoint, response):
    return {"messages": script["messages"], "question": checkpoint["question"],
            "timezone": "Asia/Shanghai", "expected": checkpoint["expected"],
            "forbidden": checkpoint.get("forbidden", []), "memories": response["memories"]}


def _judge(gateway, script, checkpoint, response):
    material = _judge_payload(script, checkpoint, response)
    result = gateway.json_chat([{"role": "system", "content": SCORING},
                               {"role": "user", "content": json.dumps(material, ensure_ascii=False)}],
                              "e2e_judge", max_tokens=16000)[0]
    return _validate_judgment(result, checkpoint, response)


def _validate_judgment(result, checkpoint, response):
    """The same strict frozen scoring schema for model preview and external files."""
    if not isinstance(result, dict):
        raise ValueError("judge result must be a JSON object")
    ids = {m["id"] for m in response["memories"]}
    for name, field, expected in (("facts", "covered", checkpoint["expected"]),
                                   ("forbidden", "present", checkpoint.get("forbidden", []))):
        values = result.get(name)
        if not isinstance(values, list) or len(values) != len(expected):
            raise ValueError("judge result length mismatch")
        for value in values:
            if not isinstance(value, dict) or type(value.get(field)) is not bool or not isinstance(value.get("reason"), str):
                raise ValueError("invalid judge decision")
            refs = value.get("memory_ids")
            if not isinstance(refs, list) or any(type(mid) is not int or mid not in ids for mid in refs):
                raise ValueError("judge referenced a memory not returned by prepare")
            if value[field] and not refs:
                raise ValueError("positive judge decision has no supporting memory")
    return result


def combine_judges(judges):
    combined = {"facts": [], "forbidden": [], "disagreements": 0, "decisions": 0}
    for name, field, combine in (("facts", "covered", all), ("forbidden", "present", any)):
        for index in range(len(judges[0][name])):
            values = [j[name][index] for j in judges]
            decisions = [v[field] for v in values]
            combined[name].append({field: combine(decisions),
                "memory_ids": sorted({mid for v in values for mid in v["memory_ids"]}),
                "reasons": [v["reason"] for v in values]})
            if len(judges) == 2:
                combined["decisions"] += 1
                combined["disagreements"] += decisions[0] != decisions[1]
    combined["passed"] = all(f["covered"] for f in combined["facts"]) and not any(f["present"] for f in combined["forbidden"])
    return combined


def _score_checkpoint(row, observed, judges):
    """Replay semantic scoring, HTTP isolation and U04 from captured evidence."""
    spec = next(c for c in row["script"]["checkpoints"] if c["id"] == observed["id"])
    combined = combine_judges(judges)
    isolation = check_recent_messages(observed["response"], spec["entry_id"],
                                      {int(mid): message for mid, message in observed["sent_messages"].items()})
    if not isolation["passed"]:
        row["failure"] = "recent_messages isolation check failed"
    sent = {(item["entry_id"], item["key"]): item["seconds"] for item in row["sent_times"]}
    first_seen = {int(mid): seconds for mid, seconds in row["first_seen_memories"].items()}
    entries = {entry["id"]: entry for entry in row["script"]["entries"]}
    observed.update(judges=judges, combined=combined, recent_message_isolation=isolation,
                    latencies=[fact_latency(expected, fact, spec["entry_id"], entries, sent, first_seen)
                               for expected, fact in zip(spec["expected"], combined["facts"], strict=True)])


def _finish_script(row):
    complete = len(row["checkpoints"]) == len(row["script"]["checkpoints"])
    if not complete and not row.get("failure"):
        row["failure"] = "evaluation infrastructure: checkpoint not reached"
    row["passed"] = not row.get("failure") and complete and all(c["combined"]["passed"] for c in row["checkpoints"])


def _run_script(configs, script, judge_runs, wait_timeout, judge_mode="model"):
    entries = {e["id"]: e for e in script["entries"]}
    with tempfile.TemporaryDirectory(prefix="iris-e2e-") as temporary:
        service = ServeProcess(temporary, configs)
        gateway = Gateway(configs) if judge_mode == "model" else None
        detail = {"id": script["id"], "script": script, "checkpoints": [], "first_seen_memories": {}, "receipts": []}
        judged_messages = detail["judged_messages"] = []
        detail["sent_times"] = []
        sent_messages = {}
        sent, first_seen, observed_batches = {}, {}, set()
        origin = time.monotonic()
        def observe():
            status = service.status()
            stamp = time.monotonic() - origin
            for batch in status["batches"]:
                if batch["state"] == "succeeded" and batch["id"] not in observed_batches:
                    observed_batches.add(batch["id"])
                    for mid in batch["result"].get("created", []) + batch["result"].get("updated", []):
                        first_seen[mid] = stamp
            return status
        try:
            service.start()
            for message in script["messages"]:
                if message.get("delay_seconds"):
                    time.sleep(message["delay_seconds"])
                entry = entries[message["entry_id"]]
                sent[(entry["id"], message["dedupe_key"])] = time.monotonic() - origin
                detail["sent_times"].append({"entry_id": entry["id"], "key": message["dedupe_key"],
                                             "seconds": sent[(entry["id"], message["dedupe_key"])]})
                receipt = service.post(f"/api/v1/entries/{entry['id']}/messages", _message(entry, message))
                detail["receipts"].append(receipt)
                judged_messages.append({**message, "message_id": receipt["message_ids"][0]})
                sent_messages[receipt["message_ids"][0]] = message
                observe()
            deadline = time.monotonic() + wait_timeout
            while True:
                status = observe()
                if all(e["pending_count"] == 0 for e in status["entries"]):
                    break
                if time.monotonic() >= deadline:
                    detail["failure"] = "background learning did not drain before wait_timeout"
                    break
                time.sleep(.2)
            detail["before_restart"] = status
            service.stop()  # Hard termination, followed by a new real serve process and HTTP readiness check.
            detail["after_restart"] = service.start()
            detail["first_seen_memories"] = first_seen
            for checkpoint in script["checkpoints"]:
                entry = entries[checkpoint["entry_id"]]
                receipt = service.post(f"/api/v1/entries/{entry['id']}/messages", _message(entry, checkpoint["question"]))
                detail["receipts"].append(receipt)
                sent_messages[receipt["message_ids"][0]] = {**checkpoint["question"], "entry_id": entry["id"]}
                response = service.post(f"/api/v1/entries/{entry['id']}/prepare", checkpoint.get("prepare", {}))
                isolation = check_recent_messages(response, entry["id"], sent_messages)
                if not isolation["passed"]:
                    detail["failure"] = "recent_messages isolation check failed"
                observed = {"id": checkpoint["id"], "response": response,
                            "sent_messages": dict(sent_messages), "recent_message_isolation": isolation}
                if judge_mode == "model":
                    judges = [_judge(gateway, {**script, "messages": judged_messages}, checkpoint, response)
                              for _ in range(judge_runs)]
                    _score_checkpoint(detail, observed, judges)
                detail["checkpoints"].append(observed)
            if judge_mode == "model":
                _finish_script(detail)
            detail["final_status"] = service.status()
            return detail
        except (OSError, RuntimeError, ValueError, ModelError, httpx.HTTPError) as error:
            detail.update(passed=False, failure=f"evaluation infrastructure: {type(error).__name__}: {error}")
            return detail
        finally:
            service.stop()
            if gateway is not None:
                gateway.close()


def run_e2e_eval(configs, root, split="all", *, corpus=None, out=None, judge_runs=2, wait_timeout=900,
                 judge_mode="model", script_ids=None):
    if (judge_runs not in (1, 2) or split not in ("all", "dev", "holdout") or wait_timeout <= 0
            or judge_mode not in ("model", "external")):
        raise ValueError("invalid e2e evaluation options")
    root = Path(root).resolve()
    corpus = Path(corpus).resolve() if corpus else root / "evals/e2e_v1.json"
    scripts = [s for s in load_corpus(corpus) if split == "all" or s.get("split", "dev") == split]
    if script_ids is not None:
        if not script_ids or len(set(script_ids)) != len(script_ids) or set(script_ids) - {s["id"] for s in scripts}:
            raise ValueError("script IDs must be unique and present in the selected corpus/split")
        scripts = [s for s in scripts if s["id"] in script_ids]
    if not scripts:
        raise ValueError("no e2e scripts for requested split")
    reports = Path(out).resolve() if out else root / "evals/reports"
    reports.mkdir(parents=True, exist_ok=True)
    metadata = {"scoring_version": SCORING_VERSION,
                "source_sha256": hashlib.sha256(b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.rglob("*"))
                    if p.suffix in (".py", ".sql", ".md", ".json"))).hexdigest(),
                "corpus_sha256": hashlib.sha256(corpus.read_bytes()).hexdigest(), "split": split,
                "script_ids": [s["id"] for s in scripts], "models": {kind: config.model for kind, config in configs.items()},
                "chat_model": configs["chat"].model,
                "timeouts_seconds": {"learning": LEARNING_TOTAL_TIMEOUT, "judge": JUDGE_TOTAL_TIMEOUT}}
    rows = []
    started = time.monotonic()
    for script in scripts:
        print(f"E2E {script['id']}: HTTP intake, background learning, restart, prepare", flush=True)
        try:
            rows.append(_run_script(configs, script, judge_runs, wait_timeout, judge_mode))
        except (OSError, RuntimeError, ValueError, ModelError, httpx.HTTPError) as error:
            rows.append({"id": script["id"], "script": script, "passed": False,
                         "failure": f"evaluation infrastructure: {type(error).__name__}: {error}", "checkpoints": []})
    metadata["duration_seconds"] = round(time.monotonic() - started, 2)
    secrets = [config.api_key for config in configs.values()]
    if judge_mode == "external":
        # Redact before writing any captured HTTP/status data to the material bundle.
        serialized = json.dumps(rows, ensure_ascii=False)
        for secret in secrets:
            if secret:
                serialized = serialized.replace(secret, "[REDACTED]")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return export_e2e_judgments(json.loads(serialized), metadata, reports / f"judging-materials-{stamp}")
    return _write_e2e_report(rows, root, reports, metadata, judge_runs=judge_runs,
                             judge_mode="model", judge_model=configs["chat"].model, secrets=secrets)


def _disagreements(rows):
    differences = []
    for row in rows:
        for checkpoint in row["checkpoints"]:
            judges = checkpoint["judges"]
            if len(judges) != 2:
                continue
            for name, field in (("facts", "covered"), ("forbidden", "present")):
                for index, (first, second) in enumerate(zip(judges[0][name], judges[1][name], strict=True)):
                    if first[field] != second[field]:
                        differences.append({"script_id": row["id"], "checkpoint_id": checkpoint["id"],
                                            "field": name, "index": index, "first": first[field], "second": second[field]})
    return differences


def _write_e2e_report(rows, root, reports, metadata, *, judge_runs, judge_mode, judge_model,
                      materials_sha256=None, judgment_rounds=(), secrets=()):
    reports.mkdir(parents=True, exist_ok=True)
    summaries = []
    calls = []
    for row in rows:
        checkpoints = row["checkpoints"]
        timings = [item for c in checkpoints for item in c["latencies"] if item["u04_applicable"]]
        summaries.append({"id": row["id"], "passed": row["passed"], "failure": row.get("failure"),
                          "u04_seconds": max((t["seconds"] for t in timings if t["seconds"] is not None), default=None),
                          "u04_passed": all(t["within_60_seconds"] for t in timings) if timings else None,
                          "checkpoints": [{"id": c["id"], **c["combined"], "latencies": c["latencies"],
                                           "recent_message_isolation": c["recent_message_isolation"]} for c in checkpoints]})
        calls.extend(row.get("final_status", row.get("before_restart", {})).get("learning_calls_24h", []))
    disagreements = sum(c["combined"]["disagreements"] for row in rows for c in row["checkpoints"])
    decisions = sum(c["combined"]["decisions"] for row in rows for c in row["checkpoints"])
    durations = [c["duration_ms"] for c in calls]
    report = {**metadata, "created_at": datetime.now(timezone.utc).isoformat(), "judge_runs": judge_runs,
              "judge_mode": judge_mode, "judge_model": judge_model, "materials_sha256": materials_sha256,
              "judgment_rounds": list(judgment_rounds), "judge_inconsistencies": _disagreements(rows), "scripts": summaries,
              "passed": sum(row["passed"] for row in rows), "total": len(rows),
              "repository_gate": sum(row["passed"] for row in rows) / len(rows) >= .8,
              "hidden_acceptance": "not_run_by_executor", "judge_disagreements": disagreements,
              "judge_decisions": decisions, "judge_disagreement_rate": disagreements / decisions if decisions else None,
              "learning_latency": {"count": len(durations), "p50_ms": percentile(durations, 50),
                  "p95_ms": percentile(durations, 95), "max_ms": max(durations, default=None),
                  "timeouts": sum(c["timed_out"] for c in calls)}}
    if not reports.is_relative_to(root):
        report["details"] = rows
    path = reports / ("e2e-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    for secret in secrets:
        if secret:
            serialized = serialized.replace(secret, "[REDACTED]")
    path.write_text(serialized, encoding="utf-8")
    return path, json.loads(serialized)


_E2E_RUN_FIELDS = ("scoring_version", "source_sha256", "corpus_sha256", "split", "script_ids",
                   "models", "chat_model", "timeouts_seconds", "duration_seconds")
_E2E_ROW_FIELDS = ("id", "script", "first_seen_memories", "receipts", "judged_messages", "sent_times",
                   "before_restart", "after_restart", "final_status", "failure")


def _e2e_rows(rows):
    """Keep captured evidence, never previous judgments, combined votes or latencies."""
    return [{**{key: copy.deepcopy(row[key]) for key in _E2E_ROW_FIELDS if key in row},
             "checkpoints": [{key: copy.deepcopy(checkpoint[key]) for key in
                              ("id", "response", "sent_messages", "recent_message_isolation") if key in checkpoint}
                             for checkpoint in row["checkpoints"]]} for row in rows]


def _material_cases(rows):
    for row in rows:
        specs = {checkpoint["id"]: checkpoint for checkpoint in row["script"]["checkpoints"]}
        for observed in row["checkpoints"]:
            yield row, specs[observed["id"]], observed


def _check_e2e_run(rows, metadata):
    for key in _E2E_RUN_FIELDS:
        if key not in metadata:
            raise ValueError(f"e2e metadata missing {key}")
    if metadata["scoring_version"] != SCORING_VERSION:
        raise ValueError("e2e scoring version mismatch")
    for key in ("corpus_sha256", "source_sha256"):
        value = metadata[key]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError(f"invalid e2e {key}")
    if not isinstance(metadata["chat_model"], str) or not metadata["chat_model"].strip():
        raise ValueError("e2e learning model is required")
    if metadata["models"].get("chat") != metadata["chat_model"] or metadata["split"] not in ("all", "dev", "holdout"):
        raise ValueError("inconsistent e2e run metadata")
    ids = [row["id"] for row in rows]
    if not ids or len(set(ids)) != len(ids) or ids != metadata["script_ids"]:
        raise ValueError("missing or duplicate e2e scripts")
    for row in rows:
        specs = row["script"]["checkpoints"]
        observed_ids = [c["id"] for c in row["checkpoints"]]
        if (row["script"]["id"] != row["id"] or not specs or len({c["id"] for c in specs}) != len(specs)
                or observed_ids != [c["id"] for c in specs[:len(observed_ids)]]):
            raise ValueError("e2e script/checkpoint mapping mismatch")
        if len(observed_ids) != len(specs) and not row.get("failure"):
            raise ValueError("unreached e2e checkpoints require an infrastructure failure record")
        for observed in row["checkpoints"]:
            # These are required to recheck isolation and U04, not supplied by the judge.
            if not isinstance(observed["sent_messages"], dict) or not isinstance(row["first_seen_memories"], dict):
                raise ValueError("missing e2e HTTP receipts or timing evidence")
            if not isinstance(row["sent_times"], list) or not isinstance(row["judged_messages"], list):
                raise ValueError("missing e2e message evidence")


def _material_document(row, spec, observed, metadata):
    return {"format_version": MATERIAL_FORMAT_VERSION, "evaluation": "e2e",
            "script_id": row["id"], "checkpoint_id": spec["id"],
            "corpus_sha256": metadata["corpus_sha256"], "source_sha256": metadata["source_sha256"],
            "scoring_version": SCORING_VERSION,
            "input": _judge_payload({**row["script"], "messages": row["judged_messages"]}, spec, observed["response"])}


def export_e2e_judgments(rows, metadata, out):
    """Export every observed checkpoint; infrastructure failures remain in run.json."""
    try:
        rows = _e2e_rows(rows)
        _check_e2e_run(rows, metadata)
        documents = [_material_document(*case, metadata) for case in _material_cases(rows)]
    except (KeyError, TypeError) as error:
        raise ValueError(f"Invalid e2e run structure: {error}") from error
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"material output directory must be empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"format_version": MATERIAL_FORMAT_VERSION, "evaluation": "e2e",
                "run": {key: metadata[key] for key in _E2E_RUN_FIELDS},
                "run_sha256": _write_json(out / "run.json", {"rows": rows}), "cases": []}
    (out / "scoring.md").write_text(SCORING, encoding="utf-8")
    manifest["scoring_sha256"] = hashlib.sha256((out / "scoring.md").read_bytes()).hexdigest()
    for index, document in enumerate(documents):
        filename = f"cases/{index:04}.json"
        manifest["cases"].append({"script_id": document["script_id"], "checkpoint_id": document["checkpoint_id"],
                                  "file": filename, "sha256": _write_json(out / filename, document),
                                  "judgment_file": f"{index:04}.json"})
    manifest["materials_sha256"] = _json_sha256(manifest)
    path = out / "manifest.json"
    _write_json(path, manifest)
    _write_json(out / "round-template.json", {"materials_sha256": manifest["materials_sha256"]})
    return path, manifest


def _load_e2e_materials(directory):
    manifest = _read_json(directory / "manifest.json")
    try:
        if manifest["format_version"] != MATERIAL_FORMAT_VERSION or manifest["evaluation"] != "e2e":
            raise ValueError("unsupported material format or evaluation kind")
        if manifest["materials_sha256"] != _json_sha256({k: v for k, v in manifest.items() if k != "materials_sha256"}):
            raise ValueError("manifest fingerprint mismatch")
        rows = _read_json(_verified_material_file(directory, "run.json", manifest["run_sha256"]))["rows"]
        scoring = _verified_material_file(directory, "scoring.md", manifest["scoring_sha256"])
        if scoring.read_text(encoding="utf-8") != SCORING:
            raise ValueError("material scoring rules differ from frozen e2e_scoring_v1")
        _check_e2e_run(rows, manifest["run"])
        cases = list(_material_cases(rows))
        if len(cases) != len(manifest["cases"]):
            raise ValueError("material checkpoint count mismatch")
        for index, (entry, case) in enumerate(zip(manifest["cases"], cases, strict=True)):
            if entry["judgment_file"] != f"{index:04}.json" or entry["file"] != f"cases/{index:04}.json":
                raise ValueError("invalid material/judgment filename")
            document = _read_json(_verified_material_file(directory, entry["file"], entry["sha256"]))
            if (document != _material_document(*case, manifest["run"])
                    or entry["script_id"] != case[0]["id"] or entry["checkpoint_id"] != case[1]["id"]):
                raise ValueError("material input differs from captured e2e data")
        return manifest, _e2e_rows(rows)
    except (KeyError, TypeError) as error:
        raise ValueError(f"Invalid e2e material structure: {error}") from error


def score_e2e_judgments(materials, judgments, root, *, judge_model, out=None):
    """Strict, offline scoring of one or two complete, independently supplied rounds."""
    judgments = [Path(directory).resolve() for directory in judgments]
    if len(judgments) not in (1, 2):
        raise ValueError("supply one or two judgment directories")
    if len(set(judgments)) != len(judgments):
        raise ValueError("two rounds require independent judgment directories")
    if not isinstance(judge_model, str) or not judge_model.strip():
        raise ValueError("--judge-model must declare the external executor model")
    manifest, rows = _load_e2e_materials(Path(materials))
    cases = list(_material_cases(rows))
    problems, rounds, round_records = [], [], []
    for number, directory in enumerate(judgments, 1):
        try:
            round_manifest = _read_json(directory / "manifest.json")
            if not isinstance(round_manifest, dict) or round_manifest.get("materials_sha256") != manifest["materials_sha256"]:
                raise ValueError("judgment material fingerprint mismatch")
        except ValueError as error:
            problems.append(f"round {number} ({directory}): {error}")
        verdicts, records = [], []
        expected_files = {"manifest.json", *(entry["judgment_file"] for entry in manifest["cases"])}
        for extra in sorted({p.name for p in directory.glob("*.json")} - expected_files):
            problems.append(f"round {number} ({directory}): unexpected judgment file {extra}")
        for entry, (_, spec, observed) in zip(manifest["cases"], cases, strict=True):
            try:
                output = _read_json(directory / entry["judgment_file"])
                _validate_judgment(output, spec, observed["response"])
                verdicts.append(output)
                records.append({"script_id": entry["script_id"], "checkpoint_id": entry["checkpoint_id"],
                                "sha256": _json_sha256(output)})
            except ValueError as error:
                problems.append(f"round {number} ({directory}), {entry['script_id']}/{entry['checkpoint_id']}: {error}")
        rounds.append(verdicts)
        round_records.append({"round": number, "cases": records})
    if problems:
        raise ValueError("Invalid external judgments:\n- " + "\n- ".join(problems))
    for index, (row, _, observed) in enumerate(cases):
        _score_checkpoint(row, observed, [votes[index] for votes in rounds])
    for row in rows:
        _finish_script(row)
    root = Path(root).resolve()
    reports = Path(out).resolve() if out else root / "evals/reports"
    return _write_e2e_report(rows, root, reports, manifest["run"], judge_runs=len(rounds), judge_mode="external",
                             judge_model=judge_model.strip(), materials_sha256=manifest["materials_sha256"],
                             judgment_rounds=round_records)
