"""Deterministic fixtures for the G line (daily goal auto review); no network."""
import json
from datetime import datetime
from itertools import count

from iris.goals import AUTO_REVIEW_KEY, DUPLICATE_AUTO_KEY
from iris.maintenance import Maintenance, PHASES

CREATED = '2026-11-02T09:13:00+00:00'  # 17:13 in Asia/Shanghai
NOW = '2026-11-03T01:00:00+00:00'
_keys = count(1)


class Clock:
    def __init__(self, value=NOW):
        self.value = datetime.fromisoformat(value)

    def __call__(self):
        return self.value


class Reviewer:
    """Fake chat gateway for G1. Records every material; asserts no open transaction."""

    def __init__(self, store, answer=None, hook=None):
        self.store, self.answer, self.hook = store, answer, hook
        self.calls = []

    def json_chat(self, messages, purpose, max_tokens=3500, *, batch_id=None):
        assert not self.store._writer.in_transaction
        payload = json.loads(messages[-1]['content'])
        self.calls.append({'purpose': purpose, 'payload': payload, 'system': messages[0]['content'],
                           'max_tokens': max_tokens})
        if self.hook:
            self.hook(payload)
        answer = self.answer(payload) if callable(self.answer) else self.answer
        if isinstance(answer, Exception):
            raise answer
        return answer, json.dumps(answer, ensure_ascii=False), None, 'direct', None


def cite(decision, pick=lambda payload: [payload['messages'][-1]['id']]):
    return lambda payload: {'decision': decision, 'evidence': pick(payload), 'reason': '测试'}


def subject(store, sid, name):
    with store.write() as conn:
        conn.execute("INSERT OR IGNORE INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)", (sid, name, CREATED))


def entry(conn, eid, visibility='shared'):
    conn.execute("INSERT OR IGNORE INTO entries(id,name,platform,kind,visibility) VALUES(?,?,'unit','private',?)",
                 (eid, eid, visibility))


def message(store, content, *, sender='p1', at='2026-11-02T19:08:00+08:00', kind='message', entry_id='e1',
            quote_author=None, quote=None):
    with store.write() as conn:
        entry(conn, entry_id)
        return conn.execute('''INSERT INTO messages(entry_id,kind,sender_subject_id,content,quote_author_subject_id,
            quote_content,occurred_at,received_at,dedupe_key,learning_state) VALUES(?,?,?,?,?,?,?,?,?,'learned')''',
            (entry_id, kind, sender, content, quote_author, quote, at, at, f'k{next(_keys)}')).lastrowid


def goal(store, content='帮程栗取南门驿站的电热毯快递', *, people=('p1',), sources=(), origin='internal',
         kind='normal', created=CREATED, entry_id='e1', deadline=None):
    with store.write() as conn:
        entry(conn, entry_id)
        gid = conn.execute('''INSERT INTO goals(content,kind,origin,state,deadline,deadline_at,entry_id,created_at,
            updated_at,schedule_initialized) VALUES(?,?,?,'open',?,?,?,?,?,1)''',
            (content, kind, origin, deadline, deadline, entry_id, created, created)).lastrowid
        conn.executemany('INSERT INTO goal_people(goal_id,subject_id) VALUES(?,?)', [(gid, p) for p in people])
        conn.executemany('INSERT INTO goal_sources(goal_id,message_id) VALUES(?,?)', [(gid, m) for m in sources])
    return gid


def basic(store):
    """One learning goal with a source before creation and one later candidate."""
    subject(store, 'p1', '程栗')
    subject(store, 'p2', '路人')
    source = message(store, '能帮我取一下南门驿站的电热毯吗', at='2026-11-02T17:10:00+08:00')
    gid = goal(store, sources=[source])
    done = message(store, '到家了，餐桌上的电热毯收到了，谢谢！', at='2026-11-02T19:08:00+08:00')
    return gid, source, done


def enable(store, *, review=True, duplicates=False):
    store.set_setting('goals', {AUTO_REVIEW_KEY: review, DUPLICATE_AUTO_KEY: duplicates})


def run_goals(store, gateway, clock=None, *, stop=None):
    """One maintenance run whose model phases are disabled; jumps to the goal phase."""
    clock = clock or Clock()
    store.set_setting('consolidation', {'enabled': False})
    store.set_setting('scheduled_backup', {'enabled': False})
    engine = Maintenance(store, gateway=gateway, clock=clock)
    rid = engine.request()
    with store.write() as conn:
        conn.execute('UPDATE maintenance_runs SET phase=? WHERE id=?', (PHASES.index('goals'), rid))
    engine.run(rid, stop=stop)
    return engine, rid


def row(store, gid):
    with store.read() as conn:
        return dict(conn.execute('SELECT * FROM goals WHERE id=?', (gid,)).fetchone())


def reviews(store, gid=None):
    with store.read() as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM goal_auto_reviews' + (' WHERE goal_id=?' if gid else '') +
                                              ' ORDER BY id', (gid,) if gid else ())]


def items(store, rid):
    with store.read() as conn:
        return [{**dict(r), 'details': json.loads(r['details_json'])} for r in conn.execute(
            "SELECT * FROM maintenance_items WHERE run_id=? AND phase='goals' ORDER BY rowid", (rid,))]
