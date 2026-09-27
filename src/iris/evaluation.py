"""Frozen learning evaluation using the production intake and learning path."""

from __future__ import annotations

import json
import random
import statistics
import tempfile
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any

from .db import Store
from .learning import LearningEngine, PROMPT_VERSION
from .memory_ops import setup_role
from .models import Gateway, ModelConfig, ModelError
from .queue import add_message, form_batch, get_batch


SCORING_VERSION = "scoring_v1"
SCORING = files("iris").joinpath("prompts", SCORING_VERSION + ".md").read_text(encoding="utf-8")


def _case_data(store: Store) -> dict[str, Any]:
    with store.read() as conn:
        memories = []
        for memory in conn.execute("""SELECT m.id,m.content,m.stance,m.belief,s.name AS speaker FROM memories m
            JOIN subjects s ON s.id=m.speaker_subject_id WHERE m.stance!='设定' ORDER BY m.id"""):
            about = [r[0] for r in conn.execute("""SELECT s.name FROM memory_subjects ms JOIN subjects s ON s.id=ms.subject_id
                WHERE ms.memory_id=?""", (memory["id"],))]
            evidence = [dict(r) for r in conn.execute("""SELECT x.id,x.kind,x.content,x.occurred_at,s.name AS sender,
                q.name AS quote_author FROM sources src JOIN messages x ON x.id=src.message_id
                JOIN subjects s ON s.id=x.sender_subject_id LEFT JOIN subjects q ON q.id=x.quote_author_subject_id
                WHERE src.memory_id=? AND src.kind='message' ORDER BY x.id""", (memory["id"],))]
            memories.append({**dict(memory), "about": about, "evidence": evidence})
        links = [dict(r) for r in conn.execute("""SELECT l.kind,l.belief,l.status,a.name AS a,b.name AS b
            FROM subject_links l JOIN subjects a ON a.id=l.subject_a JOIN subjects b ON b.id=l.subject_b""")]
        goals = [dict(r) for r in conn.execute("SELECT content,kind,deadline FROM goals ORDER BY id")]
        attempts = [dict(r) for r in conn.execute("SELECT parse_status,duration_ms,error FROM batch_attempts ORDER BY id")]
        calls = [dict(r) for r in conn.execute("""SELECT purpose,prompt_tokens,completion_tokens,reasoning_tokens,result_category
            FROM model_calls ORDER BY id""")]
        batches = [dict(r) for r in conn.execute("SELECT state,target_ids,result_json FROM batches ORDER BY id")]
    return {"memories": memories, "links": links, "goals": goals, "attempts": attempts, "calls": calls, "batches": batches}


def _judge(gateway: Gateway, case: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    payload = {"messages": case["messages"], "must": case["must"], "forbidden": case["forbidden"],
               "links": case["links"], "goals": case["goals"], "actual_memories": actual["memories"],
               "actual_links": actual["links"], "actual_goals": actual["goals"]}
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
            "goal_covered": booleans("goal_covered", len(case["goals"]))}


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
    direct = sum(1 for a in attempts if a["parse_status"] == "direct")
    repaired = sum(1 for a in attempts if a["parse_status"] == "repaired")
    total_messages = sum(len(row["case"]["messages"]) for row in rows)
    token_usage = {key: sum(call[key] or 0 for call in calls) for key in
                   ("prompt_tokens", "completion_tokens", "reasoning_tokens")}
    return {
        "cases": len(rows), "batches": len(batches), "messages": total_messages,
        "parse_direct": direct / len(batches) if batches else None,
        "parse_repaired": repaired / len(batches) if batches else None,
        "parse_total": (direct + repaired) / len(batches) if batches else None,
        "precision": sum(m["correct_worth"] for m in memories) / new_count if new_count else None,
        "fact_recall": sum(fact_covered) / len(fact_covered) if fact_covered else None,
        "false_memory_rate": sum(m["forbidden"] for m in memories) / new_count if new_count else None,
        "evidence_accuracy": sum(m["evidence_correct"] for m in memories) / new_count if new_count else None,
        "attribution_accuracy": sum(m["attribution_correct"] for m in memories) / new_count if new_count else None,
        "link_recall": sum(link_covered) / len(link_covered) if link_covered else None,
        "goal_recall": sum(goal_covered) / len(goal_covered) if goal_covered else None,
        "new_memories": new_count, "required_facts": len(fact_covered),
        "batch_p50_ms": _percentile([a["duration_ms"] for a in attempts], 50),
        "batch_p95_ms": _percentile([a["duration_ms"] for a in attempts], 95),
        "tokens": token_usage,
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
             "", "评测集为提交中冻结的 36 段虚构对话；判分使用同一个对话模型，可能偏高。未配置单价，费用无法估算。", "",
             "| 指标 | dev | holdout | 全部 | M1 门槛 |", "| --- | ---: | ---: | ---: | ---: |"]
    names = [("解析直接成功率", "parse_direct", "≥98%（总成功）"), ("解析修正后成功率", "parse_repaired", "—"),
             ("解析总成功率", "parse_total", "≥98%"), ("记忆精确率", "precision", "≥70%"),
             ("事实召回率", "fact_recall", "≥60%"), ("误记率", "false_memory_rate", "—"),
             ("证据正确率", "evidence_accuracy", "—"), ("归属正确率", "attribution_accuracy", "—"),
             ("人物联系覆盖率", "link_recall", "—"), ("目标覆盖率", "goal_recall", "—"),
             ("批次耗时 P50 ms", "batch_p50_ms", "—"), ("批次耗时 P95 ms", "batch_p95_ms", "—"),
             ("输入 token", "prompt_tokens", "—"), ("输出 token", "completion_tokens", "—"),
             ("其中推理 token", "reasoning_tokens", "—"), ("每千条消息 token", "tokens_per_1000_messages", "—"),
             ("新记忆数", "new_memories", "—"), ("必须事实数", "required_facts", "—")]
    for label, key, threshold in names:
        values = []
        for split in ("dev", "holdout", "all"):
            metric = report["metrics"].get(split)
            values.append(_fmt(metric["tokens"].get(key) if key in ("prompt_tokens", "completion_tokens", "reasoning_tokens") else metric.get(key)) if metric else "—")
        lines.append(f"| {label} | {' | '.join(values)} | {threshold} |")
    lines.extend(["", "## 与上次结果对比", ""])
    previous = report.get("previous")
    if previous:
        lines.append(f"上次报告：`{previous['path']}`。")
        for split in ("dev", "holdout", "all"):
            if split in previous["metrics"] and split in report["metrics"]:
                for key in ("parse_total", "precision", "fact_recall"):
                    old = previous["metrics"][split].get(key)
                    new = report["metrics"][split].get(key)
                    if old is not None and new is not None:
                        lines.append(f"- {split} {key}: {_fmt(old)} → {_fmt(new)}（{(new-old)*100:+.1f} 个百分点）")
    else:
        lines.append("首次评测，无上次结果。")
    lines.extend(["", "## 随机抽查清单", "", "固定随机种子 20260928；以下为至少 10% 案例的判分，供人工核对。人工结论待填写。", ""])
    for item in report["spot_check"]:
        lines.append(f"### {item['id']}（{item['split']}）")
        lines.append(f"标注事实：{'；'.join(f['fact'] for f in item['must']) or '无'}。")
        for memory in item["memories"]:
            score = next((v for v in item["judge"]["memory_results"] if v["id"] == memory["id"]), {})
            lines.append(f"- 记忆 #{memory['id']}：{memory['content']}；说话人 {memory['speaker']}；立场 {memory['stance']}；判分 {json.dumps(score, ensure_ascii=False)}。")
        lines.append(f"事实覆盖：{item['judge']['fact_covered']}。人工核对：待填写。")
        lines.append("")
    lines.extend(["## 限制", "", "本报告没有第二家对话服务商的结果；当前配置仅提供一组对话模型。人工复核尚未由用户完成。", ""])
    return "\n".join(lines)


def run_learning_eval(configs: dict[str, ModelConfig], root: Path, split: str = "all") -> tuple[Path, dict[str, Any]]:
    root = root.resolve()
    cases = [json.loads(line) for line in (root / "evals" / "learning_v1.jsonl").read_text(encoding="utf-8").splitlines() if line]
    if split not in ("dev", "holdout", "all"):
        raise ValueError("invalid split")
    if split != "all":
        cases = [case for case in cases if case["split"] == split]
    rows = []
    for position, case in enumerate(cases, 1):
        with tempfile.TemporaryDirectory(prefix="iris-eval-") as temporary:
            store = Store(Path(temporary) / "iris.db")
            setup_role(store, "Iris")
            gateway = Gateway(configs, store)
            engine = LearningEngine(store, gateway)
            try:
                for index, message in enumerate(case["messages"]):
                    add_message(store, entry_id=case["id"], entry_name=case["id"], platform="fictional",
                                entry_kind=case["entry_type"], kind=message["type"], sender=message["speaker"],
                                content=message["content"], occurred_at=message["at"], dedupe_key=f"{case['id']}-{index}",
                                account_id=f"{case['id']}:{message['speaker']}", quote_author=message.get("quote_author"))
                while True:
                    batch = form_batch(store, case["id"], PROMPT_VERSION, target_count=3)
                    if batch is None:
                        break
                    while get_batch(store, batch.id).state == "waiting":
                        engine.run_batch(batch.id, force=True)
                actual = _case_data(store)
                try:
                    judge = _judge(gateway, case, actual)
                except ModelError as error:
                    raise RuntimeError(f"judge failed for {case['id']}: {error.summary}") from error
                actual = _case_data(store)  # includes judge usage
                rows.append({"case": case, "actual": actual, "judge": judge})
                print(f"[{position}/{len(cases)}] {case['id']}: {len(actual['memories'])} memories, {len(actual['batches'])} batches", flush=True)
            finally:
                gateway.close()
                store.close()
    metrics = {name: _metrics([row for row in rows if name == "all" or row["case"]["split"] == name])
               for name in ("dev", "holdout", "all") if name == "all" or any(row["case"]["split"] == name for row in rows)}
    reports = root / "evals" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    previous_path = sorted(reports.glob("learning-*.json"))[-1] if list(reports.glob("learning-*.json")) else None
    previous = None
    if previous_path:
        old = json.loads(previous_path.read_text(encoding="utf-8"))
        previous = {"path": previous_path.name, "metrics": old["metrics"]}
    sampled = random.Random(20260928).sample(rows, max(1, (len(rows) + 9) // 10))
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "split": split,
              "prompt_version": PROMPT_VERSION, "scoring_version": SCORING_VERSION,
              "chat_model": configs["chat"].model, "embedding_model": configs["embedding"].model or "unconfigured",
              "metrics": metrics, "previous": previous,
              "spot_check": [{"id": row["case"]["id"], "split": row["case"]["split"], "must": row["case"]["must"],
                              "memories": row["actual"]["memories"], "judge": row["judge"]} for row in sampled],
              "cases": [{"id": row["case"]["id"], "split": row["case"]["split"], "judge": row["judge"],
                         "actual": row["actual"]} for row in rows]}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = reports / f"learning-{stamp}-{split}.md"
    path.write_text(_report_markdown(report), encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, report
