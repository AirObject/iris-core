"""Whole dream workflow: fake models, durable progress and snapshot settings."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin
from iris.api import create_app
from iris.db import dumps
from iris.maintenance import Maintenance
from iris.memory_ops import setup_role, edit_memory, manage_memory
from iris.models import ModelError, ModelReply
from iris.persona import current_persona
from test_lifecycle import Clock
from test_persona import add_self, FakeGateway
from test_consolidation import Model, pair, merge_answer


def persona_case(store, *, budget=50, degree='small', supported=True, **settings):
    setup_role(store, 'Iris', '我来自云城。')
    # Existing publication/recovery cases opt in to automatic small/medium changes.
    store.set_setting('persona_publish_mode', 'small_medium_auto')
    clock = Clock('2026-10-10T12:00:00+08:00')
    with store.write() as conn:
        conn.execute('UPDATE persona_versions SET created_at=?', ((clock()-timedelta(days=8)).isoformat(),))
    add_self(store, '我在阅读时喜欢安静。')
    store.set_setting('consolidation', {'max_calls':budget, 'merge_enabled':False,
        'conflict_enabled':False, 'dependency_enabled':False, **settings})
    model = FakeGateway(store, {'sentences':[]},
                        degree=degree, supported=supported)
    return Maintenance(store, gateway=model, clock=clock), model, clock


def complete(engine):
    rid = engine.request()
    engine.run(rid)
    return engine.report(rid)


@pytest.mark.parametrize('degree,supported,status', [('small',True,'published'),('large',True,'pending'),('small',False,'rejected')])
def test_persona_publishes_only_complete_checked_candidates_and_reports(store, degree, supported, status):
    engine, model, _ = persona_case(store, degree=degree, supported=supported)
    before = current_persona(store)['id']
    report = complete(engine)
    assert report['persona']['status'] == status
    assert [p for p, _ in model.calls] == ['persona_generate','persona_check']
    assert report['summary']['model_calls']['count'] == 2
    assert (current_persona(store)['id'] != before) == (status == 'published')
    assert report['persona']['version_id']
    if status == 'rejected':
        assert report['persona']['reason'] == 'checks_rejected'
        assert report['persona']['checks']['model_errors']
    assert [i for i in report['items'] if i['phase']=='persona']


@pytest.mark.parametrize('budget', [0,1])
def test_persona_insufficient_budget_does_not_start_and_next_run_can_update(store, budget):
    engine, model, _ = persona_case(store, budget=budget)
    report = complete(engine)
    assert not model.calls
    assert report['persona']['reason'] == 'call_budget'
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM persona_attempts').fetchone()[0] == 0
    store.set_setting('consolidation', {**store.setting('consolidation'), 'max_calls':2})
    assert complete(engine)['persona']['status'] == 'published'
    assert len(model.calls) == 2


def test_not_due_does_not_create_unchanged_item_or_attempt(store):
    engine, model, clock = persona_case(store)
    with store.write() as conn:
        conn.execute('UPDATE persona_versions SET created_at=?', (clock().isoformat(),))
    report = complete(engine)
    assert report['persona']['status'] == 'not_due' and not model.calls
    assert not [i for i in report['items'] if i['phase']=='persona']
    with store.read() as conn:
        assert not conn.execute('SELECT 1 FROM persona_attempts').fetchone()


def test_stale_basis_counts_as_change_on_seven_day_update(store):
    engine, model, _ = persona_case(store)
    with store.write() as conn:
        conn.execute("DELETE FROM sources WHERE memory_id=2")
        conn.execute("DELETE FROM memory_subjects WHERE memory_id=2")
        conn.execute("DELETE FROM memories WHERE id=2")
    manage_memory(store, 1, 1, action='forget')
    report = complete(engine)
    assert report['persona']['due']['due']
    assert report['persona']['due']['changed_memory_ids'] == [1]
    assert report['persona']['reason'] == 'no_self_evidence'
    assert not model.calls


@pytest.mark.parametrize('reason', ['paused','usage_limit'])
def test_persona_health_skip_and_goal_review_still_runs(store, reason):
    from types import SimpleNamespace
    engine, model, _ = persona_case(store)
    model.health = SimpleNamespace(check=lambda *a, **k: (_ for _ in ()).throw(
        ModelError('paused','paused',paused=True,reason=reason)))
    gid = goal_with_lost_basis(store)
    report = complete(engine)
    assert report['persona']['status']=='skipped' and report['persona']['reason']==reason
    assert not model.calls and report['summary']['model_calls']['count']==0
    assert report['summary']['goals_reviewed']['object_ids']==[gid]


def goal_with_lost_basis(store):
    from test_retrieval import put
    mid = put(store, '准备资料', importance=80)
    with store.write() as conn:
        gid = conn.execute("INSERT INTO goals(content,kind,origin,created_at) VALUES('提交资料','normal','internal',?)",
                           (Clock()().isoformat(),)).lastrowid
        conn.execute('INSERT INTO goal_memories(goal_id,memory_id,memory_revision) VALUES(?,?,1)', (gid,mid))
    edit_memory(store, mid, 1, content='资料不再适用')
    return gid


def test_goal_review_and_cursor_are_one_transaction_restarts_do_not_repeat(store, monkeypatch):
    gid = goal_with_lost_basis(store)
    engine = Maintenance(store, clock=Clock())
    rid = engine.request()
    original = engine._record
    def interrupt(conn, run, phase, candidate, *args):
        if phase=='goals':
            raise KeyboardInterrupt()
        return original(conn,run,phase,candidate,*args)
    monkeypatch.setattr(engine,'_record',interrupt)
    with pytest.raises(KeyboardInterrupt):
        engine.run(rid)
    with store.read() as conn:
        assert not conn.execute('SELECT 1 FROM goal_basis_annotations').fetchone()
    restarted = Maintenance(store, clock=Clock())
    restarted.run(rid)
    restarted.run(rid)
    assert restarted.report(rid)['summary']['goals_reviewed']['object_ids']==[gid]
    assert not [i for i in complete(restarted)['items'] if i['phase']=='goals']
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM goal_basis_annotations').fetchone()[0]==1


def test_persona_completed_attempt_is_recovered_without_regeneration(store, monkeypatch):
    from iris.persona import PersonaEngine
    engine, model, _ = persona_case(store)
    rid = engine.request()
    original = PersonaEngine._run
    def completed_then_crash(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt()
    monkeypatch.setattr(PersonaEngine,'_run',completed_then_crash)
    with pytest.raises(KeyboardInterrupt):
        engine.run(rid)
    monkeypatch.setattr(PersonaEngine,'_run',original)
    Maintenance(store, gateway=model, clock=engine.clock).run(rid)
    assert len(model.calls)==2
    assert engine.report(rid)['persona']['status']=='published'
    assert engine.report(rid)['summary']['model_calls']['count']==2


def test_persona_incomplete_attempt_never_publishes_or_repeats_on_resume(store):
    engine, model, _ = persona_case(store)
    before = current_persona(store)['id']
    def crash():
        raise KeyboardInterrupt()
    model.callback = crash
    rid = engine.request()
    with pytest.raises(KeyboardInterrupt):
        engine.run(rid)
    Maintenance(store,gateway=model,clock=engine.clock).run(rid)
    assert current_persona(store)['id']==before and len(model.calls)==1
    assert engine.report(rid)['persona']['reason']=='interrupted'


def test_settings_patch_is_audited_and_only_next_run_changes(store):
    a,b = pair(store)
    engine = Maintenance(store,gateway=Model(store,merge_answer),clock=Clock())
    with TestClient(create_app(store=store),base_url='http://127.0.0.1',client=('127.0.0.1',12345)) as client:
        login_admin(client)
        client.app.state.scheduler.stop()
        rid = engine.request()
        response = client.patch('/admin/api/settings/consolidation',json={'merge_enabled':False,
            'max_calls':7,'maintenance_time':'04:15','persona_enabled':False,'goal_review_enabled':False})
        assert response.status_code==200, response.text
        value = response.json()['consolidation']
        assert value['max_calls']==7 and value['conflict_enabled'] and not value['merge_enabled']
        assert response.json()['lifecycle']['maintenance_time']=='04:15'
    engine.run(rid)
    assert engine.report(rid)['summary']['merged']['count']==1
    assert engine.report(rid)['consolidation']['settings']['max_calls']==50
    next_report = complete(engine)
    assert not next_report['consolidation']['settings']['merge_enabled']
    assert next_report['persona']['reason']=='disabled'
    with store.read() as conn:
        assert conn.execute("SELECT 1 FROM admin_operations WHERE action='consolidation_settings_saved'").fetchone()


@pytest.mark.parametrize('payload', [{'max_calls':51},{'max_calls':-1},{'max_calls':True},{'merge_enabled':'false'},
                                      {'maintenance_time':'24:00'},{'method':'strict'},{'unrecognized':True}])
def test_settings_strict_validation(store, payload):
    with TestClient(create_app(store=store),base_url='http://127.0.0.1',client=('127.0.0.1',12345)) as client:
        login_admin(client)
        assert client.patch('/admin/api/settings/consolidation',json=payload).status_code==400


def test_five_changes_due_without_waiting_seven_days(store):
    engine, model, clock = persona_case(store)
    with store.write() as conn:
        conn.execute('UPDATE persona_versions SET created_at=?', (clock().isoformat(),))
    for text in ('我学会画松树。','我在写旅行札记。','我练习独奏。','我爱看星图。'):
        add_self(store, text)
    report = complete(engine)
    assert report['persona']['due']['reason']=='five_changes'
    assert report['persona']['status']=='published'


@pytest.mark.parametrize('change', ['version','basis'])
def test_persona_writeback_rechecks_version_and_evidence(store, change):
    from iris.persona import admin_edit
    engine, model, _ = persona_case(store)
    if change=='version':
        model.callback = lambda: admin_edit(store,'管理员修改摘要。',expected_version=1,clock=engine.clock)
    else:
        model.callback = lambda: edit_memory(store,1,1,content='我来自海城。')
    report = complete(engine)
    assert report['persona']['status']=='conflict'
    assert report['persona']['reason']==('current_version_changed' if change=='version' else 'evidence_changed')
    assert not [i for i in report['items'] if i['outcome']=='persona_published']


def test_budget_is_shared_after_merge_and_defers_persona(store):
    engine, persona, _ = persona_case(store, budget=2, merge_enabled=True, conflict_enabled=True)
    pair(store)
    # Keep persona's self evidence from forming irrelevant candidate pairs.
    co = Model(store,merge_answer)
    class Combined:
        def chat(self, messages, purpose, max_tokens=16000, **kwargs):
            model = persona if purpose.startswith('persona_') else co
            return model.chat(messages,purpose,max_tokens,**kwargs)
    engine.gateway = Combined()
    report = complete(engine)
    assert report['summary']['merged']['count']==1
    assert report['summary']['model_calls']['count']==1
    assert report['persona']['reason']=='call_budget' and not persona.calls


def test_D01_prepare_and_goal_operations_while_persona_is_running(store):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from iris.retrieval import Retrieval
    from iris.goals import Goals
    from conftest import msg
    engine, model, _ = persona_case(store)
    msg(store,1,'云城')
    entered, release = threading.Event(), threading.Event()
    def wait():
        entered.set()
        assert release.wait(5)
    model.callback = wait
    rid = engine.request()
    with ThreadPoolExecutor() as pool:
        future = pool.submit(engine.run,rid)
        try:
            assert entered.wait(5)
            assert Retrieval(store).prepare('A',text='云城',judge=False)['recall_id']
            assert Goals(store,clock=engine.clock).create(content='读旅行指南')['submitted_id']
        finally:
            release.set()
        future.result(timeout=5)
    assert engine.report(rid)['persona']['status']=='published'


def test_stop_between_persona_generation_and_check_never_publishes(store):
    engine, model, _ = persona_case(store)
    rid = engine.request()
    engine.run(rid,stop=lambda:bool(model.calls))
    assert current_persona(store)['id']==1 and len(model.calls)==1
    engine.run(rid)
    assert len(model.calls)==1 and engine.report(rid)['persona']['reason']=='interrupted'


@pytest.mark.parametrize('budget,first_response,expected_calls,status', [
    (2,'repair',1,'failed'), (3,'repair',3,'published'), (3,'retry',3,'published'),
    (2,'retry',1,'failed'),
])
def test_persona_http_retries_and_json_repairs_use_shared_budget(store,budget,first_response,expected_calls,status):
    import httpx
    from iris.models import Gateway, ModelConfig
    from fake_openai import completion
    engine, model, _ = persona_case(store,budget=budget)
    hits=[]
    def respond(request):
        assert not store._writer.in_transaction
        hits.append(json.loads(request.content))
        if len(hits)==1:
            if first_response=='retry':
                return httpx.Response(429,json={'error':{'code':'RateLimitExceeded'}},headers={'Retry-After':'0'})
            return httpx.Response(200,json=completion('not JSON'))
        if len(hits)==2:
            value=model.generated
        else:
            payload=json.loads(hits[-1]['messages'][-1]['content'])
            value={'sentences':[{'index':i+1,'supported':True,'fabricated':False,'scene_qualified':True,
                                  'violations':[],'reason':'依据支持'} for i,_ in enumerate(payload['candidate'])],'change_degree':'small','reason':'措辞'}
        return httpx.Response(200,json=completion(dumps(value)))
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        gateway=Gateway({'chat':ModelConfig('https://example.invalid','', 'fake',reasoning_effort='high')},store,
                        client,sleeper=lambda _:None)
        engine.gateway=gateway
        try:
            report=complete(engine)
        finally:
            gateway.close()
    assert report['persona']['status']==status
    assert len(hits)==expected_calls==report['summary']['model_calls']['count']
    with store.read() as conn:
        calls=[dict(r) for r in conn.execute('SELECT * FROM consolidation_calls')]
        assert all(c['reasoning_effort']=='high' and c['duration_ms']>=0 for c in calls)
        assert conn.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0]==expected_calls
    assert report['summary']['model_calls']['prompt_tokens']==sum(c['prompt_tokens'] or 0 for c in calls)


@pytest.mark.parametrize('reason', ['disabled','unconfigured','persona_busy'])
def test_persona_skip_reasons_persist_without_calls(store,reason):
    engine, model, _ = persona_case(store)
    if reason=='disabled':
        store.set_setting('consolidation',{**store.setting('consolidation'),'persona_enabled':False})
    elif reason=='unconfigured':
        engine.gateway=None
    else:
        from iris.persona import _reserve_attempt
        with store.write() as conn:
            _reserve_attempt(conn,1,'regenerate',engine.clock().isoformat())
    report=complete(engine)
    assert report['persona']['reason']==reason and not model.calls
    assert not [i for i in report['items'] if i['phase']=='persona']


@pytest.mark.parametrize('merge,conflict', [(False,True),(True,False),(False,False)])
def test_pair_switches_leave_disabled_work_for_later(store,merge,conflict):
    a,b=pair(store)
    def answer(payload):
        return {'decision':'conflict','reason':'两条需核对','evidence':[s['id'] for m in payload['memories'] for s in m['sources']],
                'updates':[{'id':a,'annotation':'需要核对'}]}
    model=Model(store,answer)
    engine=Maintenance(store,gateway=model,clock=Clock())
    store.set_setting('consolidation',{'merge_enabled':merge,'conflict_enabled':conflict})
    report=complete(engine)
    assert report['summary'].get('conflicts',{}).get('count',0)==int(conflict)
    assert len(model.requests)==(2 if conflict else 1 if merge else 0)
    if not merge and conflict:
        assert not model.requests[0][1]['merge_allowed']
    store.set_setting('consolidation',{'merge_enabled':True,'conflict_enabled':True})
    second=complete(engine)
    assert second['summary'].get('conflicts',{}).get('count',0)==(0 if conflict else 1)


def test_dependency_switch_does_not_repeat_M2_penalty_and_reenables_pending_review(store):
    from test_retrieval import put
    source=put(store,'依据',importance=80)
    child=put(store,'派生',stance='推断',importance=80)
    with store.write() as conn:
        conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,1,?)",
                     (child,source,Clock()().isoformat()))
    manage_memory(store,source,1,action='forget')
    store.set_setting('consolidation',{'dependency_enabled':False,'merge_enabled':False,'conflict_enabled':False})
    model=Model(store,lambda p:{'decision':'weaken','reason':'唯一依据遗忘','evidence':[],
                              'updates':[{'id':child,'assessment':'unsupported','annotation':'依据不足'}]})
    engine=Maintenance(store,gateway=model,clock=Clock())
    assert complete(engine)['summary']['dependencies_weakened']['count']==1
    assert not model.requests
    store.set_setting('consolidation',{'dependency_enabled':True,'merge_enabled':False,'conflict_enabled':False})
    report=complete(engine)
    assert report['summary']['dependencies_weakened']['count']==0
    assert report['summary']['dependencies_reviewed']['count']==1


def test_goal_switch_revision_race_and_next_scan(store,monkeypatch):
    gid=goal_with_lost_basis(store)
    store.set_setting('consolidation',{'goal_review_enabled':False})
    engine=Maintenance(store,clock=Clock())
    assert complete(engine)['goal_review']['skip_reason']=='disabled'
    store.set_setting('consolidation',{'max_calls':0})
    original=engine._process_item
    def race(run,phase,candidate):
        if phase=='goals':
            with store.write() as conn:
                conn.execute('UPDATE goals SET revision=revision+1 WHERE id=?',(gid,))
        return original(run,phase,candidate)
    monkeypatch.setattr(engine,'_process_item',race)
    report=complete(engine)
    assert report['summary']['skipped']['reasons']['revision_conflict']==1
    monkeypatch.setattr(engine,'_process_item',original)
    assert complete(engine)['summary']['goals_reviewed']['object_ids']==[gid]


def test_consolidation_settings_csrf_and_time_alias(store):
    with TestClient(create_app(store=store),base_url='http://127.0.0.1',client=('127.0.0.1',12345)) as client:
        login_admin(client)
        assert client.patch('/admin/api/settings/lifecycle',json={'maintenance_time':'05:30'}).status_code==200
        assert client.get('/admin/api/settings').json()['consolidation']['maintenance_time']=='05:30'
        client.headers.pop('X-Iris-CSRF')
        assert client.patch('/admin/api/settings/consolidation',json={'max_calls':1}).status_code==403
        assert client.get('/admin/api/settings').json()['consolidation']['max_calls']==50


def test_complete_phase_order_merges_before_persona_then_reviews_goals(store):
    engine, persona, _ = persona_case(store, merge_enabled=True, conflict_enabled=True)
    a,b=pair(store)
    gid=goal_with_lost_basis(store)
    order=[]
    class Combined:
        def chat(self, messages, purpose, max_tokens=16000, **kwargs):
            order.append(purpose)
            if purpose.startswith('persona_'):
                with store.read() as conn:
                    assert conn.execute('SELECT merged_into FROM memories WHERE id=?',(a,)).fetchone()[0]==b
                    assert not conn.execute('SELECT 1 FROM goal_basis_annotations WHERE goal_id=?',(gid,)).fetchone()
                return persona.chat(messages,purpose,max_tokens,**kwargs)
            payload=json.loads(messages[-1]['content'])
            value=merge_answer(payload) if payload.get('pair_ids')==[a,b] else {
                'decision':'separate','reason':'不同事实','evidence':[],'updates':[]}
            return ModelReply(dumps(value),'stop',{'prompt_tokens':20,'completion_tokens':10})
    engine.gateway=Combined()
    report=complete(engine)
    assert order[-2:]==['persona_generate','persona_check']
    assert report['summary']['model_calls']['count']==len(order)
    assert report['summary']['merged']['count']==1
    assert report['summary']['goals_reviewed']['object_ids']==[gid]


def test_goal_scan_is_bounded_to_request_and_interruptible(store):
    first=goal_with_lost_basis(store)
    engine=Maintenance(store,clock=Clock())
    rid=engine.request()
    later=goal_with_lost_basis(store)
    engine.run(rid)
    assert engine.report(rid)['summary']['goals_reviewed']['object_ids']==[first]
    assert complete(engine)['summary']['goals_reviewed']['object_ids']==[later]


def test_migration_preserves_old_budget_rows_and_resumes_accepted_run(tmp_path):
    from pathlib import Path
    import sqlite3
    from iris.db import Store
    from iris.search_text import segmented
    from iris import migrations
    db=tmp_path/'upgrade.db'
    conn=sqlite3.connect(db)
    conn.create_function('iris_terms',1,segmented,deterministic=True)
    conn.execute('CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL)')
    for path in sorted(Path(migrations.__file__).parent.glob('*.sql')):
        if path.name>='019':
            continue
        conn.executescript(path.read_text())
        conn.execute('INSERT INTO schema_migrations VALUES(?,?)',(path.name,Clock()().isoformat()))
    stamp=Clock()().isoformat()
    from iris.memory_ops import LIFECYCLE_DEFAULTS
    conn.execute('''INSERT INTO maintenance_runs(id,trigger,settings_json,timezone,memory_through,message_through,
        batch_through,created_at,phase) VALUES(1,'manual',?,'Asia/Shanghai',0,0,0,?,6)''',(dumps(LIFECYCLE_DEFAULTS),stamp))
    conn.execute("INSERT INTO consolidation_runs VALUES(1,?,1,1,NULL)",(dumps({'max_calls':7,'method':'broad','resolution':'report_only_v1','enabled':True}),))
    conn.execute("INSERT INTO consolidation_work VALUES(1,'old','pair','{}',80,?,'done','{}',?)",(stamp,stamp))
    conn.execute("INSERT INTO consolidation_calls(run_id,work_id,purpose,created_at,result,prompt_tokens,completion_tokens) VALUES(1,1,'consolidation_merge',?,'success',20,10)",(stamp,))
    conn.commit()
    conn.close()
    with_store=Store(db)
    try:
        engine=Maintenance(with_store,clock=Clock())
        engine.run(1)
        report=engine.report(1)
        assert report['state']=='completed' and report['summary']['model_calls']['count']==1
        assert report['consolidation']['settings']['max_calls']==7
        with with_store.read() as conn:
            assert not conn.execute('PRAGMA foreign_key_check').fetchall()
            assert conn.execute('SELECT prompt_tokens FROM consolidation_calls').fetchone()[0]==20
    finally:
        with_store.close()


def test_default_switches_preserve_frozen_work_fingerprints(store):
    from iris.consolidation import (work_fingerprint, digest, semantic, snapshot, PROMPT_VERSION, pair_policy, DEFAULTS)
    a,b=pair(store)
    with store.read() as conn:
        memories=[snapshot(conn,i) for i in (a,b)]
    payload={'memories':memories,'resolution':'report_only_v1'}
    before=digest({'version':PROMPT_VERSION,'method':'broad','kind':'pair',
                   'memories':[semantic(m) for m in memories],'losses':[], 'resolution':'report_only_v1'})
    assert work_fingerprint('pair',payload,'broad')==before
    assert work_fingerprint('pair',{**payload,'policy':pair_policy(DEFAULTS)},'broad')==before
    assert work_fingerprint('pair',{**payload,'policy':{'merge_enabled':False,'conflict_enabled':True}},'broad')!=before


def test_skip_checkpoint_survives_stop_without_persona_attempt(store):
    engine, model, _=persona_case(store,budget=1)
    rid=engine.request()
    def stopped():
        with store.read() as conn:
            return bool(conn.execute('SELECT 1 FROM maintenance_persona WHERE run_id=?',(rid,)).fetchone())
    engine.run(rid,stop=stopped)
    assert engine.report(rid)['state']=='running'
    Maintenance(store,gateway=model,clock=engine.clock).run(rid)
    assert engine.report(rid)['state']=='completed' and not model.calls
    assert engine.report(rid)['persona']['reason']=='call_budget'


@pytest.mark.parametrize('degree', ['small', 'medium', 'large'])
def test_persona_dream_step_uses_manual_product_default(store, degree):
    # The shared fixture opts in to auto-publication for older recovery tests;
    # remove that override to exercise the product fallback in the real dream step.
    engine, model, _ = persona_case(store, degree=degree)
    with store.write() as conn:
        conn.execute("DELETE FROM runtime_settings WHERE key='persona_publish_mode'")
    before = current_persona(store)['id']
    report = complete(engine)
    assert report['persona']['status'] == 'pending'
    assert current_persona(store)['id'] == before
    with store.read() as conn:
        row = conn.execute("SELECT material_json,checks_json FROM persona_versions WHERE status='pending'").fetchone()
        assert json.loads(row['material_json'])['settings']['publish_mode'] == 'all_manual'
        assert json.loads(row['checks_json'])['passed']
    assert len(model.calls) == 2


@pytest.mark.parametrize('budget,status',[(2,'failed'),(3,'failed'),(4,'published')])
def test_sentence_recovery_consumes_existing_dream_call_budget(store,budget,status):
    from test_persona_optimization import Model
    engine,_,_=persona_case(store,budget=budget)
    model=Model(store)
    engine.gateway=model
    report=complete(engine)
    assert report['persona']['status']==status
    assert len(model.calls)==budget
    assert report['summary']['model_calls']['count']==budget
    if status=='failed':
        assert report['persona']['reason']=='call_budget'
        assert current_persona(store)['id']==1
    else:
        assert len(report['persona']['checks']['repaired_sentences'])==1
