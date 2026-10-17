"""EF-19: retention-only extraction from #116 / be7f8fe; fake models only."""
import json
from datetime import timedelta

import pytest

from iris import entry_profile as ep
from iris.maintenance import Maintenance
from iris.memory_ops import adjust_retention, entry_profile_source, write_entry_profile_memory
from test_entry_profile_incremental import (
    NOW, Gateway, add, fixed_projection_clock, profiles, run, source,
)
from test_entry_profile_incremental_quality import seed, check_seed


def admission_operations(store, mid):
    with store.read() as conn:
        return [json.loads(row[0]) for row in conn.execute(
            "SELECT details_json FROM admin_operations WHERE action='entry_profile_admit' "
            "AND object_id=? ORDER BY id", (str(mid),))]


def raw(store, mid):
    with store.read() as conn:
        return dict(conn.execute('SELECT * FROM memories WHERE id=?', (mid,)).fetchone())



@pytest.mark.parametrize('category,body,texts', [
    ('topic', '讨论望远镜支架的选材。', [
        (1, '甲', '木材和钢材哪种更适合望远镜支架？'),
        (4, '乙', '望远镜支架选材还要比较携带重量。')]),
    ('saying', '搭桥', [(1, '甲', '先搭桥吧'), (4, '乙', '我来搭个桥')]),
])
def test_late_admission_raises_strength_and_restarts_decay(store, category, body, texts):
    mid, _ = seed(store, texts, body=body, category=category)
    # Persisted strength after a long candidate interval, with an old decay phase.
    with store.write() as conn:
        conn.execute('UPDATE memories SET retention=23,decay_visits=2 WHERE id=?', (mid,))
    before = raw(store, mid)
    check_seed(store)
    admitted = raw(store, mid)
    assert admitted['importance'] == 50
    assert admitted['retention'] == round(30 + 50 * .4)
    assert admitted['decay_visits'] == 0
    assert admitted['first_confirmed_at'] == before['first_confirmed_at']
    assert admitted['last_confirmed_at'] == before['last_confirmed_at']
    # The existing importance promotion owns the one revision increment;
    # retention itself must not add another revision.
    assert admitted['revision'] == before['revision'] + 1
    assert admission_operations(store, mid)[-1]['admission_retention'] == {
        'before': 23, 'after': 50, 'decay_visits': 0,
    }
    assert profiles(store)[0]['admission_state'] == 'accepted'
    for days, expected in [(1, 50), (2, 50), (3, 49)]:
        maintenance = Maintenance(store, clock=lambda: NOW + timedelta(days=days))
        maintenance.run(maintenance.request())
        assert raw(store, mid)['retention'] == expected
    # A no-new-message restart is not another admission and does not recharge.
    check_seed(store)
    assert raw(store, mid)['retention'] == 49



def test_admission_floor_does_not_erase_stronger_retention(store):
    mid, _ = seed(store, [(1, '甲', '望远镜支架可以用木材。'), (4, '乙', '比较铝材望远镜支架的重量。')])
    with store.write() as conn:
        conn.execute('UPDATE memories SET retention=70,decay_visits=2 WHERE id=?', (mid,))
    check_seed(store)
    assert raw(store, mid)['retention'] == 70
    assert raw(store, mid)['decay_visits'] == 0



def test_accepted_confirmation_and_recheck_do_not_recharge_again(store):
    mid, _ = seed(store, [(1, '甲', '望远镜支架可以用木材。'), (4, '乙', '比较铝材望远镜支架的重量。')])
    check_seed(store)
    with store.write() as conn:
        conn.execute('UPDATE memories SET retention=41,decay_visits=2 WHERE id=?', (mid,))
    add(store, '我在比较望远镜支架的钢材重量。', 8, sender='丙')
    g = Gateway(store, lambda p: {'observations': [{'op':'confirm', 'memory_id':mid,
        'source_refs':[source(p['messages'][0])]}]})
    run(store, g)
    assert raw(store, mid)['retention'] == 46
    assert raw(store, mid)['decay_visits'] == 2
    assert profiles(store)[0]['usable']
    check_seed(store)
    assert raw(store, mid)['retention'] == 46



def test_revoked_admission_keeps_strength_and_readmission_renews_once(store):
    mid, ids = seed(store, [(1, '甲', '望远镜支架可以用木材。'), (4, '乙', '比较铝材望远镜支架的重量。')])
    check_seed(store)
    strength = raw(store, mid)['retention']
    with store.write() as conn:
        conn.execute("UPDATE messages SET content='这条来源已失效。' WHERE id=?", (ids[0],))
    assert profiles(store)[0]['admission_state'] == 'candidate'
    assert raw(store, mid)['retention'] == strength
    assert raw(store, mid)['importance'] == 50
    with store.write() as conn:
        conn.execute('UPDATE memories SET retention=24,decay_visits=2 WHERE id=?', (mid,))
    add(store, '望远镜支架的木材与钢材各有重量优势。', 8, sender='丙')
    run(store, Gateway(store, lambda p: {'observations':[{'op':'confirm','memory_id':mid,
        'source_refs':[source(p['messages'][0])]}]}))
    assert raw(store, mid)['retention'] == 50
    assert raw(store, mid)['decay_visits'] == 0



def test_new_body_admission_renews_strength(store):
    mid, _ = seed(store, [(1, '甲', '望远镜支架可以用木材。'), (4, '乙', '比较铝材望远镜支架的重量。')])
    check_seed(store)
    with store.write() as conn:
        conn.execute('UPDATE memories SET retention=24,decay_visits=2 WHERE id=?', (mid,))
    add(store, '继续比较望远镜支架选材时的携带重量。', 8, sender='丙')
    def revise(p):
        return {'observations':[{'op':'change','action':'revise','memory_id':mid,
            'category':'topic','body':'讨论望远镜支架的材料。','saying':'','meaning':'',
            'definition_refs':[],'source_refs':[source(p['messages'][0])]}]}
    run(store, Gateway(store,revise))
    assert raw(store,mid)['retention'] == 50
    assert raw(store,mid)['decay_visits'] == 0
    assert raw(store,mid)['content'] == '讨论望远镜支架的材料。'



def test_rejected_recheck_revokes_admission_without_lowering_strength(store):
    mid, _ = seed(store, [(1, '甲', '望远镜支架可以用木材。'), (4, '乙', '比较铝材望远镜支架的重量。')])
    check_seed(store)
    adjust_retention(store,mid,value=48,current=NOW)
    reject={'decision':'reject','violations':['单条消息泛化'],'reason':'正文细节应更概括。','safe_sources':[]}
    task=ep.IncrementalProfileTask(store,Gateway(store,checks=lambda p:reject),clock=lambda:NOW)
    with store.write() as conn:
        row=conn.execute('SELECT m.*,p.category FROM memories m JOIN entry_profile_memories p ON p.memory_id=m.id WHERE m.id=?',(mid,)).fetchone()
        # A new bounded check group for this deterministic failure scenario.
        conn.execute('UPDATE entry_profile_admissions SET evidence_revision=evidence_revision+1 WHERE memory_id=?',(mid,))
        task._queue_admission(conn,row,ep._inc_all_sources(conn,row,NOW.isoformat()))
    task.run('group')
    assert profiles(store)[0]['admission_state'] == 'candidate'
    assert raw(store,mid)['retention'] == 48
    assert raw(store,mid)['importance'] == 50



def test_same_body_routine_recheck_preserves_strength_and_decay_phase(store):
    mid, _ = seed(store, [(1, '甲', '望远镜支架可以用木材。'),
                          (4, '乙', '比较铝材望远镜支架的重量。')])
    check_seed(store)
    with store.write() as conn:
        conn.execute('UPDATE memories SET retention=41,decay_visits=2 WHERE id=?', (mid,))
        # A new check of the same accepted body, rather than a no-work restart.
        conn.execute('UPDATE entry_profile_admissions SET evidence_revision=evidence_revision+1 WHERE memory_id=?', (mid,))
        row = conn.execute('SELECT m.*,p.category FROM memories m JOIN entry_profile_memories p ON p.memory_id=m.id WHERE m.id=?', (mid,)).fetchone()
        gateway = Gateway(store)
        task = ep.IncrementalProfileTask(store, gateway, clock=lambda: NOW)
        assert task._queue_admission(conn, row, ep._inc_all_sources(conn, row, NOW.isoformat()))
    revision = raw(store, mid)['revision']
    task.run('group')
    assert any('admit' in call['purpose'] for call in gateway.calls)
    assert raw(store, mid)['retention'] == 41
    assert raw(store, mid)['decay_visits'] == 2
    assert raw(store, mid)['revision'] == revision
    assert 'admission_retention' not in admission_operations(store, mid)[-1]


def test_replayed_admit_does_not_recharge_accepted_revision(store):
    mid, ids = seed(store, [(1, '甲', '望远镜支架可以用木材。'),
                            (4, '乙', '比较铝材望远镜支架的重量。')])
    check_seed(store)
    with store.write() as conn:
        conn.execute('UPDATE memories SET retention=41,decay_visits=2 WHERE id=?', (mid,))
        revision = conn.execute('SELECT revision FROM memories WHERE id=?', (mid,)).fetchone()[0]
        refs = [entry_profile_source(conn, message_id) for message_id in ids]
        receipt = write_entry_profile_memory(store, 'group', op='admit', memory_id=mid,
            expected_revision=revision, sources=refs, current=NOW, _conn=conn)
        assert receipt['revision'] == revision
    assert raw(store, mid)['retention'] == 41
    assert raw(store, mid)['decay_visits'] == 2
    assert 'admission_retention' not in admission_operations(store, mid)[-1]


def test_rule_admission_does_not_gain_topic_or_saying_renewal(store):
    mid, _ = seed(store, [(1, '甲', '群规公告：全体成员不公开他人的住址，此要求长期有效。')],
                  category='rule', body='本群不公开他人的住址。')
    with store.write() as conn:
        conn.execute('UPDATE memories SET retention=23,decay_visits=2 WHERE id=?', (mid,))
    check_seed(store)
    assert profiles(store)[0]['admission_state'] == 'accepted'
    assert raw(store, mid)['importance'] == 50
    assert raw(store, mid)['retention'] == 23
    assert raw(store, mid)['decay_visits'] == 2
    assert 'admission_retention' not in admission_operations(store, mid)[-1]
