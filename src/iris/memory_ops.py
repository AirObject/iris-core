"""Initial persona and small data-level memory/subject operations."""

from __future__ import annotations

import json
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import Store, dumps, now


def setup_role(store: Store, name: str, background: str = "", timezone_name: str = "Asia/Shanghai") -> str:
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown timezone") from exc
    name = name.strip() or "Iris"
    sentences = [part.strip() for part in re.split(r"[\n。！？]+", background) if part.strip()]
    persona = (f"我是{name}。" + ("初始设定：" + "；".join(sentences) + "。" if sentences else "尚无预设经历，会在相处中逐渐认识自己。"))
    with store.write() as conn:
        existing = conn.execute("SELECT id FROM persona_versions WHERE is_current=1 LIMIT 1").fetchone()
        if existing:
            raise ValueError("role already initialized")
        for key, value in (("role_name", name), ("background", background), ("timezone", timezone_name),
                           ("persona_goal", "维持稳定的发言风格，并充分认识自我"),
                           ("persona_rules", "只根据自我记忆提炼，不虚构经历；外部设定不写成亲历；别人评价不自动成为自我认知")):
            conn.execute("INSERT INTO runtime_settings(key,value_json) VALUES(?,?)", (key, dumps(value)))
        conn.execute("INSERT INTO persona_versions(content,created_at,is_current) VALUES(?,?,1)", (persona, now()))
        for sentence in sentences:
            stamp = now()
            result = conn.execute("""INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,
                retention,world,created_at,updated_at,first_confirmed_at,last_confirmed_at)
                VALUES(?,'自我','self','设定',80,65,56,'real',?,?,?,?)""",
                (sentence, stamp, stamp, stamp, stamp))
            memory_id = int(result.lastrowid)
            conn.execute("INSERT INTO memory_subjects(memory_id,subject_id) VALUES(?,'self')", (memory_id,))
            conn.execute("INSERT INTO sources(memory_id,kind,note,created_at) VALUES(?,'initial_setting',?,?)",
                         (memory_id, "first setup", stamp))
    return persona


def edit_memory(store: Store, memory_id: int, expected_revision: int, *, content: str, actor: str = "admin") -> bool:
    if not content.strip() or len(content) > 1000:
        raise ValueError("invalid memory content")
    with store.write() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=? AND lifecycle='active'", (memory_id,)).fetchone()
        if row is None or row["revision"] != expected_revision:
            return False
        before = {key: row[key] for key in ("content", "kind", "stance", "belief", "importance", "event_time")}
        after = {**before, "content": content.strip()}
        conn.execute("UPDATE memories SET content=?,revision=revision+1,updated_at=? WHERE id=?",
                     (content.strip(), now(), memory_id))
        conn.execute("""INSERT INTO memory_revisions(memory_id,revision_before,revision_after,before_json,after_json,
            reason,actor,created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (memory_id, expected_revision, expected_revision + 1, dumps(before), dumps(after), "manual edit", actor, now()))
        return True


def adjust_retention(store: Store, memory_id: int, delta: int) -> None:
    with store.write() as conn:
        conn.execute("UPDATE memories SET retention=MAX(0,MIN(100,retention+?)) WHERE id=?", (delta, memory_id))


def set_subject_link_status(store: Store, link_id: int, status: str) -> None:
    if status not in ("possible", "confirmed", "denied"):
        raise ValueError("invalid link status")
    with store.write() as conn:
        conn.execute("UPDATE subject_links SET status=? WHERE id=?", (status, link_id))
