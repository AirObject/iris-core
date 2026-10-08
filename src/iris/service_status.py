"""Read-only service diagnostics; timestamps use the role's timezone for usage windows."""
import json
from datetime import timedelta, timezone

from .model_health import usage_window, utc_now
from .models import (CHAT_TOTAL_TIMEOUT, EMBEDDING_TOTAL_TIMEOUT, JUDGE_TOTAL_TIMEOUT,
                     LEARNING_TOTAL_TIMEOUT, RECALL_JUDGE_TOTAL_TIMEOUT)
from .retrieval import backlog, latest_models


def percentile(values, percent):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * percent / 100
    low = int(index)
    return round(values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (index - low), 2)


def service_status(store, scheduler, health, *, clock=utc_now):
    current = clock()
    start, end = usage_window(store, current)
    from zoneinfo import ZoneInfo
    local = current.astimezone(ZoneInfo(str(store.setting("timezone", "Asia/Shanghai"))))
    week = (local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=local.weekday())).astimezone(timezone.utc)
    with store.read() as conn:
        entries = backlog(conn)
        batches = [dict(r) for r in conn.execute("""SELECT id,entry_id,state,attempt_count,next_retry_at,
            last_error,created_at,finished_at,result_json FROM batches ORDER BY id""")]
        for batch in batches:
            batch["result"] = json.loads(batch.pop("result_json"))
        for entry in entries:
            own = [batch for batch in batches if batch["entry_id"] == entry["entry_id"]]
            entry["current_batch"] = next((b for b in own if b["state"] in ("waiting", "running")), None)
            entry["latest_batch"] = next((b for b in reversed(own) if b["state"] not in ("waiting", "running")), None)
        def usage(since):
            row = conn.execute("""SELECT COUNT(*) AS calls,COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens),0) AS completion_tokens,COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens,
                COALESCE(SUM(result_category!='success'),0) AS failures,
                COALESCE(SUM(prompt_tokens IS NULL AND completion_tokens IS NULL),0) AS calls_without_usage
                FROM model_calls WHERE julianday(created_at)>=julianday(?) AND julianday(created_at)<=julianday(?)""",
                (since.isoformat(), current.isoformat())).fetchone()
            result = dict(row)
            result["tokens"] = result["prompt_tokens"] + result["completion_tokens"]
            result["failure_rate"] = result["failures"] / result["calls"] if result["calls"] else None
            return result
        learning = conn.execute("""SELECT purpose,batch_id,duration_ms,timed_out,error_summary,
            result_category,status_code,finish_reason,prompt_tokens,completion_tokens,reasoning_tokens,
            reasoning_effort,reasoning_present,reasoning_chars FROM model_calls
            WHERE purpose IN ('learning','learning_repair') AND julianday(created_at)>=julianday(?)
            AND julianday(created_at)<=julianday(?) ORDER BY created_at DESC,id DESC""",
            ((current-timedelta(hours=24)).isoformat(), current.isoformat())).fetchall()
        durations = [r["duration_ms"] for r in learning]
        result = {"service": "ready", "models": latest_models(conn), "backlog": entries, "entries": entries,
                  "batches": batches, "memory_gap_count": conn.execute("SELECT COUNT(*) FROM memory_gaps").fetchone()[0],
                  "missing_vectors": conn.execute("SELECT COUNT(*) FROM memories WHERE embedding IS NULL AND lifecycle!='deleted'").fetchone()[0],
                  "usage": {"today": usage(start), "week": usage(week)}, "learning_calls_24h": [dict(r) for r in learning],
                  "learning_latency_24h": {"count": len(durations), "p50_ms": percentile(durations, 50),
                      "p95_ms": percentile(durations, 95), "max_ms": max(durations, default=None),
                      "timeouts": sum(bool(r["timed_out"]) or "timeout" in (r["error_summary"] or "").casefold() for r in learning)}}
    chat = health.configs.get("chat")
    result.update(chat_reasoning_effort=chat.reasoning_effort if chat else None,
                  model_health=health.snapshot(), budget=health.budget(),
                  scheduler={"running": scheduler.running, "max_concurrent": store.setting("learning_concurrency", 2),
                             "last_error": scheduler.last_error},
                  timeouts_seconds={"learning": LEARNING_TOTAL_TIMEOUT, "chat": CHAT_TOTAL_TIMEOUT,
                                    "embedding": EMBEDDING_TOTAL_TIMEOUT, "retrieval_query": 2, "judge": JUDGE_TOTAL_TIMEOUT,
                                    "recall_judge": RECALL_JUDGE_TOTAL_TIMEOUT})
    return result


def add_health_hints(result, health):
    for kind, state in health.snapshot().items():
        if state["state"] != "normal":
            result["hints"].append({"code": "model_paused", "kind": kind, **state,
                "message": {"embedding": "embedding 已暂停，使用全文检索。",
                            "recall_judge": ("召回判断限流退避中，保留原召回。" if state["state"] == "rate_limited"
                                             else "召回判断已暂停，保留原召回。"),
                            "chat": "学习已暂停，消息仍正常接收。"}.get(kind, "模型用途已暂停。")})
    return result
