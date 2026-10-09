"""Durable review scheduling; every model here is local and deterministic."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin
from iris.api import create_app
from iris.db import Store
from iris.goals import Goals, write_learning_goal
from iris.models import ModelError
from iris.scheduler import Scheduler
from test_goal_dedup_apply import Judge
from test_goals_api import Clock
from test_scheduler import wait_for


def staged(store, goals):
    old=goals.create(content='整理操作手册')
    with store.write() as conn:
        conn.execute("INSERT OR IGNORE INTO entries(id,name,platform,kind) VALUES('A','A','unit','private')")
        new=write_learning_goal(conn,content='整理操作说明手册',kind='normal',deadline=None,
                                entry_id='A',evidence=[],current=goals.clock())
    return old,new


@pytest.fixture
def setup(store):
    store.set_setting('goal_dedup_judge',{'enabled':True,'method':'C','budget_seconds':1,'concurrency':1})
    clock=Clock()
    judge=Judge(store)
    judge.configs={}
    return Goals(store,gateway=judge,clock=clock),judge,clock


def job(store,gid):
    with store.read() as conn:
        return dict(conn.execute('SELECT * FROM goal_dedup_jobs WHERE goal_id=?',(gid,)).fetchone())


def test_learning_commit_persists_review_and_rollback_leaves_no_job(store,setup):
    goals,judge,_=setup
    _,new=staged(store,goals)
    gid=new['submitted_id']
    assert job(store,gid)['state']=='pending' and judge.calls==[]
    with pytest.raises(RuntimeError),store.write() as conn:
        write_learning_goal(conn,content='整理操作说明手册',kind='normal',deadline=None,
                            entry_id='A',evidence=[],current=goals.clock())
        raise RuntimeError('roll back the whole learning batch')
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM goal_dedup_jobs').fetchone()[0]==1
    projection=goals.get(gid)['dedup_review']
    assert projection['state']=='pending' and projection['attempts']==0
    assert 'lease_token' not in projection and 'candidate_revisions_json' not in projection


def test_scheduler_and_request_cannot_judge_the_same_goal_twice(store,setup):
    goals,judge,_=setup
    _,new=staged(store,goals)
    gid=new['submitted_id']
    entered,release=threading.Event(),threading.Event()
    def block():
        entered.set()
        assert release.wait(3)
    judge.hook=block
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(goals.review,gid)
            assert entered.wait(2)
            row=job(store,gid)
            assert row['state']=='running' and row['attempts']==1
            assert json.loads(row['candidate_revisions_json'])
            second=Goals(store,gateway=judge,clock=goals.clock).review(gid)
            assert second['dedup']['status']=='pending' and second['dedup']['reason']=='in_progress'
            assert len(judge.calls)==1
            release.set()
            assert first.result()['dedup']['status']=='merged'
    finally:
        release.set()
    assert job(store,gid)['state']=='done'
    assert not job(store,gid)['lease_token']
    assert len(goals.pull()['items'])==1


def test_restart_recovers_only_expired_lease(store,setup):
    goals,judge,clock=setup
    _,new=staged(store,goals)
    gid=new['submitted_id']
    with store.write() as conn:
        conn.execute("UPDATE goal_dedup_jobs SET state='running',lease_until=?,lease_token='interrupted' WHERE goal_id=?",
                     ((clock()+timedelta(seconds=10)).isoformat(),gid))
    # A distinct Store simulates reopening the database after process exit.
    with closing(Store(store.path)) as reopened:
        restarted=Goals(reopened,gateway=Judge(reopened),clock=clock)
        assert restarted.pending_reviews(limit=8)==[]
        clock.advance(seconds=11)
        assert restarted.pending_reviews(limit=8)==[gid]
        assert restarted.review(gid)['dedup']['status']=='merged'
    assert job(store,gid)['state']=='done'


def test_lost_lease_never_applies_an_old_model_result(store,setup):
    goals,judge,_=setup
    _,new=staged(store,goals)
    gid=new['submitted_id']
    def replace_lease():
        with store.write() as conn:
            conn.execute("UPDATE goal_dedup_jobs SET lease_token='replacement-worker' WHERE goal_id=?",(gid,))
    judge.hook=replace_lease
    result=goals.review(gid)
    assert result['dedup']['status']=='pending'
    assert job(store,gid)['lease_token']=='replacement-worker'
    with store.read() as conn:
        assert not conn.execute('SELECT 1 FROM goals WHERE merged_into IS NOT NULL').fetchone()
        assert not conn.execute("SELECT 1 FROM admin_operations WHERE action='goal_dedup_judged'").fetchone()


def test_failure_backoff_then_retry_is_durable(store,setup):
    goals,judge,clock=setup
    _,new=staged(store,goals)
    gid=new['submitted_id']
    judge.verdict=ModelError('retryable','unit timeout',reason='timeout')
    assert goals.review(gid)['dedup']['status']=='pending'
    assert job(store,gid)['attempts']==1 and job(store,gid)['state']=='pending'
    assert goals.pending_reviews(limit=8)==[]
    clock.advance(seconds=2)
    assert goals.pending_reviews(limit=8)==[gid]
    judge.verdict='same'
    assert goals.review(gid)['dedup']['status']=='merged'
    assert job(store,gid)['attempts']==2


def test_completed_goal_cancels_pending_review_and_inflight_verdict(store,setup):
    goals,judge,_=setup
    _,new=staged(store,goals)
    gid=new['submitted_id']
    judge.hook=lambda:goals.update(gid,state='completed')
    result=goals.review(gid)
    assert result['goal']['state']=='completed'
    assert job(store,gid)['state']=='cancelled'
    assert goals.pending_reviews(limit=8)==[]
    assert goals.pull()['items']==[]


def test_admin_can_resolve_model_duplicate_with_unknown_people(store,setup):
    goals,judge,_=setup
    with store.write() as conn:
        conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES('lin','person','林',?)",(goals.clock().isoformat(),))
    old=goals.create(content='帮林整理操作手册',people=['lin'])
    new=goals.create(content='帮林整理操作说明手册',origin='admin',actor='admin')
    assert new['dedup']['status']=='possible_duplicate'
    a,b=goals.get(old['submitted_id']),goals.get(new['submitted_id'])
    result=goals.merge(a['id'],b['id'],expected_revision=a['revision'],other_revision=b['revision'])
    assert result['people']==['lin']


def test_scheduler_reviews_while_learning_is_paused_and_obeys_switch(store,setup):
    goals,judge,clock=setup
    _,new=staged(store,goals)
    class Health:
        def due_probes(self): return []
        def allowed(self,kind): return kind=='goal_dedup_judge'
        def learning_allowed(self): return False
        def snapshot(self): return {'goal_dedup_judge':{'state':'normal'}}
    judge.health=Health()
    scheduler=Scheduler(store,judge,clock=clock)
    try:
        store.set_setting('goal_dedup_judge',{'enabled':False})
        scheduler.tick()
        assert judge.calls==[]
        store.set_setting('goal_dedup_judge',{'enabled':True,'method':'C','concurrency':1})
        scheduler.tick()
        wait_for(lambda:job(store,new['submitted_id'])['state']=='done')
        assert len(judge.calls)==1
        with store.read() as conn:
            assert conn.execute('SELECT COUNT(*) FROM batches').fetchone()[0]==0
            assert conn.execute("SELECT actor FROM admin_operations WHERE action='goal_dedup_judged'").fetchone()[0]=='scheduler'
    finally:
        scheduler.stop()


def test_http_uses_service_gateway_and_admin_exposes_review_without_lease(store,setup):
    _,judge,_=setup
    # The fake has an explicit gateway; no model configuration is loaded here.
    with TestClient(create_app(store=store,gateway=judge),base_url='http://127.0.0.1',
                    client=('127.0.0.1',12345)) as client:
        client.app.state.scheduler.stop()
        login_admin(client)
        first=client.post('/api/v1/goals',json={'content':'整理操作手册'}).json()
        second=client.post('/api/v1/goals',json={'content':'整理操作说明手册','host_key':'http-dedup'})
        assert second.status_code==201,second.text
        receipt=second.json()
        assert receipt['dedup']['status']=='merged'
        assert receipt['goal']['id']==first['submitted_id'] and len(judge.calls)==1
        assert client.post('/api/v1/goals',json={'content':'retry','host_key':'http-dedup'}).json()==receipt
        detail=client.get('/admin/api/goals/'+str(receipt['submitted_id'])).json()
        assert detail['dedup_review']['state']=='done'
        assert 'lease_token' not in detail['dedup_review']


def test_expired_lease_cannot_apply_even_without_a_replacement_worker(store,setup):
    goals,judge,clock=setup
    _,new=staged(store,goals)
    judge.hook=lambda:clock.advance(seconds=7)
    result=goals.review(new['submitted_id'])
    assert result['dedup']['reason']=='lease_expired'
    assert job(store,new['submitted_id'])['state']=='pending'
    assert goals.get(new['submitted_id'])['merged_into'] is None


@pytest.mark.parametrize('health_state',['temporarily_unavailable','rate_limited','usage_limit'])
def test_scheduler_does_not_claim_work_when_goal_purpose_is_unavailable(store,setup,health_state):
    goals,judge,clock=setup
    _,new=staged(store,goals)
    class Health:
        def due_probes(self): return []
        def allowed(self,kind): return False
        def learning_allowed(self): return False
        def snapshot(self): return {'goal_dedup_judge':{'state':health_state}}
    judge.health=Health()
    scheduler=Scheduler(store,judge,clock=clock)
    try:
        scheduler.tick()
        assert judge.calls==[] and job(store,new['submitted_id'])['attempts']==0
    finally:
        scheduler.stop()


def test_scheduler_limits_pending_reviews_to_configured_concurrency(store,setup):
    goals,judge,clock=setup
    _,first=staged(store,goals)
    with store.write() as conn:
        second=write_learning_goal(conn,content='整理操作手册说明',kind='normal',deadline=None,
                                   entry_id='A',evidence=[],current=clock())
    judge.verdict='uncertain'
    entered,release=threading.Event(),threading.Event()
    def block():
        entered.set()
        assert release.wait(3)
    judge.hook=block
    scheduler=Scheduler(store,judge,clock=clock)
    try:
        scheduler.tick()
        assert entered.wait(2)
        scheduler.tick()
        assert len(judge.calls)==1 and job(store,second['submitted_id'])['attempts']==0
        release.set()
        wait_for(lambda:job(store,first['submitted_id'])['state']=='done')
        wait_for(lambda:all(f.done() for f in scheduler._goal_reviews.values()))
        scheduler.tick()
        wait_for(lambda:job(store,second['submitted_id'])['state']=='done')
        assert len(judge.calls)==2
    finally:
        release.set()
        scheduler.stop()


def test_upgrade_preserves_historical_goals_without_queuing_them(tmp_path):
    import sqlite3
    from pathlib import Path
    from iris.search_text import segmented
    path=tmp_path/'before-review-jobs.db'
    stamp=Clock()().isoformat()
    with sqlite3.connect(path) as conn:
        conn.create_function('iris_terms',1,segmented)
        conn.execute('CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL)')
        for script in sorted((Path(__file__).parents[1]/'src/iris/migrations').glob('*.sql')):
            if script.name.endswith('_goal_dedup_jobs.sql'):
                break
            conn.executescript(script.read_text(encoding='utf-8'))
            conn.execute('INSERT INTO schema_migrations VALUES(?,?)',(script.name,stamp))
        conn.execute("INSERT INTO goals(content,kind,created_at) VALUES('历史目标','normal',?)",(stamp,))
    with closing(Store(path)) as upgraded:
        with upgraded.read() as conn:
            assert conn.execute('SELECT content FROM goals').fetchone()[0]=='历史目标'
            assert conn.execute('SELECT COUNT(*) FROM goal_dedup_jobs').fetchone()[0]==0
            assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert Goals(upgraded).get(1)['dedup_review'] is None
    assert list(tmp_path.glob('before-review-jobs.db.*.bak'))
