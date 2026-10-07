"""Read projections for the local management UI; browsing is not a recall."""
from __future__ import annotations

import json

from .retrieval import Retrieval
from .search_text import segmented

MEMORY_COLUMNS = """m.id,m.content,m.kind,m.speaker_subject_id,m.stance,m.belief,m.importance,
    m.retention,m.event_time,m.lifecycle,m.revision,m.entry_id,m.world,m.created_at,m.updated_at"""


def memory_rows(conn, sql, args=()):
    rows = [dict(r) for r in conn.execute(sql, args)]
    for row in rows:
        row["about"] = [dict(r) for r in conn.execute("""SELECT s.id,s.name FROM memory_subjects ms
            JOIN subjects s ON s.id=ms.subject_id WHERE ms.memory_id=? ORDER BY s.id""", (row["id"],))]
        row["tags"] = [r[0] for r in conn.execute("SELECT tag FROM memory_tags WHERE memory_id=? ORDER BY tag", (row["id"],))]
    return rows


def catalog(store):
    with store.read() as conn:
        return {"people": [dict(r) for r in conn.execute("SELECT id,name,kind FROM subjects ORDER BY name,id")],
                "entries": [dict(r) for r in conn.execute("SELECT id,name,kind FROM entries ORDER BY name,id")]}


def list_memories(store, *, text="", person_id=None, kind=None, entry_id=None,
                  time_from=None, time_to=None, lifecycle="active", sort="time", limit=30, offset=0):
    clauses, args = [], []
    if lifecycle != "all":
        clauses.append("m.lifecycle=?")
        args.append(lifecycle)
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
        rows = memory_rows(conn, f"SELECT {MEMORY_COLUMNS} FROM memories m WHERE m.id=?", (memory_id,))
        if not rows:
            raise KeyError(memory_id)
        detail = rows[0]
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
