"""EF-17: admission strength, independent short usages, and hard request bounds."""
import json
from datetime import timedelta

import pytest

from iris import entry_profile as ep
from iris.db import dumps
from iris.maintenance import Maintenance
from iris.memory_ops import adjust_retention
from iris.models import ModelError
from test_entry_profile_incremental import (
    NOW, Gateway, accept, add, fixed_projection_clock, profiles, projection, run, source,
)
from test_entry_profile_incremental_quality import seed, check_seed


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
    check_seed(store)
    admitted = raw(store, mid)
    assert admitted['importance'] == 50
    assert admitted['retention'] == round(30 + 50 * .4)
    assert admitted['decay_visits'] == 0
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


def saying_observation(p):
    return {'observations':[{'op':'propose','category':'saying','body':'搭桥','saying':'搭桥',
        'meaning':'','definition_refs':[], 'source_refs':[source(m) for m in p['messages']]}]}


@pytest.mark.parametrize('second_sender,second_day,second_minute', [
    ('乙', 1, 5), ('甲', 4, 0), ('甲', 1, 40),
])
def test_identical_short_usage_is_independent_across_people_days_or_sessions(
        store, second_sender, second_day, second_minute):
    add(store, '搭桥', 1, key='first')
    run(store, Gateway(store, saying_observation))
    mid = profiles(store)[0]['memory_id']
    add(store, '搭桥', second_day, sender=second_sender, minute=second_minute, key='second')
    g = Gateway(store, lambda p:{'observations':[{'op':'confirm','memory_id':mid,
        'source_refs':[source(m) for m in p['messages']]}]})
    run(store, g)
    row = profiles(store)[0]
    assert row['independent_uses'] == 2 and row['usable']
    assert row['retention'] == 50
    assert len(row['sources']) == 2
    assert run(store, g)['status'] == 'skipped'
    assert profiles(store)[0]['retention'] == 50
    # Another real independent use gets the ordinary +5 exactly once, including
    # an identical short utterance by the same author on a later date.
    add(store, '搭桥', 10, sender=second_sender, key='third')
    run(store, g)
    assert profiles(store)[0]['retention'] == 55
    assert len(profiles(store)[0]['sources']) == 2
    assert len(profiles(store)[0]['pending_sources']) == 1
    assert run(store, g)['status'] == 'skipped'
    assert profiles(store)[0]['retention'] == 55


@pytest.mark.parametrize('text,sender,minute,quoted', [
    ('搭桥', '乙', 0, False), ('搭桥', '甲', 5, False),
    ('转述甲的原话：搭桥', '乙', 40, False),
    ('搭桥', '乙', 40, True),
])
def test_burst_forward_and_quoted_short_usage_still_do_not_count(store,text,sender,minute,quoted):
    first = add(store, '搭桥', key='first')
    second = add(store, text, sender=sender, minute=minute, key='second')
    if quoted:
        with store.write() as conn:
            author = conn.execute('SELECT sender_subject_id FROM messages WHERE id=?', (first,)).fetchone()[0]
            conn.execute('UPDATE messages SET quote_author_subject_id=?,quote_content=? WHERE id=?',
                         (author, '搭桥', second))
    run(store, Gateway(store, saying_observation))
    row = profiles(store)[0]
    assert row['independent_uses'] == 1 and not row['usable']
    assert row['retention'] == 42


@pytest.mark.parametrize('saying,usage', [
    ('搭桥','先搭一会儿桥吧'), ('搭桥','搭一下桥'), ('搭桥','搭一下下桥'),
    ('留灯','先留灯一会儿'), ('留灯','留留灯呀'), ('留灯','留灯留灯'),
    ('留一下下灯','留灯'), ('先留灯一会儿','留个灯'),
    ('留留灯','留一会儿灯'), ('留灯留灯','留一下灯'),
])
def test_light_saying_variants_preserve_the_same_lexical_core(saying,usage):
    assert ep._inc_saying_used(saying, usage)


@pytest.mark.parametrize('saying,usage', [
    ('搭桥','拆一下桥'), ('搭桥','搭一下桥墩'), ('留灯','留一下灯罩'),
    ('留灯','关一下灯'), ('不翻页','翻一下页'), ('盖章','盖一下章鱼缸'),
    ('留灯','留很多没有关系的字再去找灯'), ('网吧','网盘'),
    ('先知','知'), ('小电梯','小一下电梯'),
    ('留灯','留台灯'), ('留台灯','留灯'), ('看看板','看板'),
])
def test_lexical_changes_negation_and_other_nouns_do_not_merge(saying,usage):
    assert not ep._inc_saying_used(saying, usage)


def test_copied_full_statement_is_not_rescued_as_an_independent_short_usage(store):
    text = '我把望远镜的连接件重新调整之后决定先搭桥再去买东西。'
    first = add(store, text, key='first')
    add(store, text, 4, sender='乙', key='second')
    run(store, Gateway(store, saying_observation))
    assert profiles(store)[0]['independent_uses'] == 1
    assert not projection(store)


@pytest.mark.parametrize('stage', ['observe','admit'])
def test_full_request_bound_includes_prompt_metadata_and_diagnostics(store,stage):
    add(store, '占位消息')
    g = Gateway(store)
    task = ep.IncrementalProfileTask(store, g, clock=lambda:NOW)
    payload = {'oversized': '材' * 24000, '_snapshot': {'diagnostics': '材' * 24000}}
    work = {'outputs_json':'{}','calls_json':'[]','attempts':1}
    with pytest.raises(ModelError) as caught:
        task._call(work, stage, payload)
    assert caught.value.reason == 'request_budget_exceeded'
    assert g.calls == []


def test_repair_bound_does_not_send_oversized_previous_output(store):
    add(store, '占位消息')
    g = Gateway(store, observations=lambda p: '格式错误' * 24000)
    task = ep.IncrementalProfileTask(store,g,clock=lambda:NOW)
    wid = task._freeze_chunk('group')
    with store.read() as conn:
        work = dict(conn.execute('SELECT * FROM entry_profile_incremental_work WHERE id=?',(wid,)).fetchone())
    with pytest.raises(ModelError) as caught:
        task._call(work,'observe',json.loads(work['payload_json']))
    assert caught.value.reason == 'repair_budget_exceeded'
    assert len(g.calls) == 1


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
