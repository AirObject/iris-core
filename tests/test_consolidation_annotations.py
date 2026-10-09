"""Report-only policy: fake models, metadata lifecycle and additive projections."""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin
from iris.api import create_app
from iris.consolidation import snapshot
from iris.memory_ops import edit_memory
from iris.retrieval import Retrieval
from test_consolidation import Model, pair, run


def conclusion(a, b, *, kind='disputed'):
    update={'id':a,'annotation':'旧安排已经被后来的安排替代，历史事实仍成立', 'assessment':kind}
    if kind=='superseded':
        update['superseded_by']=b
    return {'decision':'conflict','reason':'双方原话描述了不同时间的安排','evidence':[1,2], 'updates':[update]}


@pytest.mark.parametrize('protection', ['none','pinned','admin'])
def test_report_only_preserves_all_memory_values_and_stores_sources(store, protection):
    a,b=pair(store)
    if protection=='admin':
        edit_memory(store,a,1,content='我每周教口琴，旧安排')
    elif protection=='pinned':
        with store.write() as conn:
            conn.execute('UPDATE memories SET pinned=1 WHERE id=?',(a,))
    with store.read() as conn:
        old=snapshot(conn,a)
    _,rid,report=run(store,Model(store,conclusion(a,b,kind='superseded')))
    with store.read() as conn:
        new=snapshot(conn,a)
    for field in ('content','belief','revision','lifecycle'):
        assert new[field]==old[field]
    annotation=new['annotations'][0]
    assert annotation['kind']=='superseded' and annotation['superseded_by']==b
    assert annotation['report']['run_id']==rid and not annotation['modified_since_annotation']
    item=next(i for i in report['items'] if i['outcome']=='conflicts')
    assert {x['memory_id'] for x in item['details']['source_excerpts']}=={a,b}
    assert all(x['sources'] for x in item['details']['source_excerpts'])
    assert item['details']['decision']=='conflict' and item['details']['report']


def test_admin_clear_is_authenticated_revision_checked_and_does_not_reappear(store):
    a,b=pair(store)
    model=Model(store,conclusion(a,b))
    run(store,model)
    with TestClient(create_app(store=store, configs={}),base_url='http://127.0.0.1',client=('127.0.0.1',1234)) as client:
        client.app.state.scheduler.stop()
        path=f'/admin/api/memories/{a}'
        assert client.get(path).status_code in (401,409)
        login_admin(client)
        detail=client.get(path).json()
        annotation=detail['consolidation_annotations'][0]
        assert client.get(annotation['report']['url']).status_code==200
        clear=f"{path}/annotations/{annotation['id']}"
        assert client.request('DELETE',clear,json={'expected_revision':999}).status_code==409
        response=client.request('DELETE',clear,json={'expected_revision':detail['revision']})
        assert response.status_code==200
        assert response.json()['consolidation_annotations']==[]
        assert response.json()['revision']==detail['revision']
    # A new content revision can cause another model review, but the same pair /
    # conclusion remains cleared even if the model rephrases its explanation.
    assert edit_memory(store,a,detail['revision'],content='我每周教口琴，旧安排',actor='learning')
    run(store,model)
    with store.read() as conn:
        assert snapshot(conn,a)['annotations']==[]
        assert conn.execute('SELECT cleared_at FROM consolidation_annotations WHERE id=?',(annotation['id'],)).fetchone()[0]
        assert conn.execute("SELECT COUNT(*) FROM admin_operations WHERE action='consolidation_annotation_clear'").fetchone()[0]==1


def test_annotations_survive_edits_and_retrieval_only_adds_metadata(store, monkeypatch):
    from datetime import datetime
    from test_lifecycle import Clock
    class Frozen(datetime):
        @classmethod
        def now(cls,tz=None): return Clock()()
    monkeypatch.setattr('iris.retrieval.datetime',Frozen)
    from iris import admin_data
    a,b=pair(store)
    retrieval=Retrieval(store)
    before=retrieval.prepare('A',text='口琴',recent_limit=0,judge=False)['memories']
    run(store,Model(store,conclusion(a,b)))
    after=retrieval.prepare('A',text='口琴',recent_limit=0,judge=False)['memories']
    strip=lambda rows:[{k:v for k,v in m.items() if k!='consolidation_annotations'} for m in rows]
    assert strip(after)==strip(before)
    assert next(m for m in after if m['id']==a)['consolidation_annotations']
    assert retrieval.search(text='口琴')['memories'][0]['consolidation_annotations']
    detail=admin_data.memory_detail(store,a)
    assert edit_memory(store,a,detail['revision'],content='我每周教口琴，后来改了时间')
    annotation=admin_data.memory_detail(store,a)['consolidation_annotations'][0]
    assert annotation['modified_since_annotation'] and annotation['status_label']=='标注后已修改'


def test_interrupted_conflict_classification_is_not_repeated(store):
    from iris.maintenance import Maintenance
    from test_lifecycle import Clock
    a,b=pair(store)
    model=Model(store,conclusion(a,b))
    engine=Maintenance(store,gateway=model,clock=Clock())
    rid=engine.request()
    engine.run(rid,stop=lambda:len(model.requests)==1)
    assert len(model.requests)==1
    engine.run(rid)
    assert len(model.requests)==2
    assert [p for p,_ in model.requests]==['consolidation_merge','consolidation_conflict']


def test_invalid_superseded_target_is_rejected_without_writes(store):
    a,b=pair(store)
    result=conclusion(a,b,kind='superseded')
    result['updates'][0]['superseded_by']=99999
    _,_,report=run(store,Model(store,result))
    assert report['summary']['failed']['count']==1
    with store.read() as conn:
        assert not snapshot(conn,a)['annotations']
        assert snapshot(conn,a)['belief']==70


def test_budget_between_classification_and_report_resumes_next_run(store):
    a,b=pair(store)
    model=Model(store,conclusion(a,b))
    store.set_setting('consolidation',{'max_calls':1})
    _,_,report=run(store,model)
    assert len(model.requests)==1 and report['consolidation']['skip_reason']=='call_budget'
    with store.read() as conn:
        assert not snapshot(conn,a)['annotations']
    store.set_setting('consolidation',{'max_calls':50})
    run(store,model)
    assert len(model.requests)==2
    with store.read() as conn:
        assert snapshot(conn,a)['annotations']


def test_admin_edit_during_report_call_skips_whole_item(store):
    a,b=pair(store)
    model=Model(store,conclusion(a,b))
    def edit_during_report(payload):
        if len(model.requests)==2:
            assert edit_memory(store,a,1,content='我现在改教别的课')
    model.hook=edit_during_report
    _,_,report=run(store,model)
    assert report['summary']['skipped']['reasons']['revision_conflict']==1
    with store.read() as conn:
        assert not snapshot(conn,a)['annotations'] and not snapshot(conn,b)['annotations']


def test_annotation_addition_does_not_invalidate_inflight_learning_revision(store):
    a,b=pair(store)
    run(store,Model(store,conclusion(a,b)))
    assert edit_memory(store,a,1,content='我现在每周教口琴',actor='learning')
    with store.read() as conn:
        assert snapshot(conn,a)['annotations'][0]['modified_since_annotation']


def test_upgrade_discards_pending_v2_decisions_and_preserves_legacy_annotations(tmp_path, monkeypatch):
    from importlib.resources import files
    from iris.db import Store, dumps
    from iris.maintenance import Maintenance
    from test_lifecycle import Clock
    root=tmp_path/'old-package';(root/'migrations').mkdir(parents=True)
    for sql in files('iris').joinpath('migrations').iterdir():
        if sql.name.endswith('.sql') and sql.name<'016_':
            (root/'migrations'/sql.name).write_text(sql.read_text(encoding='utf-8'),encoding='utf-8')
    (root/'retrieval_defaults.json').write_text(files('iris').joinpath('retrieval_defaults.json').read_text(encoding='utf-8'),encoding='utf-8')
    with monkeypatch.context() as patch:
        patch.setattr('iris.db.files',lambda name:root)
        old=Store(tmp_path/'upgrade.db')
    a,b=pair(old)
    engine=Maintenance(old,clock=Clock());rid=engine.request()
    with old.write() as conn:
        payload={'pair_ids':[a,b],'memories':[],'resolution':'rewrite_v2'}
        conn.execute('INSERT INTO consolidation_runs(run_id,settings_json,planned) VALUES(?,?,1)',
                     (rid,dumps({'resolution':'rewrite_v2','method':'strict','max_calls':50})))
        wid=conn.execute("INSERT INTO consolidation_work(fingerprint,kind,payload_json,importance,changed_at,state,result_json,created_at) VALUES('old','pair',?,80,?,'decided',?,?)",
                          (dumps(payload),Clock()().isoformat(),dumps({'decision':'conflict','updates':[{'id':a,'content':'不该写入'}]}),Clock()().isoformat())).lastrowid
        conn.execute('INSERT INTO consolidation_run_work(run_id,work_id) VALUES(?,?)',(rid,wid))
        conn.execute('INSERT INTO consolidation_annotations(memory_id,work_id,text,evidence_json,created_at) VALUES(?,?,?,?,?)',
                     (a,wid,'旧结论','[1]',Clock()().isoformat()))
    old.close()
    upgraded=Store(tmp_path/'upgrade.db')
    try:
        with upgraded.read() as conn:
            assert conn.execute('SELECT state FROM consolidation_work WHERE id=?',(wid,)).fetchone()[0]=='obsolete'
            assert conn.execute('SELECT planned FROM consolidation_runs WHERE run_id=?',(rid,)).fetchone()[0]==0
            settings=json.loads(conn.execute('SELECT settings_json FROM consolidation_runs WHERE run_id=?',(rid,)).fetchone()[0])
            assert settings['resolution']=='report_only_v1' and settings['method']=='broad'
            assert snapshot(conn,a)['annotations'][0]['text']=='旧结论'
            assert snapshot(conn,a)['content']=='我每周教口琴'
    finally:
        upgraded.close()


def test_broad_uses_complete_frozen_pair_prompt_then_separate_report_prompt(store):
    from importlib.resources import files
    a,b=pair(store)
    class Capture(Model):
        def chat(self,messages,purpose,*args,**kwargs):
            if not self.requests:
                assert messages[0]['content']==files('iris').joinpath('prompts/consolidation_pair_v1.md').read_text(encoding='utf-8')
                payload=json.loads(messages[1]['content'])
                assert 'resolution' not in payload
                assert all('annotations' not in m for m in payload['memories'])
            else:
                assert purpose=='consolidation_conflict'
                assert messages[0]['content']==files('iris').joinpath('prompts/consolidation_conflict_report_only_v1.md').read_text(encoding='utf-8')
            return super().chat(messages,purpose,*args,**kwargs)
    model=Capture(store,conclusion(a,b))
    run(store,model)
    assert len(model.requests)==2
