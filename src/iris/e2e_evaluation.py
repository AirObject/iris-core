"""HTTP-only end-to-end evaluation against killed/restarted real serve processes."""
from __future__ import annotations

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


SCORING = files("iris").joinpath("prompts", "e2e_scoring_v1.md").read_text(encoding="utf-8")


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


def _judge(gateway, script, checkpoint, response):
    material = {"messages": script["messages"], "question": checkpoint["question"],
                "timezone": "Asia/Shanghai", "expected": checkpoint["expected"],
                "forbidden": checkpoint.get("forbidden", []), "memories": response["memories"]}
    result = gateway.json_chat([{"role": "system", "content": SCORING},
                               {"role": "user", "content": json.dumps(material, ensure_ascii=False)}],
                              "e2e_judge", max_tokens=16000)[0]
    ids = {m["id"] for m in response["memories"]}
    for name, field, expected in (("facts", "covered", checkpoint["expected"]),
                                   ("forbidden", "present", checkpoint.get("forbidden", []))):
        values = result.get(name)
        if not isinstance(values, list) or len(values) != len(expected):
            raise ValueError("judge result length mismatch")
        for value in values:
            if type(value.get(field)) is not bool or not isinstance(value.get("reason"), str):
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


def _run_script(configs, script, judge_runs, wait_timeout):
    entries = {e["id"]: e for e in script["entries"]}
    with tempfile.TemporaryDirectory(prefix="iris-e2e-") as temporary:
        service = ServeProcess(temporary, configs)
        gateway = Gateway(configs)
        detail = {"id": script["id"], "script": script, "checkpoints": [], "first_seen_memories": {}, "receipts": []}
        judged_messages = []
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
                judges = [_judge(gateway, {**script, "messages": judged_messages}, checkpoint, response) for _ in range(judge_runs)]
                combined = combine_judges(judges)
                isolation = check_recent_messages(response, entry["id"], sent_messages)
                if not isolation["passed"]:
                    detail["failure"] = "recent_messages isolation check failed"
                latencies = [fact_latency(expected, fact, entry["id"], entries, sent, first_seen)
                             for expected, fact in zip(checkpoint["expected"], combined["facts"])]
                detail["checkpoints"].append({"id": checkpoint["id"], "response": response, "judges": judges,
                                              "combined": combined, "latencies": latencies, "recent_message_isolation": isolation})
            detail["passed"] = not detail.get("failure") and all(c["combined"]["passed"] for c in detail["checkpoints"])
            detail["final_status"] = service.status()
            return detail
        except (OSError, RuntimeError, ValueError, ModelError, httpx.HTTPError) as error:
            detail.update(passed=False, failure=f"evaluation infrastructure: {type(error).__name__}: {error}")
            return detail
        finally:
            service.stop()
            gateway.close()


def run_e2e_eval(configs, root, split="all", *, corpus=None, out=None, judge_runs=2, wait_timeout=900):
    if judge_runs not in (1, 2) or split not in ("all", "dev", "holdout") or wait_timeout <= 0:
        raise ValueError("invalid e2e evaluation options")
    root = Path(root).resolve()
    corpus = Path(corpus).resolve() if corpus else root / "evals/e2e_v1.json"
    scripts = [s for s in load_corpus(corpus) if split == "all" or s.get("split", "dev") == split]
    if not scripts:
        raise ValueError("no e2e scripts for requested split")
    reports = Path(out).resolve() if out else root / "evals/reports"
    reports.mkdir(parents=True, exist_ok=True)
    external = not reports.is_relative_to(root)
    rows = []
    started = time.monotonic()
    for script in scripts:
        print(f"E2E {script['id']}: HTTP intake, background learning, restart, prepare", flush=True)
        try:
            rows.append(_run_script(configs, script, judge_runs, wait_timeout))
        except (OSError, RuntimeError, ValueError, ModelError, httpx.HTTPError) as error:
            rows.append({"id": script["id"], "passed": False,
                         "failure": f"evaluation infrastructure: {type(error).__name__}: {error}", "checkpoints": []})
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
    source = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.rglob("*"))
                                    if p.suffix in (".py", ".sql", ".md", ".json"))).hexdigest()
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "scoring_version": "e2e_scoring_v1", "source_sha256": source,
              "corpus_sha256": hashlib.sha256(corpus.read_bytes()).hexdigest(), "split": split, "judge_runs": judge_runs,
              "models": {kind: config.model for kind, config in configs.items()}, "scripts": summaries,
              "passed": sum(row["passed"] for row in rows), "total": len(rows),
              "repository_gate": sum(row["passed"] for row in rows) / len(rows) >= .8,
              "hidden_acceptance": "not_run_by_executor", "judge_disagreements": disagreements,
              "judge_decisions": decisions, "judge_disagreement_rate": disagreements / decisions if decisions else None,
              "duration_seconds": round(time.monotonic() - started, 2),
              "timeouts_seconds": {"learning": LEARNING_TOTAL_TIMEOUT, "judge": JUDGE_TOTAL_TIMEOUT},
              "learning_latency": {"count": len(durations), "p50_ms": percentile(durations, 50),
                  "p95_ms": percentile(durations, 95), "max_ms": max(durations, default=None),
                  "timeouts": sum(c["timed_out"] for c in calls)}}
    if external:
        report["details"] = rows
    path = reports / ("e2e-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    for config in configs.values():
        if config.api_key:
            serialized = serialized.replace(config.api_key, "[REDACTED]")
    path.write_text(serialized, encoding="utf-8")
    return path, json.loads(serialized)
