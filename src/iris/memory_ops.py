"""Initial persona and small data-level memory/subject operations."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import secrets
from weakref import WeakKeyDictionary
from collections import OrderedDict
from threading import RLock
from contextlib import nullcontext
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import Store, dumps, now


def setup_role(store: Store, name: str, background: str = "", timezone_name: str = "Asia/Shanghai", *, _conn=None, current=None) -> str:
    from .persona import DEFAULT_GOAL, DEFAULT_RULES
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
                           ("persona_goal", DEFAULT_GOAL),
                           ("persona_rules", DEFAULT_RULES)):
            conn.execute("INSERT INTO runtime_settings(key,value_json) VALUES(?,?)", (key, dumps(value)))
        memory_ids = []
        for sentence in sentences:
            stamp = current.isoformat() if current is not None else now()
            result = conn.execute("""INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,
                retention,world,created_at,updated_at,first_confirmed_at,last_confirmed_at)
                VALUES(?,'自我','self','设定',80,65,56,'real',?,?,?,?)""",
                (sentence, stamp, stamp, stamp, stamp))
            memory_id = int(result.lastrowid)
            memory_ids.append(memory_id)
            conn.execute("INSERT INTO memory_subjects(memory_id,subject_id) VALUES(?,'self')", (memory_id,))
            conn.execute("INSERT INTO sources(memory_id,kind,note,created_at) VALUES(?,'initial_setting',?,?)",
                         (memory_id, "first setup", stamp))
        from .persona import record_initial
        record_initial(conn, persona, memory_ids, name, background, current.isoformat() if current is not None else now())
    return persona


def edit_memory(store: Store, memory_id: int, expected_revision: int, *, content: str, actor: str = "admin") -> bool:
    if not content.strip() or len(content) > 1000:
        raise ValueError("invalid memory content")
    with store.write() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=? AND lifecycle!='deleted'", (memory_id,)).fetchone()
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


LIFECYCLE_DEFAULTS = {
    "forget_threshold": 20, "restore_threshold": 35,
    "feedback_increment": 8, "confirmation_increment": 5, "decay_amount": 1,
    "auto_delete_enabled": True, "auto_delete_days": 180, "upcoming_delete_days": 14,
    "message_retention_days": 30, "maintenance_time": "03:00",
    "abandoned_retry_enabled": True, "dependency_penalty": 10,
}


def lifecycle_settings(conn):
    row = conn.execute("SELECT value_json FROM runtime_settings WHERE key='lifecycle'").fetchone()
    return {**LIFECYCLE_DEFAULTS, **(json.loads(row[0]) if row else {})}


def operation(conn, action, object_type, object_id, details=None, *, actor="admin", stamp=None):
    """Record only caller-selected IDs, counts and settings; never prose or requests."""
    return conn.execute("""INSERT INTO admin_operations(actor,action,object_type,object_id,details_json,created_at)
        VALUES(?,?,?,?,?,?)""", (actor, action, object_type, str(object_id), dumps(details or {}), stamp or now())).lastrowid


def adjust_retention(store: Store, memory_id: int, delta: int = 0, *, value=None, floor=None,
                     expected_revision=None, current=None, settings=None, _conn=None):
    """Atomic strength + hysteresis, with no revision/history increment.

    Supply _conn inside an existing Store.write() transaction (learning/feedback).
    Otherwise this function owns the transaction. A missing/deleted object or a
    revision mismatch returns None. delta is additive; value sets an absolute
    strength; floor raises it to at least that value. The result is the latest row.
    """
    if any(type(v) is not int for v in (delta, *[v for v in (value, floor) if v is not None])):
        raise ValueError("retention must be an integer")
    if value is not None and delta:
        raise ValueError("use delta or value, not both")
    stamp = current.isoformat() if current is not None else now()
    with (store.write() if _conn is None else nullcontext(_conn)) as conn:
        config = settings if settings is not None else lifecycle_settings(conn)
        # SQL reads current strength under the single writer; increments cannot be lost.
        result = conn.execute("""UPDATE memories
            SET retention=MAX(0,MIN(100,MAX(COALESCE(?,retention+?),COALESCE(?,0))))
            WHERE id=? AND lifecycle!='deleted' AND (? IS NULL OR revision=?)
            RETURNING *""", (value, delta, floor, memory_id, expected_revision, expected_revision)).fetchone()
        if result is None:
            return None
        state = result["lifecycle"]
        if state == "active" and result["retention"] < config["forget_threshold"]:
            conn.execute("UPDATE memories SET lifecycle='forgotten',forgotten_at=? WHERE id=?", (stamp, memory_id))
        elif state == "forgotten" and result["retention"] >= config["restore_threshold"]:
            conn.execute("UPDATE memories SET lifecycle='active',forgotten_at=NULL WHERE id=?", (memory_id,))
        return dict(conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone())


def confirm_retention(store, memory_id, *, _conn=None, current=None):
    """Learning-line entry point; caller still owns per-batch confirmation dedupe."""
    with (store.write() if _conn is None else nullcontext(_conn)) as conn:
        config = lifecycle_settings(conn)
        result = adjust_retention(store, memory_id, config["confirmation_increment"],
                                  settings=config, current=current, _conn=conn)
        if result is not None:
            stamp = current.isoformat() if current is not None else now()
            conn.execute("UPDATE memories SET last_confirmed_at=? WHERE id=?", (stamp, memory_id))
            result["last_confirmed_at"] = stamp
        return result


def set_subject_link_status(store: Store, link_id: int, status: str) -> None:
    if status not in ("possible", "confirmed", "denied"):
        raise ValueError("invalid link status")
    with store.write() as conn:
        conn.execute("UPDATE subject_links SET status=? WHERE id=?", (status, link_id))


def delete_memory(store: Store, memory_id: int, expected_revision: int, *, actor: str = "admin", _conn=None, current=None) -> bool:
    """Revoke one object, retaining its ID, history, and shared source messages."""
    with (store.write() if _conn is None else nullcontext(_conn)) as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=? AND lifecycle!='deleted'", (memory_id,)).fetchone()
        if row is None or row["revision"] != expected_revision:
            return False
        before = {key: row[key] for key in ("content", "kind", "stance", "belief", "importance", "event_time", "lifecycle")}
        after = {**before, "lifecycle": "deleted"}
        stamp = current.isoformat() if current is not None else now()
        conn.execute("UPDATE memories SET lifecycle='deleted',revision=revision+1,updated_at=?,embedding=NULL,embedding_model=NULL WHERE id=?",
                     (stamp, memory_id))
        conn.execute("""INSERT INTO memory_revisions(memory_id,revision_before,revision_after,before_json,after_json,
            reason,actor,created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (memory_id, expected_revision, expected_revision + 1, dumps(before), dumps(after), "automatic expiry" if actor == "maintenance" else "manual delete", actor, stamp))
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
        conn.execute("UPDATE persona_versions SET is_current=0,status='history' WHERE is_current=1")
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
            from .persona import record_initial
            memory_ids = [r[0] for r in conn.execute("SELECT DISTINCT m.id FROM memories m JOIN sources s ON s.memory_id=m.id WHERE s.kind='initial_setting' AND m.lifecycle!='deleted' ORDER BY m.id")]
            version_id = conn.execute('SELECT id FROM persona_versions WHERE is_current=1').fetchone()[0]
            record_initial(conn, persona, memory_ids, name, background, now(), version_id=version_id)
            conn.execute("INSERT OR REPLACE INTO runtime_settings VALUES('background',?)", (dumps(background),))
        else:
            setup_role(store, name, background, timezone_name, _conn=conn)

        for key in ('persona_goal', 'persona_rules'):
            if key in previous:
                conn.execute('INSERT OR REPLACE INTO runtime_settings VALUES(?,?)', (key, dumps(previous[key])))


def manage_memory(store, memory_id, expected_revision, *, action=None, pinned=None,
                  importance=None, retention=None, _conn=None, _record=True):
    """Pin/forget/restore/scores; only importance is a judgment revision."""
    if action not in (None, "forget", "restore"):
        raise ValueError("invalid memory action")
    for value in (importance, retention):
        if value is not None and (type(value) is not int or not 0 <= value <= 100):
            raise ValueError("score must be an integer in 0..100")
    if pinned is not None and type(pinned) is not bool:
        raise ValueError("pinned must be a boolean")
    with (store.write() if _conn is None else nullcontext(_conn)) as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=? AND lifecycle!='deleted'", (memory_id,)).fetchone()
        if row is None or row["revision"] != expected_revision:
            return False
        config, stamp = lifecycle_settings(conn), now()
        before = {k: row[k] for k in ("pinned", "importance", "retention", "lifecycle", "forgotten_at")}
        if importance is not None and importance != row["importance"]:
            snapshot = {k: row[k] for k in ("content", "kind", "stance", "belief", "importance", "event_time", "lifecycle")}
            conn.execute("UPDATE memories SET importance=?,revision=revision+1,updated_at=? WHERE id=?", (importance, stamp, memory_id))
            conn.execute("""INSERT INTO memory_revisions(memory_id,revision_before,revision_after,before_json,after_json,reason,actor,created_at)
                VALUES(?,?,?,?,?,'manual importance','admin',?)""", (memory_id, expected_revision, expected_revision+1,
                dumps(snapshot), dumps({**snapshot, "importance": importance}), stamp))
        if pinned is not None:
            conn.execute("UPDATE memories SET pinned=? WHERE id=?", (int(pinned), memory_id))
        if action == "forget":
            conn.execute("UPDATE memories SET pinned=0 WHERE id=?", (memory_id,))
            retention = min(row["retention"], config["forget_threshold"] - 1)
        elif action == "restore":
            retention = max(row["retention"], config["restore_threshold"])
        if retention is not None:
            adjust_retention(store, memory_id, value=retention, settings=config, _conn=conn)
        latest = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if _record:
            operation(conn, "memory_" + (action or "adjust"), "memory", memory_id,
                      {"before": before, "after": {k: latest[k] for k in before}, "revision": latest["revision"]})
        return True


def message_references(conn, message_id):
    """Evidence and live work protect messages; finished batches and reply keys do not."""
    checks = (
        ("memory_source", "SELECT 1 FROM sources WHERE message_id=? LIMIT 1"),
        ("batch_segment", """SELECT 1 FROM batch_message_refs r JOIN batches b ON b.id=r.batch_id
            WHERE r.message_id=? AND b.state IN ('waiting','running') LIMIT 1"""),
        ("goal_source", "SELECT 1 FROM goal_sources WHERE message_id=? LIMIT 1"),
        ("subject_alias", "SELECT 1 FROM subject_aliases WHERE source_message_id=? LIMIT 1"),
        ("subject_link", "SELECT 1 FROM subject_links WHERE source_message_id=? LIMIT 1"),
        ("learning_request", "SELECT 1 FROM entries WHERE learn_requested_through=? LIMIT 1"),
    )
    reasons = [name for name, sql in checks if conn.execute(sql, (message_id,)).fetchone()]
    return reasons


def missing_batch_targets(conn, batch_id):
    """Check within the caller's transaction, before reset can protect the batch again."""
    return [r[0] for r in conn.execute("""SELECT j.value FROM batches b,json_each(b.target_ids) j
        LEFT JOIN messages m ON m.id=j.value WHERE b.id=? AND m.id IS NULL ORDER BY j.key""", (batch_id,))]


def purge_memory(store, memory_id, expected_revision, *, confirm=False, _conn=None, _record=True):
    if confirm is not True:
        raise ValueError("explicit confirmation required")
    with (store.write() if _conn is None else nullcontext(_conn)) as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=? AND purged_at IS NULL", (memory_id,)).fetchone()
        if row is None or row["revision"] != expected_revision:
            return None
        message_ids = [r[0] for r in conn.execute("SELECT DISTINCT message_id FROM sources WHERE memory_id=? AND message_id IS NOT NULL ORDER BY message_id", (memory_id,))]
        conn.execute("""WITH RECURSIVE ancestry(id) AS (
            SELECT ? UNION SELECT s.source_memory_id FROM ancestry a JOIN sources s ON s.memory_id=a.id
            WHERE s.kind='memory' AND s.source_memory_id IS NOT NULL)
            INSERT OR IGNORE INTO memory_visibility_roots(memory_id,entry_id)
            SELECT ?,x.entry_id FROM ancestry a JOIN sources s ON s.memory_id=a.id
                JOIN messages x ON x.id=s.message_id WHERE s.kind='message'
            UNION SELECT ?,v.entry_id FROM ancestry a JOIN memory_visibility_roots v ON v.memory_id=a.id""",
            (memory_id,memory_id,memory_id))
        # Set deleted before removing outgoing sources, preserving incoming loss events.
        conn.execute("""UPDATE memories SET lifecycle='deleted',content='',kind='其他',speaker_subject_id='self',
            stance='设定',belief=0,importance=0,retention=0,event_time=NULL,forgotten_at=NULL,pinned=0,
            revision=revision+1,entry_id=NULL,world='real',embedding=NULL,embedding_model=NULL,
            decay_visits=0,purged_at=?,updated_at=? WHERE id=?""", (now(), now(), memory_id))
        for table in ("memory_subjects", "memory_tags", "sources", "memory_revisions"):
            conn.execute(f"DELETE FROM {table} WHERE memory_id=?", (memory_id,))
        deleted, retained = [], []
        for mid in message_ids:
            reasons = message_references(conn, mid)
            if reasons:
                retained.append({"message_id": mid, "reasons": reasons})
            else:
                conn.execute("DELETE FROM messages WHERE id=?", (mid,))
                deleted.append(mid)
        result = {"memory_id": memory_id, "deleted_message_ids": deleted, "retained_messages": retained}
        if _record:
            operation(conn, "memory_purge", "memory", memory_id, result)
        return result


def recreate_memory(store, memory_id, expected_revision, *, source_revision):
    """New object from historical prose/judgments, retaining existing evidence.

    Old revisions lack complete relation snapshots. Preserve the object's current
    about/tags/sources and explicitly avoid inventing historical attribution.
    """
    with store.write() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=? AND purged_at IS NULL", (memory_id,)).fetchone()
        if row is None or row["revision"] != expected_revision:
            return None
        values = dict(row)
        if source_revision != row["revision"]:
            historical = conn.execute("""SELECT before_json AS snapshot FROM memory_revisions
                WHERE memory_id=? AND revision_before=?
                UNION ALL SELECT after_json FROM memory_revisions WHERE memory_id=? AND revision_after=? LIMIT 1""",
                (memory_id, source_revision, memory_id, source_revision)).fetchone()
            if historical is None:
                raise ValueError("revision does not exist")
            values.update(json.loads(historical[0]))
        stamp = now()
        new_id = conn.execute("""INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,
            retention,event_time,entry_id,world,created_at,updated_at,first_confirmed_at,last_confirmed_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (values["content"], values["kind"], values["speaker_subject_id"],
            values["stance"], values["belief"], values["importance"], round(30 + values["importance"] * .4),
            values["event_time"], values["entry_id"], values["world"], stamp, stamp, stamp, stamp)).lastrowid
        conn.execute("INSERT INTO memory_subjects SELECT ?,subject_id FROM memory_subjects WHERE memory_id=?", (new_id, memory_id))
        conn.execute("INSERT INTO memory_tags SELECT ?,tag FROM memory_tags WHERE memory_id=?", (new_id, memory_id))
        conn.execute("""INSERT INTO sources(memory_id,kind,message_id,source_memory_id,source_revision,note,created_at)
            SELECT ?,kind,message_id,source_memory_id,source_revision,note,created_at FROM sources WHERE memory_id=?""", (new_id, memory_id))
        operation(conn, "memory_recreate", "memory", new_id,
                  {"source_memory_id": memory_id, "source_revision": source_revision, "memory_id": new_id})
        return new_id


# Bounded immutable graph snapshots, shared by readers but never mutated after
# publication. Random transaction tokens prevent rollback/reused-path ABA bugs.
_VISIBILITY_CACHE = OrderedDict()
_VISIBILITY_LOCK = RLock()


class Visibility:
    """Current effective entry sets. None means all entries; empty means admin-only.

    The recursive query walks only restricted roots and their dependents, using
    sources_by_message/sources_by_parent. It never reads memory/message prose.
    Cache identity belongs to the SQLite snapshot, including uncommitted writers;
    another reader cannot observe it until that token is committed.
    """
    def __init__(self, conn):
        self.conn = conn
        self.token = conn.execute('SELECT token FROM visibility_version WHERE id=1').fetchone()[0]
        with _VISIBILITY_LOCK:
            cached = _VISIBILITY_CACHE.get(self.token)
            if cached is not None:
                _VISIBILITY_CACHE.move_to_end(self.token)
        if cached is None:
            cached = self._load(conn)
            with _VISIBILITY_LOCK:
                _VISIBILITY_CACHE[self.token] = cached
                while len(_VISIBILITY_CACHE) > 8:
                    _VISIBILITY_CACHE.popitem(last=False)
        self.entries, self.memories, self.goals = cached

    @staticmethod
    def intersect(*scopes):
        result = None
        for scope in scopes:
            if scope is not None:
                result = scope if result is None else result & scope
        return result

    @classmethod
    def _load(cls, conn):
        rows = conn.execute('SELECT id,visibility,visible_in_json FROM entries').fetchall()
        universe = frozenset(r['id'] for r in rows)
        entries = {}
        for row in rows:
            allowed = (universe if row['visibility']=='shared' else frozenset({row['id']}) if
                       row['visibility']=='entry_only' else frozenset([row['id'], *json.loads(row['visible_in_json'])]) & universe)
            entries[row['id']] = None if allowed == universe else allowed
        restricted = [key for key,value in entries.items() if value is not None]
        memories, goals = {}, {}
        if restricted:
            roots = dumps(restricted)
            for mid,eid in conn.execute("""WITH RECURSIVE bounds(memory_id,entry_id) AS (
                SELECT s.memory_id,x.entry_id FROM messages x JOIN sources s ON s.message_id=x.id
                    WHERE s.kind='message' AND x.entry_id IN (SELECT value FROM json_each(?))
                UNION SELECT memory_id,entry_id FROM memory_visibility_roots
                    WHERE entry_id IN (SELECT value FROM json_each(?))
                UNION SELECT s.memory_id,b.entry_id FROM bounds b JOIN sources s ON s.source_memory_id=b.memory_id
                    WHERE s.kind='memory'
            ) SELECT memory_id,entry_id FROM bounds""", (roots,roots)):
                memories[mid] = cls.intersect(memories.get(mid), entries[eid])
            for gid,eid in conn.execute("""SELECT id,entry_id FROM goals
                    WHERE entry_id IN (SELECT value FROM json_each(?))
                UNION SELECT gs.goal_id,x.entry_id FROM goal_sources gs JOIN messages x ON x.id=gs.message_id
                    JOIN goals g ON g.id=gs.goal_id WHERE g.entry_id IS NOT NULL
                    AND x.entry_id IN (SELECT value FROM json_each(?))""", (roots,roots)):
                goals[gid] = cls.intersect(goals.get(gid), entries[eid])
        return entries, memories, goals

    def entry(self, entry_id):
        if entry_id is None:
            return None
        if entry_id not in self.entries:
            raise KeyError(entry_id)
        return self.entries[entry_id]

    def memory(self, memory_id):
        return self.memories.get(memory_id)

    def goal(self, goal_id):
        return self.goals.get(goal_id)

    @staticmethod
    def allows(scope, entry_id):
        return scope is None or (entry_id is not None and entry_id in scope)

    def memory_visible(self, memory_id, entry_id=None):
        return self.allows(self.memory(memory_id), entry_id)

    def goal_visible(self, goal_id, entry_id=None):
        return self.allows(self.goal(goal_id), entry_id)

    def evidence(self, message_ids=(), memory_ids=()):
        scopes = [self.memory(mid) for mid in memory_ids]
        for row in self.conn.execute('SELECT entry_id FROM messages WHERE id IN (SELECT value FROM json_each(?))',
                                     (dumps(list(message_ids)),)):
            scopes.append(self.entry(row[0]))
        return self.intersect(*scopes)

    def memory_sql(self, entry_id=None, column='m.id'):
        self.entry(entry_id)
        if not self.memories:
            return '1'
        self.conn.create_function('iris_memory_visible', 1, lambda mid: self.memory_visible(mid, entry_id), deterministic=True)
        return f'iris_memory_visible({column})'

    def goal_sql(self, entry_id=None, column='g.id'):
        self.entry(entry_id)
        if not self.goals:
            return '1'
        self.conn.create_function('iris_goal_visible', 1, lambda gid: self.goal_visible(gid, entry_id), deterministic=True)
        return f'iris_goal_visible({column})'

    def describe_memory(self, memory_id):
        scope = self.memory(memory_id)
        return {'shared': scope is None, 'visible_in': sorted(self.entries if scope is None else scope)}


def entry_visibility(row):
    mode = row['visibility']
    return {'visibility': mode, 'visible_in': sorted({row['id'], *json.loads(row['visible_in_json'])}) if mode=='entries' else []}


def set_entry_visibility(store, entry_id, visibility, *, visible_in=(), actor='admin'):
    if visibility not in ('shared','entry_only','entries'):
        raise ValueError('invalid visibility')
    if not isinstance(visible_in, (list,tuple)) or any(not isinstance(e,str) or not e for e in visible_in):
        raise ValueError('visible_in must contain entry IDs')
    if visibility!='entries' and visible_in:
        raise ValueError('visible_in requires entries visibility')
    with store.write() as conn:
        row = conn.execute('SELECT * FROM entries WHERE id=?',(entry_id,)).fetchone()
        if row is None:
            raise KeyError(entry_id)
        allowed = sorted({entry_id, *visible_in}) if visibility=='entries' else []
        if allowed and conn.execute('SELECT COUNT(*) FROM entries WHERE id IN (SELECT value FROM json_each(?))',
                                    (dumps(allowed),)).fetchone()[0] != len(allowed):
            raise ValueError('visible_in contains an unknown entry')
        before = entry_visibility(row)
        result = {'visibility': visibility, 'visible_in': allowed}
        conn.execute('UPDATE entries SET visibility=?,visible_in_json=? WHERE id=?', (visibility,dumps(allowed),entry_id))
        operation(conn, 'entry_visibility', 'entry', entry_id, {'before':before,'after':result}, actor=actor)
        return result


# Previews are short-lived capabilities in this Store's process, not settings or
# persisted copies of private prose. A restart/eviction requires a new preview.
# Durable progress belongs in the single admin_operations row, committed with
# each chunk. Never hold the cache lock while taking the Store's write lock.
BULK_PREVIEW_SECONDS = 15 * 60
BULK_BATCH_SIZE = 50
_BULK_PREVIEWS = WeakKeyDictionary()
_BULK_LOCK = RLock()


class BulkMemoryConflict(Exception):
    def __init__(self, message="记忆范围或内容已变化，请重新预览。", result=None):
        super().__init__(message)
        self.result = deepcopy(result)


def bulk_memory_rows(conn, scope, include_pinned, memory_ids=None):
    """Indexed scope membership; never use ownership or indirect entry ancestry.

    Snapshot relation IDs as well as revisions: about/evidence, pinning and
    strength can change without a judgment revision. Embedding/use counters do
    not affect the operation and deliberately do not invalidate the preview.
    """
    if "subject_id" in scope:
        condition = """(m.speaker_subject_id=? OR EXISTS (
            SELECT 1 FROM memory_subjects ms WHERE ms.memory_id=m.id AND ms.subject_id=?))"""
        args = [scope["subject_id"], scope["subject_id"]]
    else:
        condition = """EXISTS (SELECT 1 FROM sources s JOIN messages x ON x.id=s.message_id
            WHERE s.memory_id=m.id AND x.entry_id=?)"""
        args = [scope["entry_id"]]
    if memory_ids is not None:
        condition += " AND m.id IN (SELECT value FROM json_each(?))"
        args.append(dumps(memory_ids))
    if not include_pinned:
        condition += " AND m.pinned=0"
    return {r["id"]: dict(r) for r in conn.execute(f"""SELECT m.id,m.revision,m.lifecycle,m.pinned,
        m.retention,m.speaker_subject_id,
        (SELECT json_group_array(subject_id) FROM (SELECT subject_id FROM memory_subjects
            WHERE memory_id=m.id ORDER BY subject_id)) AS about,
        (SELECT json_group_array(json_array(id,kind,message_id,source_memory_id,source_revision))
            FROM (SELECT id,kind,message_id,source_memory_id,source_revision FROM sources
            WHERE memory_id=m.id ORDER BY id)) AS evidence
        FROM memories m WHERE m.purged_at IS NULL AND {condition} ORDER BY m.id""", args)}


def remember_bulk_preview(store, *, scope, action, include_pinned, rows, settings):
    token = secrets.token_urlsafe(32)
    current = datetime.now(timezone.utc)
    expires = current + timedelta(seconds=BULK_PREVIEW_SECONDS)
    result = {"scope": scope, "action": action, "include_pinned": include_pinned,
              "count": 0, "memory_ids": [], "status": "preview",
              "skipped_count": sum(r["lifecycle"] == "deleted" and action != "purge" for r in rows.values()),
              "deleted_message_ids": [], "retained_messages": []}
    with _BULK_LOCK:
        cache = _BULK_PREVIEWS.setdefault(store, OrderedDict())
        for key in list(cache):
            if cache[key]["expires"] <= current and cache[key]["status"] != "running":
                del cache[key]
        # Bound idle previews; active operations retain their local snapshot.
        while len(cache) >= 32:
            key = next((k for k, v in cache.items() if v["status"] != "running"), None)
            if key is None:
                raise ValueError("批量操作繁忙，请稍后再预览")
            del cache[key]
        cache[token] = {"scope": scope, "action": action, "include_pinned": include_pinned,
                        "rows": rows, "settings": settings, "expires": expires,
                        "status": "preview", "result": result}
    return token, expires.isoformat()


def apply_bulk_memories(store, *, snapshot_token, action, confirm=False):
    """Preflight the whole selection; recheck each chunk under its write lock.

    A conflict before the first commit changes no memories. Conflicts after a
    commit return the exact completed IDs, never an all-or-nothing fiction.
    Replays of completed tokens return the same result without another write.
    """
    if action not in ("forget", "delete", "purge"):
        raise ValueError("无效的批量操作")
    if action == "purge" and confirm is not True:
        raise ValueError("彻底清除需要明确的二次确认")
    with _BULK_LOCK:
        snapshot = _BULK_PREVIEWS.get(store, {}).get(snapshot_token)
        if snapshot is None or snapshot["expires"] <= datetime.now(timezone.utc):
            raise BulkMemoryConflict("预览已过期或服务已重启，请查看操作记录并重新预览。",
                                     snapshot["result"] if snapshot else None)
        if snapshot["action"] != action:
            raise ValueError("操作与预览不一致，请重新预览")
        if snapshot["status"] == "completed":
            return deepcopy(snapshot["result"])
        if snapshot["status"] != "preview":
            raise BulkMemoryConflict("此预览正在执行或已失效，请查看操作记录后重新预览。", snapshot["result"])
        snapshot["status"] = "running"
        snapshot["result"]["status"] = "running"
        result = deepcopy(snapshot["result"])
    expected = snapshot["rows"].copy()
    ids = [mid for mid, row in expected.items() if action == "purge" or row["lifecycle"] != "deleted"]
    scope, include_pinned = snapshot["scope"], snapshot["include_pinned"]
    audit_id = None
    try:
        # Even an empty/no-op selection is checked and recorded exactly once.
        for start in range(0, max(1, len(ids)), BULK_BATCH_SIZE):
            chunk = ids[start:start + BULK_BATCH_SIZE]
            last = start + BULK_BATCH_SIZE >= len(ids)
            pending = deepcopy(result)
            next_expected = expected.copy()
            with store.write() as conn:
                if lifecycle_settings(conn) != snapshot["settings"]:
                    raise BulkMemoryConflict()
                current = bulk_memory_rows(conn, scope, include_pinned, None if start == 0 else chunk)
                wanted = expected if start == 0 else {mid: expected[mid] for mid in chunk}
                if current != wanted:
                    raise BulkMemoryConflict()
                for mid in chunk:
                    revision = expected[mid]["revision"]
                    if action == "forget":
                        ok = manage_memory(store, mid, revision, action="forget", _conn=conn, _record=False)
                    elif action == "delete":
                        ok = delete_memory(store, mid, revision, _conn=conn)
                    else:
                        ok = purge_memory(store, mid, revision, confirm=True, _conn=conn, _record=False)
                        if ok:
                            pending["deleted_message_ids"] = sorted(set(pending["deleted_message_ids"] + ok["deleted_message_ids"]))
                            retained = {r["message_id"]: r for r in pending["retained_messages"] + ok["retained_messages"]}
                            pending["retained_messages"] = [retained[k] for k in sorted(retained) if k not in pending["deleted_message_ids"]]
                    if not ok:
                        raise BulkMemoryConflict()
                for mid in chunk:
                    next_expected.pop(mid)
                next_expected.update(bulk_memory_rows(conn, scope, include_pinned, chunk))
                # Catch new matches and changes to already processed/skipped
                # objects. The current chunk rolls back on a late conflict.
                if last and bulk_memory_rows(conn, scope, include_pinned) != next_expected:
                    raise BulkMemoryConflict()
                pending["memory_ids"] += chunk
                pending["count"] = len(pending["memory_ids"])
                pending["status"] = "completed" if last else "running"
                if audit_id is None:
                    next_audit_id = operation(conn, "memory_bulk_" + action, "memory_bulk", secrets.token_hex(8), pending)
                else:
                    next_audit_id = audit_id
                    conn.execute("UPDATE admin_operations SET details_json=? WHERE id=?", (dumps(pending), audit_id))
            audit_id, expected, result = next_audit_id, next_expected, pending
            with _BULK_LOCK:
                snapshot["result"] = deepcopy(result)
        with _BULK_LOCK:
            snapshot["status"] = "completed"
            snapshot["rows"] = {}  # Completed retries only need the result.
        return result
    except Exception as exc:
        result["status"] = "conflict" if isinstance(exc, BulkMemoryConflict) else "interrupted"
        with _BULK_LOCK:
            snapshot["status"] = result["status"]
            snapshot["result"] = deepcopy(result)
            snapshot["rows"] = {}
        if audit_id is not None:
            with store.write() as conn:
                conn.execute("UPDATE admin_operations SET details_json=? WHERE id=?", (dumps(result), audit_id))
        if isinstance(exc, BulkMemoryConflict):
            raise BulkMemoryConflict(result=result) from exc
        raise
