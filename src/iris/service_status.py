"""Read-only service diagnostics; timestamps use the role's timezone for usage windows."""
import json
from datetime import timedelta, timezone
from collections import defaultdict

from .model_health import usage_window, utc_now
from .models import (CHAT_TOTAL_TIMEOUT, EMBEDDING_TOTAL_TIMEOUT, JUDGE_TOTAL_TIMEOUT,
                     LEARNING_TOTAL_TIMEOUT, RECALL_JUDGE_TOTAL_TIMEOUT, IMAGE_TOTAL_TIMEOUT)
from .retrieval import backlog, latest_models


def percentile(values, percent):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * percent / 100
    low = int(index)
    return round(values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (index - low), 2)


def model_health_snapshot(health):
    result = health.snapshot()
    image = health.configs.get('image_understanding')
    # Health normally hides an unconfigured optional image model. A saved model
    # whose credential is unavailable still needs a visible configuration error.
    if image and image.model and not image.base_url and 'image_understanding' not in result:
        state = health.store.setting('model_health.image_understanding', {})
        result['image_understanding'] = {key: value for key, value in state.items() if key != 'fingerprint'}
        result['image_understanding']['timeout_seconds'] = IMAGE_TOTAL_TIMEOUT
    return result


def service_status(store, scheduler, health, *, clock=utc_now):
    current = clock()
    start, end = usage_window(store, current)
    from zoneinfo import ZoneInfo
    local = current.astimezone(ZoneInfo(str(store.setting("timezone", "Asia/Shanghai"))))
    week = (local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=local.weekday())).astimezone(timezone.utc)
    with store.read() as conn:
        entries = backlog(conn)
        batches = [dict(r) for r in conn.execute("""SELECT id,entry_id,state,attempt_count,next_retry_at,
            last_error,created_at,finished_at,result_json FROM batches
            WHERE state IN ('waiting','running') OR id IN (
                SELECT id FROM (SELECT id,ROW_NUMBER() OVER (
                    PARTITION BY entry_id ORDER BY id DESC) AS position
                    FROM batches WHERE state NOT IN ('waiting','running')) WHERE position<=20)
            ORDER BY id""")]
        by_entry = defaultdict(list)
        for batch in batches:
            batch["result"] = json.loads(batch.pop("result_json"))
            by_entry[batch["entry_id"]].append(batch)
        for entry in entries:
            own = by_entry[entry["entry_id"]]
            entry["current_batch"] = next((b for b in own if b["state"] in ("waiting", "running")), None)
            entry["latest_batch"] = next((b for b in reversed(own) if b["state"] not in ("waiting", "running")), None)
        # Both windows share one aggregate scan; preserve the original inclusive
        # timestamp and missing-usage semantics without returning call bodies.
        terms = {'calls': '1', 'prompt_tokens': 'prompt_tokens', 'completion_tokens': 'completion_tokens',
                 'reasoning_tokens': 'reasoning_tokens', 'failures': "result_category!='success'",
                 'calls_without_usage': 'prompt_tokens IS NULL AND completion_tokens IS NULL'}
        windows = {'today': start, 'week': week}
        columns, arguments = [], []
        for window, since in windows.items():
            for field, expression in terms.items():
                columns.append(f"COALESCE(SUM(CASE WHEN julianday(created_at)>=julianday(?) THEN ({expression}) ELSE 0 END),0) AS {window}_{field}")
                arguments.append(since.isoformat())
        row = conn.execute('SELECT '+','.join(columns)+
            ' FROM model_calls WHERE julianday(created_at)>=julianday(?) AND julianday(created_at)<=julianday(?)',
            (*arguments, min(windows.values()).isoformat(), current.isoformat())).fetchone()
        usage = {}
        for window in windows:
            values = {field: row[f'{window}_{field}'] for field in terms}
            values['tokens'] = values['prompt_tokens'] + values['completion_tokens']
            values['failure_rate'] = values['failures'] / values['calls'] if values['calls'] else None
            usage[window] = values
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
                  "usage": usage, "learning_calls_24h": [dict(r) for r in learning],
                  "learning_latency_24h": {"count": len(durations), "p50_ms": percentile(durations, 50),
                      "p95_ms": percentile(durations, 95), "max_ms": max(durations, default=None),
                      "timeouts": sum(bool(r["timed_out"]) or "timeout" in (r["error_summary"] or "").casefold() for r in learning)}}
    chat = health.configs.get("chat")
    result.update(chat_reasoning_effort=chat.reasoning_effort if chat else None,
                  model_health=model_health_snapshot(health), budget=health.budget(),
                  scheduler={"running": scheduler.running, "max_concurrent": store.setting("learning_concurrency", 2),
                             "last_error": scheduler.last_error},
                  timeouts_seconds={"learning": LEARNING_TOTAL_TIMEOUT, "chat": CHAT_TOTAL_TIMEOUT,
                                    "embedding": EMBEDDING_TOTAL_TIMEOUT, "retrieval_query": 2, "judge": JUDGE_TOTAL_TIMEOUT,
                                    "recall_judge": RECALL_JUDGE_TOTAL_TIMEOUT})
    return result


def add_health_hints(result, health):
    for kind, state in model_health_snapshot(health).items():
        if state["state"] != "normal":
            result["hints"].append({"code": "model_paused", "kind": kind, **state,
                "message": {"embedding": "embedding 已暂停，使用全文检索。",
                            "recall_judge": ("召回判断限流退避中，保留原召回。" if state["state"] == "rate_limited"
                                             else "召回判断已暂停，保留原召回。"),
                            "chat": "学习已暂停，消息仍正常接收。"}.get(kind, "模型用途已暂停。")})
    return result
