"""Read projections for the local management UI; browsing is not a recall."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .retrieval import Retrieval
from .state import report_history
from .goals import goal_list, goal_detail, notification_list
from .people import list_people, person_detail, canonical_subject
from .memory_ops import lifecycle_settings, missing_batch_targets
from .model_health import utc_now
from .queue import entry_settings, batch_rate_status
from .search_text import segmented

MEMORY_COLUMNS = """m.id,m.content,m.kind,m.speaker_subject_id,m.stance,m.belief,m.importance,
    m.retention,m.event_time,m.lifecycle,m.forgotten_at,m.pinned,m.revision,m.entry_id,m.world,m.created_at,m.updated_at"""


def memory_rows(conn, sql, args=()):
    rows = [dict(r) for r in conn.execute(sql, args)]
    for row in rows:
        row["about"] = [dict(r) for r in conn.execute("""SELECT s.id,s.name FROM memory_subjects ms
            JOIN subjects s ON s.id=ms.subject_id WHERE ms.memory_id=? ORDER BY s.id""", (row["id"],))]
        row["tags"] = [r[0] for r in conn.execute("SELECT tag FROM memory_tags WHERE memory_id=? ORDER BY tag", (row["id"],))]
    return rows


def catalog(store):
    with store.read() as conn:
        return {"people": [dict(r) for r in conn.execute("SELECT id,name,kind FROM subjects WHERE merged_into IS NULL ORDER BY name,id")],
                "entries": [dict(r) for r in conn.execute("SELECT id,name,kind FROM entries ORDER BY name,id")]}


def list_memories(store, *, text="", person_id=None, kind=None, entry_id=None,
                  time_from=None, time_to=None, lifecycle="active", sort="time", limit=30, offset=0, pinned=None):
    clauses, args = ["m.purged_at IS NULL"], []
    if lifecycle != "all":
        clauses.append("m.lifecycle=?")
        args.append(lifecycle)
    if pinned is not None:
        clauses.append("m.pinned=?")
        args.append(int(pinned))
    person_arg = len(args)
    if person_id:
        clauses.append("(m.speaker_subject_id=? OR EXISTS (SELECT 1 FROM memory_subjects ms WHERE ms.memory_id=m.id AND ms.subject_id=?))")
        args.extend([person_id, person_id])
    if kind:
        clauses.append("m.kind=?")
        args.append(kind)
    if entry_id:
        clauses.append("""(m.entry_id=? OR EXISTS (SELECT 1 FROM sources s JOIN messages msg ON msg.id=s.message_id
            WHERE s.memory_id=m.id AND msg.entry_id=?))""")
        args.extend([entry_id, entry_id])
    for operator, value in ((">=", time_from), ("<=", time_to)):
        if value:
            if operator == "<=" and len(value) == 10:
                clauses.append("julianday(COALESCE(m.event_time,m.created_at))<julianday(?,'+1 day')")
            else:
                clauses.append(f"julianday(COALESCE(m.event_time,m.created_at)){operator}julianday(?)")
            args.append(value)
    text = text.strip()
    tokens = list(dict.fromkeys(segmented(text).split()))
    rank = "0.0"
    join = ""
    prefix = []
    if tokens:
        # The management browser uses the existing lexical index, without recall
        # recording, a model call, or the reply path's relevance thresholds.
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
        join = "LEFT JOIN (SELECT rowid,rank FROM memory_fts_jieba WHERE memory_fts_jieba MATCH ?) f ON f.rowid=m.id"
        prefix.append(match)
        clauses.append("(f.rowid IS NOT NULL OR (m.lifecycle='deleted' AND instr(m.content,?)>0))")
        args.append(text)
        rank = "COALESCE(f.rank,0.0)"
    elif text:
        clauses.append("instr(m.content,?)>0")
        args.append(text)
    where = " AND ".join(clauses) or "1"
    order = {"time": "julianday(m.updated_at) DESC,m.id DESC", "retention": "m.retention DESC,m.id DESC",
             "relevance": f"{rank} ASC,julianday(m.updated_at) DESC,m.id DESC"}[sort]
    with store.read() as conn:
        if person_id and conn.execute("SELECT 1 FROM subjects WHERE id=?", (person_id,)).fetchone():
            canonical = canonical_subject(conn, person_id)
            args[person_arg:person_arg + 2] = [canonical, canonical]
        total = conn.execute(f"SELECT COUNT(*) FROM memories m {join} WHERE {where}", [*prefix, *args]).fetchone()[0]
        items = memory_rows(conn, f"SELECT {MEMORY_COLUMNS} FROM memories m {join} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                            [*prefix, *args, limit, offset])
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def messages(conn, entry_id, *, before=None, limit=100):
    where, args = "m.entry_id=?", [entry_id]
    if before is not None:
        where += " AND m.id<?"
        args.append(before)
    return [dict(r) for r in reversed(conn.execute(f"""SELECT m.*,s.name AS sender_name
        FROM messages m JOIN subjects s ON s.id=m.sender_subject_id
        WHERE {where} ORDER BY m.id DESC LIMIT ?""", [*args, limit]).fetchall())]


def memory_detail(store, memory_id):
    with store.read() as conn:
        rows = memory_rows(conn, f"SELECT {MEMORY_COLUMNS} FROM memories m WHERE m.id=? AND m.purged_at IS NULL", (memory_id,))
        if not rows:
            raise KeyError(memory_id)
        detail = rows[0]
        from .memory_ops import Visibility
        detail["visibility"] = Visibility(conn).describe_memory(memory_id)
        from .consolidation import memory_annotations
        detail['consolidation_annotations'] = memory_annotations(conn,[memory_id],include_reports=True)[memory_id]
        detail["speaker"] = dict(conn.execute("SELECT id,name FROM subjects WHERE id=?", (detail["speaker_subject_id"],)).fetchone())
        detail["sources"] = []
        for row in conn.execute("SELECT * FROM sources WHERE memory_id=? ORDER BY id", (memory_id,)):
            source = dict(row)
            if source["message_id"]:
                message = conn.execute("""SELECT m.*,s.name AS sender_name,e.name AS entry_name FROM messages m
                    JOIN subjects s ON s.id=m.sender_subject_id JOIN entries e ON e.id=m.entry_id WHERE m.id=?""", (source["message_id"],)).fetchone()
                source["message"] = dict(message) if message else None
                if message:
                    before = messages(conn, message["entry_id"], before=message["id"], limit=2)
                    after = [dict(r) for r in conn.execute("""SELECT m.*,s.name AS sender_name FROM messages m
                        JOIN subjects s ON s.id=m.sender_subject_id WHERE m.entry_id=? AND m.id>? ORDER BY m.id LIMIT 2""",
                        (message["entry_id"], message["id"]))]
                    source["context"] = [*before, dict(message), *after]
            if source["source_memory_id"]:
                parent = conn.execute("SELECT id,content,lifecycle,revision FROM memories WHERE id=?", (source["source_memory_id"],)).fetchone()
                source["memory"] = dict(parent) if parent else None
                source["needs_review"] = not parent or parent["lifecycle"] == "deleted" or parent["revision"] != source["source_revision"]
            detail["sources"].append(source)
        detail["derived_memories"] = [dict(r) for r in conn.execute("""SELECT m.id,m.content,m.lifecycle,s.source_revision,
            s.source_revision!=? AS needs_review FROM sources s JOIN memories m ON m.id=s.memory_id
            WHERE s.source_memory_id=? ORDER BY m.id""", (detail["revision"], memory_id))]
        detail["revisions"] = []
        detail["operations"] = []
        for row in conn.execute("SELECT * FROM memory_revisions WHERE memory_id=? ORDER BY id DESC", (memory_id,)):
            revision = dict(row)
            revision["before"] = json.loads(revision.pop("before_json"))
            revision["after"] = json.loads(revision.pop("after_json"))
            detail["revisions"].append(revision)
            if revision["actor"] == "admin":
                detail["operations"].append({k: revision[k] for k in ("id", "actor", "reason", "created_at", "revision_before", "revision_after")} | {
                    "action": "delete" if revision["after"].get("lifecycle") == "deleted" else "edit"})
        counts = conn.execute("SELECT COUNT(*),COUNT(used_at) FROM recall_items WHERE memory_id=?", (memory_id,)).fetchone()
        detail.update(recall_count=counts[0], used_count=counts[1])
        return detail


def trial_snapshot(store, entry_id, *, before=None):
    from .trial import require_entry
    with store.read() as conn:
        entry = require_entry(conn, entry_id)
        recent = messages(conn, entry_id, before=before)
        persona = conn.execute("SELECT id AS version,content,created_at AS generated_at FROM persona_versions WHERE is_current=1 ORDER BY id DESC LIMIT 1").fetchone()
        return {"entry": entry, "messages": recent,
                "has_older": bool(recent and conn.execute("SELECT 1 FROM messages WHERE entry_id=? AND id<? LIMIT 1", (entry_id, recent[0]["id"])).fetchone()),
                "recent_memories": memory_rows(conn, f"""SELECT {MEMORY_COLUMNS} FROM memories m WHERE m.lifecycle='active' AND
                    (m.entry_id=? OR EXISTS (SELECT 1 FROM sources s JOIN messages msg ON msg.id=s.message_id
                        WHERE s.memory_id=m.id AND msg.entry_id=?))
                    ORDER BY m.updated_at DESC,m.id DESC LIMIT 12""", (entry_id, entry_id)),
                "persona": dict(persona) if persona else {"version": None, "content": "", "generated_at": None},
                "state": {}, "goals": Retrieval._goals(conn, entry_id, 10)}


BATCH_COLUMNS = """b.id,b.entry_id,b.state,b.attempt_count,b.next_retry_at,b.last_error,
    b.prompt_version,b.created_at,b.finished_at,b.result_json,
    EXISTS(SELECT 1 FROM json_each(b.target_ids) j LEFT JOIN messages m ON m.id=j.value
        WHERE m.id IS NULL) AS targets_missing"""
CALL_COLUMNS = """id,purpose,model,duration_ms,result_category,error_summary,status_code,
    created_at,finish_reason,reasoning_effort,timed_out"""


def batch_summary(row):
    item = dict(row)
    result = json.loads(item.pop("result_json"))
    item["result"] = {key: result.get(key, []) for key in ("created", "updated", "confirmed")}
    # reset_batch retains the previous terminal timestamp; it isn't the end of
    # this new run. Attempts and the gap still retain the historical timestamps.
    if item["state"] in ("waiting", "running"):
        item["finished_at"] = None
    cleared = bool(item.pop("targets_missing", False))
    item["can_relearn"] = item["state"] in ("abandoned", "refused") and not cleared
    item["relearn_blocked_reason"] = "目标段消息已清理，无法重新学习" if cleared else None
    return item


def entry_learning_settings(store, entry_id):
    with store.read() as conn:
        entry = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if entry is None:
            raise KeyError(entry_id)
        from .memory_ops import entry_visibility
        return {**entry_settings(entry), **entry_visibility(entry)}


def entry_queue_wait(conn, entry, current):
    filters = json.loads(entry["filters_json"])
    counts = conn.execute("""SELECT
        COALESCE(SUM(learning_state='pending'),0),
        COALESCE(SUM(learning_state='pending' AND a.decided=0),0),
        COALESCE(SUM(learning_state='filtered'),0)
        FROM messages m LEFT JOIN message_admission a ON a.message_id=m.id
        WHERE m.entry_id=?""", (entry["id"],)).fetchone()
    undecided = counts[1] if filters["min_chars"] or filters["mention_only"] else 0
    rate = batch_rate_status(conn, entry["id"], filters["max_batches_per_hour"], current)
    active = conn.execute("SELECT 1 FROM batches WHERE entry_id=? AND state IN ('waiting','running') LIMIT 1", (entry["id"],)).fetchone()
    reason = None
    if not active:
        if counts[0] > undecided and rate["retry_at"]:
            reason = "hourly_batch_limit"
        elif undecided and counts[0] == undecided:
            reason = "filter_context"
    return {**rate, "reason": reason, "retry_at": rate["retry_at"] if reason == "hourly_batch_limit" else None,
            "filter_waiting_count": undecided, "filtered_count": counts[2]}


def add_entry_waits(store, items):
    """Enrich management status without changing host/retrieval diagnostics."""
    current = utc_now()
    with store.read() as conn:
        for item in items:
            entry = conn.execute("SELECT * FROM entries WHERE id=?", (item["entry_id"],)).fetchone()
            item["queue_wait"] = entry_queue_wait(conn, entry, current)


def learning_entries(store):
    instant = utc_now()
    with store.read() as conn:
        items = [dict(r) for r in conn.execute("""SELECT e.*,
            (SELECT COUNT(*) FROM messages m WHERE m.entry_id=e.id
                AND m.learning_state IN ('pending','batched')) AS pending_count
            FROM entries e ORDER BY e.name,e.id""")]
        for entry in items:
            entry["filters"] = json.loads(entry["filters_json"])
            entry["queue_wait"] = entry_queue_wait(conn, entry, instant)
            entry.pop("filters_json")
            from .memory_ops import entry_visibility
            entry.update(entry_visibility(entry))
            entry.pop("visible_in_json")
            latest = conn.execute(f"SELECT {BATCH_COLUMNS} FROM batches b WHERE b.entry_id=? ORDER BY b.id DESC LIMIT 1", (entry["id"],)).fetchone()
            current = conn.execute(f"""SELECT {BATCH_COLUMNS} FROM batches b WHERE b.entry_id=?
                AND b.state IN ('waiting','running') ORDER BY b.id LIMIT 1""", (entry["id"],)).fetchone()
            entry["latest_batch"] = batch_summary(latest) if latest else None
            entry["current_batch"] = batch_summary(current) if current else None
        return {"items": items}


def learning_filter(store, *, entry_id, time_from, time_to, entry_column, start_column, end_column):
    clauses, args, bounds = [], [], {}
    zone = ZoneInfo(str(store.setting("timezone", "Asia/Shanghai")))
    if entry_id:
        clauses.append(f"{entry_column}=?")
        args.append(entry_id)
    for value, column, operator in ((time_from, end_column, ">="), (time_to, start_column, "<=")):
        if not value:
            continue
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=zone)
        if operator == "<=" and len(value) == 10:
            try:
                stamp += timedelta(days=1)
            except OverflowError as error:
                raise ValueError("结束日期超出支持范围") from error
            operator = "<"
        bounds[operator] = stamp
        clauses.append(f"julianday({column}){operator}julianday(?)")
        args.append(stamp.isoformat())
    since, until = bounds.get(">="), bounds.get("<=", bounds.get("<"))
    if since and until and (since > until or ("<" in bounds and since == until)):
        raise ValueError("开始时间不能晚于结束时间")
    return " AND ".join(clauses) or "1", args


def learning_batches(store, *, entry_id=None, time_from=None, time_to=None, limit=30, offset=0):
    where, args = learning_filter(store, entry_id=entry_id, time_from=time_from, time_to=time_to,
                                  entry_column="b.entry_id", start_column="b.created_at", end_column="b.created_at")
    with store.read() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM batches b WHERE {where}", args).fetchone()[0]
        items = [batch_summary(r) for r in conn.execute(f"""SELECT {BATCH_COLUMNS},e.name AS entry_name,
            EXISTS(SELECT 1 FROM memory_gaps g WHERE g.batch_id=b.id) AS has_gap
            FROM batches b JOIN entries e ON e.id=b.entry_id WHERE {where}
            ORDER BY b.id DESC LIMIT ? OFFSET ?""", [*args, limit, offset])]
        for item in items:
            item["relearning"] = bool(item.pop("has_gap") and item["state"] in ("waiting", "running"))
        entries = conn.execute("SELECT * FROM entries" + (" WHERE id=?" if entry_id else "") + " ORDER BY id",
                               (entry_id,) if entry_id else ()).fetchall()
        instant = utc_now()
        waits = [{"entry_id": e["id"], "queue_wait": entry_queue_wait(conn, e, instant)} for e in entries]
    return {"items": items, "total": total, "limit": limit, "offset": offset, "entry_waits": waits}


def batch_detail(store, batch_id):
    with store.read() as conn:
        row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise KeyError(batch_id)
        detail = batch_summary(dict(row) | {"targets_missing": bool(missing_batch_targets(conn, batch_id))})
        detail["result"] = json.loads(row["result_json"])
        snapshot = detail.pop("entry_settings_json")
        detail["entry_settings"] = json.loads(snapshot) if snapshot else None
        detail["entry"] = dict(conn.execute("SELECT id,name,platform,kind,pace FROM entries WHERE id=?", (row["entry_id"],)).fetchone())
        detail["segments"] = {}
        for segment, column in (("history", "history_ids"), ("target", "target_ids"), ("future", "future_ids")):
            ids = json.loads(detail.pop(column))
            rows = conn.execute(f"""SELECT m.*,s.name AS sender_name,q.name AS quote_author_name FROM messages m
                JOIN subjects s ON s.id=m.sender_subject_id LEFT JOIN subjects q ON q.id=m.quote_author_subject_id
                WHERE m.id IN ({','.join('?' for _ in ids)})""", ids).fetchall() if ids else []
            by_id = {r["id"]: dict(r) for r in rows}
            detail["segments"][segment] = [by_id.get(i, {"id": i, "missing": True, "content": "已清理"}) for i in ids]
        attempts = [dict(r) for r in conn.execute("SELECT * FROM batch_attempts WHERE batch_id=? ORDER BY number", (batch_id,))]
        # The gateway stores only message.content in raw/repair_output. Project a
        # whitelist of call diagnostics, never provider envelopes or reasoning.
        calls = [dict(r) for r in conn.execute(f"""SELECT {CALL_COLUMNS} FROM model_calls
            WHERE batch_id=? AND purpose IN ('learning','learning_repair') ORDER BY id""", (batch_id,))]
        for attempt in attempts:
            attempt["calls"] = []
        unassigned = []
        for call in calls:
            stamp = datetime.fromisoformat(call["created_at"])
            matches = [a for a in attempts if datetime.fromisoformat(a["started_at"]) <= stamp <= datetime.fromisoformat(a["finished_at"])]
            if len(matches) == 1:
                matches[0]["calls"].append(call)
            else:
                unassigned.append(call)
        detail["attempts"], detail["unassigned_calls"] = attempts, unassigned
        detail["memories"] = []
        for change in ("created", "updated", "confirmed"):
            for mid in detail["result"].get(change, []):
                memory = conn.execute("SELECT id,content,lifecycle,revision FROM memories WHERE id=?", (mid,)).fetchone()
                detail["memories"].append({**(dict(memory) if memory else {"id": mid, "missing": True}), "change": change})
        gap = conn.execute("SELECT batch_id,entry_id,started_at,ended_at,reason,created_at FROM memory_gaps WHERE batch_id=?", (batch_id,)).fetchone()
        detail["gap"] = dict(gap) if gap else None
        detail["relearning"] = bool(gap and row["state"] in ("waiting", "running"))
    return detail


def memory_gaps(store, *, entry_id=None, time_from=None, time_to=None, limit=30, offset=0):
    where, args = learning_filter(store, entry_id=entry_id, time_from=time_from, time_to=time_to,
                                  entry_column="g.entry_id", start_column="g.started_at", end_column="g.ended_at")
    with store.read() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM memory_gaps g WHERE {where}", args).fetchone()[0]
        items = [dict(r) for r in conn.execute(f"""SELECT g.batch_id,g.entry_id,g.started_at,g.ended_at,g.reason,g.created_at,
            e.name AS entry_name,b.state AS batch_state
            FROM memory_gaps g JOIN entries e ON e.id=g.entry_id JOIN batches b ON b.id=g.batch_id
            WHERE {where} ORDER BY julianday(g.started_at) DESC,g.batch_id DESC LIMIT ? OFFSET ?""", [*args, limit, offset])]
        for item in items:
            item["relearning"] = item["batch_state"] in ("waiting", "running")
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def upcoming_deletion(store, *, limit=30, offset=0, current=None):
    current = current or utc_now()
    with store.read() as conn:
        config = lifecycle_settings(conn)
        if not config["auto_delete_enabled"]:
            return {"items": [], "total": 0, "limit": limit, "offset": offset, "enabled": False}
        # Include overdue objects: a stopped service may not have removed them yet.
        due = current+timedelta(days=config["upcoming_delete_days"]-config["auto_delete_days"])
        where = "m.lifecycle='forgotten' AND m.pinned=0 AND julianday(m.forgotten_at)<=julianday(?)"
        total = conn.execute(f"SELECT COUNT(*) FROM memories m WHERE {where}", (due.isoformat(),)).fetchone()[0]
        items = memory_rows(conn, f"SELECT {MEMORY_COLUMNS} FROM memories m WHERE {where} ORDER BY julianday(m.forgotten_at),m.id LIMIT ? OFFSET ?",
                            (due.isoformat(), limit, offset))
        for item in items:
            item["delete_after"] = (datetime.fromisoformat(item["forgotten_at"])+timedelta(days=config["auto_delete_days"])).isoformat()
        return {"items": items, "total": total, "limit": limit, "offset": offset, "enabled": True}


def operations(store, *, action=None, actor=None, object_type=None, object_id=None,
               time_from=None, time_to=None, limit=30, offset=0):
    where, args = learning_filter(store, entry_id=None, time_from=time_from, time_to=time_to,
                                  entry_column="o.object_id", start_column="o.created_at", end_column="o.created_at")
    for key, value in (("action", action), ("actor", actor), ("object_type", object_type), ("object_id", object_id)):
        if value is not None:
            where += f" AND o.{key}=?"
            args.append(value)
    with store.read() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM admin_operations o WHERE {where}", args).fetchone()[0]
        items = [dict(r) for r in conn.execute(f"SELECT o.* FROM admin_operations o WHERE {where} ORDER BY julianday(o.created_at) DESC,o.id DESC LIMIT ? OFFSET ?", [*args, limit, offset])]
        for item in items:
            item["details"] = json.loads(item.pop("details_json"))
        return {"items": items, "total": total, "limit": limit, "offset": offset}


def maintenance_runs(store, *, limit=30, offset=0):
    with store.read() as conn:
        total = conn.execute("SELECT COUNT(*) FROM maintenance_runs").fetchone()[0]
        items = [dict(r) for r in conn.execute("SELECT id,trigger,state,phase,created_at,finished_at,summary_json FROM maintenance_runs ORDER BY id DESC LIMIT ? OFFSET ?", (limit,offset))]
        for item in items:
            item["summary"] = json.loads(item.pop("summary_json"))
        return {"items": items, "total": total, "limit": limit, "offset": offset}


def state_reports(store, *, limit=30, offset=0):
    with store.read() as conn:
        return report_history(conn, limit=limit, offset=offset)


def goals(store, **filters):
    with store.read() as conn:
        return goal_list(conn, **filters)


def _goal_source_context(conn, message_id):
    message = conn.execute("""SELECT m.*,s.name AS sender_name,e.name AS entry_name FROM messages m
        JOIN subjects s ON s.id=m.sender_subject_id JOIN entries e ON e.id=m.entry_id WHERE m.id=?""",(message_id,)).fetchone()
    result = {'id':message_id,'message_id':message_id,'message':dict(message) if message else None,
              'missing':message is None,'notice':'来源消息已清理或不可用' if message is None else None,'context':[]}
    if message:
        before = messages(conn,message['entry_id'],before=message_id,limit=2)
        after = [dict(r) for r in conn.execute("""SELECT m.*,s.name AS sender_name FROM messages m
            JOIN subjects s ON s.id=m.sender_subject_id WHERE m.entry_id=? AND m.id>? ORDER BY m.id LIMIT 2""",
            (message['entry_id'],message_id))]
        result['context'] = [*before,dict(message),*after]
    return result


def goal_sources(store, goal_id, *, limit=30, offset=0):
    with store.read() as conn:
        if not conn.execute('SELECT 1 FROM goals WHERE id=?',(goal_id,)).fetchone():
            raise KeyError(goal_id)
        total = conn.execute('SELECT COUNT(*) FROM goal_sources WHERE goal_id=?',(goal_id,)).fetchone()[0]
        ids = [r[0] for r in conn.execute('SELECT message_id FROM goal_sources WHERE goal_id=? ORDER BY message_id LIMIT ? OFFSET ?',
                                        (goal_id,limit,offset))]
        return {'items':[_goal_source_context(conn,mid) for mid in ids],'total':total,'limit':limit,'offset':offset}


def goal_revisions(store, goal_id, **page):
    from .goals import goal_revisions as project_revisions
    with store.read() as conn:
        return project_revisions(conn,goal_id,**page)


def goal(store, goal_id, *, current=None):
    with store.read() as conn:
        detail = goal_detail(conn, goal_id, current=current)
        for source in detail['sources']:
            source.update(_goal_source_context(conn,source['id']))
        return detail


def notifications(store, **filters):
    with store.read() as conn:
        return notification_list(conn, **filters)


PERSONA_COLUMNS = """id,content,is_current,status,source,change_degree,created_at AS generated_at,
    published_at,base_version_id,rollback_of"""
PERSONA_ATTEMPT_COLUMNS = """id,source,base_version_id,state,reason,version_id,created_at,finished_at,
    CASE WHEN state='queued' THEN 'queued'
         WHEN state!='running' THEN 'finished'
         WHEN json_type(outputs_json,'$.persona_generate') IS NOT NULL THEN 'checking'
         ELSE 'generating' END AS stage"""


def _persona_summary(row):
    item = dict(row)
    item['is_current'] = bool(item['is_current'])
    return item


def _persona_memory(conn, memory_id):
    row = conn.execute('''SELECT id,content,revision,lifecycle,speaker_subject_id,stance FROM memories
        WHERE id=? AND purged_at IS NULL''', (memory_id,)).fetchone()
    return dict(row) if row else None


def _persona_detail(conn, version_id):
    row = conn.execute(f'''SELECT {PERSONA_COLUMNS},sentences_json,checks_json,material_json
        FROM persona_versions WHERE id=?''', (version_id,)).fetchone()
    if row is None:
        raise KeyError(version_id)
    result = _persona_summary(row)
    result['sentences'] = json.loads(result.pop('sentences_json'))
    result['checks'] = json.loads(result.pop('checks_json'))
    material = json.loads(result.pop('material_json'))
    result['settings'] = material.get('settings')
    evidence = {m['memory_id']: m for m in material.get('evidence', {}).get('memories', [])}
    for sentence in result['sentences']:
        sentence['admin_written'] = sentence['origin'] == 'admin'
        for basis in sentence['basis']:
            mid, revision = basis['memory_id'], basis['revision']
            memory = _persona_memory(conn, mid)
            basis['memory'] = memory
            cited = evidence.get(mid)
            content = cited['content'] if cited and cited['revision'] == revision else None
            if content is None and memory is not None:
                if memory['revision'] == revision:
                    content = memory['content']
                else:
                    history = conn.execute('''SELECT before_json,after_json,revision_before FROM memory_revisions
                        WHERE memory_id=? AND (revision_before=? OR revision_after=?) ORDER BY id LIMIT 1''',
                        (mid, revision, revision)).fetchone()
                    if history:
                        content = json.loads(history['before_json' if history['revision_before'] == revision else 'after_json']).get('content')
            basis['content_at_revision'] = content
    checks = result['checks']
    reasons = [*checks.get('deterministic', {}).get('errors', []), *checks.get('model_errors', [])]
    if checks.get('administrator_rejection'):
        reasons.append(checks['administrator_rejection'])
    result['rejection_reasons'] = list(dict.fromkeys(reasons))
    return result


def persona_version(store, version_id):
    with store.read() as conn:
        return _persona_detail(conn, version_id)


def persona_versions(store, *, status=None, source=None, limit=30, offset=0):
    clauses, args = [], []
    for key, value in (('status', status), ('source', source)):
        if value is not None:
            clauses.append(f'{key}=?')
            args.append(value)
    where = ' AND '.join(clauses) or '1'
    with store.read() as conn:
        total = conn.execute(f'SELECT COUNT(*) FROM persona_versions WHERE {where}', args).fetchone()[0]
        items = [_persona_summary(r) for r in conn.execute(f'''SELECT {PERSONA_COLUMNS} FROM persona_versions
            WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?''', [*args, limit, offset])]
    return {'items': items, 'total': total, 'limit': limit, 'offset': offset}


def persona_snapshot(store):
    from .persona import persona_context
    with store.read() as conn:
        context = persona_context(conn, include_basis=True)
        current = conn.execute(f'SELECT {PERSONA_COLUMNS} FROM persona_versions WHERE is_current=1').fetchone()
        pending = conn.execute(f"SELECT {PERSONA_COLUMNS} FROM persona_versions WHERE status='pending'").fetchone()
        latest = conn.execute(f'SELECT {PERSONA_ATTEMPT_COLUMNS} FROM persona_attempts ORDER BY id DESC LIMIT 1').fetchone()
        for basis in context['stale_basis']:
            basis['memory'] = _persona_memory(conn, basis['memory_id'])
        return {'current': _persona_summary(current) if current else None,
                'pending': _persona_summary(pending) if pending else None,
                'needs_update': context['needs_update'], 'stale_basis_count': context['stale_basis_count'],
                'stale_basis': context['stale_basis'], 'latest_attempt': dict(latest) if latest else None}


def persona_attempt(store, attempt_id):
    with store.read() as conn:
        row = conn.execute(f'SELECT {PERSONA_ATTEMPT_COLUMNS} FROM persona_attempts WHERE id=?', (attempt_id,)).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return dict(row)


def persona_attempts(store, *, limit=30, offset=0):
    with store.read() as conn:
        total = conn.execute('SELECT COUNT(*) FROM persona_attempts').fetchone()[0]
        items = [dict(r) for r in conn.execute(f'''SELECT {PERSONA_ATTEMPT_COLUMNS} FROM persona_attempts
            ORDER BY id DESC LIMIT ? OFFSET ?''', (limit, offset))]
    return {'items': items, 'total': total, 'limit': limit, 'offset': offset}


def persona_self_memories(store, *, limit=30, offset=0):
    where = """m.lifecycle='active' AND m.purged_at IS NULL
        AND (m.speaker_subject_id='self' OR m.stance='设定')
        AND EXISTS(SELECT 1 FROM memory_subjects ms WHERE ms.memory_id=m.id AND ms.subject_id='self')"""
    with store.read() as conn:
        total = conn.execute(f'SELECT COUNT(*) FROM memories m WHERE {where}').fetchone()[0]
        items = memory_rows(conn, f'''SELECT {MEMORY_COLUMNS} FROM memories m WHERE {where}
            ORDER BY m.pinned DESC,m.importance DESC,m.retention DESC,m.id LIMIT ? OFFSET ?''', (limit, offset))
    return {'items': items, 'total': total, 'limit': limit, 'offset': offset}


def host_tokens(store):
    """Never project credential hashes or salts into management responses."""
    from .tokens import list_tokens, DEFAULT_LIMITS
    with store.read() as conn:
        items = list_tokens(conn)
    return {'items': items, 'rate_limits': store.setting('host_tokens', DEFAULT_LIMITS)}
