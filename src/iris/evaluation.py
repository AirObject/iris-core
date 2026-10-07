"""Frozen learning evaluation using the production intake and learning path."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import random
import tempfile
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any

from .db import Store
from .learning import LearningEngine, PROMPT_VERSION, _same_goal
from .memory_ops import setup_role
from .models import LEARNING_TOTAL_TIMEOUT, JUDGE_TOTAL_TIMEOUT, Gateway, ModelConfig, ModelError
from .queue import add_message, form_batch, get_batch


SCORING_VERSION = "scoring_v3"
JUDGE_MAX_TOKENS = 16000
SCORING = files("iris").joinpath("prompts", SCORING_VERSION + ".md").read_text(encoding="utf-8")


def _pace_for_entry(kind: str) -> str:
    return "realtime" if kind in ("live", "stream") else "standard"


def _case_data(store: Store) -> dict[str, Any]:
    with store.read() as conn:
        memories = []
        for memory in conn.execute("""SELECT m.id,m.content,m.stance,m.belief,m.speaker_subject_id,s.name AS speaker FROM memories m
            JOIN subjects s ON s.id=m.speaker_subject_id WHERE m.stance!='设定' ORDER BY m.id"""):
            about = [dict(r) for r in conn.execute("""SELECT s.id,s.name FROM memory_subjects ms JOIN subjects s ON s.id=ms.subject_id
                WHERE ms.memory_id=? ORDER BY s.id""", (memory["id"],))]
            evidence = [dict(r) for r in conn.execute("""SELECT x.id,x.kind,x.content,x.occurred_at,x.entry_id,
                x.quote_content,x.sender_subject_id,x.quote_author_subject_id,s.name AS sender,q.name AS quote_author
                FROM sources src JOIN messages x ON x.id=src.message_id
                JOIN subjects s ON s.id=x.sender_subject_id LEFT JOIN subjects q ON q.id=x.quote_author_subject_id
                WHERE src.memory_id=? AND src.kind='message' ORDER BY x.id""", (memory["id"],))]
            memories.append({**dict(memory), "about": [p["name"] for p in about],
                             "about_subject_ids": [p["id"] for p in about], "evidence": evidence})
        links = [dict(r) for r in conn.execute("""SELECT l.kind,l.belief,l.status,a.name AS a,b.name AS b,
            l.subject_a,l.subject_b,l.source_message_id,m.sender_subject_id AS evidence_sender_subject_id,
            m.quote_author_subject_id AS evidence_quote_author_subject_id,
            m.quote_content AS evidence_quote_content,
            m.content AS evidence_content,m.kind AS evidence_kind FROM subject_links l
            JOIN subjects a ON a.id=l.subject_a JOIN subjects b ON b.id=l.subject_b
            LEFT JOIN messages m ON m.id=l.source_message_id ORDER BY l.id""")]
        aliases = [dict(r) for r in conn.execute("""SELECT a.subject_id,a.alias,a.source_message_id,s.name,
            m.content AS evidence_content,m.kind AS evidence_kind,m.sender_subject_id AS evidence_sender_subject_id,
            m.quote_content AS evidence_quote_content,
            m.quote_author_subject_id AS evidence_quote_author_subject_id
            FROM subject_aliases a JOIN subjects s ON s.id=a.subject_id
            LEFT JOIN messages m ON m.id=a.source_message_id ORDER BY a.subject_id,a.alias""")]
        links.extend({**a, "kind": "alias", "a": a["name"], "b": a["alias"], "subject_a": a["subject_id"]}
                     for a in aliases if a["source_message_id"] is not None)
        goals = [dict(r) for r in conn.execute("SELECT content,kind,deadline,entry_id,state FROM goals ORDER BY id")]
        attempts = [dict(r) for r in conn.execute("""SELECT a.number,a.parse_status,a.duration_ms,a.error,
            a.raw_output,a.batch_id FROM batch_attempts a ORDER BY a.id""")]
        calls = [dict(r) for r in conn.execute("""SELECT purpose,prompt_tokens,completion_tokens,reasoning_tokens,result_category,
            finish_reason,batch_id,duration_ms,error_summary,status_code,timed_out,
            reasoning_effort,reasoning_present,reasoning_chars
            FROM model_calls ORDER BY id""")]
        batches = [dict(r) for r in conn.execute("SELECT id,state,target_ids,result_json FROM batches ORDER BY id")]
        identities = [dict(r) for r in conn.execute("""SELECT s.id,s.name,p.account_id FROM subjects s
            LEFT JOIN platform_identities p ON p.subject_id=s.id ORDER BY s.id""")]
        for person in identities:
            person["aliases"] = [a["alias"] for a in aliases if a["subject_id"] == person["id"]]
    return {"memories": memories, "links": links, "goals": goals, "attempts": attempts,
            "calls": calls, "batches": batches, "identities": identities, "aliases": aliases}


def _normal_name(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    # A stringified JSON/Python container is still not a person's name.
    try:
        return not isinstance(ast.literal_eval(value), (list, dict, tuple, set))
    except (ValueError, SyntaxError):
        return True


def _link_matches(expected: dict[str, Any], actual: dict[str, Any], identities: list[dict[str, Any]]) -> bool:
    if expected.get("kind") != actual.get("kind"):
        return False
    def names(side: str) -> set[str]:
        result = {actual.get(side)}
        for person in identities:
            if person["id"] == actual.get("subject_" + side):
                result.update([person["name"], *person.get("aliases", [])])
        return result
    a, b = names("a"), names("b")
    return ((expected.get("a") in a and expected.get("b") in b) or
            (expected.get("kind") == "same_as" and expected.get("a") in b and expected.get("b") in a))


def _has_required_subjects(fact: dict[str, Any], memory: dict[str, Any], identities: list[dict[str, Any]]) -> bool:
    """Necessary structural constraints only; the model still checks meaning and stance."""
    names: dict[str, set[str]] = {}
    accounts: dict[str, set[str]] = {}
    for person in identities:
        names.setdefault(person["id"], set()).update([person["name"], *person.get("aliases", [])])
        if person.get("account_id") is not None:
            accounts.setdefault(person["id"], set()).add(person["account_id"])
    speaker_id = memory.get("speaker_subject_id")
    if fact.get("speaker") and fact["speaker"] not in names.get(speaker_id, set()):
        return False
    if fact.get("speaker_account_id") and fact["speaker_account_id"] not in accounts.get(speaker_id, set()):
        return False
    about_ids = memory.get("about_subject_ids", [])
    about_names = {name for sid in about_ids for name in names.get(sid, set())}
    about_accounts = {account for sid in about_ids for account in accounts.get(sid, set())}
    return set(fact.get("about", [])) <= about_names and set(fact.get("about_account_ids", [])) <= about_accounts


def _judge_payload(case: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    return {"messages": [{"id": i, **message} for i, message in enumerate(case["messages"], 1)],
            "must": case["must"], "forbidden": case["forbidden"],
            "links": case["links"], "goals": case["goals"], "actual_memories": actual["memories"],
            "actual_links": actual["links"], "actual_goals": actual["goals"],
            "actual_subjects": actual["identities"], "actual_aliases": actual.get("aliases", []),
            "target_segments": [json.loads(batch["target_ids"]) for batch in actual["batches"]]}


def _judge(gateway: Gateway, case: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    payload = _judge_payload(case, actual)
    output, *_ = gateway.json_chat(
        [{"role": "system", "content": SCORING},
         {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        "learning_judge", max_tokens=JUDGE_MAX_TOKENS)
    return _score_judgment(output, case, actual)


def _score_judgment(output: dict[str, Any], case: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    """Legacy normalization and deterministic vetoes, shared by both judging paths."""
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
    actual_link_correct = booleans("actual_link_correct", len(actual["links"]))
    model_verdicts = {"actual_link_correct": actual_link_correct[:],
                      "link_covered": booleans("link_covered", len(case["links"])),
                      "fact_covered": booleans("fact_covered", len(case["must"]))}
    for index, link in enumerate(actual["links"]):
        if not all(_normal_name(link.get(key)) for key in ("a", "b")):
            actual_link_correct[index] = False
        if link.get("kind") == "alias" and not any(
                a["subject_id"] == link.get("subject_a") and a["alias"] == link.get("b")
                for a in actual.get("aliases", [])):
            actual_link_correct[index] = False
    covered = model_verdicts["link_covered"][:]
    for index, expected in enumerate(case["links"]):
        covered[index] = covered[index] and any(
            correct and _link_matches(expected, link, actual["identities"])
            for correct, link in zip(actual_link_correct, actual["links"], strict=True))
    fact_covered = [covered and any(_has_required_subjects(fact, m, actual["identities"]) for m in actual["memories"])
                    for fact, covered in zip(case["must"], model_verdicts["fact_covered"], strict=True)]
    return {"memory_results": results, "fact_covered": fact_covered,
            "link_covered": covered,
            "goal_covered": booleans("goal_covered", len(case["goals"])),
            "actual_link_correct": actual_link_correct, "model_verdicts": model_verdicts}


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
            # Shape/type checks can force both scores false; still disclose differing model votes.
            old = first.get("model_verdicts", {}).get(key, first[key])[index - 1]
            new = second.get("model_verdicts", {}).get(key, second[key])[index - 1]
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
    alias_covered = [value for row in rows for link, value in
                     zip(row["case"].get("links", []), row["judge"]["link_covered"])
                     if link.get("kind") == "alias"]
    actual_alias_correct = [value for row in rows for link, value in
                            zip(row["actual"].get("links", []), row["judge"].get("actual_link_correct", []))
                            if link.get("kind") == "alias"]
    learning_calls = [c for c in calls if c["purpose"] in ("learning", "learning_repair")]
    length_batches = sum(len({c["batch_id"] for c in row["actual"]["calls"]
                              if c["purpose"] in ("learning", "learning_repair") and c.get("finish_reason") == "length"})
                         for row in rows)
    quote_repaired = sum(1 for a in attempts if a["parse_status"] == "quote_repaired")
    direct = sum(1 for a in attempts if a["parse_status"] == "direct") + quote_repaired
    repaired = sum(1 for a in attempts if a["parse_status"] == "repaired")
    duplicates = [_duplicate_goals(row["actual"].get("goals", []),
                                   {item["name"] for item in row["actual"].get("identities", [])
                                    if item["id"] not in ("self", "scene")}) for row in rows]
    judge_changes = sum(len(row.get("judge_inconsistencies", [])) for row in rows)
    judge_decisions = sum(row.get("judge_decisions", 0) for row in rows)
    total_messages = sum(len(row["case"]["messages"]) for row in rows)
    token_usage = {key: sum(call.get(key) or 0 for call in calls) for key in
                   ("prompt_tokens", "completion_tokens", "reasoning_tokens")}
    def usage_for(prefixes: tuple[str, ...]) -> dict[str, int]:
        chosen = [call for call in calls if call["purpose"] in prefixes]
        return {key: sum(call.get(key) or 0 for call in chosen) for key in
                ("prompt_tokens", "completion_tokens", "reasoning_tokens")}
    learning_usage = usage_for(("learning", "learning_repair"))
    judge_usage = usage_for(("learning_judge", "learning_judge_repair"))
    embedding_usage = usage_for(("learning_context", "memory_embedding", "retrieval_query"))
    return {
        "cases": len(rows), "batches": len(batches), "messages": total_messages,
        "parse_direct": direct / len(batches) if batches else None,
        "parse_quote_repaired": quote_repaired,
        "parse_repaired": repaired / len(batches) if batches else None,
        "parse_total": (direct + repaired) / len(batches) if batches else None,
        "length_truncated_batches": length_batches,
        "length_truncated_calls": sum(c.get("finish_reason") == "length" for c in learning_calls),
        "judge_length_truncated_calls": sum(c.get("finish_reason") == "length" for c in calls
                                             if c["purpose"] in ("learning_judge", "learning_judge_repair")),
        "learning_timeout_calls": sum("timeout" in (c.get("error_summary") or "").casefold() for c in learning_calls),
        "judge_timeout_calls": sum("timeout" in (c.get("error_summary") or "").casefold() for c in calls
                                   if c["purpose"] in ("learning_judge", "learning_judge_repair")),
        "learning_call_p95_ms": _percentile([c["duration_ms"] for c in learning_calls if c.get("duration_ms") is not None], 95),
        "learning_call_max_ms": max((c["duration_ms"] for c in learning_calls if c.get("duration_ms") is not None), default=None),
        "learning_max_completion_tokens": max((c["completion_tokens"] for c in learning_calls if c.get("completion_tokens") is not None), default=None),
        "precision": sum(m["correct_worth"] for m in memories) / new_count if new_count else None,
        "fact_recall": sum(fact_covered) / len(fact_covered) if fact_covered else None,
        "false_memory_rate": sum(m["forbidden"] for m in memories) / new_count if new_count else None,
        "evidence_accuracy": sum(m["evidence_correct"] for m in memories) / new_count if new_count else None,
        "attribution_accuracy": sum(m["attribution_correct"] for m in memories) / new_count if new_count else None,
        "link_recall": sum(link_covered) / len(link_covered) if link_covered else None,
        "link_precision": sum(actual_link_correct) / len(actual_link_correct) if actual_link_correct else None,
        "alias_recall": sum(alias_covered) / len(alias_covered) if alias_covered else None,
        "alias_precision": sum(actual_alias_correct) / len(actual_alias_correct) if actual_alias_correct else None,
        "required_aliases": len(alias_covered), "new_aliases": len(actual_alias_correct),
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
    external = report.get("judge_mode", "model") == "external"
    judging = ("外部执行者判分；外部判分的用量、耗时与超时不由本程序测量。" if external else
               "对话模型判分（预览，不作门槛依据）；判分与学习使用同一模型。")
    lines = ["# Iris 学习评测", "", f"时间：{report['created_at']}",
             f"学习提示词：`{report['prompt_version']}`；评分说明：`{report['scoring_version']}`；学习模型：`{report['chat_model']}`；embedding：`{report['embedding_model']}`。",
             f"对话推理档位：`{report.get('chat_reasoning_effort', '未知（旧记录）') or '未配置（不发送）'}`。",
             f"判分方式：{'外部' if external else '对话模型预览'}；判分模型：`{report.get('judge_model', report['chat_model'])}`。",
             f"判分材料 SHA-256：`{report.get('materials_sha256') or '不适用'}`。",
             f"学习请求（含重试和 JSON 修正）总超时 {report.get('timeouts_seconds', {}).get('learning', LEARNING_TOTAL_TIMEOUT)} 秒；"
             f"评测判分请求总超时 {report.get('timeouts_seconds', {}).get('judge', JUDGE_TOTAL_TIMEOUT)} 秒。",
             "", f"评测集：{report['corpus']['cases']} 段、{report['corpus']['messages']} 条消息、"
             f"{report['corpus']['must']} 条 must；当前运行 {report['split']}。每段判 {report.get('judge_runs', 2)} 次；双判分歧按不利结论计分，单判不计算分歧率。"
             f"{judging}仍需人工抽查。未配置单价，费用无法估算。", "",
             "| 指标 | dev | holdout | 全部 | M1 门槛 |", "| --- | ---: | ---: | ---: | ---: |"]
    names = [("解析直接成功率", "parse_direct", "≥98%"),
             ("因长度截断的批次数", "length_truncated_batches", "单列"),
             ("学习长度截断调用数（含修正）", "length_truncated_calls", "单列"),
             ("判分长度截断调用数", "judge_length_truncated_calls", "单列"),
             ("其中引号确定性修复次数", "parse_quote_repaired", "单列"),
             ("解析模型修正后成功率", "parse_repaired", "单列"),
             ("解析总成功率", "parse_total", "—"), ("记忆精确率", "precision", "≥70%"),
             ("事实召回率", "fact_recall", "≥60%"), ("误记率", "false_memory_rate", "—"),
             ("证据正确率", "evidence_accuracy", "—"), ("归属正确率", "attribution_accuracy", "—"),
             ("人物联系精确率", "link_precision", "—"), ("人物联系覆盖率", "link_recall", "—"),
             ("别名精确率", "alias_precision", "—"), ("别名覆盖率", "alias_recall", "—"),
             ("实际别名数", "new_aliases", "—"), ("必需别名数", "required_aliases", "—"),
             ("目标覆盖率", "goal_recall", "—"),
             ("目标重复数", "goal_duplicates", "—"), ("询问重复数", "question_duplicates", "—"),
             ("判分不一致率", "judge_inconsistency_rate", "—"),
             ("批次耗时 P50 ms", "batch_p50_ms", "—"), ("批次耗时 P95 ms", "batch_p95_ms", "—"),
             ("学习生成调用 P95 ms", "learning_call_p95_ms", "—"),
             ("学习生成调用最长 ms", "learning_call_max_ms", "120 秒超时"),
             ("学习生成超时调用数", "learning_timeout_calls", "单列"),
             ("判分生成超时调用数", "judge_timeout_calls", "单列"),
             ("学习单次最大输出 token（含推理）", "learning_max_completion_tokens", "上限 16000"),
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
        lines.extend(["", f"本评测集 M1 三项学习指标（全部与 holdout 同时满足）：{'达标' if passed else '未达标'}。"
                      "仓库历史 holdout 只作回归对照；M1 最终验收还需规划者运行隐藏验收集。"])
        for label, key, limit in checks:
            lines.append(f"- {label}：全部 {_fmt(all_metrics.get(key))}，holdout {_fmt(holdout.get(key))}；门槛 {_fmt(limit)}。")
    else:
        lines.extend(["", "本次没有 holdout 结果，不能判定 M1 学习门槛是否最终达标；最终还需规划者运行隐藏验收集。"])
    lines.extend(["", "M2 参考门槛（本 PR 不要求）：记忆精确率 ≥85%；事实召回率 ≥75%；"
                  "误记率 ≤5%；证据正确率 ≥90%；归属正确率 ≥90%。"])
    lines.extend(["", "## 与上次结果对比", ""])
    previous = report.get("previous")
    if (previous and previous.get("corpus") == report["corpus"]
            and previous.get("scoring_version") == report["scoring_version"]
            and previous.get("judge_runs", 2) == report.get("judge_runs", 2)
            and previous.get("chat_model") == report.get("chat_model")
            and previous.get("judge_mode", "model") == report.get("judge_mode", "model")):
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
        lines.append("没有同一评测集、评分规则、判分次数、学习模型和判分方式的可比前次结果。")
    lines.extend(["", "## 判分不一致清单", ""])
    if report["judge_inconsistencies"]:
        for item in report["judge_inconsistencies"]:
            identifier = f"记忆 #{item['id']}" if item["item"] == "memory" else f"{item['item']} 第 {item['index']} 项"
            lines.append(f"- {item['case']} {identifier} {item.get('field', '')}：第一次 {item['first']}，第二次 {item['second']}，计分 {item['scored']}。")
    elif report.get("judge_runs", 2) == 1:
        lines.append("本次为单判，不计算双判分歧。")
    else:
        lines.append("两次判分结论一致。")
    lines.extend(["", "## 首轮解析异常输出", ""])
    if report["parse_failures"]:
        for item in report["parse_failures"]:
            lines.extend([f"### {item['case']} 批次 {item['batch']} 尝试 {item['attempt']}（{item['parse_status']}）",
                          "", *("    " + part for part in item["first_raw"].splitlines()), ""])
    else:
        lines.append("所有批次首轮直接解析成功。")
    lines.extend(["", "## 长度截断调用", ""])
    truncated = [c for c in report.get("generation_calls", []) if c["finish_reason"] == "length"]
    for call in truncated:
        lines.append(f"- {call['case']} 批次 {call['batch_id']} {call['purpose']}：finish_reason=length，输出 {call['completion_tokens']} token。")
    if not truncated:
        lines.append("学习无 finish_reason=length；外部判分调用未测量。" if external else "学习与判分均无 finish_reason=length。")
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
        lines.append(f"人物联系及别名：{json.dumps(item.get('links', []), ensure_ascii=False)}。")
        lines.append("")
    lines.extend(["## 限制", "", "本报告没有第二家对话服务商的结果；当前配置仅提供一组对话模型。人工复核尚未由用户完成。"])
    for note in report.get("review_notes", []):
        lines.append("- " + note)
    lines.append("")
    return "\n".join(lines)


def _run_case(configs: dict[str, ModelConfig], case: dict[str, Any], judge_runs: int = 2,
              judge_mode: str = "model") -> dict[str, Any]:
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
            if judge_mode == "external":
                return {"case": case, "actual": actual}
            try:
                first = _judge(gateway, case, actual)
                second = _judge(gateway, case, actual) if judge_runs == 2 else None
            except ModelError as error:
                raise RuntimeError(f"judge failed for {case['id']}: {error.summary}") from error
            judge, differences, decisions = _combine_judges(first, second, case["id"]) if second is not None else (first, [], 0)
            actual = _case_data(store)  # include both judging calls in aggregate usage
            return {"case": case, "actual": actual, "judge": judge,
                    "judge_inconsistencies": differences, "judge_decisions": decisions,
                    "judges": [first, second] if second is not None else [first]}
        finally:
            gateway.close()
            store.close()


def run_learning_eval(configs: dict[str, ModelConfig], root: Path, split: str = "all", *,
                      corpus: Path | None = None, out: Path | None = None, judge_runs: int = 2,
                      judge_mode: str = "model") -> tuple[Path, dict[str, Any]]:
    root = root.resolve()
    if judge_mode not in ("model", "external"):
        raise ValueError("judge_mode must be model or external")
    if judge_runs not in (1, 2):
        raise ValueError("judge_runs must be 1 or 2")
    if split not in ("dev", "holdout", "all"):
        raise ValueError("invalid split")
    paths = [corpus.resolve()] if corpus is not None else [root / "evals" / name for name in
                                                         ("learning_v1.jsonl", "learning_v2.jsonl", "learning_v3.jsonl")]
    cases = [json.loads(line) for path in paths for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len({c["id"] for c in cases}) != len(cases):
        raise ValueError("duplicate evaluation case IDs")
    if split != "all":
        cases = [case for case in cases if case["split"] == split]
    if not cases:
        raise ValueError("no evaluation cases for requested split")
    started = time.monotonic()
    reports = out.resolve() if out is not None else root / "evals" / "reports"
    sources = sorted(p for p in Path(__file__).parent.rglob("*") if p.suffix in (".py", ".md", ".sql", ".json"))
    source_hash = hashlib.sha256(b"".join(p.name.encode() + p.read_bytes() for p in sources)).hexdigest()
    signature = hashlib.sha256(json.dumps({"sources": source_hash, "cases": cases, "judge_runs": judge_runs if judge_mode == "model" else 0,
        "models": {k: (v.base_url, v.model, v.dimensions, v.reasoning_effort if k == "chat" else None) for k, v in configs.items()}}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    checkpoints = (reports / ".lc" if not reports.is_relative_to(root) else root / "data/lc") / signature[:16]
    checkpoints.mkdir(parents=True, exist_ok=True)
    # The shortened path is not the identity: verify all 256 bits before reuse.
    metadata = checkpoints / 'meta.json'
    if metadata.exists() and json.loads(metadata.read_text(encoding='utf-8'))['signature'] != signature:
        raise ValueError('checkpoint fingerprint collision')
    metadata.write_text(json.dumps({'signature': signature}), encoding='utf-8')
    def save_checkpoint(path, value):
        serialized = json.dumps(value, ensure_ascii=False, indent=2)
        for config in configs.values():
            if config.api_key:
                serialized = serialized.replace(config.api_key, "[REDACTED]")
        temp = path.with_suffix(".tmp")
        temp.write_text(serialized, encoding="utf-8")
        temp.replace(path)
    resumed = 0
    failures = []
    ordered: list[dict[str, Any] | None] = [None] * len(cases)
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="iris-eval") as pool:
        pending = {}
        for index, case in enumerate(cases):
            checkpoint = checkpoints / f"{index:04}.json"
            if checkpoint.exists():
                ordered[index] = json.loads(checkpoint.read_text(encoding="utf-8"))
                resumed += 1
            else:
                pending[pool.submit(_run_case, configs, case, judge_runs, judge_mode)] = index
        for completed, future in enumerate(as_completed(pending), 1):
            index = pending[future]
            try:
                row = future.result()
            except Exception as error:
                failures.append({"case": cases[index]["id"], "error": str(error)})
                save_checkpoint(checkpoints / "failures.json", failures)
                continue
            ordered[index] = row
            save_checkpoint(checkpoints / f"{index:04}.json", row)
            actual = row["actual"]
            print(f"[{completed}/{len(cases)}] {cases[index]['id']}: "
                  f"{len(actual['memories'])} memories, {len(actual['batches'])} batches", flush=True)
    if failures:
        raise RuntimeError(f"{len(failures)} evaluation cases failed; completed cases retained in {checkpoints}")
    rows = [row for row in ordered if row is not None]
    metadata = {"split": split, "checkpoint_signature": signature, "source_sha256": source_hash,
                "resumed_cases": resumed, "elapsed_seconds": round(time.monotonic() - started, 1),
                "prompt_version": PROMPT_VERSION, "scoring_version": SCORING_VERSION,
                "timeouts_seconds": {"learning": LEARNING_TOTAL_TIMEOUT, "judge": JUDGE_TOTAL_TIMEOUT},
                "chat_model": configs["chat"].model, "chat_reasoning_effort": configs["chat"].reasoning_effort,
                "embedding_model": configs["embedding"].model or "unconfigured",
                "corpus": _corpus_info(cases), "cases": [{"id": case["id"]} for case in cases]}
    save_checkpoint(checkpoints / "metadata.json", metadata)
    if judge_mode == "external":
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return export_learning_judgments(checkpoints, reports / f"judging-materials-{stamp}")
    return _write_learning_report(rows, root, reports, metadata, judge_runs=judge_runs,
                                  judge_mode="model", judge_model=configs["chat"].model,
                                  secrets=[config.api_key for config in configs.values()])


def _corpus_info(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {"cases": len(cases), "messages": sum(len(case["messages"]) for case in cases),
            "must": sum(len(case["must"]) for case in cases), "sha256": _json_sha256(cases)}


def _write_learning_report(rows: list[dict[str, Any]], root: Path, reports: Path,
                           metadata: dict[str, Any], *, judge_runs: int, judge_mode: str,
                           judge_model: str, secrets: Sequence[str] = (),
                           materials_sha256: str | None = None,
                           judgment_rounds: Sequence[dict[str, Any]] = ()) -> tuple[Path, dict[str, Any]]:
    # Redact before truncating raw parse failures, including keys crossing the excerpt boundary.
    serialized_rows = json.dumps(rows, ensure_ascii=False)
    for secret in secrets:
        if secret:
            serialized_rows = serialized_rows.replace(secret, "[REDACTED]")
    rows = json.loads(serialized_rows)
    split = metadata["split"]
    metrics = {name: _metrics([row for row in rows if name == "all" or row["case"]["split"] == name])
               for name in ("dev", "holdout", "all") if name == "all" or any(row["case"]["split"] == name for row in rows)}
    reports.mkdir(parents=True, exist_ok=True)
    previous_paths = sorted(reports.glob("learning-*.json"))
    previous = None
    if previous_paths:
        previous_path = previous_paths[-1]
        old = json.loads(previous_path.read_text(encoding="utf-8"))
        previous = {"path": previous_path.name, "metrics": old.get("metrics", {}), "corpus": old.get("corpus"),
                    "scoring_version": old.get("scoring_version"), "judge_runs": old.get("judge_runs", 2),
                    "chat_model": old.get("chat_model"), "chat_reasoning_effort": old.get("chat_reasoning_effort"), "judge_mode": old.get("judge_mode", "model")}
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
    parse_failures = _parse_failure_records(rows, "")
    report = {**{key: value for key, value in metadata.items() if key != "cases"},
              "created_at": datetime.now(timezone.utc).isoformat(), "judge_runs": judge_runs,
              "judge_mode": judge_mode, "judge_model": judge_model,
              "materials_sha256": materials_sha256, "judgment_rounds": list(judgment_rounds),
              "metrics": metrics, "previous": previous,
              "judge_inconsistencies": [item for row in rows for item in row["judge_inconsistencies"]],
              "parse_failures": parse_failures,
              "generation_calls": [{"case": row["case"]["id"], **{key: call.get(key) for key in
                                     ("purpose", "batch_id", "finish_reason", "prompt_tokens", "completion_tokens", "reasoning_tokens",
                                      "duration_ms", "result_category", "status_code", "timed_out",
                                      "reasoning_effort", "reasoning_present", "reasoning_chars")}}
                                   for row in rows for call in row["actual"]["calls"]
                                   if call["purpose"] in ("learning", "learning_repair", "learning_judge", "learning_judge_repair")],
              "spot_check": [{"id": row["case"]["id"], "split": row["case"]["split"], "must": row["case"]["must"],
                              "forbidden": row["case"]["forbidden"],
                              "memories": row["actual"]["memories"], "judge": row["judge"],
                              "links": row["actual"]["links"], "subjects": row["actual"]["identities"]} for row in sampled],
              "cases": [{"id": row["case"]["id"], "split": row["case"]["split"],
                         "messages": len(row["case"]["messages"]), "batches": len(row["actual"]["batches"]),
                         "new_memories": len(row["actual"]["memories"]),
                         "required_facts": len(row["case"]["must"]),
                         "covered_facts": sum(row["judge"]["fact_covered"]),
                         "judge_inconsistencies": len(row["judge_inconsistencies"])} for row in rows]}
    if not reports.is_relative_to(root):
        report["details"] = rows
    # Reports may quote model output or corpus text; redact every configured key.
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    for secret in secrets:
        if secret:
            serialized = serialized.replace(secret, "[REDACTED]")
    report = json.loads(serialized)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = reports / f"learning-{stamp}-{split}.md"
    path.write_text(_report_markdown(report), encoding="utf-8")
    path.with_suffix(".json").write_text(serialized, encoding="utf-8")
    return path, report


# The transport envelope is independent of learning's payload and scoring schema,
# so another evaluation (e.g. e2e) can use the same manifest/round convention.
MATERIAL_FORMAT_VERSION = 1
_RUN_FIELDS = ("split", "checkpoint_signature", "source_sha256", "resumed_cases", "elapsed_seconds",
               "prompt_version", "scoring_version", "timeouts_seconds", "chat_model", "embedding_model", "corpus")
_OPTIONAL_RUN_FIELDS = ("chat_reasoning_effort",)  # Older materials remain scoreable without inventing a setting.
_ACTUAL_FIELDS = ("memories", "links", "goals", "attempts", "calls", "batches", "identities", "aliases")
_MEMORY_VERDICTS = ("correct_worth", "forbidden", "evidence_correct", "attribution_correct")


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _read_json(path: Path) -> Any:
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys)
    except (OSError, ValueError) as error:
        raise ValueError(f"{path}: {error}") from error


def _write_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
    return hashlib.sha256(data).hexdigest()


def _learning_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Allowlist learning data: never export prior judgments, votes or judge calls."""
    cleaned = []
    for row in rows:
        actual = {key: row["actual"][key] for key in _ACTUAL_FIELDS}
        actual["calls"] = [call for call in actual["calls"]
                           if call["purpose"] not in ("learning_judge", "learning_judge_repair")]
        cleaned.append({"case": row["case"], "actual": actual})
    return cleaned


def _check_learning_metadata(metadata: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    problems = []
    for key in _RUN_FIELDS:
        if key not in metadata:
            problems.append(f"metadata missing {key}")
    for key in ("source_sha256", "checkpoint_signature"):
        value = metadata.get(key)
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            problems.append(f"metadata {key} must be a SHA-256 fingerprint")
    effort = metadata.get("chat_reasoning_effort")
    if effort is not None and (not isinstance(effort, str) or not effort.strip()):
        problems.append("metadata chat_reasoning_effort must be a nonempty string or null")
    if metadata.get("scoring_version") != SCORING_VERSION:
        problems.append(f"metadata scoring_version must be {SCORING_VERSION}")
    if not isinstance(metadata.get("chat_model"), str) or not metadata.get("chat_model", "").strip():
        problems.append("metadata chat_model is required")
    if metadata.get("split") not in ("dev", "holdout", "all"):
        problems.append("metadata split must be dev, holdout or all")
    cases = [row["case"] for row in rows]
    if not cases or len({case["id"] for case in cases}) != len(cases):
        problems.append("missing or duplicate evaluation case IDs")
    if _corpus_info(cases) != metadata.get("corpus"):
        problems.append("corpus fingerprint/counts do not match metadata; all completed cases are required")
    for row in rows:
        if any(batch["state"] in ("waiting", "running") for batch in row["actual"]["batches"]):
            problems.append(f"{row['case']['id']}: incomplete learning checkpoint")
    if problems:
        raise ValueError("Invalid learning metadata:\n- " + "\n- ".join(problems))


def export_learning_judgments(checkpoints: Path, out: Path, *,
                              checkpoint_report: Path | None = None) -> tuple[Path, dict[str, Any]]:
    """Export completed checkpoints offline, including checkpoints predating metadata.json."""
    metadata_path = checkpoint_report if checkpoint_report is not None else checkpoints / "metadata.json"
    if not metadata_path.exists():
        raise ValueError("checkpoint metadata missing; supply --checkpoint-report with the original report JSON")
    metadata = _read_json(metadata_path)
    if not isinstance(metadata, dict) or not isinstance(metadata.get("cases"), list):
        raise ValueError("checkpoint metadata must contain the expected cases list")
    signature = metadata.get("checkpoint_signature")
    identity_path = checkpoints / "meta.json"
    identity = _read_json(identity_path) if identity_path.exists() else None
    if identity is not None and (not isinstance(identity, dict) or identity.get("signature") != signature):
        raise ValueError("checkpoint fingerprint collision")
    # Legacy full paths remain valid. A short prefix is valid only with the
    # independent full-fingerprint sidecar written before checkpoint reuse.
    if signature != checkpoints.name and not (
            identity is not None and isinstance(signature, str) and checkpoints.name == signature[:16]):
        raise ValueError("checkpoint directory fingerprint does not match metadata checkpoint_signature")
    rows, problems = [], []
    for index, expected in enumerate(metadata["cases"]):
        path = checkpoints / f"{index:04}.json"
        try:
            row = _read_json(path)
            if row["case"]["id"] != expected["id"]:
                raise ValueError(f"{path}: case ID does not match metadata")
            rows.append(row)
        except (ValueError, KeyError, TypeError) as error:
            problems.append(f"{path}: {error}")
    if problems:
        raise ValueError("Cannot export checkpoints:\n- " + "\n- ".join(problems))
    try:
        rows = _learning_rows(rows)
        _check_learning_metadata(metadata, rows)
        payloads = [_judge_payload(row["case"], row["actual"]) for row in rows]
    except (KeyError, TypeError) as error:
        raise ValueError(f"Invalid learning checkpoint structure: {error}") from error
    # Use a fresh directory so exporting never replaces materials already being judged.
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"material output directory must be empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    run = {key: metadata[key] for key in (*_RUN_FIELDS, *_OPTIONAL_RUN_FIELDS) if key in metadata}
    manifest = {"format_version": MATERIAL_FORMAT_VERSION, "evaluation": "learning", "run": run,
                "run_sha256": _write_json(out / "run.json", {"rows": rows}), "cases": []}
    (out / "scoring.md").write_text(SCORING, encoding="utf-8")
    manifest["scoring_sha256"] = hashlib.sha256((out / "scoring.md").read_bytes()).hexdigest()
    for index, (row, payload) in enumerate(zip(rows, payloads, strict=True)):
        filename = f"cases/{index:04}.json"
        document = {"format_version": MATERIAL_FORMAT_VERSION, "evaluation": "learning",
                    "case_id": row["case"]["id"], "corpus_sha256": run["corpus"]["sha256"],
                    "source_sha256": run["source_sha256"], "scoring_version": run["scoring_version"],
                    "input": payload}
        digest = _write_json(out / filename, document)
        manifest["cases"].append({"case_id": row["case"]["id"], "file": filename,
                                  "sha256": digest, "judgment_file": f"{index:04}.json"})
    manifest["materials_sha256"] = _json_sha256(manifest)
    path = out / "manifest.json"
    _write_json(path, manifest)
    _write_json(out / "round-template.json", {"materials_sha256": manifest["materials_sha256"]})
    return path, manifest


def _verified_material_file(directory: Path, filename: str, digest: str) -> Path:
    path = (directory / filename).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError(f"material file must be inside its bundle: {filename}")
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError(f"material fingerprint mismatch: {filename}")
    return path


def _load_learning_materials(directory: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = _read_json(directory / "manifest.json")
    try:
        if manifest["format_version"] != MATERIAL_FORMAT_VERSION or manifest["evaluation"] != "learning":
            raise ValueError("unsupported material format or evaluation kind")
        if manifest["materials_sha256"] != _json_sha256({k: v for k, v in manifest.items() if k != "materials_sha256"}):
            raise ValueError("manifest fingerprint mismatch")
        rows = _read_json(_verified_material_file(directory, "run.json", manifest["run_sha256"]))["rows"]
        scoring = _verified_material_file(directory, "scoring.md", manifest["scoring_sha256"])
        if scoring.read_text(encoding="utf-8") != SCORING:
            raise ValueError("material scoring rules differ from frozen scoring_v3")
        _check_learning_metadata(manifest["run"], rows)
        if len(rows) != len(manifest["cases"]):
            raise ValueError("material case count mismatch")
        for index, (entry, row) in enumerate(zip(manifest["cases"], rows, strict=True)):
            if entry["judgment_file"] != f"{index:04}.json":
                raise ValueError("invalid judgment filename")
            document = _read_json(_verified_material_file(directory, entry["file"], entry["sha256"]))
            if (entry["case_id"] != row["case"]["id"] or document["case_id"] != entry["case_id"]
                    or document["input"] != _judge_payload(row["case"], row["actual"])):
                raise ValueError(f"{entry['case_id']}: material input differs from learning data")
            for key, value in (("source_sha256", manifest["run"]["source_sha256"]),
                               ("corpus_sha256", manifest["run"]["corpus"]["sha256"]),
                               ("scoring_version", SCORING_VERSION), ("evaluation", "learning"),
                               ("format_version", MATERIAL_FORMAT_VERSION)):
                if document[key] != value:
                    raise ValueError(f"{entry['case_id']}: inconsistent {key}")
        return manifest, _learning_rows(rows)
    except (KeyError, TypeError) as error:
        raise ValueError(f"Invalid material structure: {error}") from error


def _judgment_errors(output: Any, case: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Strict scoring_v3 schema. Extra per-item or top-level reasons do not affect scores."""
    if not isinstance(output, dict):
        return ["judgment must be a JSON object"]
    errors = []
    lengths = {"memory_results": len(actual["memories"]), "fact_covered": len(case["must"]),
               "link_covered": len(case["links"]), "goal_covered": len(case["goals"]),
               "actual_link_correct": len(actual["links"])}
    for key, length in lengths.items():
        values = output.get(key)
        if not isinstance(values, list):
            errors.append(f"{key}: expected array of length {length}")
            continue
        if len(values) != length:
            errors.append(f"{key}: expected length {length}, got {len(values)}")
        for index, value in enumerate(values):
            location = f"{key}[{index}]"
            if key != "memory_results":
                if type(value) is not bool:
                    errors.append(f"{location}: expected boolean")
                continue
            if not isinstance(value, dict):
                errors.append(f"{location}: expected object")
                continue
            if index < length:
                expected_id = actual["memories"][index]["id"]
                if type(value.get("id")) is not type(expected_id) or value["id"] != expected_id:
                    errors.append(f"{location}.id: expected {expected_id!r} in input order")
            for field in _MEMORY_VERDICTS:
                if type(value.get(field)) is not bool:
                    errors.append(f"{location}.{field}: expected boolean")
    return errors


def score_learning_judgments(materials: Path, judgments: list[Path], root: Path, *,
                             judge_model: str, out: Path | None = None) -> tuple[Path, dict[str, Any]]:
    """Score one or two independent rounds offline; reject all invalid files before reporting."""
    if len(judgments) not in (1, 2):
        raise ValueError("supply one or two judgment directories")
    if len({directory.resolve() for directory in judgments}) != len(judgments):
        raise ValueError("two rounds require independent judgment directories")
    if not judge_model or not judge_model.strip():
        raise ValueError("--judge-model must declare the external executor model")
    manifest, rows = _load_learning_materials(materials)
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
        for entry, row in zip(manifest["cases"], rows, strict=True):
            path = directory / entry["judgment_file"]
            try:
                output = _read_json(path)
                errors = _judgment_errors(output, row["case"], row["actual"])
                if errors:
                    raise ValueError("; ".join(errors))
                verdicts.append(output)
                records.append({"case_id": entry["case_id"], "sha256": _json_sha256(output)})
            except ValueError as error:
                problems.append(f"round {number} ({directory}), case {entry['case_id']}: {error}")
        rounds.append(verdicts)
        round_records.append({"round": number, "cases": records})
    if problems:
        raise ValueError("Invalid external judgments:\n- " + "\n- ".join(problems))
    for index, row in enumerate(rows):
        votes = [_score_judgment(items[index], row["case"], row["actual"]) for items in rounds]
        judge, differences, decisions = (_combine_judges(*votes, row["case"]["id"])
                                         if len(votes) == 2 else (votes[0], [], 0))
        row.update(judge=judge, judges=votes, judge_inconsistencies=differences, judge_decisions=decisions,
                   external_judgments=[items[index] for items in rounds])
    root = root.resolve()
    reports = out.resolve() if out is not None else root / "evals" / "reports"
    return _write_learning_report(rows, root, reports, manifest["run"], judge_runs=len(rounds),
                                  judge_mode="external", judge_model=judge_model.strip(),
                                  materials_sha256=manifest["materials_sha256"], judgment_rounds=round_records)
