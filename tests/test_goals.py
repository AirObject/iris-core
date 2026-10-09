from datetime import datetime, timedelta, timezone

import pytest

from iris.goals import (Goals, GoalConflict, GoalError, GoalMerged, decide_dedup,
                        goal_list, notification_list)


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def goals(store, clock):
    store.set_setting('goal_dedup_judge', {'enabled': False})
    return Goals(store, clock=clock)


def create(goals, content='周五提交项目报告', **fields):
    return goals.create(content=content, **fields)['goal']


def due(clock, **delta):
    return (clock() + timedelta(**delta)).isoformat()


def test_S10_multiple_goals_and_questions_remain_open(goals, store):
    a = create(goals)
    b = create(goals, '问问小林的新地址', kind='question')
    with store.read() as conn:
        result = goal_list(conn, state='open')
    assert {g['id'] for g in result['items']} == {a['id'], b['id']}
    assert result['total'] == 2
    assert b['deadline'] is None and b['reminder_minutes'] is None


def test_S11_save_before_dedup_and_host_key_receipt_is_durable(goals, store, clock):
    first = goals.create(content='整理报告', host_key='host-1')
    second = goals.create(content='整理报告。', host_key='host-2')
    assert second['submitted_id'] != first['submitted_id']
    assert second['dedup'] == {'status': 'merged', 'target_id': first['goal']['id']}
    assert second['goal']['id'] == first['goal']['id']
    with store.read() as conn:
        assert conn.execute('SELECT merged_into FROM goals WHERE id=?', (second['submitted_id'],)).fetchone()[0] == first['goal']['id']
    goals.update(first['goal']['id'], state='completed')
    assert Goals(store, clock=clock).create(content='different retry body', host_key='host-2') == second


def test_cross_entry_dedup_keeps_earlier_and_host_deadline(goals, store, clock):
    with store.write() as conn:
        conn.executemany("INSERT INTO entries(id,name,platform,kind) VALUES(?,?,'test','group')", [('A','A'),('B','B')])
    a = create(goals, entry_id='A')
    clock.advance(minutes=1)
    b = goals.create(content=a['content'], entry_id='B', deadline=due(clock, hours=2))
    assert b['dedup'] == {'status': 'merged', 'target_id': a['id']}
    assert b['goal']['entry_id'] == 'A'
    assert b['goal']['deadline'] is not None
    assert len(goals.get(a['id'])['merged_goals']) == 1


@pytest.mark.parametrize('field,value', [('content','周五提交三份报告'),('content','周五不提交两份报告'),
                                        ('deadline','2026-10-11T10:00:00+08:00'),('people',['b'])])
def test_S12_incompatible_numbers_negation_people_deadline_do_not_merge(field, value):
    existing = dict(id=1, content='周五提交两份报告', kind='normal', people=['a'],
                    deadline='2026-10-10T10:00:00+08:00', state='open', created_at='2026-10-01', merged_into=None)
    proposed = {**existing, 'id': 2, field: value}
    assert decide_dedup([existing], proposed)['status'] == 'created'


def test_dedup_is_replaceable_conservative_decision():
    old = dict(id=1, content='周五问小林面试结果', kind='normal', people=['a'], deadline=None, state='open')
    new = {**old, 'id':2, 'content':'周五问小林的面试结果'}
    assert decide_dedup([old], new) == {'status':'possible_duplicate', 'target_id':1}
    assert decide_dedup([{**old, 'state':'completed'}], old)['status'] == 'created'
    assert decide_dedup([{**old, 'kind':'question'}], old)['status'] == 'created'


def test_decimal_punctuation_does_not_auto_merge():
    old = dict(id=1,content='准备3.5份材料',kind='normal',people=[],deadline=None,state='open')
    assert decide_dedup([old], {**old,'id':2,'content':'准备35份材料'})['status'] != 'merged'


def test_revisions_and_merged_ids_are_explicit(goals):
    a = create(goals)
    with pytest.raises(GoalConflict):
        goals.update(a['id'], expected_revision=a['revision'] + 1, state='completed')
    changed = goals.update(a['id'], expected_revision=a['revision'], reminder_minutes=30)
    assert changed['revision'] == a['revision'] + 1
    b = goals.create(content=a['content'])
    with pytest.raises(GoalMerged) as caught:
        goals.update(b['submitted_id'], state='completed')
    assert caught.value.canonical_id == a['id']


@pytest.mark.parametrize('state', ['completed', 'abandoned'])
def test_S15_closing_cancels_untaken_but_preserves_taken(goals, clock, store, state):
    a = create(goals, deadline=due(clock, minutes=30))
    taken = goals.pull()['items'][0]
    clock.advance(minutes=30)
    goals.generate_notifications()
    goals.update(a['id'], state=state, actor='host:demo')
    assert goals.pull(after=taken['id'])['items'] == []
    detail = goals.get(a['id'])
    assert detail['state'] == state and detail['closed_by'] == 'host:demo'
    assert detail['closed_at'] is not None
    assert {n['status'] for n in detail['notifications']} == {'taken', 'cancelled'}
    clock.advance(days=2)
    assert goals.generate_notifications() == 0


def test_S13_individual_lead_and_due_publications(goals, clock):
    a = create(goals, '交项目甲报告', deadline=due(clock, hours=3), reminder_minutes=120)
    b = create(goals, '交项目乙报告', deadline=due(clock, hours=3), reminder_minutes=30)
    assert goals.pull()['items'] == []
    clock.advance(hours=1)
    assert goals.generate_notifications() == 1
    first = goals.pull()
    assert [(n['goal_id'],n['reminder_kind']) for n in first['items']] == [(a['id'],'soon')]
    clock.advance(minutes=90)
    goals.generate_notifications()
    second = goals.pull(after=first['next_cursor'])
    assert [(n['goal_id'],n['reminder_kind']) for n in second['items']] == [(b['id'],'soon')]
    clock.advance(minutes=30)
    assert goals.generate_notifications() == 2
    assert {n['reminder_kind'] for n in goals.pull(after=second['next_cursor'])['items']} == {'due'}


def test_S14_overdue_daily_never_ends_or_postpones_and_can_disable(goals, clock):
    a = create(goals, deadline=due(clock, hours=-1))
    first = goals.pull()
    assert len(first['items']) == 1 and first['items'][0]['reminder_kind'] == 'immediate'
    clock.advance(hours=2)
    assert goals.generate_notifications() == 0
    clock.advance(days=3)
    assert goals.generate_notifications() == 1
    overdue = goals.pull(after=first['next_cursor'])
    assert overdue['items'][0]['reminder_kind'] == 'overdue'
    assert '放弃或修改截止时间' in overdue['items'][0]['content']
    assert goals.generate_notifications() == 0
    current = goals.get(a['id'])
    assert current['state'] == 'open' and current['deadline'] == a['deadline'] and current['overdue']
    goals.configure(overdue_reminders=False)
    clock.advance(days=3)
    assert goals.generate_notifications() == 0


@pytest.mark.parametrize('minutes', [-120, -1, 0, 30, 60])
def test_creation_missed_stages_emit_only_one_immediate(goals, clock, minutes):
    a = create(goals, deadline=due(clock, minutes=minutes))
    notifications = goals.pull()['items']
    assert len(notifications) == 1
    assert notifications[0]['reminder_kind'] == 'immediate'
    assert notifications[0]['goal_id'] == a['id']
    assert goals.generate_notifications() == 0


def test_restart_missed_stages_collapse_to_one_immediate(goals, clock, store):
    create(goals, deadline=due(clock, hours=2))
    clock.advance(days=4)
    restarted = Goals(store, clock=clock)
    assert restarted.generate_notifications() == 1
    assert restarted.pull()['items'][0]['reminder_kind'] == 'immediate'
    assert restarted.generate_notifications() == 0


def test_cursor_uses_publication_order_not_plan_creation(goals, clock):
    later = create(goals, '晚到期的任务', deadline=due(clock, hours=5))
    sooner = create(goals, '先到期的任务', deadline=due(clock, hours=2))
    clock.advance(hours=1)
    goals.generate_notifications()
    first = goals.pull()
    assert [n['goal_id'] for n in first['items']] == [sooner['id']]
    clock.advance(hours=3)
    goals.generate_notifications()
    second = goals.pull(after=first['next_cursor'])
    assert later['id'] in {n['goal_id'] for n in second['items']}
    assert all(n['id'] > first['next_cursor'] for n in second['items'])


def test_pagination_and_retry_cursor_preserve_notifications(goals, clock):
    for content in ['整理红色文件','检查会议室设备','购买火车票']:
        create(goals, content, deadline=due(clock, minutes=1))
    first = goals.pull(limit=2)
    assert len(first['items']) == 2 and first['has_more']
    # Cursor replay can recover a lost response; taking is not delivery acknowledgement.
    assert goals.pull(limit=2)['items'] == first['items']
    second = goals.pull(after=first['next_cursor'],limit=2)
    assert len(second['items']) == 1 and not second['has_more']
    assert goals.pull(after=second['next_cursor'])['next_cursor'] == second['next_cursor']


def test_deadline_lead_changes_cancel_and_replace_untaken(goals, clock):
    a = create(goals, deadline=due(clock, minutes=20))
    old = goals.get(a['id'])['notifications'][0]
    changed = goals.update(a['id'],deadline=due(clock,hours=3),reminder_minutes=30)
    assert changed['revision'] == a['revision'] + 1
    assert goals.pull()['items'] == []
    assert goals.get(a['id'])['notifications'][0]['status'] == 'cancelled'
    clock.advance(hours=2,minutes=30)
    goals.generate_notifications()
    assert goals.get(a['id'])['notifications'][-1]['reminder_kind'] == 'soon'
    goals.update(a['id'],deadline=None)
    assert goals.pull(after=old['id'])['items'] == []


def test_defaults_change_only_default_leads_and_no_repeat_overdue(goals, clock):
    a = create(goals, '默认提前量任务', deadline=due(clock,hours=2))
    b = create(goals, '自定提前量任务', deadline=due(clock,hours=2),reminder_minutes=10)
    goals.configure(default_reminder_minutes=120)
    notes = goals.pull()['items']
    assert [n['goal_id'] for n in notes] == [a['id']]
    assert goals.get(b['id'])['reminder_minutes'] == 10


def test_question_rejects_deadline_or_reminder(goals, clock):
    with pytest.raises(GoalError):
        create(goals,kind='question',deadline=due(clock,hours=1))
    with pytest.raises(GoalError):
        create(goals,kind='question',reminder_minutes=0)


def test_timezone_date_deadline_end_of_day(goals, store):
    store.set_setting('timezone','America/New_York')
    a = create(goals,deadline='2026-10-10')
    assert a['deadline'] == '2026-10-10T23:59:59-04:00'
    assert a['created_at'].endswith('-04:00')


def test_possible_duplicate_admin_merge_and_dismiss(goals):
    a = create(goals,'周五问小林面试结果')
    receipt = goals.create(content='周五问小林的面试结果')
    b = receipt['goal']
    assert receipt['dedup']['status'] == 'possible_duplicate'
    assert goals.get(a['id'])['possible_duplicate']
    with pytest.raises(GoalConflict):
        goals.dismiss_duplicate(a['id'],b['id'],expected_revision=999,other_revision=b['revision'])
    current_a, current_b = goals.get(a['id']), goals.get(b['id'])
    goals.dismiss_duplicate(a['id'],b['id'],expected_revision=current_a['revision'],other_revision=current_b['revision'])
    assert not goals.get(a['id'])['possible_duplicate']
    assert not goals.get(b['id'])['possible_duplicate']


def test_merge_migrates_sources_and_untaken_reminders(goals, store, clock):
    from conftest import msg
    first = msg(store,1,'我会提交面试报告')
    second = msg(store,2,'记得问小林的面试结果')
    a = create(goals,'周五问小林面试结果')
    b = create(goals,'周五问小林的面试结果',deadline=due(clock,minutes=30))
    note = goals.get(b['id'])['notifications'][0]
    with store.write() as conn:
        conn.executemany('INSERT INTO goal_sources(goal_id,message_id) VALUES(?,?)', [(a['id'],first),(b['id'],second)])
    a,b=goals.get(a['id']),goals.get(b['id'])
    merged=goals.merge(b['id'],a['id'],expected_revision=b['revision'],other_revision=a['revision'])
    assert merged['id'] == a['id'] and merged['deadline'] == b['deadline']
    detail=goals.get(a['id'])
    assert {m['id'] for m in detail['sources']} == {first,second}
    notes=goals.pull()['items']
    assert len(notes)==1 and notes[0]['id']==note['id'] and notes[0]['goal_id']==a['id']


def test_notification_admin_list_is_read_only(goals, clock, store):
    a=create(goals,deadline=due(clock,minutes=10))
    with store.read() as conn:
        one=notification_list(conn,status='pending',goal_id=a['id'])
        two=notification_list(conn,status='pending',goal_id=a['id'])
    assert one==two and one['total']==1
    assert one['items'][0]['taken_at'] is None


def test_dedup_preserves_english_word_boundaries():
    old = dict(id=1,content='Meet Ann A',kind='normal',people=[],deadline=None,state='open')
    assert decide_dedup([old],{**old,'id':2,'content':'Meet Anna'})['status'] != 'merged'


def test_zero_lead_coalesces_simultaneous_phases(goals, clock):
    create(goals,deadline=due(clock,hours=1),reminder_minutes=0)
    clock.advance(hours=1)
    assert goals.generate_notifications()==1
    assert len(goals.pull()['items'])==1


def test_merge_keeps_only_one_pending_notification_per_stage(goals,clock):
    a=create(goals,'周五问小林面试结果',deadline=due(clock,minutes=30))
    clock.advance(minutes=1)
    b=create(goals,'周五问小林的面试结果',deadline=a['deadline'])
    a,b=goals.get(a['id']),goals.get(b['id'])
    goals.merge(a['id'],b['id'],expected_revision=a['revision'],other_revision=b['revision'])
    assert len(goals.pull()['items'])==1
    assert len(goals.get(a['id'])['notifications'])==2


def test_explicit_leads_merge_using_earlier_warning(goals,clock):
    a=create(goals,deadline=due(clock,hours=5),reminder_minutes=30)
    receipt=goals.create(content=a['content'],deadline=a['deadline'],reminder_minutes=120)
    assert receipt['goal']['reminder_minutes']==120
    clock.advance(hours=3)
    assert goals.generate_notifications()==1


def test_failed_merge_or_revision_conflict_is_atomic(goals,store):
    a=create(goals,'周五问小林面试结果')
    b=create(goals,'周五问小林的面试结果')
    before=goals.get(a['id'])
    with pytest.raises(GoalConflict):
        goals.merge(a['id'],b['id'],expected_revision=before['revision'],other_revision=999)
    assert goals.get(a['id'])==before
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM goals WHERE merged_into IS NOT NULL').fetchone()[0]==0


def test_people_merge_is_resolved_for_goal_dedup(goals,store):
    stamp='2026-10-09T00:00:00+00:00'
    with store.write() as conn:
        conn.executemany("INSERT INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)",[('a','小林',stamp),('b','林老师',stamp)])
    a=create(goals,people=['a'])
    with store.write() as conn:
        conn.execute("UPDATE subjects SET merged_into='b' WHERE id='a'")
    assert goals.get(a['id'])['people']==['b']
    assert goals.create(content=a['content'],people=['b'])['dedup']['status']=='merged'


def test_partition_priority_limit_and_overdue_flags(goals,store,clock):
    from iris.goals import goal_partition
    with store.write() as conn:
        conn.executemany("INSERT INTO entries(id,name,platform,kind) VALUES(?,?,'test','group')",[('A','A'),('B','B')])
        conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES('p','person','林老师',?)",(clock().isoformat(),))
    ordinary=create(goals,'月底整理藏书')
    involved=create(goals,'和林老师去博物馆',people=['p'],entry_id='B')
    local=create(goals,'核对交通费',entry_id='A')
    soon=create(goals,'交实验报告',entry_id='B',deadline=due(clock,minutes=30))
    overdue=create(goals,'申请返校证明',entry_id='B',deadline=due(clock,hours=-2))
    with store.read() as conn:
        items=goal_partition(conn,entry_id='A',participants=['p'],current=clock(),limit=10)
    assert [g['id'] for g in items]==[overdue['id'],soon['id'],local['id'],involved['id'],ordinary['id']]
    assert items[0]['overdue'] and items[0]['due_soon']
    assert not items[1]['overdue'] and items[1]['due_soon']


def test_migration_normalizes_legacy_states_and_preserves_ids(tmp_path,clock):
    import sqlite3
    from pathlib import Path
    from iris.db import Store
    from iris.search_text import segmented
    path=tmp_path/'old-goals.db'
    migrations=Path(__file__).parents[1]/'src/iris/migrations'
    legacy=['open','in_progress','pending','done','completed','cancelled','canceled','abandoned','failed','unknown']
    with sqlite3.connect(path) as conn:
        conn.create_function('iris_terms',1,segmented)
        conn.execute('CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL)')
        for script in sorted(migrations.glob('*.sql')):
            if script.name >= '012':
                break
            conn.executescript(script.read_text())
            conn.execute('INSERT INTO schema_migrations VALUES(?,?)',(script.name,clock().isoformat()))
        for state in legacy:
            conn.execute("INSERT INTO goals(content,kind,state,origin,created_at) VALUES(?,'normal',?,'legacy',?)",(state,state,clock().isoformat()))
        conn.execute("INSERT INTO goals(content,kind,deadline,created_at) VALUES('询问','question','2026-10-10',?)",(clock().isoformat(),))
    store=Store(path)
    try:
        with store.read() as conn:
            rows=conn.execute('SELECT * FROM goals ORDER BY id').fetchall()
            assert [r['state'] for r in rows[:-1]]==['open','open','open','completed','completed','abandoned','abandoned','abandoned','open','open']
            assert [r['id'] for r in rows]==list(range(1,12))
            assert all(r['origin']=='internal' for r in rows)
            assert rows[-1]['deadline'] is None
            assert all(r['closed_at'] is None for r in rows)  # Unknown legacy close time is not invented.
            assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            with store.write() as conn:
                conn.execute("UPDATE goals SET state='failed' WHERE id=1")
        Goals(store,clock=clock).generate_notifications()
    finally:
        store.close()



def test_restart_with_only_one_missed_phase_is_immediate(goals,clock,store):
    create(goals,deadline=due(clock,hours=2))
    goals.generate_notifications()  # Running service's initial idle tick.
    clock.advance(hours=1)
    goals.generate_notifications()
    first=goals.pull()
    clock.advance(hours=3)
    restarted=Goals(store,clock=clock)
    assert restarted.generate_notifications()==1
    assert restarted.pull(after=first['next_cursor'])['items'][0]['reminder_kind']=='immediate'


def test_old_taken_reminder_does_not_suppress_a_new_adopted_deadline(goals,clock):
    a=create(goals,deadline=due(clock,minutes=30))
    first=goals.pull()
    goals.update(a['id'],deadline=None)
    clock.advance(hours=2)
    receipt=goals.create(content=a['content'],deadline=due(clock,minutes=-20))
    assert receipt['dedup']['status']=='merged'
    assert len(goals.pull(after=first['next_cursor'])['items'])==1


def test_merge_does_not_repeat_source_taken_reminder(goals,clock):
    a=create(goals,'周五问小林面试结果')
    b=create(goals,'周五问小林的面试结果',deadline=due(clock,minutes=30))
    first=goals.pull()
    a,b=goals.get(a['id']),goals.get(b['id'])
    goals.merge(a['id'],b['id'],expected_revision=a['revision'],other_revision=b['revision'])
    assert goals.pull(after=first['next_cursor'])['items']==[]


def test_taken_soon_does_not_suppress_due_on_merge(goals,clock):
    a=create(goals,deadline=due(clock,minutes=30))
    first=goals.pull()
    clock.advance(hours=1)
    goals.create(content=a['content'],deadline=a['deadline'])
    assert len(goals.pull(after=first['next_cursor'])['items'])==1


def test_unrepresentable_date_filter_is_field_error(store,clock):
    with store.read() as conn:
        with pytest.raises(GoalError) as caught:
            goal_list(conn,deadline_from='0001-01-01',current=clock())
    assert caught.value.field=='deadline_from'


@pytest.mark.parametrize('origin,expected', [('admin', 'possible_duplicate'), ('host', 'merged'), ('internal', 'merged')])
@pytest.mark.parametrize('kind', ['normal', 'question'])
def test_creation_origin_controls_automatic_merge(goals, origin, expected, kind):
    original = goals.create(content='确认展览开放时间', kind=kind, origin='admin')['goal']
    receipt = goals.create(content=original['content'], kind=kind, origin=origin)
    assert receipt['dedup'] == {'status': expected, 'target_id': original['id']}
    if origin == 'admin':
        assert receipt['goal']['id'] == receipt['submitted_id'] != original['id']
        assert goals.get(original['id'])['merged_into'] is None
        assert goals.get(receipt['submitted_id'])['merged_into'] is None
        assert receipt['goal']['possible_duplicate_ids'] == [original['id']]
    else:
        assert receipt['goal']['id'] == original['id']
        assert goals.get(receipt['submitted_id'])['merged_into'] == original['id']


def test_partition_selects_only_requested_rows_before_people_projection(goals, store, clock, monkeypatch):
    import iris.goals as module
    with store.write() as conn:
        conn.executemany("INSERT INTO goals(content,kind,state,created_at,updated_at) VALUES(?,'normal','open',?,?)",
                         [(f'合成性能目标 {i}', clock().isoformat(), clock().isoformat()) for i in range(1000)])
    people_batches, projections = [], []
    original_people, original_project = module._people, module._project
    def people(conn, ids):
        people_batches.append(list(ids))
        return original_people(conn, ids)
    def project(conn, row, **kwargs):
        projections.append(row['id'])
        return original_project(conn, row, **kwargs)
    monkeypatch.setattr(module, '_people', people)
    monkeypatch.setattr(module, '_project', project)
    with store.read() as conn:
        rows = module.goal_partition(conn, current=clock(), limit=10)
    assert len(rows) == 10
    assert people_batches == [[r['id'] for r in rows]]
    assert projections == [r['id'] for r in rows]


def test_partition_ranking_resolves_merged_people_and_legacy_deadlines(goals, store, clock):
    from iris.goals import goal_partition
    store.set_setting('timezone', 'America/New_York')
    stamp = clock().isoformat()
    with store.write() as conn:
        conn.executemany("INSERT INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)",
                         [('old', '旧称', stamp), ('middle', '中间称', stamp), ('person', '新称', stamp)])
        conn.execute("UPDATE subjects SET merged_into='middle' WHERE id='old'")
        conn.execute("UPDATE subjects SET merged_into='person' WHERE id='middle'")
        conn.execute("INSERT INTO goals(content,kind,created_at,updated_at) VALUES('普通目标','normal',?,?)", (stamp,stamp))
        involved = conn.execute("INSERT INTO goals(content,kind,created_at,updated_at) VALUES('涉及旧主体','normal',?,?)", (stamp,stamp)).lastrowid
        conn.execute("INSERT INTO goal_people VALUES(?,'old')", (involved,))
        legacy = conn.execute("INSERT INTO goals(content,kind,deadline,created_at,updated_at) VALUES('旧日期期限','normal','2026-10-08',?,?)", (stamp,stamp)).lastrowid
        conn.execute("INSERT INTO goals(content,kind,deadline,created_at,updated_at) VALUES('未知期限','normal','某天',?,?)", (stamp,stamp))
    clock.advance(hours=4)
    with store.read() as conn:
        rows = goal_partition(conn, participants=['person', 'missing'], current=clock())
    assert [r['id'] for r in rows[:2]] == [legacy, involved]
    assert rows[0]['overdue'] and rows[1]['people'] == ['person']
    assert rows[-1]['deadline_unresolved']


@pytest.mark.parametrize('microseconds', [-1, 0, 1])
def test_partition_urgency_preserves_exact_lead_boundary(goals, store, clock, microseconds):
    from iris.goals import goal_partition
    with store.write() as conn:
        conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES('A','A','test','group')")
    local = create(goals, '本入口无期限', entry_id='A')
    boundary = create(goals, '恰好临近的目标', deadline=due(clock, hours=1, microseconds=1))
    clock.advance(microseconds=1 + microseconds)
    with store.read() as conn:
        rows = goal_partition(conn, entry_id='A', current=clock())
    assert rows[0]['id'] == (boundary['id'] if microseconds >= 0 else local['id'])
    assert next(r for r in rows if r['id'] == boundary['id'])['due_soon'] == (microseconds >= 0)
