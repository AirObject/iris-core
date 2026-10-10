"""The new step shares the existing dream call ledger, independent of persona."""
import json

import pytest

from iris.db import dumps
from iris.maintenance import Maintenance
from iris.entry_profile import current_profile, EntryProfileEngine
from iris.consolidation import Consolidation
from test_entry_profile import Clock, Model, messages


def dream(store, clock, **settings):
    store.set_setting('consolidation',dict(enabled=True,max_calls=50,merge_enabled=False,
        conflict_enabled=False,dependency_enabled=False,persona_enabled=False,goal_review_enabled=False,**settings))
    engine=Maintenance(store,gateway=Model(store),clock=clock)
    return engine,engine.request()


def test_profiles_run_when_other_model_steps_are_disabled_and_not_repeated(store):
    clock=Clock(); messages(store,clock)
    engine,rid=dream(store,clock)
    engine.run(rid)
    first=current_profile(store,'group')
    assert first['status']=='published'
    engine.run(rid)
    assert current_profile(store,'group')['version']==first['version']
    report=engine.report(rid)
    assert report['summary']['model_calls']['count']==2
    assert [i for i in report['items'] if i['outcome']=='entry_profile']
    with store.read() as conn:
        assert [r[0] for r in conn.execute('SELECT purpose FROM consolidation_calls ORDER BY id')]==[
            'consolidation_entry_profile_generate','consolidation_entry_profile_check']


@pytest.mark.parametrize('setting,value',[('enabled',False),('entry_profile_enabled',False),('max_calls',0),('max_calls',1)])
def test_switches_and_small_budgets_do_not_call(store,setting,value):
    clock=Clock(); messages(store,clock)
    engine,rid=dream(store,clock)
    with store.write() as conn:
        config=json.loads(conn.execute('SELECT settings_json FROM consolidation_runs WHERE run_id=?',(rid,)).fetchone()[0])
        config[setting]=value
        conn.execute('UPDATE consolidation_runs SET settings_json=? WHERE run_id=?',(dumps(config),rid))
    engine.run(rid)
    assert not engine.gateway.calls
    assert current_profile(store,'group') is None


def test_two_entries_share_run_cap_and_budget_skips_do_not_consume_daily_generation(store):
    clock=Clock(); messages(store,clock); messages(store,clock,entry='second')
    engine,rid=dream(store,clock)
    with store.write() as conn:
        config=json.loads(conn.execute('SELECT settings_json FROM consolidation_runs WHERE run_id=?',(rid,)).fetchone()[0])
        config['max_calls']=2
        conn.execute('UPDATE consolidation_runs SET settings_json=? WHERE run_id=?',(dumps(config),rid))
    engine.run(rid)
    assert len(engine.gateway.calls)==2
    assert current_profile(store,'group') and current_profile(store,'second') is None
    another=EntryProfileEngine(store,Model(store),clock=clock).regenerate('second',expected_version=0)
    assert another['status']=='published'


def test_gateway_reserves_checker_on_each_generation_retry(store):
    clock=Clock(); messages(store,clock)
    _,rid=dream(store,clock)
    with store.write() as conn:
        config=json.loads(conn.execute('SELECT settings_json FROM consolidation_runs WHERE run_id=?',(rid,)).fetchone()[0]);config['max_calls']=2
        conn.execute('UPDATE consolidation_runs SET settings_json=? WHERE run_id=?',(dumps(config),rid))
    owner=Consolidation(store,Model(store),clock=clock);owner.run_id=rid
    owner.reserve({'id':None},'consolidation_entry_profile_generate')
    from iris.models import ModelError
    with pytest.raises(ModelError) as error:
        owner.reserve({'id':None},'consolidation_entry_profile_generate')
    assert error.value.reason=='call_budget'
    owner.reserve({'id':None},'consolidation_entry_profile_check')


def test_interrupted_attempt_is_reported_without_publishing_or_repeating(store,monkeypatch):
    clock=Clock();messages(store,clock)
    engine,rid=dream(store,clock)
    original=engine.gateway.chat
    def interrupt(messages,purpose,*args,**kwargs):
        if purpose.endswith('check'):raise KeyboardInterrupt()
        return original(messages,purpose,*args,**kwargs)
    engine.gateway.chat=interrupt
    with pytest.raises(KeyboardInterrupt):engine.run(rid)
    assert current_profile(store,'group') is None
    engine.gateway.chat=original
    engine.run(rid)
    with store.read() as conn:
        attempt=conn.execute('SELECT * FROM entry_profile_attempts WHERE run_id=?',(rid,)).fetchone()
        assert attempt['state']=='failed' and attempt['reason']=='interrupted'
    assert len(engine.gateway.calls)==1
    assert [i for i in engine.report(rid)['items'] if i['outcome']=='entry_profile']
