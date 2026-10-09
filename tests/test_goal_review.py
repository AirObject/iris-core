"""Goal basis review is deterministic, revision-safe, and never closes a goal."""
import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from conftest import msg
from iris import goals as module
from iris.db import Store
from iris.goals import Goals, GoalConflict
from iris.memory_ops import adjust_retention, edit_memory, delete_memory, purge_memory
from iris.retrieval import Retrieval
from iris.search_text import segmented
from test_goals import clock, goals
from test_retrieval import put


def backed(store, goals, *, content='整理采访记录', origin='internal', kind='normal'):
    source = msg(store, 1, '我答应整理采访记录', sender='我', kind='self_output')
    mid = put(store, '我答应整理采访记录')
    with store.write() as conn:
        conn.execute("UPDATE memories SET speaker_subject_id='self' WHERE id=?", (mid,))
        conn.execute("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,'2026-10-09')", (mid,source))
    goal = goals.create(content=content, kind=kind, origin=origin, entry_id='A', evidence=[source])['goal']
    return goal, mid


def review(store, clock, goal_id=None, **kwargs):
    with store.write() as conn:
        return module.review_goal_basis(conn, clock(), goal_id=goal_id, **kwargs)


def history(store, gid):
    with store.read() as conn:
        return module.goal_revisions(conn, gid)


@pytest.mark.parametrize('change,expected', [('edit', {'revision_changed'}), ('forget', {'forgotten'}),
    ('delete', {'revision_changed','deleted'}), ('purge', {'revision_changed','purged'})])
def test_basis_change_is_visible_without_changing_goal_or_reminders(store, goals, clock, change, expected):
    goal, mid = backed(store, goals)
    goal = goals.update(goal['id'], deadline='2026-10-08T10:00:00Z', reminder_minutes=30)
    before = goals.get(goal['id'])
    if change=='edit': assert edit_memory(store, mid, 1, content='我取消了整理采访记录的承诺')
    elif change=='forget': adjust_retention(store, mid, value=1)
    elif change=='delete': assert delete_memory(store, mid, 1)
    else: assert purge_memory(store, mid, 1, confirm=True)
    report = review(store, clock, goal['id'], expected_revision=goal['revision'])
    assert report['changed_goal_ids']==[goal['id']]
    after = goals.get(goal['id'])
    assert after['basis_needs_review'] and len(after['basis_annotations'])==1
    mark = after['basis_annotations'][0]
    assert mark['memory_id']==mid and mark['basis_revision']==1
    assert set(mark['changes'])==expected and '依据可能不成立' in mark['text']
    assert after['revision']==before['revision']+1
    for field in ('content','state','deadline','reminder_minutes','notifications','reminder_plans'):
        assert after[field]==before[field]
    assert after['overdue']
    assert review(store, clock)['changed_goal_ids']==[]
    assert goals.get(goal['id'])['revision']==after['revision']
    retrieval = Retrieval(store, clock=clock)
    for value in (retrieval.prepare('A', text='', participants=[], judge=False),
                  retrieval.search(text='', include_goals=True)):
        assert value['goals'][0]['basis_annotations']==after['basis_annotations']
    with store.read() as conn:
        assert conn.execute('SELECT memory_revision FROM goal_memories WHERE goal_id=?', (goal['id'],)).fetchone()[0]==1
        assert json.loads(conn.execute("SELECT details_json FROM admin_operations WHERE action='goal_basis_review'").fetchone()[0])['annotation_ids']==[mark['id']]


def test_clear_suppresses_same_observation_but_new_revision_reopens(store, goals, clock):
    goal, mid = backed(store, goals)
    assert edit_memory(store, mid, 1, content='新的承诺措辞')
    review(store, clock)
    current = goals.get(goal['id'])
    mark = current['basis_annotations'][0]
    with pytest.raises(GoalConflict):
        goals.clear_basis_annotation(goal['id'], mark['id'], expected_revision=goal['revision'])
    cleared = goals.clear_basis_annotation(goal['id'], mark['id'], expected_revision=current['revision'], reason='已经与本人核对')
    assert not cleared['basis_needs_review']
    assert review(store, clock)['changed_goal_ids']==[]
    assert not goals.get(goal['id'])['basis_needs_review']
    assert edit_memory(store, mid, 2, content='又一次修订承诺')
    review(store, clock)
    again = goals.get(goal['id'])
    assert again['basis_annotations'][0]['id']!=mark['id']
    assert again['basis_annotations'][0]['observed_revision']==3
    changes = history(store, goal['id'])['items']
    assert next(r for r in changes if r['action']=='basis_clear')['reason']=='已经与本人核对'
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_operations WHERE action='goal_basis_clear'").fetchone()[0]==1


def test_restore_resolves_lifecycle_only_but_keeps_modified_revision_warning(store, goals, clock):
    goal, mid = backed(store, goals)
    adjust_retention(store, mid, value=1)
    review(store, clock)
    adjust_retention(store, mid, value=80)
    assert review(store, clock)['ended_annotation_ids']
    assert not goals.get(goal['id'])['basis_needs_review']
    assert edit_memory(store, mid, 1, content='修订后的承诺')
    adjust_retention(store, mid, value=1)
    review(store, clock)
    adjust_retention(store, mid, value=80)
    review(store, clock)
    active = goals.get(goal['id'])['basis_annotations']
    assert len(active)==1 and active[0]['changes']==['revision_changed']


@pytest.mark.parametrize('origin,state', [('host','open'),('admin','open'),('internal','completed'),('internal','abandoned')])
def test_review_skips_external_and_closed_goals(store, goals, clock, origin, state):
    goal, mid = backed(store, goals, origin=origin)
    if state!='open': goals.update(goal['id'], state=state)
    assert delete_memory(store, mid, 1)
    assert review(store, clock)['checked']==0
    assert not goals.get(goal['id'])['basis_needs_review']


def test_questions_are_reviewed_and_missing_memory_is_explicit(store, goals, clock):
    goal, mid = backed(store, goals, kind='question')
    # Simulate a legacy missing basis; current purge retains its placeholder.
    store._writer.execute('PRAGMA foreign_keys=OFF')
    with store.write() as conn:
        conn.execute('DELETE FROM memories WHERE id=?', (mid,))
    store._writer.execute('PRAGMA foreign_keys=ON')
    review(store, clock)
    detail = goals.get(goal['id'])
    assert detail['basis_annotations'][0]['changes']==['missing']
    assert detail['promise_memories'][0]['missing'] is True
    assert detail['deadline'] is None and detail['state']=='open'


def test_basis_review_and_history_rollback_together(store, goals, clock):
    goal, mid = backed(store, goals)
    delete_memory(store, mid, 1)
    before = history(store, goal['id'])
    with pytest.raises(RuntimeError):
        with store.write() as conn:
            module.review_goal_basis(conn, clock(), goal_id=goal['id'])
            raise RuntimeError('rollback')
    assert not goals.get(goal['id'])['basis_needs_review']
    assert history(store, goal['id'])==before


def test_small_transactions_paginate_and_skip_a_concurrent_goal_edit(store, goals, clock, monkeypatch):
    first, mid = backed(store, goals)
    second = goals.create(content='另一项工作', origin='internal')['goal']
    with store.write() as conn:
        conn.execute('INSERT INTO goal_memories VALUES(?,?,1)', (second['id'], mid))
    delete_memory(store, mid, 1)
    original = store.write
    calls=[]
    @contextmanager
    def interleave():
        if not calls:
            with original() as conn:
                conn.execute('UPDATE goals SET revision=revision+1 WHERE id=?', (first['id'],))
        calls.append(True)
        with original() as conn: yield conn
    monkeypatch.setattr(store, 'write', interleave)
    page = module.review_goal_basis_items(store, clock(), limit=1)
    assert page['conflicts']==[first['id']] and page['has_more']
    tail = module.review_goal_basis_items(store, clock(), after_id=page['next_after_id'], limit=1)
    assert tail['checked']==1 and tail['changed_goal_ids']==[second['id']] and not tail['has_more']
    assert len(calls)==2


def test_snapshot_deltas_actor_reason_noops_and_revision_conflict(store, goals, clock):
    goal = goals.create(content='修订目标正文')['goal']
    first = history(store, goal['id'])
    assert first['history_status']=='complete' and first['total']==1
    assert first['items'][0]['before']=={} and first['items'][0]['after']['content']==goal['content']
    goals.update(goal['id'], expected_revision=1, reminder_minutes=20, actor='admin', reason='提前通知')
    item = history(store, goal['id'])['items'][0]
    assert item['before']=={'reminder_minutes':None} and item['after']=={'reminder_minutes':20}
    assert (item['revision_before'],item['revision_after'],item['actor'],item['reason'])==(1,2,'admin','提前通知')
    assert item['created_at'].endswith('+08:00')
    goals.update(goal['id'], expected_revision=2, reminder_minutes=20)
    with pytest.raises(GoalConflict): goals.update(goal['id'], expected_revision=1, state='completed')
    assert history(store, goal['id'])['total']==2
    goals.update(goal['id'], expected_revision=2, content='新的目标正文', deadline='2026-10-12', state='completed')
    item = history(store, goal['id'])['items'][0]
    assert item['before']['state']=='open' and item['after']['state']=='completed'
    assert item['before']['content']=='修订目标正文'
    assert 'receipt_json' not in str(history(store, goal['id']))


def test_dismiss_and_merge_snapshot_both_sides_and_inherited_basis(store, goals, clock):
    first, mid = backed(store, goals, origin='host')
    second = goals.create(content=first['content'], origin='admin')['goal']
    goals.dismiss_duplicate(first['id'], second['id'], expected_revision=goals.get(first['id'])['revision'], other_revision=second['revision'], reason='不是同一件事')
    for gid in (first['id'], second['id']):
        assert history(store, gid)['items'][0]['action']=='duplicate_dismiss'
        assert history(store, gid)['items'][0]['reason']=='不是同一件事'
    third = goals.create(content=first['content'], origin='internal')['submitted_id']
    assert goals.get(third)['merged_into']==first['id']
    for gid in (first['id'], third):
        assert history(store, gid)['items'][0]['action']=='merge'
    delete_memory(store, mid, 1)
    assert review(store, clock)['changed_goal_ids']==[first['id']]


def test_migration_marks_old_history_without_inventing_snapshots(tmp_path):
    path=tmp_path/'legacy.db'
    with sqlite3.connect(path) as conn:
        conn.create_function('iris_terms',1,segmented)
        conn.execute('CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL)')
        for script in sorted((Path(__file__).parents[1]/'src/iris/migrations').glob('*.sql')):
            if script.name>='018': break
            conn.executescript(script.read_text())
            conn.execute("INSERT INTO schema_migrations VALUES(?,'2026-10-09')", (script.name,))
        conn.execute("INSERT INTO goals(content,kind,created_at,revision) VALUES('旧目标','normal','2026-10-09',7)")
    with closing(Store(path)) as upgraded:
        page=history(upgraded,1)
        assert page['total']==0 and page['history_status']=='pre_migration_no_snapshot'
        assert page['missing_through_revision']==7 and page['notice']=='迁移前无快照'
        Goals(upgraded).update(1,reminder_minutes=5)
        assert history(upgraded,1)['items'][0]['revision_before']==7
        assert history(upgraded,1)['history_status']=='pre_migration_no_snapshot'


def test_merge_copies_cleared_basis_receipts_and_preserves_redirect_history(store, goals, clock):
    # Create an earlier root, and two later internal goals with a recorded basis.
    root=goals.create(content='最早的安排',origin='admin')['goal']
    clock.advance(minutes=1)
    first,mid=backed(store,goals)
    child=goals.create(content=first['content'],origin='internal')['submitted_id']
    edit_memory(store,mid,1,content='依据改过了')
    review(store,clock)
    current=goals.get(first['id'])
    goals.clear_basis_annotation(first['id'],current['basis_annotations'][0]['id'],expected_revision=current['revision'])
    # An explicit compatible pair allows testing the merge plumbing, not judging.
    with store.write() as conn:
        module._possible(conn,root['id'],first['id'],clock(),actor='admin')
    merged=goals.merge(root['id'],first['id'],expected_revision=goals.get(root['id'])['revision'],
        other_revision=goals.get(first['id'])['revision'],reason='管理员明确确认')
    assert merged['id']==root['id'] and not merged['basis_needs_review']
    assert review(store,clock)['changed_goal_ids']==[]
    assert goals.get(child)['merged_into']==root['id']
    latest=history(store,child)['items'][0]
    assert latest['before']['merged_into']==first['id'] and latest['after']['merged_into']==root['id']
    assert latest['revision_after']==latest['revision_before']+1
    edit_memory(store,mid,2,content='依据再次修改')
    assert review(store,clock)['changed_goal_ids']==[root['id']]


def test_merge_copies_active_warning_and_records_all_affected_pairs(store, goals, clock):
    first=goals.create(content='最早的安排',origin='admin')['goal']
    clock.advance(minutes=1)
    other,mid=backed(store,goals)
    third=goals.create(content='需稍后处理的安排',origin='admin')['goal']
    edit_memory(store,mid,1,content='依据已变化')
    review(store,clock)
    with store.write() as conn:
        module._possible(conn,first['id'],other['id'],clock())
        module._possible(conn,other['id'],third['id'],clock())
    result=goals.merge(first['id'],other['id'],expected_revision=goals.get(first['id'])['revision'],
                      other_revision=goals.get(other['id'])['revision'])
    assert result['basis_needs_review'] and result['possible_duplicate_ids']==[third['id']]
    record=history(store,third['id'])['items'][0]
    assert record['action']=='merge'
    assert record['before']['duplicates']=={str(other['id']):'possible'}
    assert record['after']['duplicates']=={str(first['id']):'possible',str(other['id']):'merged'}


def test_snapshot_failure_rolls_back_whole_host_create(store,goals):
    with store.write() as conn:
        conn.execute("CREATE TRIGGER reject_goal_history BEFORE INSERT ON goal_revisions BEGIN SELECT RAISE(ABORT,'test history failure'); END")
    with pytest.raises(sqlite3.IntegrityError): goals.create(content='不得只写目标',host_key='atomic-goal')
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM goals').fetchone()[0]==0
        assert conn.execute("SELECT COUNT(*) FROM admin_operations WHERE object_type='goal'").fetchone()[0]==0


def test_learning_transaction_snapshots_and_basis_are_committed_together(store,goals,clock):
    goal,mid=backed(store,goals)
    with store.write() as conn:
        # Keep the real learning entry point and the original write transaction.
        receipt=module.write_learning_goal(conn,content='另一项独立承诺',kind='normal',deadline=None,
            entry_id='A',evidence=[r[0] for r in conn.execute('SELECT message_id FROM goal_sources WHERE goal_id=?',(goal['id'],))],current=clock())
    page=history(store,receipt['submitted_id'])
    assert page['items'][-1]['actor']=='learning'
    assert page['items'][-1]['after']['promise_memories']==[{'memory_id':mid,'memory_revision':1}]


def test_review_does_not_read_revision_history_or_modify_memories(store,goals,clock):
    goal,mid=backed(store,goals)
    edit_memory(store,mid,1,content='改后的依据')
    statements=[]
    with store.write() as conn:
        before=dict(conn.execute('SELECT * FROM memories WHERE id=?',(mid,)).fetchone())
        conn.set_trace_callback(statements.append)
        module.review_goal_basis(conn,clock(),goal_id=goal['id'])
        conn.set_trace_callback(None)
        assert dict(conn.execute('SELECT * FROM memories WHERE id=?',(mid,)).fetchone())==before
    assert not any(s.lstrip().upper().startswith('SELECT') and any(t in s for t in ('memory_revisions','goal_revisions','admin_operations')) for s in statements)


def test_clear_wrong_goal_or_inactive_mark_does_not_write_history(store,goals,clock):
    goal,mid=backed(store,goals)
    adjust_retention(store,mid,value=1)
    review(store,clock)
    current=goals.get(goal['id'])
    aid=current['basis_annotations'][0]['id']
    other=goals.create(content='另一件独立的事')['goal']
    with pytest.raises(KeyError): goals.clear_basis_annotation(other['id'],aid,expected_revision=other['revision'])
    adjust_retention(store,mid,value=80)
    review(store,clock)
    current=goals.get(goal['id'])
    before=history(store,goal['id'])
    with pytest.raises(GoalConflict): goals.clear_basis_annotation(goal['id'],aid,expected_revision=current['revision'])
    assert history(store,goal['id'])==before


@pytest.mark.parametrize('limit,after',[(0,0),(1001,0),(True,0),(1,-1),(1,True)])
def test_small_transaction_parameters_are_bounded(store,clock,limit,after):
    with pytest.raises(ValueError): module.review_goal_basis_items(store,clock(),limit=limit,after_id=after)
