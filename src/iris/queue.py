"""Message intake, independent entry queues, and frozen three-part batches."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Store, dumps, now


PACE = {
    "realtime": (4, timedelta(seconds=5), timedelta(minutes=1)),
    "standard": (12, timedelta(minutes=10), timedelta(minutes=60)),
    "economy": (30, timedelta(minutes=30), timedelta(hours=4)),
}
FOCUS = re.compile(r"@|记住|别忘了")


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
    count, idle, max_wait = PACE[pace]
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
        return row[0]
    subject_id = uuid.uuid4().hex
    conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)", (subject_id, name, now()))
    conn.execute("INSERT INTO platform_identities(subject_id,platform,account_id,display_name) VALUES(?,?,?,?)",
                 (subject_id, platform, account_id, name))
    return subject_id


def add_message(store: Store, *, entry_id: str, entry_name: str, platform: str, entry_kind: str,
                kind: str, sender: str, content: str, occurred_at: str, dedupe_key: str,
                account_id: str | None = None, scene_identity: str | None = None,
                quote_author: str | None = None, quote_content: str | None = None,
                pace: str = "standard") -> int:
    if kind not in ("message", "self_output", "action_result", "event"):
        raise ValueError("invalid message type")
    if len(content.encode("utf-8")) > 32768:
        raise ValueError("message exceeds 32 KB")
    with store.write() as conn:
        conn.execute("INSERT OR IGNORE INTO entries(id,name,platform,kind,pace) VALUES(?,?,?,?,?)",
                     (entry_id, entry_name, platform, entry_kind, pace))
        existing = conn.execute("SELECT id FROM messages WHERE entry_id=? AND dedupe_key=?", (entry_id, dedupe_key)).fetchone()
        if existing:
            return int(existing[0])
        sender_id = "self" if kind != "message" else _subject(conn, platform, account_id or sender, sender)
        quote_id = _subject(conn, platform, f"quoted:{quote_author}", quote_author) if quote_author else None
        result = conn.execute("""INSERT INTO messages
            (entry_id,kind,sender_subject_id,scene_identity,content,quote_author_subject_id,quote_content,
             occurred_at,received_at,dedupe_key)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (entry_id, kind, sender_id, scene_identity, content, quote_id, quote_content,
             occurred_at, now(), dedupe_key))
        conn.execute("UPDATE entries SET last_message_at=? WHERE id=?", (occurred_at, entry_id))
        return int(result.lastrowid)


def get_batch(store: Store, batch_id: int) -> Batch:
    with store.read() as conn:
        row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if row is None:
        raise KeyError(batch_id)
    return Batch(row["id"], row["entry_id"], json.loads(row["history_ids"]),
                 json.loads(row["target_ids"]), json.loads(row["future_ids"]),
                 row["state"], row["attempt_count"], row["prompt_version"])


def form_batch(store: Store, entry_id: str, prompt_version: str, *, target_count: int | None = None,
               token_limit: int = 4000, history_count: int = 4, future_count: int = 4) -> Batch | None:
    with store.write() as conn:
        active = conn.execute("SELECT id FROM batches WHERE entry_id=? AND state IN ('running','waiting') ORDER BY id LIMIT 1", (entry_id,)).fetchone()
        if active:
            return None
        entry = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if entry is None:
            return None
        pending = conn.execute("SELECT id,content FROM messages WHERE entry_id=? AND learning_state='pending' ORDER BY id", (entry_id,)).fetchall()
        if not pending:
            return None
        maximum = target_count or PACE[entry["pace"]][0]
        selected: list[int] = []
        total = 0
        for row in pending:
            cost = min(estimate_tokens(row["content"]), 1500)
            if selected and (len(selected) >= maximum or total + cost > token_limit):
                break
            selected.append(row["id"])
            total += cost
        tail = conn.execute("""SELECT target_ids,state FROM batches WHERE entry_id=?
            AND state IN ('succeeded','abandoned','refused') ORDER BY id DESC LIMIT 1""", (entry_id,)).fetchone()
        history = [] if tail is None or tail["state"] == "refused" else json.loads(tail["target_ids"])[-history_count:]
        future = [row["id"] for row in pending[len(selected):len(selected) + future_count]]
        result = conn.execute("""INSERT INTO batches(entry_id,history_ids,target_ids,future_ids,state,prompt_version,created_at)
            VALUES(?,?,?,?,'waiting',?,?)""", (entry_id, dumps(history), dumps(selected), dumps(future), prompt_version, now()))
        batch_id = int(result.lastrowid)
        conn.executemany("UPDATE messages SET learning_state='batched',batch_id=? WHERE id=?", ((batch_id, i) for i in selected))
    return get_batch(store, batch_id)


def recover_unfinished(store: Store) -> None:
    with store.write() as conn:
        conn.execute("""UPDATE batches SET state='waiting',attempt_count=attempt_count+1,
            next_retry_at=?,last_error='interrupted by restart' WHERE state='running'""", (now(),))


def reset_batch(store: Store, batch_id: int) -> None:
    with store.write() as conn:
        row = conn.execute("SELECT state,target_ids FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None or row["state"] not in ("abandoned", "refused"):
            raise ValueError("batch cannot be relearned")
        conn.execute("UPDATE batches SET state='waiting',attempt_count=0,next_retry_at=NULL,last_error=NULL WHERE id=?", (batch_id,))
        conn.execute("DELETE FROM memory_gaps WHERE batch_id=?", (batch_id,))
        conn.executemany("UPDATE messages SET learning_state='batched' WHERE id=?", ((i,) for i in json.loads(row["target_ids"])))
