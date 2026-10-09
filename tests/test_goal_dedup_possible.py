"""Hand-written C2 tests. No real model, endpoint configuration, or corpus labels."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin
from iris import goal_dedup_judge as dedup
from iris.api import create_app
from iris.goals import Goals, GoalError, write_learning_goal
from iris.models import ModelError
from iris.scheduler import Scheduler
from test_goal_dedup_apply import Judge
from test_goal_dedup_judge import goal
from test_scheduler import wait_for


@pytest.fixture
def setup(store):
    store.set_setting('goal_dedup_judge',{'enabled':True,'method':'C2','budget_seconds':1})
    clock=lambda:datetime(2026,10,10,tzinfo=timezone.utc)
    judge=Judge(store)
    judge.configs={}
    with store.write() as conn:
        conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES('A','A','unit','private')")
        conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES('B','B','unit','private')")
        conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES('lin','person','林',?)",(clock().isoformat(),))
        conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES('alias','person','阿林',?)",(clock().isoformat(),))
    return Goals(store,gateway=judge,clock=clock),judge


def source(store,sender='lin',entry='A'):
    with store.write() as conn:
        return conn.execute("""INSERT INTO messages(entry_id,kind,sender_subject_id,content,occurred_at,received_at,dedupe_key)
            VALUES(?,'message',?,'这件事', '2026-10-10T00:00:00+00:00','2026-10-10T00:00:00+00:00',lower(hex(randomblob(16))))""",
            (entry,sender)).lastrowid


def test_c2_keeps_c_model_method_and_current_product_default():
    assert dedup.candidate_method({'enabled':True,'method':'C2'})=='C2'
    assert dedup.method({'enabled':True,'method':'C2'})=='C'
    assert dedup.method(dedup.settings())=='C'
    assert dedup.candidate_method(dedup.settings())=='C2'
    assert dedup.candidate_method({'enabled':False,'method':'C2'})=='A'


@pytest.mark.parametrize('text',['周五20点检查服务器','不检查服务器','检查三台服务器'])
def test_guard_exclusions_become_possible_but_remain_incompatible(text):
    old,new=goal(1,'检查服务器'),goal(2,text)
    assert dedup.select_sequence_conflicts([old],new)==[old]
    assert dedup.select_candidates([old],new)==[]
    assert not dedup.compatible(old,new)


@pytest.mark.parametrize('extra',[{'people':['other']},{'deadline':'2026-10-12'},
    {'kind':'question'},{'state':'completed'},{'state':'abandoned'},{'merged_into':3}])
def test_c2_never_weakens_other_compatibility_guards(extra):
    old=goal(1,'检查两台服务器',deadline='2026-10-11',**extra) if 'deadline' not in extra else goal(1,'检查两台服务器',**extra)
    new=goal(2,'检查服务器',deadline='2026-10-11')
    assert dedup.select_sequence_conflicts([old],new)==[]


def test_similarity_or_same_entry_real_source_not_shared_people_alone():
    a,b=goal(1,'买3本书',entry_id='A'),goal(2,'检查服务器',entry_id='A')
    assert dedup.select_sequence_conflicts([a],b)==[]
    assert dedup.select_sequence_conflicts([a],b,source_speakers={1:{'lin'},2:{'lin'}})==[a]
    assert dedup.select_sequence_conflicts([a],{**b,'entry_id':'B'},source_speakers={1:{'lin'},2:{'lin'}})==[]
    assert dedup.select_sequence_conflicts([a],b,source_speakers={1:{'lin'},2:{'other'}})==[]
    assert dedup.select_sequence_conflicts([a],b,source_speakers={1:{'lin'},2:{'lin'}},dismissed={1})==[]
    assert dedup.select_sequence_conflicts([goal(1,'检查服务器')],b)==[]


def test_new_marker_limit_and_order_do_not_displace_model_candidates():
    new=goal(20,'检查服务器',people=[])
    rows=[goal(i,'检查三台服务器') for i in range(10,0,-1)]
    rows.append(goal(11,'检查服务器'))
    assert [r['id'] for r in dedup.select_sequence_conflicts(rows,new)]==list(range(1,9))
    assert [r['id'] for r in dedup.select_candidates(rows,new)]==[11]


@pytest.mark.parametrize('origin',['host','internal','admin'])
def test_numeric_only_match_saves_visible_mark_without_job_or_call(store,setup,origin):
    goals,judge=setup
    old=goals.create(content='周五20点检查服务器',entry_id='A')
    if origin=='internal':
        with store.write() as conn:
            result=write_learning_goal(conn,content='周五晚上检查服务器',kind='normal',deadline=None,
                entry_id='B',evidence=[],current=goals.clock())
            assert judge.calls==[]
    else:
        result=goals.create(content='周五晚上检查服务器',entry_id='B',origin=origin,host_key='unit-possible')
        assert goals.create(content='ignored retry',host_key='unit-possible')==result
    assert result['dedup']['status']=='possible_duplicate'
    assert result['dedup']['target_ids']==[old['submitted_id']]
    assert result['goal']['possible_duplicate_ids']==[old['submitted_id']]
    assert judge.calls==[] and not goals.pending_reviews()
    assert goals.get(result['submitted_id'])['dedup_review'] is None
    before=goals.get(result['submitted_id'])['revision']
    assert goals.review(result['submitted_id'])['dedup']['status']=='possible_duplicate'
    assert goals.get(result['submitted_id'])['revision']==before
    assert goals.get(old['submitted_id'])['possible_duplicate_ids']==[result['submitted_id']]
    with pytest.raises(GoalError):
        goals.merge(old['submitted_id'],result['submitted_id'],expected_revision=goals.get(old['submitted_id'])['revision'],other_revision=before)


@pytest.mark.parametrize('sender,source_entry,new_entry,expected',[
    ('alias','A','A',True),('self','A','A',False),('scene','A','A',False),
    ('alias','B','A',False),('alias','A','B',False),
])
def test_source_fallback_uses_canonical_nonself_speaker_in_goal_entry(store,setup,sender,source_entry,new_entry,expected):
    goals,judge=setup
    old=goals.create(content='买3本书',entry_id='A',evidence=[source(store,'lin','A')])
    evidence=source(store,sender,source_entry)
    if sender=='alias':
        with store.write() as conn:
            conn.execute("UPDATE subjects SET merged_into='lin' WHERE id='alias'")
    new=goals.create(content='检查服务器',entry_id=new_entry,evidence=[evidence])
    assert (new['dedup']['status']=='possible_duplicate')==expected
    assert new['goal']['possible_duplicate_ids']==([old['submitted_id']] if expected else [])
    assert judge.calls==[]


def mixed(store,goals):
    # Seed distinct historical goals without attempting to judge them at load time.
    with store.write() as conn:
        for text in ('检查3台服务器','检查服务器日志'):
            conn.execute("INSERT INTO goals(content,kind,created_at,updated_at,schedule_initialized) VALUES(?,'normal',?,?,1)",
                (text,goals.clock().isoformat(),goals.clock().isoformat()))
    return 1,2


@pytest.mark.parametrize('verdict',['same','different','uncertain'])
def test_markers_preserved_alongside_model_candidates_never_auto_merge(store,setup,verdict):
    goals,judge=setup
    blocked,ordinary=mixed(store,goals)
    judge.verdict=verdict
    new=goals.create(content='检查服务器')
    expected={blocked,ordinary} if verdict!='different' else {blocked}
    assert new['dedup']['status']=='possible_duplicate'
    assert set(new['goal']['possible_duplicate_ids'])==expected
    assert set(new['dedup']['target_ids'])==expected
    assert len(judge.calls)==1
    assert [row['id'] for row in judge.calls[0]['candidates']]==[ordinary]
    assert goals.get(new['submitted_id'])['dedup_review']['method']=='C2'
    assert goals.get(blocked)['merged_into'] is None
    assert goals.get(ordinary)['merged_into'] is None


def test_degradation_keeps_mark_and_scheduler_uses_c2_without_new_loop(store,setup):
    goals,judge=setup
    blocked,ordinary=mixed(store,goals)
    judge.verdict=ModelError('retryable','unit timeout',reason='timeout')
    with store.write() as conn:
        new=write_learning_goal(conn,content='检查服务器',kind='normal',deadline=None,
            entry_id='A',evidence=[],current=goals.clock())
    assert new['dedup']['status']=='pending' and new['goal']['possible_duplicate_ids']==[blocked]
    failed=goals.review(new['submitted_id'])
    assert failed['dedup']['status']=='pending' and failed['goal']['possible_duplicate_ids']==[blocked]
    judge.verdict='same'
    with store.write() as conn:
        conn.execute('UPDATE goal_dedup_jobs SET available_at=?',(goals.clock().isoformat(),))
    scheduler=Scheduler(store,judge,clock=goals.clock)
    try:
        scheduler.tick()
        wait_for(lambda:goals.get(new['submitted_id'])['dedup_review']['state']=='done')
    finally:
        scheduler.stop()
    assert goals.get(new['submitted_id'])['dedup_review']['method']=='C2'
    assert set(goals.get(new['submitted_id'])['possible_duplicate_ids'])=={blocked,ordinary}
    assert any(note['kind']=='goal_dedup_result' for note in goals.pull()['items'])


@pytest.mark.parametrize('change',['revision','source','method','dismiss'])
def test_inflight_c2_checks_blocked_candidates_and_method_before_write(store,setup,change):
    goals,judge=setup
    blocked,ordinary=mixed(store,goals)
    def mutate():
        gid=3
        if change=='method':
            store.set_setting('goal_dedup_judge',{'enabled':True,'method':'C','budget_seconds':1})
        elif change=='dismiss':
            goals.dismiss_duplicate(gid,blocked,expected_revision=goals.get(gid)['revision'],other_revision=goals.get(blocked)['revision'])
        else:
            mid=source(store)
            with store.write() as conn:
                if change=='revision':
                    conn.execute('UPDATE goals SET revision=revision+1 WHERE id=?',(blocked,))
                else:
                    conn.execute('INSERT INTO goal_sources(goal_id,message_id) VALUES(?,?)',(blocked,mid))
    judge.hook=mutate
    new=goals.create(content='检查服务器')
    assert new['dedup']['status']=='pending'
    assert new['dedup']['reason']==('configuration_changed' if change=='method' else 'stale')
    assert goals.get(ordinary)['merged_into'] is None
    judge.hook=None
    if change=='dismiss':
        # Explicit dismissal resolves this ambiguity; ordinary C judgment may merge.
        assert goals.review(new['submitted_id'])['dedup']['status']=='merged'
        assert goals.get(blocked)['possible_duplicate_ids']==[]


def test_http_host_and_admin_read_existing_possible_fields(store,setup):
    _,judge=setup
    with TestClient(create_app(store=store,gateway=judge),base_url='http://127.0.0.1',client=('127.0.0.1',12345)) as client:
        client.app.state.scheduler.stop()
        login_admin(client)
        old=client.post('/api/v1/goals',json={'content':'周五20点检查服务器'}).json()
        new=client.post('/api/v1/goals',json={'content':'周五晚上检查服务器'}).json()
        assert new['dedup']['status']=='possible_duplicate'
        host=client.get('/api/v1/goals').json()['items']
        admin=client.get('/admin/api/goals/'+str(new['submitted_id'])).json()
        assert next(row for row in host if row['id']==new['submitted_id'])['possible_duplicate_ids']==[old['submitted_id']]
        assert admin['possible_duplicate_ids']==[old['submitted_id']]
        assert judge.calls==[]


def test_c_and_disabled_a_do_not_enable_experimental_markers(store,setup):
    goals,judge=setup
    goals.create(content='周五20点检查服务器')
    store.set_setting('goal_dedup_judge',{'enabled':True,'method':'C'})
    assert goals.create(content='周五晚上检查服务器')['dedup']['status']=='created'
    store.set_setting('goal_dedup_judge',{'enabled':False,'method':'C2'})
    assert goals.create(content='周五21点检查服务器')['dedup']['status']=='created'
    assert judge.calls==[]


def test_selected_default_marks_sequence_conflicts_without_needing_a_gateway(store):
    # No probe override: an ordinary installation uses the selected C2 policy.
    assert store.setting('goal_dedup_judge',None) is None
    goals=Goals(store)
    old=goals.create(content='周五20点检查服务器')
    new=goals.create(content='周五晚上检查服务器',host_key='default-c2')
    assert new['dedup']['status']=='possible_duplicate'
    assert new['goal']['possible_duplicate_ids']==[old['submitted_id']]
    assert goals.get(new['submitted_id'])['dedup_review'] is None
    assert goals.create(content='ignored retry',host_key='default-c2')==new
    assert goals.pending_reviews()==[]
