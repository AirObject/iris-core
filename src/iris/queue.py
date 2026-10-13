"""Message intake, independent entry queues, and frozen three-part batches."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from bisect import bisect_left, bisect_right
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Store, dumps, now
from .memory_ops import operation
from .people import canonical_subject


PACE = {
    "realtime": (4, timedelta(seconds=5), timedelta(minutes=1)),
    "standard": (12, timedelta(minutes=10), timedelta(minutes=60)),
    "economy": (30, timedelta(minutes=30), timedelta(hours=4)),
}
FOCUS = re.compile(r"@|记住|别忘了")
FILTER_DEFAULTS = {"min_chars": 0, "mention_only": False, "context_messages": 0,
                   "max_batches_per_hour": 0}
FILTER_LIMITS = {"min_chars": 32768, "context_messages": 100, "max_batches_per_hour": 1000}


def pace_parameters(pace):
    if isinstance(pace, str):
        if pace in PACE:
            return PACE[pace]
        try:
            pace = json.loads(pace)
        except ValueError as error:
            raise ValueError("invalid learning pace") from error
    limits = {"count": 1000, "idle_seconds": 86400, "max_wait_seconds": 604800}
    if not isinstance(pace, dict) or set(pace) != set(limits) or any(
            type(pace[key]) is not int or not 1 <= pace[key] <= limit for key, limit in limits.items()):
        raise ValueError("custom pace needs count (1..1000), idle_seconds (1..86400), max_wait_seconds (1..604800)")
    return pace["count"], timedelta(seconds=pace["idle_seconds"]), timedelta(seconds=pace["max_wait_seconds"])


def filter_parameters(filters):
    if not isinstance(filters, dict) or set(filters) != set(FILTER_DEFAULTS):
        raise ValueError("invalid entry filters")
    if type(filters["mention_only"]) is not bool or any(
            type(filters[key]) is not int or not 0 <= filters[key] <= limit
            for key, limit in FILTER_LIMITS.items()):
        raise ValueError("invalid entry filter value")
    return dict(filters)


def entry_settings(entry):
    pace = entry["pace"]
    return {"pace": pace if pace in PACE else json.loads(pace),
            "filters": json.loads(entry["filters_json"])}


def update_entry_settings(store: Store, entry_id: str, *, pace=None, filters=None):
    """Apply validated partial settings and audit in the same write transaction."""
    with store.write() as conn:
        entry = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if entry is None:
            raise KeyError(entry_id)
        before = entry_settings(entry)
        after = {"pace": before["pace"] if pace is None else pace,
                 "filters": before["filters"] if filters is None else {**before["filters"], **filters}}
        pace_parameters(after["pace"])
        filter_parameters(after["filters"])
        stored_pace = dumps(after["pace"]) if isinstance(after["pace"], dict) else after["pace"]
        conn.execute("UPDATE entries SET pace=?,filters_json=? WHERE id=?",
                     (stored_pace, dumps(after["filters"]), entry_id))
        # Final exclusions stay final. Any admission already used in a frozen
        # segment also stays fixed, including a pending future-segment message.
        if before["filters"] != after["filters"]:
            conn.execute("""UPDATE message_admission SET decided=0 WHERE message_id IN (
                SELECT m.id FROM messages m WHERE m.entry_id=? AND m.learning_state='pending'
                AND NOT EXISTS (SELECT 1 FROM batch_message_refs r WHERE r.message_id=m.id))""", (entry_id,))
        entry = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        _resolve_filters(conn, entry, datetime.fromisoformat(now()))
        operation(conn, "entry_settings_updated", "entry", entry_id,
                  {"before": before, "after": after})
        return after


def _resolve_filters(conn, entry, current, *, settle_idle=True):
    """Decide each unfrozen input once, with K raw messages of lookahead.

    The undecided suffix stays pending so the existing scheduler's idle timer
    can revisit it. It is never used as frozen target or context. Intake usually
    reads only that suffix and its K predecessors, not the admitted backlog.
    """
    filters = json.loads(entry["filters_json"])
    if not (filters["min_chars"] or filters["mention_only"]):
        return
    first = conn.execute("""SELECT MIN(message_id) FROM message_admission
        WHERE entry_id=? AND decided=0""", (entry["id"],)).fetchone()[0]
    if first is None:
        return
    k = filters["context_messages"] if filters["mention_only"] else 0
    # All kinds and short/previously filtered messages count as raw positions.
    first_position = conn.execute("SELECT position FROM message_admission WHERE message_id=?", (first,)).fetchone()[0]
    rows = conn.execute("""SELECT m.id,m.content,m.received_at,m.learning_state,a.position,a.decided
        FROM message_admission a JOIN messages m ON m.id=a.message_id
        WHERE a.entry_id=? AND a.position>=? ORDER BY a.position""", (entry["id"], first_position-k)).fetchall()
    positions = [r["position"] for r in rows]
    role_row = conn.execute("SELECT value_json FROM runtime_settings WHERE key='role_name'").fetchone()
    role_name = str(json.loads(role_row[0])) if role_row else "Iris"
    names = {role_name} | {r[0] for r in conn.execute("SELECT alias FROM subject_aliases WHERE subject_id='self'")}
    names.discard("")
    hits = [0]
    for row in rows:
        hits.append(hits[-1] + int(any(name in row["content"] for name in names)))
    _, idle, _ = pace_parameters(entry["pace"])
    # Reuse M1's attention semantics. A bare @ remains an attention signal,
    # but only a role name/confirmed self alias is a mention-filter anchor.
    settled_through = entry["message_sequence"] - k
    if settle_idle and k:
        focus = conn.execute("""SELECT content FROM messages WHERE entry_id=?
            AND learning_state='pending'""", (entry["id"],))
        if any(FOCUS.search(r[0]) or role_name in r[0] for r in focus):
            idle = min(idle, timedelta(minutes=1))
        latest = max(datetime.fromisoformat(r["received_at"]) for r in rows)
        if current - latest >= idle:
            settled_through = entry["message_sequence"]
    changes = []
    for row in rows:
        if row["learning_state"] != "pending" or row["decided"]:
            continue
        reason = None
        if len(row["content"].strip()) < filters["min_chars"]:
            reason = "short_message"
        elif row["position"] > settled_through:
            continue
        elif filters["mention_only"] and hits[bisect_right(positions, row["position"]+k)] == hits[bisect_left(positions, row["position"]-k)]:
            reason = "outside_mention_window"
        changes.append(("filtered" if reason else "pending", reason, row["id"]))
    conn.executemany("UPDATE messages SET learning_state=? WHERE id=?", ((state, mid) for state, _, mid in changes))
    conn.executemany("UPDATE message_admission SET reason=?,decided=1 WHERE message_id=?", ((reason, mid) for _, reason, mid in changes))
    through = entry["learn_requested_through"]
    if through is not None and conn.execute("SELECT 1 FROM messages WHERE id=? AND learning_state='filtered'", (through,)).fetchone():
        # A manual watermark must not retain a final exclusion forever. Move
        # it to the remaining requested work, or clear it when there is none.
        conn.execute("""UPDATE entries SET learn_requested_through=(SELECT MAX(id) FROM messages
            WHERE entry_id=? AND id<=? AND learning_state IN ('pending','batched')) WHERE id=?""",
            (entry["id"], through, entry["id"]))


def batch_rate_status(conn, entry_id, limit, current):
    if not limit:
        return {"limit": 0, "batches_last_hour": None, "retry_at": None}
    # Count new frozen batches, independently of result/attempts. Rolling time
    # avoids a double burst at a wall-clock hour boundary. A backwards clock
    # conservatively continues counting already-created batches.
    args = (entry_id, (current - timedelta(hours=1)).isoformat())
    where = "entry_id=? AND parent_batch_id IS NULL AND julianday(created_at)>julianday(?)"
    count = conn.execute("SELECT COUNT(*) FROM batches WHERE " + where, args).fetchone()[0]
    retry_at = None
    if count >= limit:
        row = conn.execute("SELECT created_at FROM batches WHERE " + where +
                           " ORDER BY julianday(created_at) DESC,id DESC LIMIT 1 OFFSET ?", (*args, limit-1)).fetchone()
        retry_at = (datetime.fromisoformat(row[0]) + timedelta(hours=1)).isoformat()
    return {"limit": limit, "batches_last_hour": count, "retry_at": retry_at}


@dataclass(frozen=True)
class Batch:
    id: int
    entry_id: str
    history_ids: list[int]
    target_ids: list[int]
    future_ids: list[int]
    state: str
    attempt_count: int
    prompt_version: str
    parent_batch_id: int | None = None
    root_batch_id: int | None = None
    split_slot: int | None = None
    split_path: str = ""
    context_mode: str = "full"


def estimate_tokens(text: str) -> int:
    # Conservative for mixed Chinese and Latin text; exact tokenizer is provider-specific.
    han = sum("\u4e00" <= ch <= "\u9fff" for ch in text)
    return max(1, han + (len(text) - han + 2) // 3)


def truncate_material(text: str, limit: int = 1500) -> str:
    if estimate_tokens(text) <= limit:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + " [已截断；原文仍完整保存]"


def should_learn(pending_count: int, oldest_at: datetime | None, latest_at: datetime | None,
                 current: datetime, pace: str = "standard", focus: bool = False,
                 manual: bool = False, role_name: str | None = None,
                 latest_content: str = "") -> bool:
    if pending_count <= 0:
        return False
    count, idle, max_wait = pace_parameters(pace)
    if role_name and role_name in latest_content:
        focus = True
    focus = focus or bool(FOCUS.search(latest_content))
    if focus:
        idle = min(idle, timedelta(minutes=1))
    return bool(manual or pending_count >= count or
                (latest_at and current - latest_at >= idle) or
                (oldest_at and current - oldest_at >= max_wait))


def _subject(conn: sqlite3.Connection, platform: str, account_id: str, name: str) -> str:
    row = conn.execute("SELECT subject_id FROM platform_identities WHERE platform=? AND account_id=?", (platform, account_id)).fetchone()
    if row:
        return canonical_subject(conn, row[0])
    subject_id = uuid.uuid4().hex
    conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)", (subject_id, name, now()))
    conn.execute("INSERT INTO platform_identities(subject_id,platform,account_id,display_name) VALUES(?,?,?,?)",
                 (subject_id, platform, account_id, name))
    return subject_id


def _quote_subject(conn: sqlite3.Connection, *, entry_id: str, platform: str,
                   name: str | None, account_id: str | None,
                   current_sender_id: str | None) -> str | None:
    if not name and not account_id:
        return None
    if account_id:
        return _subject(conn, platform, account_id, name or account_id)
    participants = {canonical_subject(conn, row[0]) for row in conn.execute("""SELECT DISTINCT m.sender_subject_id
        FROM messages m JOIN subjects s ON s.id=m.sender_subject_id
        LEFT JOIN subject_aliases a ON a.subject_id=s.id AND a.folded_into IS NULL
        WHERE m.entry_id=? AND m.kind='message' AND (s.name=? OR (a.alias=? AND EXISTS(SELECT 1 FROM subjects retired WHERE retired.merged_into=s.id)))""", (entry_id, name, name))}
    if current_sender_id:
        current_sender_id = canonical_subject(conn, current_sender_id)
        if conn.execute("""SELECT 1 FROM subjects s LEFT JOIN subject_aliases a ON a.subject_id=s.id
            AND a.folded_into IS NULL WHERE s.id=? AND (s.name=? OR (a.alias=? AND EXISTS(SELECT 1 FROM subjects retired WHERE retired.merged_into=s.id)))""", (current_sender_id, name, name)).fetchone():
            participants.add(current_sender_id)
    if len(participants) == 1:
        return next(iter(participants))
    # A display name alone never binds this mention to a platform account.
    mentioned_id = uuid.uuid4().hex
    conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)",
                 (mentioned_id, name, now()))
    return mentioned_id


def add_message(store: Store, *, entry_id: str, entry_name: str, platform: str, entry_kind: str,
                kind: str, sender: str, content: str, occurred_at: str, dedupe_key: str,
                account_id: str | None = None, scene_identity: str | None = None,
                quote_author: str | None = None, quote_author_account_id: str | None = None,
                quote_content: str | None = None, media_ids: list[str] | None = None,
                pace: str | dict = "standard", _conn: sqlite3.Connection | None = None) -> int:
    if kind not in ("message", "self_output", "action_result", "event"):
        raise ValueError("invalid message type")
    if not entry_id or not dedupe_key:
        raise ValueError("entry, dedupe key, and pace are required")
    pace_parameters(pace)
    stored_pace = dumps(pace) if isinstance(pace, dict) else pace
    try:
        occurrence = datetime.fromisoformat(occurred_at)
    except ValueError as exc:
        raise ValueError("occurred_at must be an ISO datetime") from exc
    if occurrence.tzinfo is None:
        raise ValueError("occurred_at needs a timezone offset")
    if len(content.encode("utf-8")) > 32768:
        raise ValueError("message exceeds 32 KB")
    with (nullcontext(_conn) if _conn is not None else store.write()) as conn:
        conn.execute("INSERT OR IGNORE INTO entries(id,name,platform,kind,pace) VALUES(?,?,?,?,?)",
                     (entry_id, entry_name, platform, entry_kind, stored_pace))
        existing = conn.execute("SELECT id FROM messages WHERE entry_id=? AND dedupe_key=?", (entry_id, dedupe_key)).fetchone()
        if existing:
            return int(existing[0])
        sender_id = ("self" if kind in ("self_output", "action_result") else
                     "scene" if kind == "event" else _subject(conn, platform, account_id or sender, sender))
        quote_id = _quote_subject(conn, entry_id=entry_id, platform=platform,
                                  name=quote_author, account_id=quote_author_account_id,
                                  current_sender_id=sender_id if kind == "message" else None)
        position = conn.execute("UPDATE entries SET message_sequence=message_sequence+1 WHERE id=? RETURNING message_sequence",
                                (entry_id,)).fetchone()[0]
        result = conn.execute("""INSERT INTO messages
            (entry_id,kind,sender_subject_id,scene_identity,content,quote_author_subject_id,quote_content,
             occurred_at,received_at,dedupe_key)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (entry_id, kind, sender_id, scene_identity, content, quote_id, quote_content,
             occurred_at, now(), dedupe_key))
        if media_ids is not None:
            from .media import attach_media
            attach_media(store, conn, int(result.lastrowid), media_ids)
        conn.execute("INSERT INTO message_admission(message_id,entry_id,position) VALUES(?,?,?)",
                     (result.lastrowid, entry_id, position))
        conn.execute("UPDATE entries SET last_message_at=? WHERE id=?", (occurred_at, entry_id))
        entry = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        _resolve_filters(conn, entry, datetime.fromisoformat(now()), settle_idle=False)
        return int(result.lastrowid)


def get_batch(store: Store, batch_id: int) -> Batch:
    with store.read() as conn:
        row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if row is None:
        raise KeyError(batch_id)
    return Batch(row["id"], row["entry_id"], json.loads(row["history_ids"]),
                 json.loads(row["target_ids"]), json.loads(row["future_ids"]),
                 row["state"], row["attempt_count"], row["prompt_version"],
                 row["parent_batch_id"], row["root_batch_id"], row["split_slot"],
                 row["split_path"], row["context_mode"])


def form_batch(store: Store, entry_id: str, prompt_version: str, *, target_count: int | None = None,
               token_limit: int = 4000, history_count: int = 4, future_count: int = 4) -> Batch | None:
    with store.write() as conn:
        active = next_learning_batch(conn, entry_id)
        if active:
            return None
        entry = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if entry is None:
            return None
        current = datetime.fromisoformat(now())
        _resolve_filters(conn, entry, current)
        settings = entry_settings(entry)
        filters = settings["filters"]
        decided = " AND EXISTS(SELECT 1 FROM message_admission a WHERE a.message_id=messages.id AND a.decided=1)" if filters["min_chars"] or filters["mention_only"] else ""
        pending = conn.execute("SELECT id,content,EXISTS(SELECT 1 FROM message_media r WHERE r.message_id=messages.id) AS has_media FROM messages WHERE entry_id=? AND learning_state='pending'" + decided + " ORDER BY id", (entry_id,)).fetchall()
        if not pending:
            return None
        if batch_rate_status(conn, entry_id, filters["max_batches_per_hour"], current)["retry_at"]:
            return None
        maximum = target_count or pace_parameters(entry["pace"])[0]
        selected: list[int] = []
        total = 0
        for row in pending:
            # The description may arrive only after freezing. Reserve the same
            # per-message ceiling so images cannot inflate the target budget.
            cost = 1500 if row["has_media"] else min(estimate_tokens(row["content"]), 1500)
            if selected and (len(selected) >= maximum or total + cost > token_limit):
                break
            selected.append(row["id"])
            total += cost
        tail = conn.execute("""SELECT target_ids,state FROM batches WHERE entry_id=?
            AND state IN ('succeeded','abandoned','refused')
            ORDER BY COALESCE(root_batch_id,id) DESC,split_path DESC,id DESC LIMIT 1""", (entry_id,)).fetchone()
        history = [] if tail is None or tail["state"] == "refused" else json.loads(tail["target_ids"])[-history_count:]
        future = [row["id"] for row in pending[len(selected):len(selected) + future_count]]
        result = conn.execute("""INSERT INTO batches(entry_id,history_ids,target_ids,future_ids,state,prompt_version,created_at,entry_settings_json)
            VALUES(?,?,?,?,'waiting',?,?,?)""", (entry_id, dumps(history), dumps(selected), dumps(future), prompt_version, now(), dumps(settings)))
        conn.executemany("UPDATE message_admission SET decided=1 WHERE message_id=?", ((i,) for i in selected + future))
        batch_id = int(result.lastrowid)
        conn.executemany("UPDATE messages SET learning_state='batched',batch_id=? WHERE id=?", ((batch_id, i) for i in selected))
    return get_batch(store, batch_id)


def reset_batch(store: Store, batch_id: int, *, _conn: sqlite3.Connection | None = None) -> None:
    with (nullcontext(_conn) if _conn is not None else store.write()) as conn:
        row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None or row["state"] not in ("abandoned", "refused"):
            raise ValueError("batch cannot be relearned")
        _owned_targets(conn, row)
        if conn.execute("SELECT 1 FROM batches WHERE parent_batch_id=?", (batch_id,)).fetchone():
            raise ValueError("split parent cannot be relearned")
        conn.execute("UPDATE batches SET state='waiting',attempt_count=0,next_retry_at=NULL,last_error=NULL WHERE id=?", (batch_id,))
        # Design 7.6/17.7: the successful learning transaction resolves the gap.
        conn.executemany("UPDATE messages SET learning_state='batched' WHERE id=?", ((i,) for i in json.loads(row["target_ids"])))


def next_learning_batch(conn, entry_id: str):
    """One shared queue head: any running leaf, otherwise DFS left to right.

    A waiting head in backoff still blocks its siblings. Callers decide whether
    its time is due; forced offline/admin work cannot skip the ordering check.
    """
    return conn.execute("""SELECT * FROM batches WHERE entry_id=?
        AND state IN ('waiting','running')
        ORDER BY (state='running') DESC,COALESCE(root_batch_id,id),split_path,id LIMIT 1""",
        (entry_id,)).fetchone()


def _owned_targets(conn, row):
    ids = json.loads(row["target_ids"])
    owned = {r[0] for r in conn.execute("""SELECT id FROM messages
        WHERE entry_id=? AND batch_id=? AND id IN (SELECT value FROM json_each(?))""",
        (row["entry_id"], row["id"], row["target_ids"]))}
    if not ids or len(set(ids)) != len(ids) or owned != set(ids):
        raise ValueError("batch targets missing or no longer owned")
    return ids


def claim_batch(store: Store, batch_id: int, *, force=False, current=None):
    """Atomically claim the entry's next leaf; model work happens after commit."""
    instant = current or datetime.fromisoformat(now())
    with store.write() as conn:
        row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None or row["state"] != "waiting":
            raise ValueError("batch is not waiting")
        head = next_learning_batch(conn, row["entry_id"])
        if head["id"] != batch_id or head["state"] == "running":
            raise ValueError("another batch precedes this leaf or is running")
        if row["attempt_count"] >= 4:
            raise ValueError("batch attempts exhausted")
        if not force and row["next_retry_at"] and datetime.fromisoformat(row["next_retry_at"]) > instant:
            raise ValueError("batch retry is not due")
        _owned_targets(conn, row)
        conn.execute("UPDATE batches SET state='running',next_retry_at=NULL WHERE id=?", (batch_id,))
    return get_batch(store, batch_id)


def split_targets(ids):
    """Ordered bisection; no text, quote or attachment is split within a message."""
    if len(ids) < 2:
        raise ValueError("a singleton cannot be split")
    middle = len(ids) // 2
    return ids[:middle], ids[middle:]


def learning_batch(batch: Batch) -> Batch:
    """Effective evidence/media input, leaving frozen audit segments untouched."""
    return replace(batch, history_ids=[], future_ids=[]) if batch.context_mode == "target_only" else batch


def _leaf_gap(conn, row, reason, stamp):
    bounds = conn.execute("""SELECT MIN(occurred_at),MAX(occurred_at) FROM messages
        WHERE id IN (SELECT value FROM json_each(?))""", (row["target_ids"],)).fetchone()
    conn.execute("""INSERT OR REPLACE INTO memory_gaps(batch_id,entry_id,started_at,ended_at,reason,created_at)
        VALUES(?,?,?,?,?,?)""", (row["id"], row["entry_id"], *bounds, reason, stamp))


def split_refused_batch(conn, batch_id: int, *, prompt_version: str,
                        error="content safety refusal", stamp=None):
    """Commit with the failed attempt in the caller's short write transaction.

    Only the learning/repair explicit-refusal handler may call this. Each child
    starts its own four attempts, even if the parent was on its fourth attempt.
    """
    if not conn.in_transaction:
        raise ValueError("refusal changes require a write transaction")
    row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if row is None or row["state"] != "running":
        raise ValueError("only a running leaf can split")
    ids = _owned_targets(conn, row)
    parts = split_targets(ids)
    if conn.execute("SELECT 1 FROM batches WHERE parent_batch_id=?", (batch_id,)).fetchone():
        raise ValueError("batch already split")
    stamp = stamp or now()
    root_id = row["root_batch_id"] or batch_id
    root = conn.execute("SELECT * FROM batches WHERE id=?", (root_id,)).fetchone()
    frozen = json.loads(root["history_ids"]) + json.loads(root["target_ids"]) + json.loads(root["future_ids"])
    # Never admit new arrivals or copy a known terminal refusal into optional
    # context. Pending future messages remain context, not owned learning work.
    eligible = {r[0] for r in conn.execute("""SELECT id FROM messages
        WHERE id IN (SELECT value FROM json_each(?)) AND learning_state NOT IN ('refused','filtered')""",
        (dumps(frozen),))}
    children = []
    for slot, targets in enumerate(parts):
        start, end = frozen.index(targets[0]), frozen.index(targets[-1]) + 1
        history = [mid for mid in frozen[:start] if mid in eligible][-4:]
        future = [mid for mid in frozen[end:] if mid in eligible][:4]
        inserted = conn.execute("""INSERT INTO batches(entry_id,history_ids,target_ids,future_ids,state,
            prompt_version,created_at,entry_settings_json,parent_batch_id,root_batch_id,split_slot,split_path)
            VALUES(?,?,?,?,'waiting',?,?,?,?,?,?,?)""",
            (row["entry_id"], dumps(history), dumps(targets), dumps(future), prompt_version, stamp,
             row["entry_settings_json"], batch_id, root_id, slot, row["split_path"] + str(slot)))
        child_id = int(inserted.lastrowid)
        conn.executemany("UPDATE messages SET batch_id=?,learning_state='batched' WHERE id=?",
                         ((child_id, mid) for mid in targets))
        child = conn.execute("SELECT * FROM batches WHERE id=?", (child_id,)).fetchone()
        _leaf_gap(conn, child, "content_rejection", stamp)
        children.append(child_id)
    conn.execute("DELETE FROM memory_gaps WHERE batch_id=?", (batch_id,))
    conn.execute("""UPDATE batches SET state='split',attempt_count=attempt_count+1,
        next_retry_at=NULL,last_error=?,finished_at=? WHERE id=?""", (error, stamp, batch_id))
    operation(conn, "batch_split", "batch", batch_id,
              {"root_batch_id": root_id, "child_ids": children, "target_counts": [len(p) for p in parts]}, actor="system")
    return tuple(children)


def degrade_refused_context(conn, batch_id: int, *, sent_context_ids,
                            error="content safety refusal", stamp=None):
    """Try once without actually-sent optional context, within the same budget.

    False leaves all state unchanged; the caller then records the usual terminal
    refusal. Frozen quote/media data belonging to the target is never removed.
    """
    if not conn.in_transaction:
        raise ValueError("refusal changes require a write transaction")
    row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if row is None or row["state"] != "running":
        raise ValueError("only a running leaf can reduce context")
    ids = _owned_targets(conn, row)
    optional = set(json.loads(row["history_ids"]) + json.loads(row["future_ids"]))
    if (len(ids) != 1 or row["context_mode"] != "full" or row["attempt_count"] >= 3
            or not optional.intersection(sent_context_ids)):
        return False
    stamp = stamp or now()
    conn.execute("""UPDATE batches SET context_mode='target_only',state='waiting',
        attempt_count=attempt_count+1,next_retry_at=NULL,finished_at=NULL,last_error=? WHERE id=?""",
        (error, batch_id))
    _leaf_gap(conn, row, "content_rejection", stamp)
    operation(conn, "batch_context_reduced", "batch", batch_id,
              {"context_mode": "target_only"}, actor="system")
    return True


def other_entry_pending(store: Store, entry_id: str, *, scope) -> dict | None:
    """R02: aggregate authorized queues without reading any message content.

    Pending includes frozen/in-flight batches; terminal gaps and filtered inputs
    are separate states. Receipt timestamps are generated in UTC by intake.
    """
    clause, args = scope.sql('m.entry_id')
    with store.read() as conn:
        row = conn.execute("""SELECT COUNT(DISTINCT m.entry_id) AS entry_count,
            MIN(m.received_at) AS time_from, MAX(m.received_at) AS time_to
            FROM messages m WHERE m.entry_id!=? AND m.learning_state IN ('pending','batched')
            AND """ + clause, (entry_id, *args)).fetchone()
    return dict(row) if row['entry_count'] else None
