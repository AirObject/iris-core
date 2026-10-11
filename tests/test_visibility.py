"""R14: scope boundaries on every cognition lane, with no network calls."""
import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from conftest import FakeGateway, batch, login_admin, msg
from iris.api import create_app
from iris.consolidation import Consolidation, DEFAULTS, dependency_material, merge_exclusion, snapshot
from iris.db import now
from iris.goals import Goals, GoalError
from iris.learning import LearningEngine, PROMPT_VERSION
from iris.memory_ops import Visibility, set_entry_visibility, purge_memory
from iris.persona import select_evidence
from iris.queue import form_batch
from iris.retrieval import Retrieval
from test_retrieval import put, Embeddings


@pytest.fixture
def scene(store):
    ids = {e: msg(store, 1, '我喜欢天文摄影', entry=e, sender='我', kind='self_output') for e in ('A', 'B', 'C')}
    return ids


def scope(store, entry, mode='entry_only', visible_in=()):
    return set_entry_visibility(store, entry, mode, visible_in=list(visible_in))


def memory(store, sources, **kwargs):
    return put(store, '我喜欢天文摄影', evidence=sources, **kwargs)


def derive(store, child, *parents):
    with store.write() as conn:
        conn.executemany("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,1,?)", [(child,p,now()) for p in parents])


def ids(result):
    return {m['id'] for m in result['memories']}


def test_r14_filter_before_recall_dedup_and_keep_different_scopes(store, scene):
    scope(store, 'A')
    private = memory(store, [scene['A']], importance=100)
    public = memory(store, [scene['B']], importance=50)
    child = put(store, '摄影器材需要防潮', evidence=[scene['B']])
    derive(store, child, private)
    r = Retrieval(store)
    assert ids(r.search(entry_id='B', text='天文摄影')) == {public}
    assert {private, public} <= ids(r.search(entry_id='A', text='天文摄影'))
    assert child not in ids(r.search(entry_id='B', text='摄影器材'))
    assert ids(r.search(text='天文摄影')) == {public}


def test_visibility_intersection_lists_cycle_and_admin_only(store, scene):
    scope(store, 'A', 'entries', ['B'])
    scope(store, 'B', 'entries', ['C'])
    first = memory(store, [scene['A']])
    mixed = memory(store, [scene['A'], scene['B']])
    child = put(store, '摄影器材保养')
    derive(store, child, mixed)
    derive(store, mixed, child)  # Imported cyclic provenance must terminate.
    with store.read() as conn:
        v = Visibility(conn)
        assert v.memory(first) == frozenset({'A','B'})
        assert v.memory(child) == frozenset({'B'})
    scope(store, 'B')
    scope(store, 'A')
    with store.read() as conn:
        assert Visibility(conn).memory(child) == frozenset()
    r = Retrieval(store)
    assert all(child not in ids(r.search(entry_id=e)) for e in ('A','B','C'))
    from iris.admin_data import memory_detail
    assert memory_detail(store, child)['visibility'] == {'shared': False, 'visible_in': []}


@pytest.mark.parametrize('method', ['search', 'prepare'])
def test_settings_take_effect_after_cache_warmup_and_new_entry(store, scene, method):
    mid = memory(store, [scene['A']])
    r = Retrieval(store)
    query = lambda: r.search(entry_id='B') if method == 'search' else r.prepare('B', text='天文摄影', recent_limit=0, judge=False)
    assert mid in ids(query())
    scope(store, 'A')
    assert mid not in ids(query())
    scope(store, 'A', 'entries', ['A','B','C'])
    assert mid in ids(r.search())
    msg(store, 1, '新入口', entry='D')
    assert mid not in ids(r.search())
    scope(store, 'A', 'shared')
    assert mid in ids(query())


def test_depth_and_person_highlights_and_name_anchor(store, scene):
    person = 'guest'
    with store.write() as conn:
        conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES(?,'person','小林',?)", (person,now()))
    scope(store, 'A')
    private = put(store, '小林喜欢天文摄影', about=[person], speaker=person, evidence=[scene['A']], importance=100)
    public = put(store, '小林喜欢水彩绘画', about=[person], speaker=person, evidence=[scene['B']], importance=60)
    r = Retrieval(store)
    assert ids(r.prepare('B', text='小林', participants=[person], recent_limit=0, judge=False)) == {public}
    assert ids(r.prepare('B', text='你好', participants=[person], recent_limit=0, judge=False)) == {public}
    with store.write() as conn:
        conn.execute("UPDATE memories SET lifecycle='forgotten' WHERE id=?", (private,))
    assert private not in ids(r.search(entry_id='B', include_forgotten=True))
    assert private in ids(r.search(entry_id='A', include_forgotten=True))


def test_vector_candidates_filter_before_relative_cutoff(store, scene):
    scope(store, 'A')
    hidden = memory(store, [scene['A']], vector=[1,0])
    visible = put(store, '水彩练习', evidence=[scene['B']], vector=[.7,.714])
    r = Retrieval(store, Embeddings(store), vector_min=.5, vector_relative=.9)
    assert ids(r.search(entry_id='B', text='完全不同词')) == {visible}
    assert hidden not in ids(r.prepare('B', text='完全不同词', recent_limit=0, judge=False))


def test_goals_intersection_global_injection_and_dedup(store, scene):
    scope(store, 'A')
    store.set_setting('goal_dedup', {'method':'A'})
    goals = Goals(store)
    private = goals.create(content='准备天文摄影', entry_id='A', origin='internal', evidence=[scene['A']])['goal']['id']
    public = goals.create(content='准备天文摄影', entry_id='B', origin='internal', evidence=[scene['B']])['goal']['id']
    mixed = goals.create(content='检查摄影器材', entry_id='B', origin='internal', evidence=[scene['A']])['goal']['id']
    injected = goals.create(content='宿主摄影任务', entry_id='A')['goal']['id']
    global_goal = goals.create(content='全局摄影任务')['goal']['id']
    assert private != public
    with pytest.raises(GoalError):
        goals.merge(private, public, expected_revision=1, other_revision=1)
    r = Retrieval(store)
    assert {g['id'] for g in r.search(entry_id='B', include_goals=True)['goals']} == {public, global_goal}
    assert {g['id'] for g in r.search(include_goals=True)['goals']} == {public, global_goal}
    assert {g['id'] for g in r.prepare('A', text='', judge=False)['goals']} == {private, public, mixed, injected, global_goal}
    assert 'goals' not in r.search(entry_id='A', include_goals=False)


def test_learning_material_only_visible_and_confirmation_scope_equal(store, scene):
    scope(store, 'A')
    private = memory(store, [scene['A']])
    public = memory(store, [scene['B']])
    r = Retrieval(store)
    assert {m['id'] for m in r.learning_context('天文摄影',['self'], entry_id='B')} == {public}
    assert {m['id'] for m in r.learning_context('天文摄影',['self'], entry_id='A')} == {private,public}
    # A private repetition must not confirm/absorb the public fact.
    with store.write() as conn:
        conn.execute("DELETE FROM sources WHERE memory_id=?", (private,))
        conn.execute("DELETE FROM memory_subjects WHERE memory_id=?", (private,))
        conn.execute("DELETE FROM memories WHERE id=?", (private,))
    gateway = FakeGateway({'memories':[{'content':'我喜欢天文摄影','type':'事实','speaker':'我','about':['我'], 'stance':'亲历','evidence':[1]}]})
    _, result = batch(store, gateway, entry='A', count=1)
    assert result['created'] and public not in result['confirmed']
    with store.read() as conn:
        assert conn.execute('SELECT retention FROM memories WHERE id=?',(public,)).fetchone()[0] == 50


def test_consolidation_never_plans_cross_scope_conflict_or_dependency(store, scene):
    scope(store, 'A')
    a = memory(store, [scene['A']])
    b = memory(store, [scene['B']])
    child = put(store, '摄影器材需要保养', evidence=[scene['A']])
    derive(store, child, b)
    with store.write() as conn:
        conn.execute("UPDATE memories SET lifecycle='forgotten' WHERE id=?", (b,))
    with store.read() as conn:
        get = lambda mid: snapshot(conn, mid)
        assert merge_exclusion(get(a),get(b)) == 'visibility'
        assert Consolidation(store)._plan_pair(get,get(a),get(b),DEFAULTS) is None
        assert dependency_material(conn,get(child)) is None


def test_persona_evidence_only_global(store, scene):
    scope(store, 'A')
    private = memory(store, [scene['A']])
    public = memory(store, [scene['B']])
    assert [m['memory_id'] for m in select_evidence(store)['memories']] == [public]
    scope(store, 'A', 'shared')
    assert {m['memory_id'] for m in select_evidence(store)['memories']} == {private,public}


def test_purge_does_not_publish_descendants_and_setting_still_dynamic(store, scene):
    scope(store, 'A')
    parent = memory(store, [scene['A']])
    child = put(store, '摄影器材防潮')
    derive(store, child, parent)
    purge_memory(store, parent, 1, confirm=True)
    r = Retrieval(store)
    assert child not in ids(r.search(entry_id='B'))
    scope(store, 'A', 'shared')
    assert child in ids(r.search(entry_id='B'))


def test_rollback_snapshot_cache_isolation(store, scene):
    mid = memory(store, [scene['A']])
    with pytest.raises(RuntimeError):
        with store.write() as conn:
            conn.execute("UPDATE entries SET visibility='entry_only' WHERE id='A'")
            assert Visibility(conn).memory(mid) == frozenset({'A'})
            raise RuntimeError()
    scope(store, 'B')
    with store.read() as conn:
        assert Visibility(conn).memory(mid) is None


def test_inflight_judgment_rechecks_memories_and_goals(store, scene):
    mid = memory(store, [scene['A']])
    goal = Goals(store).create(content='摄影计划', entry_id='A')['goal']['id']
    def judgment(*args, **kwargs):
        scope(store, 'A')
        return {'status':'disabled','reason':'test','network_ms':0,'removed_memory_ids':[]}
    with patch('iris.recall_judge.judge', judgment):
        result = Retrieval(store).prepare('B', text='摄影', recent_limit=0, judge=False)
    assert mid not in ids(result)
    assert goal not in {g['id'] for g in result['goals']}


def test_admin_setting_audit_and_validation(store, scene):
    with TestClient(create_app(store=store), base_url='http://127.0.0.1', client=('127.0.0.1',12345)) as client:
        login_admin(client)
        response = client.patch('/admin/api/entries/A/visibility', json={'visibility':'entries','visible_in':['B']})
        assert response.status_code == 200, response.text
        assert response.json() == {'visibility':'entries','visible_in':['A','B']}
        assert client.get('/admin/api/entries/A/settings').json()['visibility'] == 'entries'
        assert client.patch('/admin/api/entries/A/visibility',json={'visibility':'entries','visible_in':['missing']}).status_code == 400
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_operations WHERE action='entry_visibility'").fetchone()[0] == 1


@pytest.mark.parametrize('action', ['确认','修正'])
def test_learning_updates_cannot_use_private_evidence_on_public_memory(store, scene, action):
    scope(store, 'A')
    public = memory(store,[scene['B']])
    response = {'updates':[{'ref':'M1','action':action,'content':'我偏爱摄影器材','evidence':[1]}]}
    _, result = batch(store,FakeGateway(response),entry='A',count=1)
    assert not result['confirmed'] and not result['updated']
    assert any('visibility' in d['reason'] for d in result['dropped'])
    with store.read() as conn:
        assert conn.execute('SELECT retention,revision FROM memories WHERE id=?',(public,)).fetchone()[:] == (50,1)


def test_learning_source_scope_change_during_model_call_rechecked(store, scene):
    public = memory(store,[scene['B']])
    response = {'memories':[{'content':'我计划摄影展览','type':'计划','speaker':'我','about':['我'],
                            'stance':'推断','evidence':[1],'derived_from':['M1']}]}
    _, result = batch(store,FakeGateway(response,hook=lambda _:scope(store,'B')),entry='A',count=1)
    assert not result['created']
    assert any(d['reason']=='derived memory changed or invisible' for d in result['dropped'])


def test_shared_default_matches_explicit_shared_across_lanes(store, scene):
    a=memory(store,[scene['A']]); b=put(store,'我喜欢水彩绘画',evidence=[scene['B']])
    r=Retrieval(store)
    def snapshot_all():
        search=r.search(text='摄影',include_goals=True)
        prepared=r.prepare('C',text='摄影',recent_limit=0,judge=False)
        with store.read() as conn:
            consolidation=[snapshot(conn,mid) for mid in (a,b)]
        return search['memories'],prepared['memories'],r.learning_context('摄影',['self'],entry_id='C'),select_evidence(store),consolidation
    before=snapshot_all()
    for e in scene:
        scope(store,e,'shared')
    after=snapshot_all()
    # Freeze ranking time so fractional recency is exactly reproducible.
    for results in (before,after):
        for lane in results[:3]:
            for m in lane:
                m.pop('score',None)
    assert before == after


def test_published_persona_drops_sentences_when_basis_becomes_private(store, scene):
    from iris.memory_ops import setup_role
    from iris.persona import PersonaEngine, current_persona, persona_context
    from test_persona import FakeGateway as PersonaGateway
    setup_role(store,'Iris')
    mid=memory(store,[scene['A']])
    old=current_persona(store)['id']
    # The existing model fake validates the actual evidence refs.
    store.set_setting('persona_publish_mode','all_auto')
    generated={'sentences':[{'text':'我喜欢天文摄影。','basis':['M1']}]}
    result=PersonaEngine(store,PersonaGateway(store,generated)).regenerate(expected_version=old)
    assert result['status']=='current'
    scope(store,'A')
    with store.read() as conn:
        context=persona_context(conn)
        assert '天文摄影' not in context['content'] and context['needs_update']
    formed=form_batch(store,'B',PROMPT_VERSION,target_count=1,history_count=0,future_count=0)
    assert '天文摄影' not in LearningEngine(store,FakeGateway())._snapshot(formed)['persona']


def test_cached_graph_rebuilds_when_sources_are_added_and_on_new_store(store, scene, tmp_path):
    from iris.db import Store
    mid=memory(store,[scene['B']]);scope(store,'A')
    with store.read() as conn:
        assert Visibility(conn).memory(mid) is None
    with store.write() as conn:
        conn.execute("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,?)",(mid,scene['A'],now()))
    with store.read() as conn:
        assert Visibility(conn).memory(mid)==frozenset({'A'})
    reopened=Store(store.path)
    try:
        with reopened.read() as conn:
            assert Visibility(conn).memory(mid)==frozenset({'A'})
    finally:
        reopened.close()


def test_consolidation_apply_rechecks_scope_after_model_call(store, scene):
    from iris.consolidation import UnsafeWrite
    a=memory(store,[scene['A']]);b=memory(store,[scene['B']])
    co=Consolidation(store)
    with store.read() as conn:
        get=lambda mid:snapshot(conn,mid)
        payload=co._plan_pair(get,get(a),get(b),DEFAULTS)
    scope(store,'A')
    with store.write() as conn:
        assert co._stale(conn,payload)
        with pytest.raises(UnsafeWrite,match='visibility'):
            co.apply(conn,{'id':1,'kind':'pair'},payload,{'decision':'merge'})


def test_goal_semantic_candidates_exclude_other_scope(store, scene):
    scope(store,'A')
    goals=Goals(store)
    first=goals.create(content='帮我准备摄影展的资料',entry_id='A')['goal']['id']
    second=goals.create(content='帮我准备摄影展的资料',entry_id='B')
    assert second['goal']['id'] != first and second['dedup']['status']=='created'


def test_http_search_entry_context_and_global_default(store, scene):
    scope(store, 'A')
    private = memory(store, [scene['A']])
    public = memory(store, [scene['B']])
    forgotten = put(store, '摄影旧器材', evidence=[scene['A']])
    with store.write() as conn:
        conn.execute("UPDATE memories SET lifecycle='forgotten' WHERE id=?", (forgotten,))
    private_goal = Goals(store).create(content='准备摄影', entry_id='A')['goal']['id']
    public_goal = Goals(store).create(content='全局摄影任务')['goal']['id']
    with patch('iris.api.Scheduler.start'), TestClient(create_app(store=store, configs={}),
            base_url='http://127.0.0.1', client=('127.0.0.1', 12345)) as client:
        from conftest import authorize_host
        authorize_host(client)
        for entry, expected_memories, expected_goals in (
            ('A', {private, public, forgotten}, {private_goal, public_goal}),
            ('B', {public}, {public_goal}),
            (None, {public}, {public_goal}),
        ):
            payload = {'include_goals': True, 'include_forgotten': True}
            if entry is not None:
                payload['entry_id'] = entry
            response = client.post('/api/v1/memories/search', json=payload)
            assert response.status_code == 200, response.text
            assert ids(response.json()) == expected_memories
            assert {g['id'] for g in response.json()['goals']} == expected_goals
        prepared = client.post('/api/v1/entries/B/prepare',
                               json={'text': '天文摄影', 'recent_limit': 0, 'judge': False})
        assert prepared.status_code == 200, prepared.text
        assert ids(prepared.json()) == {public}
        assert {g['id'] for g in prepared.json()['goals']} == {public_goal}


def test_http_search_rejects_unauthorized_entry_and_scope_never_widens_default(store, scene):
    scope(store, 'A')
    private = memory(store, [scene['A']])
    public = memory(store, [scene['B']])
    with patch('iris.api.Scheduler.start'), TestClient(create_app(store=store, configs={}),
            base_url='http://127.0.0.1', client=('127.0.0.1', 12345)) as client:
        token = client.app.state.tokens.create(host='visibility-test',
            scope={'kind': 'entries', 'entries': ['A']}, actor='local_cli')
        client.headers['Authorization'] = 'Bearer ' + token['token']
        with patch('iris.api.HostRetrieval.search') as search:
            denied = client.post('/api/v1/memories/search', json={'entry_id': 'B'})
            assert denied.status_code == 403
            assert denied.json()['error']['code'] == 'entry_forbidden'
            search.assert_not_called()
        allowed = client.post('/api/v1/memories/search', json={'entry_id': 'A'})
        assert allowed.status_code == 200, allowed.text
        assert ids(allowed.json()) == {private, public}
        omitted = client.post('/api/v1/memories/search', json={})
        assert omitted.status_code == 200, omitted.text
        assert ids(omitted.json()) == {public}
