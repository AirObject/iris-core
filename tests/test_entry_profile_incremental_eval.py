"""Production SQLite replay uses daily arrivals and never lends future evidence."""
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from iris.db import dumps
from iris.models import ModelError
from test_entry_profile_incremental import Gateway, propose, source, accept
from evals.entry_profile_eval.memory_runner import run_sqlite


def corpus(tmp_path):
    value={'format_version':1,'cases':[{'id':'unit-daily','entry':{'id':'group','kind':'group'},
        'as_of':'2026-10-03T23:00:00+08:00','timezone':'Asia/Shanghai',
        'messages':[
            {'id':'a','sender':'甲','text':'轻量背包与结实背包各有什么取舍？','at':'2026-10-01T12:00:00+08:00'},
            {'id':'b','sender':'乙','text':'防水鞋与透气鞋怎样选择？','at':'2026-10-03T12:00:00+08:00'}],
        'memories':[],'initial_profile':'','must_cover':['徒步装备'],'forbidden':[]} ]}
    path=tmp_path/'input.json';path.write_text(dumps(value));return path


def test_daily_replay_uses_sequential_arrivals_and_final_tail(tmp_path):
    gateways=[]
    def factory(case,store):
        def observe(p):
            with store.read() as conn:
                count=conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
            assert count==1 if p['as_of'].startswith('2026-10-02') else count==2
            if p['profiles']:
                return {'observations':[{'op':'confirm','memory_id':p['profiles'][0]['memory_id'],
                    'source_refs':[source(p['messages'][0])]}]}
            return propose(p)
        g=Gateway(store,observe);gateways.append(g);return g
    out=tmp_path/'out';manifest=run_sqlite(corpus(tmp_path),out,gateway_factory=factory)
    assert manifest['metadata']['replay_mode']=='daily_plus_final_flush'
    assert not manifest['metadata']['gate_eligible']
    material=json.loads((out/'judging-materials/cases/0001.json').read_text())['input']
    assert len(material['memories'])==1 and len(material['memories'][0]['sources'])==2
    assert material['stages']['generated'] and material['stages']['deterministic']
    assert len(gateways[0].calls)==3
    assert gateways[0].calls[0]['payload']['as_of'].startswith('2026-10-02T03:00:00')
    assert gateways[0].calls[1]['payload']['as_of'].startswith('2026-10-03T23:00:00')
    assert not any(k in dumps(material) for k in ('decision','safe_sources','checks_json','outputs_json'))


def test_timeout_stops_daily_replay_without_retrying_a_tainted_round(tmp_path):
    gateways=[]
    def factory(case,store):
        g=Gateway(store,lambda p:(_ for _ in ()).throw(ModelError('retryable','late',reason='timeout')))
        gateways.append(g);return g
    out=tmp_path/'out';manifest=run_sqlite(corpus(tmp_path),out,gateway_factory=factory)
    assert manifest['metadata']['invalidated']['reason']=='timeout'
    assert len(gateways[0].calls)==1
    assert (out/'progress.json').exists()
