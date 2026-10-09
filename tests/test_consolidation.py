"""CO-01 invariants, exercised with local models before any real probe."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from iris.consolidation import Consolidation, merge_exclusion, snapshot
from iris.maintenance import Maintenance
from iris.memory_ops import edit_memory, delete_memory, confirm_retention
from iris.models import ModelError, ModelReply
from iris.retrieval import Retrieval
from test_retrieval import put
from test_lifecycle import Clock
from conftest import msg


class Model:
    def __init__(self, store, answer=None, hook=None):
        self.store, self.answer, self.hook = store, answer, hook
        self.requests = []
        self.health = None

    def chat(self, messages, purpose, max_tokens=16000, **kwargs):
        assert not self.store._writer.in_transaction
        payload = json.loads(messages[-1]['content'])
        self.requests.append((purpose, payload))
        if self.hook:
            self.hook(payload)
        value = self.answer(payload) if callable(self.answer) else self.answer
        if isinstance(value, Exception):
            raise value
        if value is None:
            value = {'decision': 'separate', 'reason': '不同事实', 'evidence': [], 'updates': []}
        return ModelReply(json.dumps(value, ensure_ascii=False), 'stop', {'prompt_tokens': 20, 'completion_tokens': 10})


def pair(store):
    first = msg(store, 1, '我每周教口琴，已经固定了。')
    second = msg(store, 2, '口琴课还是我每周教。')
    a = put(store, '我每周教口琴', evidence=[first], importance=80)
    b = put(store, '我固定每周教口琴课', evidence=[second], importance=80)
    return a, b


def merge_answer(payload):
    return {'decision': 'merge', 'keep_id': payload['preferred_keep_id'], 'reason': '原话确认同一每周课程，保留较完整的一条',
            'evidence': [s['id'] for m in payload['memories'] for s in m['sources']], 'updates': []}


def run(store, model=None, **kwargs):
    engine = Maintenance(store, gateway=model, clock=Clock())
    rid = engine.request()
    engine.run(rid, **kwargs)
    return engine, rid, engine.report(rid)


def test_D05_sources_placeholder_belief_and_history(store):
    a, b = pair(store)
    with store.write() as conn:
        conn.execute("UPDATE memories SET lifecycle='forgotten',retention=15,forgotten_at=? WHERE id=?", (Clock()().isoformat(), a))
    _, _, report = run(store, Model(store, merge_answer))
    with store.read() as conn:
        left, right = snapshot(conn, a), snapshot(conn, b)
        assert left['lifecycle'] == 'deleted' and left['merged_into'] == b
        assert right['lifecycle'] == 'active' and right['belief'] == 70
        assert len(right['sources']) == 2
        assert conn.execute('SELECT COUNT(*) FROM memory_revisions').fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM admin_operations WHERE action='consolidation_merge'").fetchone()[0] == 1
    assert report['summary']['merged']['count'] == 1
    assert confirm_retention(store, a) is None
    assert not edit_memory(store, a, left['revision'], content='复活')
    assert a not in [m['id'] for m in Retrieval(store).search(text='口琴', include_forgotten=True)['memories']]


@pytest.mark.parametrize('actor', ['admin', 'learning'])
def test_D02_concurrent_revision_skip_and_retry(store, actor):
    a, b = pair(store)
    def mutate(payload):
        assert edit_memory(store, a, 1, content='我每周教口琴，新安排', actor=actor)
    model = Model(store, merge_answer, mutate)
    _, _, report = run(store, model)
    assert report['summary']['skipped']['reasons']['revision_conflict'] >= 1
    with store.read() as conn:
        assert all(snapshot(conn, i)['lifecycle'] == 'active' for i in (a,b))


def test_D01_prepare_during_model_call(store):
    a, b = pair(store)
    entered, release = threading.Event(), threading.Event()
    def pause(payload):
        entered.set()
        assert release.wait(10)
    model = Model(store, merge_answer, pause)
    engine = Maintenance(store, gateway=model, clock=Clock())
    rid = engine.request()
    with ThreadPoolExecutor() as pool:
        future = pool.submit(engine.run, rid)
        try:
            assert entered.wait(10)
            assert Retrieval(store).prepare('A', text='口琴', recent_limit=0)['recall_id']
        finally:
            release.set()
        future.result()


def test_D03_stop_resumes_without_repeating_judged_calls(store):
    pair(store)
    model = Model(store, merge_answer)
    engine = Maintenance(store, gateway=model, clock=Clock())
    rid = engine.request()
    engine.run(rid, stop=lambda: bool(model.requests))
    assert len(model.requests) == 1
    Maintenance(store, gateway=model, clock=Clock()).run(rid)
    assert len(model.requests) == 1
    assert engine.report(rid)['summary']['merged']['count'] == 1


@pytest.mark.parametrize('protection', ['pinned', 'admin'])
def test_protection_forbids_merge_and_body_or_belief_changes(store, protection):
    a, b = pair(store)
    if protection == 'admin':
        edit_memory(store, a, 1, content='我固定每周教口琴')
    else:
        with store.write() as conn:
            conn.execute('UPDATE memories SET pinned=1 WHERE id=?', (a,))
    with store.read() as conn:
        before = snapshot(conn,a)
    def conflict(p):
        return {'decision':'conflict','reason':'新说法需要核对','evidence': [s['id'] for m in p['memories'] for s in m['sources']],
                'updates':[{'id':a,'content':'不教了','belief':30,'annotation':'新旧说法争议，请管理员核对'}]}
    _, _, report = run(store, Model(store, conflict))
    with store.read() as conn:
        after = snapshot(conn,a)
    assert after['content'] == before['content'] and after['belief'] == before['belief']
    assert not after['merged_into'] and after['annotations']


def test_conflict_original_pair_is_never_merged_same_run(store):
    a, b = pair(store)
    def conflict(p):
        return {'decision':'conflict','reason':'旧摘要丢限定','evidence':[s['id'] for m in p['memories'] for s in m['sources']],
                'updates':[{'id':a,'content':p['memories'][1]['content'],'annotation':'此前摘要范围过宽，按原话修正'}]}
    model = Model(store, conflict)
    _, _, report = run(store, model)
    assert report['summary']['conflicts']['count'] == 1
    with store.read() as conn:
        assert not snapshot(conn,a)['merged_into'] and not snapshot(conn,b)['merged_into']


def test_budget_pause_and_missing_gateway_are_reported(store):
    pair(store)
    store.set_setting('consolidation', {'max_calls':0})
    model = Model(store)
    _, _, report = run(store, model)
    assert not model.requests
    assert report['summary']['skipped']['reasons']['call_budget']
    store.set_setting('consolidation', {'max_calls':50})
    _, _, report = run(store)
    assert report['summary']['skipped']['reasons']['unconfigured']
    model.health = SimpleNamespace(check=lambda *a, **k: (_ for _ in ()).throw(ModelError('paused','paused',paused=True,reason='usage_limit')))
    _, _, report = run(store, model)
    assert not model.requests
    assert report['summary']['skipped']['reasons']['usage_limit']
    _, _, report = run(store, Model(store, merge_answer))
    assert report['summary']['merged']['count'] == 1


@pytest.mark.parametrize('decision', ['keep','weaken','review'])
def test_dependency_outcomes_no_extra_retention_penalty(store, decision):
    a = put(store, '我租过厨房', importance=80)
    d = put(store, '我可能经常烘焙', stance='推断', importance=80)
    with store.write() as conn:
        conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,1,?)", (d,a,Clock()().isoformat()))
    delete_memory(store,a,1)
    def answer(p):
        if 'target_id' not in p:
            return {'decision':'separate','reason':'不同事实','evidence':[],'updates':[]}
        return {'decision':decision,'reason':'唯一依据已删除，依据不足' if decision!='keep' else '仍有独立依据',
                'evidence':[],'updates': [] if decision=='keep' else [{'id':d,'annotation':'唯一依据已删除，当前依据不足'}]}
    model = Model(store, answer)
    run(store, model)
    with store.read() as conn:
        after = snapshot(conn,d)
    assert after['retention'] == 40 and after['belief'] == 70 and after['lifecycle']=='active'
    assert bool(after['annotations']) == (decision!='keep')
    count = len(model.requests)
    run(store, model)
    assert len(model.requests) == count


def test_numeric_negation_and_identity_exclusions(store):
    a,b=pair(store)
    with store.read() as conn:
        one,two=snapshot(conn,a),snapshot(conn,b)
    for changed in ({'content':'我不教口琴'}, {'speaker':'scene'}, {'stance':'推断'}, {'about':[]},
                    {'event_time':'2026-11-01T00:00:00+08:00','content':'我买十二支毛笔'}):
        left={**one,'content':'我买二十一支毛笔'} if 'event_time' in changed else one
        assert merge_exclusion(left, {**two,**changed})


def test_real_gateway_retry_attempts_share_exact_persisted_budget(store):
    import httpx
    from iris.models import Gateway, ModelConfig
    pair(store)
    store.set_setting('consolidation',{'max_calls':2})
    hits=[]
    def respond(request):
        assert not store._writer.in_transaction
        hits.append(request)
        return httpx.Response(429,json={'error':{'code':'RateLimitExceeded'}},headers={'Retry-After':'0'})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        gateway=Gateway({'chat':ModelConfig('https://example.invalid','', 'fake',reasoning_effort='high')},store,client,sleeper=lambda _:None)
        try:
            _,_,report=run(store,gateway)
        finally:
            gateway.close()
    assert len(hits)==2
    assert report['summary']['model_calls']['count']==2
    assert report['summary']['skipped']['reasons']['call_budget']
    with store.read() as conn:
        assert [tuple(r) for r in conn.execute('SELECT purpose,result_category,reasoning_effort FROM model_calls')]==[
            ('consolidation_merge','retryable','high')]*2


def test_source_snapshot_limit_and_deleted_parent_not_reintroduced(store):
    from iris.consolidation import material, dependency_material
    evidence=[msg(store,i,'原话'*200) for i in range(1,7)]
    a=put(store,'基础事实',evidence=evidence)
    b=put(store,'推断',stance='推断')
    with store.write() as conn:
        conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,1,?)",(b,a,Clock()().isoformat()))
    with store.read() as conn:
        value=material(snapshot(conn,a))
    assert [s['id'] for s in value['sources']]==[evidence[0],*evidence[-3:]]
    assert all(len(s['text'].encode())<=300 for s in value['sources'])
    delete_memory(store,a,1)
    with store.read() as conn:
        payload=dependency_material(conn,snapshot(conn,b))
    deleted=material(payload['memories'][1])
    assert deleted['content'] is None and deleted['sources']==[]


def test_transitive_loss_and_forgetting_preserve_belief_without_model_penalty(store):
    a=put(store,'我每周维修自行车',importance=80)
    b=put(store,'我复述每周维修自行车',importance=80,stance='转述')
    d=put(store,'我可能熟悉维修自行车',importance=80,stance='推断')
    with store.write() as conn:
        for child,parent in ((b,a),(d,b),(d,a)):
            conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,1,?)",(child,parent,Clock()().isoformat()))
        conn.execute("UPDATE memories SET lifecycle='forgotten',retention=19,forgotten_at=? WHERE id=?",(Clock()().isoformat(),a))
    def answer(p):
        return {'decision':'weaken','reason':'唯一源头已遗忘，支持不足','evidence':[],
                'updates':[{'id':p['target_id'],'belief':1,'annotation':'唯一源头已遗忘，支持不足'}]}
    model=Model(store,answer)
    run(store,model)
    assert len(model.requests)==2
    run(store,model)
    assert len(model.requests)==2
    with store.read() as conn:
        for mid in (b,d):
            value=snapshot(conn,mid)
            assert value['belief']==70 and value['retention']==40 and value['annotations']
        assert len(conn.execute('SELECT * FROM memory_dependency_losses WHERE applied_at IS NOT NULL').fetchall())==2


def test_old_recall_cannot_feedback_absorbed_id(store):
    a,b=pair(store)
    retrieval=Retrieval(store)
    recall=retrieval.search(text='口琴')
    run(store,Model(store,merge_answer))
    with pytest.raises(KeyError):
        retrieval.feedback(recall['recall_id'],[a])
