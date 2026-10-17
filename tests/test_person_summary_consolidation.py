"""Independent step budgets and the PS2 automatic consolidation step."""
import json

import pytest

from iris.consolidation import Consolidation, _PersonaGateway
from iris.db import dumps
from iris.maintenance import Maintenance
from iris.person_summary import current_summary, update_summaries_for_run
from test_person_summary import Clock, Model, seed, memory


def prepare(store, *, budget=50, limit=10, enabled=True):
    clock=Clock(); seed(store,clock)
    for i in range(8): memory(store,clock,f'乙同学测试{i}',speaker='b',days_ago=3)
    config={'enabled':True,'person_summary_max_calls':budget,'merge_enabled':False,'conflict_enabled':False,
            'dependency_enabled':False,'persona_enabled':False,'goal_review_enabled':False,'entry_profile_enabled':False,
            'person_summary_enabled':enabled}
    store.set_setting('consolidation',config)
    store.set_setting('person_summary',{**store.setting('person_summary'),'max_subjects_per_run':limit})
    model=Model(store); run_id=Maintenance(store,gateway=model,clock=clock).request()
    owner=Consolidation(store,model,clock=clock); owner.run_id=run_id
    return clock,model,_PersonaGateway(owner),{'id':run_id},config


def test_recent_person_first_shared_call_limit_and_durable_replay(store):
    clock,model,gateway,run,config=prepare(store,budget=2)
    assert update_summaries_for_run(store,gateway,run,config,clock=clock)
    assert len(model.calls)==2 and current_summary(store,'a') and current_summary(store,'b') is None
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM consolidation_calls').fetchone()[0]==2
    update_summaries_for_run(store,gateway,run,config,clock=clock)
    assert len(model.calls)==2


def test_count_limit_and_stop_before_work(store):
    clock,model,gateway,run,config=prepare(store,limit=1)
    assert update_summaries_for_run(store,gateway,run,config,clock=clock,stop=lambda:True) is False
    assert not model.calls
    assert update_summaries_for_run(store,gateway,run,config,clock=clock)
    update_summaries_for_run(store,gateway,run,config,clock=clock)
    assert len(model.calls)==2
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM person_summary_attempts').fetchone()[0]==1


@pytest.mark.parametrize('budget',[0,1])
def test_two_call_preflight(store,budget):
    clock,model,gateway,run,config=prepare(store,budget=budget)
    assert update_summaries_for_run(store,gateway,run,config,clock=clock)
    assert not model.calls


def test_daily_gate_stops_checker_and_keeps_previous(store):
    from iris.models import ModelError
    clock,model,gateway,run,config=prepare(store)
    class Health:
        def check(self,kind,purpose,**kwargs):
            assert (kind,purpose)==('chat','learning')
            if model.calls: raise ModelError('budget','usage limit',reason='usage_limit')
    gateway.health=Health()
    update_summaries_for_run(store,gateway,run,config,clock=clock)
    assert len(model.calls)==1
    assert current_summary(store,'a') is None


def test_disabled_automatic_consolidation_hook(store):
    clock,model,gateway,run,config=prepare(store,enabled=False)
    engine=Maintenance(store,gateway=model,clock=clock); engine.run(run['id'])
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM person_summary_attempts').fetchone()[0]==0
    assert not model.calls


def test_replay_repairs_report_even_when_limit_filled_and_activity_order_changes(store):
    clock,model,gateway,run,config=prepare(store,limit=1)
    update_summaries_for_run(store,gateway,run,config,clock=clock)
    assert current_summary(store,'a')
    with store.write() as conn:
        conn.execute("DELETE FROM maintenance_items WHERE outcome='person_summary'")
        conn.execute("UPDATE memories SET last_confirmed_at='2026-10-11T12:00:01+00:00' WHERE speaker_subject_id='b'")
    update_summaries_for_run(store,gateway,run,config,clock=clock)
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM maintenance_items WHERE outcome='person_summary'").fetchone()[0]==1
    assert len(model.calls)==2


def test_purge_clears_shared_consolidation_raw_output_and_late_response(store):
    from iris.memory_ops import purge_memory
    clock,model,gateway,run,config=prepare(store,limit=1)
    update_summaries_for_run(store,gateway,run,config,clock=clock)
    with store.read() as conn:
        mid=current_summary(store,'a')['sentences'][0]['basis'][0]['memory_id']
        assert conn.execute('SELECT COUNT(*) FROM consolidation_calls WHERE raw_output IS NOT NULL').fetchone()[0]==2
    with store.write() as conn:
        purge_memory(store,mid,1,confirm=True,_conn=conn)
    with store.read() as conn:
        rows=list(conn.execute('SELECT * FROM consolidation_calls'))
        assert len(rows)==2 and all(r['raw_output'] is None and r['error']=='因彻底清除而清空' for r in rows)


def test_purge_during_budgeted_generation_clears_late_ledger_output(store):
    from iris.memory_ops import purge_memory
    clock,model,gateway,run,config=prepare(store,limit=1)
    def clear():
        with store.write() as conn:
            purge_memory(store,1,1,confirm=True,_conn=conn)
    model.hook=clear
    update_summaries_for_run(store,gateway,run,config,clock=clock)
    with store.read() as conn:
        rows=list(conn.execute('SELECT * FROM consolidation_calls'))
        assert len(rows)==1 and rows[0]['raw_output'] is None
        assert conn.execute('SELECT COUNT(*) FROM person_summary_versions').fetchone()[0]==0


@pytest.mark.parametrize("automatic", [False, True])
def test_person_summary_purge_read_projection_marks_late_ledger(store, automatic):
    from iris import admin_data
    from iris.memory_ops import purge_memory
    clock,model,gateway,run,config=prepare(store,limit=1)
    model.hook=lambda:purge_memory(store,1,1,confirm=True)
    if automatic:
        engine=Maintenance(store,gateway=model,clock=clock)
        engine.run(run['id'])
        report=engine.report(run['id'])
        assert report['state']=='completed'
        assert any(item['outcome']=='person_summary' and item['details']['status']=='conflict'
                   for item in report['items'])
    else:
        update_summaries_for_run(store,gateway,run,config,clock=clock)
        report={'id':run['id'],'items':[]}
    report=admin_data.maintenance_redactions(store,report)
    assert len(report['consolidation_calls'])==1
    assert report['consolidation_calls'][0]['redacted']
    assert report['consolidation_calls'][0]['redaction_reason']=='因彻底清除而清空'


@pytest.mark.parametrize('budget,limit,expected',[(50,1,1),(50,2,2),(2,2,1),(1,2,0),(0,2,0)])
def test_automatic_step_limits_shared_ledger_and_report(store,budget,limit,expected):
    clock,model,gateway,run,config=prepare(store,budget=budget,limit=limit)
    engine=Maintenance(store,gateway=model,clock=clock)
    engine.run(run['id'])
    report=engine.report(run['id'])
    assert report['state']=='completed'
    assert len(model.calls)==expected*2
    assert report['summary']['model_calls']['count']==expected*2
    results=[item for item in report['items'] if item['outcome']=='person_summary']
    assert sum(item['details']['status']=='published' for item in results)==expected
    engine.run(run['id'])
    assert len(model.calls)==expected*2


def test_unexpected_step_failure_is_reported_without_stopping_maintenance(store,monkeypatch):
    from iris import person_summary
    clock,model,gateway,run,config=prepare(store)
    def broken(*args,**kwargs): raise RuntimeError('must never appear in report')
    monkeypatch.setattr(person_summary,'update_summaries_for_run',broken)
    engine=Maintenance(store,gateway=model,clock=clock); engine.run(run['id'])
    report=engine.report(run['id'])
    assert report['state']=='completed'
    assert 'must never appear' not in json.dumps(report)
    assert any(i['outcome']=='person_summary' and i['reason']=='unexpected_error' for i in report['items'])


def test_generation_reservation_keeps_one_call_for_checker(store):
    from iris.models import ModelError
    clock,model,gateway,run,config=prepare(store,budget=2)
    owner=gateway.owner
    owner.reserve({'id':None},'consolidation_person_summary_generate')
    with pytest.raises(ModelError) as error:
        owner.reserve({'id':None},'consolidation_person_summary_generate')
    assert error.value.reason=='call_budget'
    owner.reserve({'id':None},'consolidation_person_summary_check')


def test_one_person_failure_continues_next_person(store):
    clock,model,gateway,run,config=prepare(store,limit=2)
    def broken(): raise RuntimeError('private exception')
    model.hook=broken
    engine=Maintenance(store,gateway=model,clock=clock); engine.run(run['id'])
    report=engine.report(run['id'])
    statuses={i['details']['subject_id']:i['details']['status'] for i in report['items'] if i['outcome']=='person_summary'}
    assert statuses=={'a':'failed','b':'published'} and report['state']=='completed'


@pytest.mark.parametrize('limit,other_published', [(1, False), (2, True)])
def test_admin_job_busy_does_not_block_maintenance_or_repeat_events(store, limit, other_published):
    from threading import Event
    from iris.person_summary import PersonSummaryJobs
    clock, model, gateway, run, config = prepare(store, limit=limit)
    entered, release = Event(), Event()
    def block():
        entered.set()
        assert release.wait(20), 'test did not release admin generation'
    jobs = PersonSummaryJobs(store, Model(store, hook=block), clock=clock)
    try:
        attempt = jobs.submit('a', expected_version=0)
        assert entered.wait(5)
        maintenance = Maintenance(store, gateway=model, clock=clock)
        maintenance.run(run['id'])
        report = maintenance.report(run['id'])
        assert report['state'] == 'completed'
        results = {i['details']['subject_id']:i for i in report['items'] if i['outcome']=='person_summary'}
        assert results['a']['details']['status'] == 'skipped'
        assert results['a']['reason'] == 'generation_in_progress'
        assert (current_summary(store, 'b') is not None) == other_published
        with store.read() as conn:
            events = conn.execute('SELECT COUNT(*) FROM runtime_events').fetchone()[0]
            assert conn.execute('SELECT state FROM person_summary_attempts WHERE id=?', (attempt,)).fetchone()[0] == 'running'
            assert conn.execute('SELECT COUNT(*) FROM person_summary_attempts WHERE run_id=?', (run['id'],)).fetchone()[0] == limit
        for _ in range(3): maintenance.run(run['id'])
        with store.read() as conn:
            assert conn.execute('SELECT COUNT(*) FROM runtime_events').fetchone()[0] == events
    finally:
        release.set()
        jobs.close()
    assert current_summary(store, 'a')['version'] == 1


def test_discovery_builds_names_once_and_uses_precomputed_unique_labels(store, monkeypatch):
    from iris import person_summary as ps
    clock, model, gateway, run, config = prepare(store)
    with store.write() as conn:
        conn.execute("UPDATE memories SET lifecycle='forgotten'")
        conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES('c','person','甲同学',?)", (clock().isoformat(),))
    original = ps._SummaryNames
    built = []
    def names(conn):
        result = original(conn); built.append(result)
        return result
    monkeypatch.setattr(ps, '_SummaryNames', names)
    assert ps.update_summaries_for_run(store, gateway, run, config, clock=clock)
    assert len(built) == 1
    assert '甲同学' not in built[0].unique_labels.get('a', ())
    assert built[0].unique_labels['b'] == ['乙同学']


def test_memory_consolidation_does_not_spend_person_summary_budget(store, monkeypatch):
    clock, model, gateway, run, config = prepare(store, budget=4, limit=2)
    config['merge_enabled'] = True
    config['max_calls'] = 1
    store.set_setting('consolidation', config)
    with store.write() as conn:
        conn.execute('UPDATE consolidation_runs SET settings_json=? WHERE run_id=?', (dumps(config),run['id']))
    order = []
    def plan(owner, run, config):
        with store.write() as conn:
            wid = conn.execute("""INSERT INTO consolidation_work(fingerprint,kind,payload_json,importance,changed_at,created_at)
                VALUES('review-order','dependency','{}',100,?,?)""", (clock().isoformat(),clock().isoformat())).lastrowid
            conn.execute('INSERT INTO consolidation_run_work(run_id,work_id) VALUES(?,?)', (run['id'],wid))
            conn.execute('UPDATE consolidation_runs SET planned=1 WHERE run_id=?', (run['id'],))
    def decide(owner, work, config, stop):
        order.append('memory')
        owner.reserve(work, 'consolidation_dependency')
        with store.write() as conn:
            conn.execute("UPDATE consolidation_work SET state='decided',result_json='{}' WHERE id=?", (work['id'],))
        return True
    def apply(owner, work, payload, result, config, blocked):
        order.append('applied')
        with store.write() as conn: owner.record(conn, work, 'skipped', 'no_changes')
    monkeypatch.setattr(Consolidation, 'plan', plan)
    monkeypatch.setattr(Consolidation, '_decide', decide)
    monkeypatch.setattr(Consolidation, '_apply_work', apply)
    model.hook = lambda: order.append('summary')
    maintenance = Maintenance(store, gateway=model, clock=clock)
    maintenance.run(run['id'])
    assert order == ['memory', 'applied', 'summary']
    assert len(model.calls) == 4
    report = maintenance.report(run['id'])
    assert report['state'] == 'completed'
    assert sum(i['outcome']=='person_summary' and i['details']['status']=='published' for i in report['items']) == 2
    with store.read() as conn:
        purposes = [r[0] for r in conn.execute('SELECT purpose FROM consolidation_calls ORDER BY id')]
    assert purposes == ['consolidation_dependency',
                        'consolidation_person_summary_generate','consolidation_person_summary_check',
                        'consolidation_person_summary_generate','consolidation_person_summary_check']
