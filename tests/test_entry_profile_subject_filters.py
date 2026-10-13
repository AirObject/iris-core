"""EF-06: new entry subjects/profile memories stay outside people and goals."""
from datetime import datetime, timezone
from types import SimpleNamespace

from iris.db import now
from iris.goals import _create, goal_detail
from iris.learning import LearningEngine
from conftest import FakeGateway, msg


def test_learning_snapshot_ignores_entry_subject_and_name_collision(store):
    mid = msg(store, 1, '介绍一个叫同名的人')
    with store.write() as conn:
        # Seed the future subject kind on migration 030 too: the red test must
        # expose the missing read filter, not merely a missing schema feature.
        conn.execute('PRAGMA ignore_check_constraints=ON')
        conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES('entry-probe','entry','同名',?)", (now(),))
        conn.execute('PRAGMA ignore_check_constraints=OFF')
        conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES('person-probe','person','同名',?)", (now(),))
    engine = LearningEngine(store, FakeGateway())
    snapshot = engine._snapshot(SimpleNamespace(history_ids=[], target_ids=[mid], future_ids=[]))
    snapshot['participant_refs'] = {}
    assert 'entry-probe' not in {s['id'] for s in snapshot['subjects']}
    assert engine._known_subject('同名', snapshot) == 'person-probe'


def test_goals_exclude_profile_kind_from_evidence_and_material(store):
    mid = msg(store, 1, '我会整理书单', sender='Iris', kind='self_output')
    stamp = now()
    with store.write() as conn:
        ids = []
        for kind in ('事实', '入口画像'):
            if kind == '入口画像':
                from iris.memory_ops import write_entry_profile_memory, entry_profile_source
                ids.append(write_entry_profile_memory(store,'A',op='new',category='rule',content='整理书单',manual=True,
                    sources=[entry_profile_source(conn,mid)],_conn=conn)['memory_id'])
            else:
                ids.append(conn.execute('''INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,
                    retention,created_at,updated_at,first_confirmed_at,last_confirmed_at)
                    VALUES(?,?,'self','转述',80,50,50,?,?,?,?)''',
                    ('整理书单',kind,stamp,stamp,stamp,stamp)).lastrowid)
                conn.execute("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,?)", (ids[-1],mid,stamp))
        _create(conn, content='整理书单', entry_id='A', evidence=[mid], current=datetime(2026,10,13,tzinfo=timezone.utc))
        gid = conn.execute('SELECT id FROM goals').fetchone()[0]
        actual = [r[0] for r in conn.execute('SELECT memory_id FROM goal_memories WHERE goal_id=?', (gid,))]
        assert actual == [ids[0]]
        # Defensive read filter also covers legacy/imported references.
        conn.execute('INSERT INTO goal_memories VALUES(?,?,1)', (gid,ids[1]))
        detail = goal_detail(conn,gid)
        assert [m['id'] for m in detail['promise_memories']] == [ids[0]]
