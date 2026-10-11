"""Exercise the future call site explicitly; consolidation.py is not changed."""
import json

import pytest

from iris.consolidation import Consolidation, _PersonaGateway
from iris.db import dumps
from iris.maintenance import Maintenance
from iris.person_summary import current_summary, update_summaries_for_run
from test_person_summary import Clock, Model, seed, memory


def prepare(store, *, budget=50, limit=10):
    clock=Clock(); seed(store,clock)
    for i in range(8): memory(store,clock,f'乙同学测试{i}',speaker='b',days_ago=3)
    config={'enabled':True,'max_calls':budget,'merge_enabled':False,'conflict_enabled':False,
            'dependency_enabled':False,'persona_enabled':False,'goal_review_enabled':False,'entry_profile_enabled':False}
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


def test_phase_one_has_no_automatic_consolidation_hook(store):
    clock,model,gateway,run,config=prepare(store)
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


def test_person_summary_purge_read_projection_marks_late_ledger(store):
    from iris import admin_data
    from iris.memory_ops import purge_memory
    clock,model,gateway,run,config=prepare(store,limit=1)
    model.hook=lambda:purge_memory(store,1,1,confirm=True)
    update_summaries_for_run(store,gateway,run,config,clock=clock)
    report=admin_data.maintenance_redactions(store,{'id':run['id'],'items':[]})
    assert len(report['consolidation_calls'])==1
    assert report['consolidation_calls'][0]['redacted']
    assert report['consolidation_calls'][0]['redaction_reason']=='因彻底清除而清空'
