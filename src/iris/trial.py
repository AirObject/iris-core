"""Trial-only reply generation and publication; host prepare stays generation-free."""
from __future__ import annotations

import threading
import uuid
from importlib.resources import files

from .db import dumps, now
from .models import ModelError
from .media import MediaError
from .queue import _subject, add_message
from .retrieval import Retrieval
from .service_status import add_health_hints

PLATFORM = "iris-trial"


class TrialBusy(Exception):
    pass


class TrialReplyFailed(Exception):
    pass


def require_entry(conn, entry_id):
    row = conn.execute("SELECT * FROM entries WHERE id=? AND platform=?", (entry_id, PLATFORM)).fetchone()
    if row is None:
        raise KeyError(entry_id)
    return dict(row)


def create_entry(store, name, kind):
    entry_id = "trial-" + uuid.uuid4().hex
    with store.write() as conn:
        conn.execute("INSERT INTO entries(id,name,platform,kind,pace) VALUES(?,?,?,?,'realtime')", (entry_id, name, PLATFORM, kind))
        speaker_id = _subject(conn, PLATFORM, "user", "我（用户）")
        return require_entry(conn, entry_id) | {"default_speaker_id": speaker_id}


def create_speaker(store, name):
    with store.write() as conn:
        sid = _subject(conn, PLATFORM, uuid.uuid4().hex, name)
    return {"id": sid, "name": name}


def trial_catalog(store):
    with store.read() as conn:
        speakers = [dict(r) for r in conn.execute("""SELECT s.id,s.name,p.account_id='user' AS is_default
            FROM platform_identities p JOIN subjects s ON s.id=p.subject_id WHERE p.platform=?
            ORDER BY is_default DESC,s.created_at,s.id""", (PLATFORM,))]
        return {"entries": [dict(r) for r in conn.execute("SELECT * FROM entries WHERE platform=? ORDER BY rowid", (PLATFORM,))],
                "speakers": speakers, "role_name": store.setting("role_name", "Iris")}


def receive(store, entry_id, speaker_id, content, dedupe_key, media_ids=None):
    with store.write() as conn:
        entry = require_entry(conn, entry_id)
        speaker = conn.execute("""SELECT s.name,p.account_id FROM subjects s JOIN platform_identities p ON p.subject_id=s.id
            WHERE s.id=? AND p.platform=?""", (speaker_id, PLATFORM)).fetchone()
        if not speaker:
            raise KeyError(speaker_id)
        existing = conn.execute('SELECT id FROM messages WHERE entry_id=? AND dedupe_key=?', (entry_id, dedupe_key)).fetchone()
        if not existing:
            for media_id in media_ids or []:
                item = conn.execute('SELECT kind FROM media_objects WHERE id=?', (media_id,)).fetchone()
                if item is None:
                    raise MediaError('invalid_media')
                if item['kind'] != 'image':
                    raise MediaError('unsupported_media_type')
        mid = add_message(store, entry_id=entry_id, entry_name=entry["name"], entry_kind=entry["kind"],
                          platform=PLATFORM, kind="message", sender=speaker["name"], account_id=speaker["account_id"],
                          content=content, occurred_at=now(), dedupe_key=dedupe_key, media_ids=media_ids, pace="realtime", _conn=conn)
        state = conn.execute("SELECT learning_state FROM messages WHERE id=?", (mid,)).fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM messages WHERE entry_id=? AND learning_state IN ('pending','batched')", (entry_id,)).fetchone()[0]
    return {"message_id": mid, "learning_state": state, "pending_count": pending}


class TrialReplies:
    def __init__(self, store, gateway, health):
        self.store, self.gateway, self.health = store, gateway, health
        self._locks = {}
        self._guard = threading.Lock()

    def reply(self, entry_id, message_id):
        with self._guard:
            lock = self._locks.setdefault(entry_id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise TrialBusy()
        try:
            return self._reply(entry_id, message_id)
        finally:
            lock.release()

    def _reply(self, entry_id, message_id):
        key = f"trial-reply:{message_id}"
        with self.store.read() as conn:
            entry = require_entry(conn, entry_id)
            trigger = conn.execute("SELECT id FROM messages WHERE id=? AND entry_id=? AND kind='message'", (message_id, entry_id)).fetchone()
            if not trigger:
                raise KeyError(message_id)
            published = conn.execute("SELECT * FROM messages WHERE entry_id=? AND dedupe_key=? AND kind='self_output'", (entry_id, key)).fetchone()
            if published:
                return {"message": dict(published), "prepared": None, "reused": True}
        prepared = add_health_hints(Retrieval(self.store, self.gateway).prepare(entry_id), self.health)
        if message_id not in {m["id"] for m in prepared["recent_messages"]}:
            raise ValueError("这条消息已不在近期对话中，请发送新消息后再请求回复")
        prompt = files("iris").joinpath("prompts/trial_reply_v1.md").read_text(encoding="utf-8")
        try:
            result = self.gateway.json_chat([
                {"role": "system", "content": prompt},
                {"role": "user", "content": dumps({"reply_to_message_id": message_id, "prepared": prepared})},
            ], "trial_reply", max_tokens=16000)[0]
        except ModelError as error:
            raise TrialReplyFailed() from error
        reply = result.get("reply") if isinstance(result, dict) else None
        if not isinstance(reply, str) or not reply.strip() or len(reply.encode("utf-8")) > 32768:
            raise TrialReplyFailed()
        # Publication is the actual output, persisted for redisplay after a lost
        # HTTP response. Only this text is new evidence, never the prepare payload.
        mid = add_message(self.store, entry_id=entry_id, entry_name=entry["name"], entry_kind=entry["kind"],
                          platform=PLATFORM, kind="self_output", sender="我", content=reply.strip(),
                          occurred_at=now(), dedupe_key=key, pace="realtime")
        with self.store.read() as conn:
            message = dict(conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone())
        return {"message": message, "prepared": prepared, "reused": False}
