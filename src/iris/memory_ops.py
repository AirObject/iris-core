"""Initial persona and small data-level memory/subject operations."""

from __future__ import annotations

import json
from contextlib import nullcontext
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import Store, dumps, now


def setup_role(store: Store, name: str, background: str = "", timezone_name: str = "Asia/Shanghai", *, _conn=None) -> str:
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown timezone") from exc
    name = name.strip() or "Iris"
    sentences = [part.strip() for part in re.split(r"[\n。！？]+", background) if part.strip()]
    persona = (f"我是{name}。" + ("初始设定：" + "；".join(sentences) + "。" if sentences else "尚无预设经历，会在相处中逐渐认识自己。"))
    with (store.write() if _conn is None else nullcontext(_conn)) as conn:
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
        before = {key: row[key] for key in ("content", "kind", "stance", "belief", "importance", "event_time", "lifecycle")}
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


def delete_memory(store: Store, memory_id: int, expected_revision: int, *, actor: str = "admin") -> bool:
    """Revoke one object, retaining its ID, history, and shared source messages."""
    with store.write() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=? AND lifecycle!='deleted'", (memory_id,)).fetchone()
        if row is None or row["revision"] != expected_revision:
            return False
        before = {key: row[key] for key in ("content", "kind", "stance", "belief", "importance", "event_time", "lifecycle")}
        after = {**before, "lifecycle": "deleted"}
        stamp = now()
        conn.execute("UPDATE memories SET lifecycle='deleted',revision=revision+1,updated_at=?,embedding=NULL,embedding_model=NULL WHERE id=?",
                     (stamp, memory_id))
        conn.execute("""INSERT INTO memory_revisions(memory_id,revision_before,revision_after,before_json,after_json,
            reason,actor,created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (memory_id, expected_revision, expected_revision + 1, dumps(before), dumps(after), "manual delete", actor, stamp))
        return True


def update_role(store, name, background, timezone_name, *, _conn=None):
    """Update initial settings while preserving revoked memories and persona history."""
    ZoneInfo(timezone_name)
    with (store.write() if _conn is None else nullcontext(_conn)) as conn:
        current = conn.execute("SELECT id FROM persona_versions WHERE is_current=1").fetchone()
        if not current:
            return setup_role(store, name, background, timezone_name, _conn=conn)
        previous = {r[0]: json.loads(r[1]) for r in conn.execute(
            "SELECT key,value_json FROM runtime_settings WHERE key IN ('role_name','background','timezone','persona_goal','persona_rules')")}
        if previous.get('role_name') == name and previous.get('background') == background:
            conn.execute("INSERT OR REPLACE INTO runtime_settings VALUES('timezone',?)", (dumps(timezone_name),))
            return
        conn.execute("UPDATE persona_versions SET is_current=0 WHERE is_current=1")
        # Only initial-setting objects are replaced. Learned memories remain intact.
        if previous.get('background') != background:
            rows = conn.execute("SELECT * FROM memories WHERE lifecycle!='deleted' AND id IN (SELECT memory_id FROM sources WHERE kind='initial_setting')").fetchall()
            for row in rows:
                before = {k: row[k] for k in ('content', 'kind', 'stance', 'belief', 'importance', 'event_time', 'lifecycle')}
                after = {**before, 'lifecycle': 'deleted'}
                conn.execute("UPDATE memories SET lifecycle='deleted',revision=revision+1,updated_at=?,embedding=NULL,embedding_model=NULL WHERE id=?", (now(), row['id']))
                conn.execute("INSERT INTO memory_revisions(memory_id,revision_before,revision_after,before_json,after_json,reason,actor,created_at) VALUES(?,?,?,?,?,'initial background changed','admin',?)", (row['id'], row['revision'], row['revision']+1, dumps(before), dumps(after), now()))
        # setup_role already owns deterministic initial persona/background conversion.
        for key in ('role_name', 'background', 'timezone', 'persona_goal', 'persona_rules'):
            conn.execute('DELETE FROM runtime_settings WHERE key=?', (key,))
        if previous.get('background') == background:
            # A name-only change must not duplicate initial memories.
            setup_role(store, name, '', timezone_name, _conn=conn)
            sentences = [part.strip() for part in re.split(r"[\n。！？]+", background) if part.strip()]
            persona = f"我是{name}。" + ("初始设定："+"；".join(sentences)+"。" if sentences else "尚无预设经历，会在相处中逐渐认识自己。")
            conn.execute('UPDATE persona_versions SET content=? WHERE is_current=1', (persona,))
            conn.execute("INSERT OR REPLACE INTO runtime_settings VALUES('background',?)", (dumps(background),))
        else:
            setup_role(store, name, background, timezone_name, _conn=conn)

        for key in ('persona_goal', 'persona_rules'):
            if key in previous:
                conn.execute('INSERT OR REPLACE INTO runtime_settings VALUES(?,?)', (key, dumps(previous[key])))
