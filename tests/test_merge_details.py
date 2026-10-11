"""MG2: semantic detail checks with local models only."""
import json

import pytest

from iris.consolidation import snapshot, confirm_annotation, clear_annotation
from iris.maintenance import Maintenance
from iris.models import ModelReply
from iris.retrieval import Retrieval
from test_consolidation import pair, merge_answer, run
from test_lifecycle import Clock


class DetailModel:
    health = None

    def __init__(self, store, details, keep=None):
        self.store, self.details, self.keep = store, details, keep
        self.requests = []

    def chat(self, messages, purpose, max_tokens=16000, **kwargs):
        assert not self.store._writer.in_transaction
        payload = json.loads(messages[-1]['content'])
        self.requests.append(payload)
        result = merge_answer(payload)
        result['unique_details'] = self.details
        if self.keep is not None:
            result['keep_id'] = self.keep
        return ModelReply(json.dumps(result, ensure_ascii=False), 'stop', {'prompt_tokens': 20, 'completion_tokens': 10})


def bodies(store, a, b, left, right):
    # Fixture setup, with matching original evidence and no manual-edit protection.
    with store.write() as conn:
        for mid, text in ((a, left), (b, right)):
            conn.execute('UPDATE memories SET content=? WHERE id=?', (text, mid))
            conn.execute('UPDATE messages SET content=? WHERE id IN (SELECT message_id FROM sources WHERE memory_id=?)', (text, mid))


@pytest.mark.parametrize('side', ['a', 'b'])
def test_one_sided_details_override_model_keep_and_length(store, side):
    a, b = pair(store)
    detailed = a if side == 'a' else b
    plain = b if side == 'a' else a
    texts = {detailed: '我每周教口琴，在线。', plain: '关于口琴课程，我确实是每周都在教口琴课程。'}
    bodies(store, a, b, texts[a], texts[b])
    model = DetailModel(store, {'a': ['在线'] if side == 'a' else [], 'b': ['在线'] if side == 'b' else []}, plain)
    _, _, report = run(store, model)
    with store.read() as conn:
        survivor, absorbed = snapshot(conn, detailed), snapshot(conn, plain)
    assert survivor['content'] == texts[detailed] and survivor['lifecycle'] == 'active'
    assert absorbed['merged_into'] == detailed and absorbed['lifecycle'] == 'deleted'
    assert len(survivor['sources']) == 2 and survivor['belief'] == 70
    assert report['summary']['merged']['count'] == 1
    assert len(model.requests) == 1


def test_bilateral_details_make_admin_only_suggestion_and_resume_once(store):
    a, b = pair(store)
    bodies(store, a, b, '我每周在线教口琴', '我每周教口琴，面向初学者')
    model = DetailModel(store, {'a': ['在线'], 'b': ['面向初学者']})
    engine = Maintenance(store, gateway=model, clock=Clock())
    rid = engine.request()
    def reached_model():
        with store.read() as conn:
            return conn.execute('SELECT phase FROM maintenance_runs WHERE id=?',(rid,)).fetchone()[0]>=5
    engine.run(rid,stop=reached_model)
    before_recall = Retrieval(store,clock=Clock()).search(text='口琴')['memories']
    with store.read() as conn:
        before = [snapshot(conn, mid) for mid in (a,b)]
    engine.run(rid, stop=lambda: bool(model.requests))
    engine.run(rid)
    engine.run(rid)
    report = engine.report(rid)
    with store.read() as conn:
        after = [snapshot(conn, mid) for mid in (a,b)]
        assert conn.execute('SELECT COUNT(*) FROM memory_revisions').fetchone()[0] == 0
    for old, new in zip(before, after):
        assert all(old[k] == new[k] for k in ('content','belief','revision','lifecycle'))
        assert len(new['annotations']) == 1
        note = new['annotations'][0]
        assert note['kind'] == 'disputed' and note['visibility'] == 'admin_only'
        assert '在线' in note['text'] and '面向初学者' in note['text'] and '合并' in note['text']
    item = next(i for i in report['items'] if i['phase'] == 'consolidation' and i['outcome']=='conflicts')
    assert item['details']['suggestion_type'] == 'merge_details'
    assert item['details']['model_suggestion'] is True
    assert len(model.requests) == 1
    assert not [i for i in report['items'] if i['outcome']=='failed']
    # Decay is deliberately separate from the suggestion and does not affect recall fields.
    assert Retrieval(store,clock=Clock()).search(text='口琴')['memories'] == before_recall
    note = after[0]['annotations'][0]
    assert confirm_annotation(store, a, note['id'], after[0]['revision'], clock=Clock())
    assert clear_annotation(store, a, note['id'], after[0]['revision'], clock=Clock())
    assert Retrieval(store,clock=Clock()).search(text='口琴')['memories'] == before_recall
    run(store,model)
    assert len(model.requests)==1  # Cleared suggestions do not return on the next run.


@pytest.mark.parametrize('details', [
    {'a': ['不在原文里的细节'], 'b': []},
    {'a': ['固定'], 'b': []},  # A must not cite B's wording.
    {'a': ['  '], 'b': []}, {'a': [3], 'b': []},
    {'a': '每周', 'b': []}, {'a': []}, None,
])
def test_invalid_detail_quote_is_uncertain_without_repair(store, details):
    a,b = pair(store)
    model = DetailModel(store, details)
    _,_,report = run(store, model)
    with store.read() as conn:
        assert all(snapshot(conn, mid)['lifecycle'] == 'active' for mid in (a,b))
        result = json.loads(conn.execute('SELECT result_json FROM consolidation_work').fetchone()[0])
        assert result['decision'] == 'uncertain'
        assert result['merge_detail_check']['outcome'] == 'invalid_details'
    assert len(model.requests) == 1
    assert not any(i['outcome'] == 'merged' for i in report['items'])


def test_detail_quotes_normalize_whitespace_only(store):
    a,b = pair(store)
    bodies(store, a,b,'我每周教口琴，online   course', '我每周教口琴')
    _,_,report = run(store, DetailModel(store, {'a':['online\n course'], 'b':[]}, b))
    assert report['summary']['merged']['count'] == 1
    with store.read() as conn:
        assert snapshot(conn,a)['content'] == '我每周教口琴，online   course'


def test_legacy_decided_merge_cannot_bypass_detail_check(store):
    a,b = pair(store)
    model = DetailModel(store, {'a':[], 'b':[]})
    engine = Maintenance(store,gateway=model,clock=Clock())
    rid = engine.request()
    engine.run(rid,stop=lambda:bool(model.requests))
    with store.write() as conn:
        work = conn.execute('SELECT * FROM consolidation_work').fetchone()
        old = json.loads(work['result_json'])
        old.pop('unique_details',None)
        old.pop('merge_detail_check',None)
        conn.execute('UPDATE consolidation_work SET result_json=? WHERE id=?',(json.dumps(old),work['id']))
    engine.run(rid)
    with store.read() as conn:
        assert all(snapshot(conn,mid)['lifecycle']=='active' for mid in (a,b))
    assert len(model.requests) == 1


@pytest.mark.parametrize('pending', [False,True])
def test_upgrade_rechecks_old_pairs_without_running_old_queue_twice(store, monkeypatch, pending):
    import iris.consolidation as consolidation
    from test_consolidation import Model
    pair(store)
    current=consolidation.MERGE_PROMPT_VERSION
    monkeypatch.setattr(consolidation,'MERGE_PROMPT_VERSION','consolidation_pair_v1')
    if pending:
        store.set_setting('consolidation',{'max_calls':0})
    run(store,Model(store))
    monkeypatch.setattr(consolidation,'MERGE_PROMPT_VERSION',current)
    store.set_setting('consolidation',{'max_calls':50})
    model=DetailModel(store,{'a':[],'b':[]})
    _,_,report=run(store,model)
    assert len(model.requests)==1 and report['summary']['merged']['count']==1


@pytest.mark.parametrize('actor', ['admin','learning'])
def test_mutual_detail_suggestion_checks_revision_before_annotation(store, actor):
    from iris.memory_ops import edit_memory
    a,b=pair(store)
    bodies(store,a,b,'我每周在线教口琴','我每周教口琴，面向初学者')
    class Concurrent(DetailModel):
        def chat(self,*args,**kwargs):
            result=super().chat(*args,**kwargs)
            assert edit_memory(store,a,1,content='我每周在线教口琴，安排已变',actor=actor)
            return result
    _,_,report=run(store,Concurrent(store,{'a':['在线'],'b':['面向初学者']}))
    assert report['summary']['skipped']['reasons']['revision_conflict']>=1
    with store.read() as conn:
        assert not snapshot(conn,a)['annotations'] and not snapshot(conn,b)['annotations']
        assert not snapshot(conn,a)['merged_into'] and not snapshot(conn,b)['merged_into']


def test_detail_claims_cannot_override_pinned_protection(store):
    a,b=pair(store)
    with store.write() as conn:
        conn.execute('UPDATE memories SET pinned=1 WHERE id=?',(b,))
    _,_,report=run(store,DetailModel(store,{'a':[],'b':['固定']}))
    with store.read() as conn:
        assert all(snapshot(conn,mid)['lifecycle']=='active' for mid in (a,b))
    assert report['summary']['failed']['count']==1
