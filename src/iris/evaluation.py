"""Frozen learning evaluation using the production intake and learning path."""

from __future__ import annotations

import json
import math
import random
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any

from .db import Store
from .learning import LearningEngine, PROMPT_VERSION, _same_goal
from .memory_ops import setup_role
from .models import Gateway, ModelConfig, ModelError
from .queue import add_message, form_batch, get_batch


SCORING_VERSION = "scoring_v2"
SCORING = files("iris").joinpath("prompts", SCORING_VERSION + ".md").read_text(encoding="utf-8")


def _pace_for_entry(kind: str) -> str:
    return "realtime" if kind in ("live", "stream") else "standard"


def _case_data(store: Store) -> dict[str, Any]:
    with store.read() as conn:
        memories = []
        for memory in conn.execute("""SELECT m.id,m.content,m.stance,m.belief,m.speaker_subject_id,s.name AS speaker FROM memories m
            JOIN subjects s ON s.id=m.speaker_subject_id WHERE m.stance!='设定' ORDER BY m.id"""):
            about = [r[0] for r in conn.execute("""SELECT s.name FROM memory_subjects ms JOIN subjects s ON s.id=ms.subject_id
                WHERE ms.memory_id=?""", (memory["id"],))]
            evidence = [dict(r) for r in conn.execute("""SELECT x.id,x.kind,x.content,x.occurred_at,x.entry_id,
                x.quote_content,s.name AS sender,q.name AS quote_author FROM sources src JOIN messages x ON x.id=src.message_id
                JOIN subjects s ON s.id=x.sender_subject_id LEFT JOIN subjects q ON q.id=x.quote_author_subject_id
                WHERE src.memory_id=? AND src.kind='message' ORDER BY x.id""", (memory["id"],))]
            memories.append({**dict(memory), "about": about, "evidence": evidence})
        links = [dict(r) for r in conn.execute("""SELECT l.kind,l.belief,l.status,a.name AS a,b.name AS b,
            m.content AS evidence_content,m.kind AS evidence_kind FROM subject_links l
            JOIN subjects a ON a.id=l.subject_a JOIN subjects b ON b.id=l.subject_b
            LEFT JOIN messages m ON m.id=l.source_message_id ORDER BY l.id""")]
        goals = [dict(r) for r in conn.execute("SELECT content,kind,deadline,entry_id,state FROM goals ORDER BY id")]
        attempts = [dict(r) for r in conn.execute("""SELECT a.number,a.parse_status,a.duration_ms,a.error,
            a.raw_output,a.batch_id FROM batch_attempts a ORDER BY a.id""")]
        calls = [dict(r) for r in conn.execute("""SELECT purpose,prompt_tokens,completion_tokens,reasoning_tokens,result_category
            FROM model_calls ORDER BY id""")]
        batches = [dict(r) for r in conn.execute("SELECT id,state,target_ids,result_json FROM batches ORDER BY id")]
        identities = [dict(r) for r in conn.execute("""SELECT s.id,s.name,p.account_id FROM subjects s
            LEFT JOIN platform_identities p ON p.subject_id=s.id ORDER BY s.id""")]
    return {"memories": memories, "links": links, "goals": goals, "attempts": attempts,
            "calls": calls, "batches": batches, "identities": identities}


def _judge(gateway: Gateway, case: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    payload = {"messages": [{"id": i, **message} for i, message in enumerate(case["messages"], 1)],
               "must": case["must"], "forbidden": case["forbidden"],
               "links": case["links"], "goals": case["goals"], "actual_memories": actual["memories"],
               "actual_links": actual["links"], "actual_goals": actual["goals"],
               "actual_subjects": actual["identities"],
               "target_segments": [json.loads(batch["target_ids"]) for batch in actual["batches"]]}
    output, *_ = gateway.json_chat(
        [{"role": "system", "content": SCORING},
         {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        "learning_judge", max_tokens=6000)
    memory_by_id = {item.get("id"): item for item in output.get("memory_results", []) if isinstance(item, dict)}
    results = []
    for memory in actual["memories"]:
        item = memory_by_id.get(memory["id"], {})
        results.append({"id": memory["id"], **{key: item.get(key) is True for key in
                       ("correct_worth", "forbidden", "evidence_correct", "attribution_correct")}})
    def booleans(key: str, length: int) -> list[bool]:
        values = output.get(key)
        if not isinstance(values, list):
            values = []
        return [values[i] is True if i < len(values) else False for i in range(length)]
    return {"memory_results": results, "fact_covered": booleans("fact_covered", len(case["must"])),
            "link_covered": booleans("link_covered", len(case["links"])),
            "goal_covered": booleans("goal_covered", len(case["goals"])),
            "actual_link_correct": booleans("actual_link_correct", len(actual["links"]))}


def _combine_judges(first: dict[str, Any], second: dict[str, Any], case_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """Count every changed verdict and score with the less favorable one."""
    differences: list[dict[str, Any]] = []
    decisions = 0
    combined: dict[str, Any] = {"memory_results": []}
    for before, after in zip(first["memory_results"], second["memory_results"], strict=True):
        row: dict[str, Any] = {"id": before["id"]}
        for key in ("correct_worth", "forbidden", "evidence_correct", "attribution_correct"):
            decisions += 1
            old, new = before[key], after[key]
            result = (old or new) if key == "forbidden" else (old and new)
            row[key] = result
            if old != new:
                differences.append({"case": case_id, "item": "memory", "id": before["id"],
                                    "field": key, "first": old, "second": new, "scored": result})
        combined["memory_results"].append(row)
    for key in ("fact_covered", "link_covered", "goal_covered", "actual_link_correct"):
        values = []
        for index, (old, new) in enumerate(zip(first[key], second[key], strict=True), 1):
            decisions += 1
            result = old and new
            values.append(result)
            if old != new:
                differences.append({"case": case_id, "item": key, "index": index,
                                    "first": old, "second": new, "scored": result})
        combined[key] = values
    return combined, differences, decisions


def _duplicate_goals(goals: list[dict[str, Any]], known_people: set[str] | None = None) -> dict[str, int]:
    counts = {"normal": 0, "question": 0}
    known_people = known_people or set()
    for index, current in enumerate(goals):
        if current.get("state") != "open":
            continue
        if any(prior.get("state") == "open" and prior.get("kind") == current.get("kind") and
               prior.get("entry_id") == current.get("entry_id") and
               (not prior.get("deadline") or not current.get("deadline") or
                prior["deadline"] == current["deadline"]) and
               _same_goal(prior["content"], current["content"], known_people)
               for prior in goals[:index]):
            counts[current["kind"]] += 1
    return counts


def _parse_failure_records(rows: list[dict[str, Any]], api_key: str) -> list[dict[str, Any]]:
    records = []
    for row in rows:
        for attempt in row["actual"]["attempts"]:
            if attempt["parse_status"] in ("direct", "quote_repaired"):
                continue
            raw = attempt.get("raw_output") or ""
            if api_key:
                raw = raw.replace(api_key, "[REDACTED]")
            records.append({"case": row["case"]["id"], "batch": attempt["batch_id"],
                            "attempt": attempt["number"], "parse_status": attempt["parse_status"],
                            "error": attempt["error"], "first_raw": raw[:2000]})
    return records


def _percentile(values: list[int], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile / 100
    lower, upper = int(index), min(len(ordered) - 1, int(index) + 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower), 1)


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    memories = [result for row in rows for result in row["judge"]["memory_results"]]
    attempts = [attempt for row in rows for attempt in row["actual"]["attempts"]]
    calls = [call for row in rows for call in row["actual"]["calls"]]
    batches = [batch for row in rows for batch in row["actual"]["batches"]]
    new_count = len(memories)
    fact_covered = [value for row in rows for value in row["judge"]["fact_covered"]]
    link_covered = [value for row in rows for value in row["judge"]["link_covered"]]
    goal_covered = [value for row in rows for value in row["judge"]["goal_covered"]]
    actual_link_correct = [value for row in rows for value in row["judge"].get("actual_link_correct", [])]
    quote_repaired = sum(1 for a in attempts if a["parse_status"] == "quote_repaired")
    direct = sum(1 for a in attempts if a["parse_status"] == "direct") + quote_repaired
    repaired = sum(1 for a in attempts if a["parse_status"] == "repaired")
    duplicates = [_duplicate_goals(row["actual"].get("goals", []),
                                   {item["name"] for item in row["actual"].get("identities", [])
                                    if item["id"] not in ("self", "scene")}) for row in rows]
    judge_changes = sum(len(row.get("judge_inconsistencies", [])) for row in rows)
    judge_decisions = sum(row.get("judge_decisions", 0) for row in rows)
    total_messages = sum(len(row["case"]["messages"]) for row in rows)
    token_usage = {key: sum(call[key] or 0 for call in calls) for key in
                   ("prompt_tokens", "completion_tokens", "reasoning_tokens")}
    def usage_for(prefixes: tuple[str, ...]) -> dict[str, int]:
        chosen = [call for call in calls if call["purpose"] in prefixes]
        return {key: sum(call[key] or 0 for call in chosen) for key in
                ("prompt_tokens", "completion_tokens", "reasoning_tokens")}
    learning_usage = usage_for(("learning", "learning_repair"))
    judge_usage = usage_for(("learning_judge", "learning_judge_repair"))
    embedding_usage = usage_for(("learning_context", "memory_embedding"))
    return {
        "cases": len(rows), "batches": len(batches), "messages": total_messages,
        "parse_direct": direct / len(batches) if batches else None,
        "parse_quote_repaired": quote_repaired,
        "parse_repaired": repaired / len(batches) if batches else None,
        "parse_total": (direct + repaired) / len(batches) if batches else None,
        "precision": sum(m["correct_worth"] for m in memories) / new_count if new_count else None,
        "fact_recall": sum(fact_covered) / len(fact_covered) if fact_covered else None,
        "false_memory_rate": sum(m["forbidden"] for m in memories) / new_count if new_count else None,
        "evidence_accuracy": sum(m["evidence_correct"] for m in memories) / new_count if new_count else None,
        "attribution_accuracy": sum(m["attribution_correct"] for m in memories) / new_count if new_count else None,
        "link_recall": sum(link_covered) / len(link_covered) if link_covered else None,
        "link_precision": sum(actual_link_correct) / len(actual_link_correct) if actual_link_correct else None,
        "goal_recall": sum(goal_covered) / len(goal_covered) if goal_covered else None,
        "goal_duplicates": sum(item["normal"] for item in duplicates),
        "question_duplicates": sum(item["question"] for item in duplicates),
        "judge_inconsistencies": judge_changes,
        "judge_inconsistency_rate": judge_changes / judge_decisions if judge_decisions else None,
        "new_memories": new_count, "new_links": len(actual_link_correct),
        "required_facts": len(fact_covered),
        "batch_p50_ms": _percentile([a["duration_ms"] for a in attempts], 50),
        "batch_p95_ms": _percentile([a["duration_ms"] for a in attempts], 95),
        "tokens": token_usage,
        "learning_tokens": learning_usage,
        "judge_tokens": judge_usage,
        "embedding_tokens": embedding_usage,
        "learning_tokens_per_1000_messages": round((learning_usage["prompt_tokens"] + learning_usage["completion_tokens"]) * 1000 / total_messages) if total_messages else None,
        "tokens_per_1000_messages": round((token_usage["prompt_tokens"] + token_usage["completion_tokens"]) * 1000 / total_messages) if total_messages else None,
        "estimated_cost": None,
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    return f"{value:.1%}" if isinstance(value, float) and value <= 1 else str(value)


def _report_markdown(report: dict[str, Any]) -> str:
    lines = ["# Iris 学习评测", "", f"时间：{report['created_at']}",
             f"学习提示词：`{report['prompt_version']}`；评分说明：`{report['scoring_version']}`；对话模型：`{report['chat_model']}`；embedding：`{report['embedding_model']}`。",
             "", f"评测集：{report['corpus']['cases']} 段、{report['corpus']['messages']} 条消息、"
             f"{report['corpus']['must']} 条 must；当前运行 {report['split']}。每段判两次，分歧按不利结论计分。"
             "判分与学习使用同一模型，仍需人工抽查。未配置单价，费用无法估算。", "",
             "| 指标 | dev | holdout | 全部 | M1 门槛 |", "| --- | ---: | ---: | ---: | ---: |"]
    names = [("解析直接成功率", "parse_direct", "≥98%"),
             ("其中引号确定性修复次数", "parse_quote_repaired", "单列"),
             ("解析模型修正后成功率", "parse_repaired", "单列"),
             ("解析总成功率", "parse_total", "—"), ("记忆精确率", "precision", "≥70%"),
             ("事实召回率", "fact_recall", "≥60%"), ("误记率", "false_memory_rate", "—"),
             ("证据正确率", "evidence_accuracy", "—"), ("归属正确率", "attribution_accuracy", "—"),
             ("人物联系精确率", "link_precision", "—"), ("人物联系覆盖率", "link_recall", "—"),
             ("目标覆盖率", "goal_recall", "—"),
             ("目标重复数", "goal_duplicates", "—"), ("询问重复数", "question_duplicates", "—"),
             ("判分不一致率", "judge_inconsistency_rate", "—"),
             ("批次耗时 P50 ms", "batch_p50_ms", "—"), ("批次耗时 P95 ms", "batch_p95_ms", "—"),
             ("输入 token", "prompt_tokens", "—"), ("输出 token", "completion_tokens", "—"),
             ("其中推理 token", "reasoning_tokens", "—"), ("每千条消息 token", "tokens_per_1000_messages", "—"),
             ("每千条消息学习生成 token", "learning_tokens_per_1000_messages", "—"),
             ("判分输入 token", "judge_prompt_tokens", "—"), ("判分输出 token", "judge_completion_tokens", "—"),
             ("embedding 输入 token", "embedding_prompt_tokens", "—"),
             ("估算费用（未配置单价）", "estimated_cost", "—"),
             ("新记忆数", "new_memories", "—"), ("必须事实数", "required_facts", "—")]
    for label, key, threshold in names:
        values = []
        for split in ("dev", "holdout", "all"):
            metric = report["metrics"].get(split)
            if not metric:
                values.append("—")
            elif key in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
                values.append(_fmt(metric["tokens"].get(key)))
            elif key.startswith("judge_") and key.endswith("_tokens"):
                values.append(_fmt(metric.get("judge_tokens", {}).get(key.removeprefix("judge_"))))
            elif key.startswith("embedding_") and key.endswith("_tokens"):
                values.append(_fmt(metric.get("embedding_tokens", {}).get(key.removeprefix("embedding_"))))
            else:
                values.append(_fmt(metric.get(key)))
        lines.append(f"| {label} | {' | '.join(values)} | {threshold} |")
    all_metrics = report["metrics"].get("all")
    holdout = report["metrics"].get("holdout")
    if all_metrics and holdout:
        checks = (("直接解析", "parse_direct", 0.98), ("记忆精确率", "precision", 0.70),
                  ("事实召回率", "fact_recall", 0.60))
        passed = all((all_metrics.get(key) or 0) >= limit and (holdout.get(key) or 0) >= limit
                     for _, key, limit in checks)
        lines.extend(["", f"M1 学习门槛（全部与 holdout 同时满足）：{'达标' if passed else '未达标'}。"])
        for label, key, limit in checks:
            lines.append(f"- {label}：全部 {_fmt(all_metrics.get(key))}，holdout {_fmt(holdout.get(key))}；门槛 {_fmt(limit)}。")
    else:
        lines.extend(["", "本次未运行新 holdout，不能判定 M1 学习门槛是否最终达标。"])
    lines.extend(["", "M2 参考门槛（本 PR 不要求）：记忆精确率 ≥85%；事实召回率 ≥75%；"
                  "误记率 ≤5%；证据正确率 ≥90%；归属正确率 ≥90%。"])
    lines.extend(["", "## 与上次结果对比", ""])
    previous = report.get("previous")
    if previous and previous.get("corpus") == report["corpus"]:
        lines.append(f"上次报告：`{previous['path']}`。")
        for split in ("dev", "holdout", "all"):
            if split == "all" and not ("holdout" in previous["metrics"] and "holdout" in report["metrics"]):
                continue
            if split in previous["metrics"] and split in report["metrics"]:
                for key in ("parse_direct", "precision", "fact_recall"):
                    old = previous["metrics"][split].get(key)
                    new = report["metrics"][split].get(key)
                    if old is not None and new is not None:
                        lines.append(f"- {split} {key}: {_fmt(old)} → {_fmt(new)}（{(new-old)*100:+.1f} 个百分点）")
    else:
        lines.append("没有同一评测集的可比前次结果；PR #1 使用较短的 v1 集和旧评分规则。")
    lines.extend(["", "## 判分不一致清单", ""])
    if report["judge_inconsistencies"]:
        for item in report["judge_inconsistencies"]:
            identifier = f"记忆 #{item['id']}" if item["item"] == "memory" else f"{item['item']} 第 {item['index']} 项"
            lines.append(f"- {item['case']} {identifier} {item.get('field', '')}：第一次 {item['first']}，第二次 {item['second']}，计分 {item['scored']}。")
    else:
        lines.append("两次判分结论一致。")
    lines.extend(["", "## 首轮解析异常输出", ""])
    if report["parse_failures"]:
        for item in report["parse_failures"]:
            lines.extend([f"### {item['case']} 批次 {item['batch']} 尝试 {item['attempt']}（{item['parse_status']}）",
                          "", *("    " + part for part in item["first_raw"].splitlines()), ""])
    else:
        lines.append("所有批次首轮直接解析成功。")
    lines.extend(["", "## 随机抽查清单", "", "固定随机种子 20260928；以下为至少 10% 案例的判分，供人工核对。人工结论待填写。", ""])
    for item in report["spot_check"]:
        lines.append(f"### {item['id']}（{item['split']}）")
        lines.append(f"标注事实：{'；'.join(f['fact'] for f in item['must']) or '无'}；不应记住：{'；'.join(item['forbidden']) or '无'}。")
        for memory in item["memories"]:
            score = next((v for v in item["judge"]["memory_results"] if v["id"] == memory["id"]), {})
            lines.append(f"- 记忆 #{memory['id']}：{memory['content']}；说话人 {memory['speaker']}；立场 {memory['stance']}；判分 {json.dumps(score, ensure_ascii=False)}。")
            for source in memory["evidence"]:
                lines.append(f"  - 来源 #{source['id']}（{source['sender']}，{source['kind']}）：{source['content'][:160]}")
        lines.append(f"事实覆盖：{item['judge']['fact_covered']}。人工核对：待填写。")
        lines.append("")
    lines.extend(["## 限制", "", "本报告没有第二家对话服务商的结果；当前配置仅提供一组对话模型。人工复核尚未由用户完成。"])
    for note in report.get("review_notes", []):
        lines.append("- " + note)
    lines.append("")
    return "\n".join(lines)


def _run_case(configs: dict[str, ModelConfig], case: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="iris-eval-") as temporary:
        store = Store(Path(temporary) / "iris.db")
        store.recover_inflight()
        setup_role(store, "Iris")
        gateway = Gateway(configs, store)
        engine = LearningEngine(store, gateway)
        try:
            for index, message in enumerate(case["messages"]):
                add_message(store, entry_id=case["id"], entry_name=case["id"], platform="fictional",
                            entry_kind=case["entry_type"], kind=message["type"], sender=message["speaker"],
                            content=message["content"], occurred_at=message["at"], dedupe_key=f"{case['id']}-{index}",
                            account_id=message.get("account_id", f"{case['id']}:{message['speaker']}"),
                            quote_author=message.get("quote_author"),
                            quote_author_account_id=message.get("quote_author_account_id"),
                            quote_content=message.get("quote_content"),
                            pace=_pace_for_entry(case["entry_type"]))
            while True:
                batch = form_batch(store, case["id"], PROMPT_VERSION)
                if batch is None:
                    break
                while get_batch(store, batch.id).state == "waiting":
                    engine.run_batch(batch.id, force=True)
            actual = _case_data(store)
            try:
                first = _judge(gateway, case, actual)
                second = _judge(gateway, case, actual)  # same payload, independent call
            except ModelError as error:
                raise RuntimeError(f"judge failed for {case['id']}: {error.summary}") from error
            judge, differences, decisions = _combine_judges(first, second, case["id"])
            actual = _case_data(store)  # include both judging calls in aggregate usage
            return {"case": case, "actual": actual, "judge": judge,
                    "judge_inconsistencies": differences, "judge_decisions": decisions}
        finally:
            gateway.close()
            store.close()


def run_learning_eval(configs: dict[str, ModelConfig], root: Path, split: str = "all") -> tuple[Path, dict[str, Any]]:
    root = root.resolve()
    if split not in ("dev", "holdout", "all"):
        raise ValueError("invalid split")
    cases = [json.loads(line) for name in ("learning_v1.jsonl", "learning_v2.jsonl")
             for line in (root / "evals" / name).read_text(encoding="utf-8").splitlines() if line]
    if split != "all":
        cases = [case for case in cases if case["split"] == split]
    started = time.monotonic()
    ordered: list[dict[str, Any] | None] = [None] * len(cases)
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="iris-eval") as pool:
        pending = {pool.submit(_run_case, configs, case): index for index, case in enumerate(cases)}
        for completed, future in enumerate(as_completed(pending), 1):
            index = pending[future]
            row = future.result()
            ordered[index] = row
            actual = row["actual"]
            print(f"[{completed}/{len(cases)}] {cases[index]['id']}: "
                  f"{len(actual['memories'])} memories, {len(actual['batches'])} batches", flush=True)
    rows = [row for row in ordered if row is not None]
    metrics = {name: _metrics([row for row in rows if name == "all" or row["case"]["split"] == name])
               for name in ("dev", "holdout", "all") if name == "all" or any(row["case"]["split"] == name for row in rows)}
    reports = root / "evals" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    previous_paths = sorted(reports.glob("learning-*.json"))
    previous = None
    if previous_paths:
        previous_path = previous_paths[-1]
        old = json.loads(previous_path.read_text(encoding="utf-8"))
        previous = {"path": previous_path.name, "metrics": old.get("metrics", {}), "corpus": old.get("corpus")}
    shuffled = rows[:]
    random.Random(20260928).shuffle(shuffled)
    sample_cases = math.ceil(len(rows) * 0.1)
    sample_memories = math.ceil(sum(len(row["actual"]["memories"]) for row in rows) * 0.1)
    sampled = []
    covered_memories = 0
    for row in shuffled:
        if len(sampled) >= sample_cases and covered_memories >= sample_memories:
            break
        sampled.append(row)
        covered_memories += len(row["actual"]["memories"])
    parse_failures = _parse_failure_records(rows, configs["chat"].api_key)
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "split": split,
              "elapsed_seconds": round(time.monotonic() - started, 1),
              "prompt_version": PROMPT_VERSION, "scoring_version": SCORING_VERSION,
              "chat_model": configs["chat"].model, "embedding_model": configs["embedding"].model or "unconfigured",
              "corpus": {"cases": len(cases), "messages": sum(len(row["case"]["messages"]) for row in rows),
                         "must": sum(len(row["case"]["must"]) for row in rows)},
              "metrics": metrics, "previous": previous,
              "judge_inconsistencies": [item for row in rows for item in row["judge_inconsistencies"]],
              "parse_failures": parse_failures,
              "spot_check": [{"id": row["case"]["id"], "split": row["case"]["split"], "must": row["case"]["must"],
                              "forbidden": row["case"]["forbidden"],
                              "memories": row["actual"]["memories"], "judge": row["judge"]} for row in sampled],
              "cases": [{"id": row["case"]["id"], "split": row["case"]["split"],
                         "messages": len(row["case"]["messages"]), "batches": len(row["actual"]["batches"]),
                         "new_memories": len(row["actual"]["memories"]),
                         "required_facts": len(row["case"]["must"]),
                         "covered_facts": sum(row["judge"]["fact_covered"]),
                         "judge_inconsistencies": len(row["judge_inconsistencies"])} for row in rows]}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = reports / f"learning-{stamp}-{split}.md"
    path.write_text(_report_markdown(report), encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, report
