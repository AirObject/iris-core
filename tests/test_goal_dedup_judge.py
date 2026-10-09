from datetime import datetime, timezone
import json

import httpx
import pytest

from iris.model_health import ModelHealth
from iris.models import Gateway, ModelConfig, ModelError


def goal(gid,content='整理说明书',**extra):
    return {'id':gid,'content':content,'kind':'normal','state':'open','merged_into':None,
            'people':['person'],'deadline':None,'created_at':'2026-10-01T00:00:00+00:00','revision':1,**extra}


def test_candidate_guards_reuse_numbers_negation_people_deadlines():
    from iris.goal_dedup_judge import select_candidates
    incoming=goal(10,'给小林整理两份说明书',deadline='2026-10-03')
    rows=[goal(1,incoming['content']),goal(2,'给小林整理三份说明书'),
          goal(3,'不给小林整理两份说明书'),goal(4,incoming['content'],people=['other']),
          goal(5,incoming['content'],deadline='2026-10-04'),goal(6,incoming['content'],state='completed'),
          goal(7,incoming['content'],kind='question'),goal(8,incoming['content'],merged_into=1)]
    assert [r['id'] for r in select_candidates(rows,incoming)]==[1]


def test_missing_people_is_unknown_and_candidate_order_is_deterministic():
    from iris.goal_dedup_judge import select_candidates
    proposed=goal(8,people=[])
    assert [r['id'] for r in select_candidates([goal(2),goal(1)],proposed)]==[1,2]
    assert select_candidates([goal(1)],proposed,dismissed={1})==[]


@pytest.mark.parametrize('raw',[
    '{"decisions":[{"id":1,"verdict":"same"},{"id":1,"verdict":"different"}]}',
    '{"decisions":[{"id":true,"verdict":"same"}]}',
    '{"decisions":[{"id":1,"verdict":"same","reason":"x"}]}',
    '{"decisions":[{"id":1,"verdict":"merge"}]}',
    '{"decisions":[{"id":1,"verdict":"same"}],"decisions":[]}',
    '{"decisions":[]}',
])
def test_judgments_strictly_validate_exact_candidate_ids(raw):
    from iris.goal_dedup_judge import validate_decisions
    with pytest.raises(ValueError):
        validate_decisions(raw,[1])


def test_ambiguity_and_admin_creation_never_auto_merge():
    from iris.goal_dedup_judge import decision_from_verdicts
    assert decision_from_verdicts({1:'same',2:'uncertain'},origin='host')['status']=='possible_duplicate'
    assert decision_from_verdicts({1:'same',2:'same'},origin='host')['target_ids']==[1,2]
    assert decision_from_verdicts({1:'same'},origin='admin')['status']=='possible_duplicate'
    assert decision_from_verdicts({1:'same',2:'different'},origin='host')['status']=='merged'
    assert decision_from_verdicts({1:'different'},origin='internal')['status']=='created'


def gateway(store,handler,clock=None):
    configs={'chat':ModelConfig('https://unit.invalid/v1','','unit-model')}
    health=ModelHealth(store,configs,**({'clock':clock} if clock else {}))
    return Gateway(configs,store,client=httpx.Client(transport=httpx.MockTransport(handler)),health=health,
                   **({'clock':clock} if clock else {}))


def test_goal_judgment_is_an_independent_inherited_purpose(store):
    def respond(request):
        assert not store._writer.in_transaction
        body=json.loads(request.content)
        assert body['reasoning_effort']=='high'
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"decisions":[{"id":1,"verdict":"same"}]}'},
                                                    'finish_reason':'stop'}],
                                      'usage':{'prompt_tokens':12,'completion_tokens':7}})
    model=gateway(store,respond)
    try:
        result=model.goal_dedup_judge([{'role':'user','content':'unit'}],[1],budget_seconds=1)
        assert result['decisions']=={1:'same'}
        assert model._goal_judge_admission is not model._judge_admission
        assert model._goal_judge_pool is not model._judge_pool
        assert model.health.snapshot()['goal_dedup_judge']['state']=='normal'
        with store.read() as conn:
            call=conn.execute('SELECT * FROM model_calls').fetchone()
        assert call['model_kind']==call['purpose']=='goal_dedup_judge'
        assert call['prompt_tokens']==12
    finally:
        model.close()


def test_goal_rate_limits_back_off_twice_then_pause_only_own_purpose(store):
    from datetime import timedelta
    stamp=[datetime(2026,10,9,tzinfo=timezone.utc)]
    model=gateway(store,lambda request:httpx.Response(429,headers={'Retry-After':'3'},
                   json={'error':{'code':'RateLimitExceeded','message':'ignored'}}),clock=lambda:stamp[0])
    try:
        for attempt in range(3):
            with pytest.raises(ModelError):
                model.goal_dedup_judge([],[],budget_seconds=1)
            state=model.health.snapshot()['goal_dedup_judge']
            assert state['state']==('temporarily_unavailable' if attempt==2 else 'rate_limited')
            stamp[0]+=timedelta(seconds=3)
        assert model.health.snapshot()['chat']['state']=='normal'
        assert model.health.snapshot()['recall_judge']['state']=='normal'
        with store.read() as conn:
            assert conn.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0]==3
    finally:
        model.close()


def test_goal_judgment_obeys_daily_limit_before_any_request(store):
    model=gateway(store,lambda request:pytest.fail('should not call exhausted endpoint'))
    try:
        model.health.set_daily_token_limit(1)
        model._record('test','unit',1,'success',None,usage={'prompt_tokens':1})
        with pytest.raises(ModelError) as exc:
            model.goal_dedup_judge([],[],budget_seconds=1)
        assert exc.value.reason=='usage_limit'
    finally:
        model.close()


def test_timed_out_worker_holds_only_goal_admission_until_it_finishes(store):
    import threading
    import time
    entered,release=threading.Event(),threading.Event()
    def respond(request):
        entered.set()
        release.wait(2)
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"decisions":[]}'},'finish_reason':'stop'}]})
    store.set_setting('goal_dedup_judge',{'concurrency':1,'queue_limit':0})
    model=gateway(store,respond)
    try:
        with pytest.raises(ModelError) as exc:
            model.goal_dedup_judge([],[],budget_seconds=.02)
        assert entered.is_set() and exc.value.reason=='timeout'
        assert model._goal_judge_admission.active==1
        assert model._judge_admission.active==0
        with pytest.raises(ModelError) as exc:
            model.goal_dedup_judge([],[],budget_seconds=.02)
        assert (exc.value.reason or exc.value.category)=='queue_full'
        release.set()
        deadline=time.monotonic()+1
        while model._goal_judge_admission.active and time.monotonic()<deadline:
            time.sleep(.001)
        assert model._goal_judge_admission.active==0
    finally:
        release.set()
        model.close()


def test_b_and_c_use_same_material_and_only_differ_in_grouping():
    from iris.goal_dedup_judge import judge
    class Fake:
        def __init__(self):
            self.requests=[]
        def goal_dedup_judge(self,messages,ids,*,budget_seconds):
            self.requests.append(json.loads(messages[-1]['content']))
            return {'decisions':{gid:'uncertain' for gid in ids}}
    incoming={'id':3,'content':{'text':'整理手册','at':'2026-10-03'}}
    candidates=[{'id':1,'content':{'text':'整理手册','at':'2026-10-01'}},
                {'id':2,'content':{'text':'整理手册','at':'2026-10-02'}}]
    pair,batch=Fake(),Fake()
    assert judge(pair,incoming,candidates,method='B',budget_seconds=1)=={1:'uncertain',2:'uncertain'}
    assert judge(batch,incoming,candidates,method='C',budget_seconds=1)=={1:'uncertain',2:'uncertain'}
    assert len(pair.requests)==2 and len(batch.requests)==1
    assert [row for request in pair.requests for row in request['candidates']]==batch.requests[0]['candidates']
    assert all(request['incoming']==incoming for request in pair.requests)


def test_material_exposes_only_evidence_fields_and_marks_truncation(store):
    from iris.goal_dedup_judge import material
    from iris.goals import _row
    with store.write() as conn:
        conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES('A','A','unit','private')")
        gid=conn.execute("INSERT INTO goals(content,kind,origin,entry_id,created_at) VALUES('整理手册','normal','host','A','2026-10-01T00:00:00+00:00')").lastrowid
        mid=conn.execute("INSERT INTO messages(entry_id,kind,sender_subject_id,content,occurred_at,received_at,dedupe_key) VALUES('A','self_output','self',?,'2026-10-01T00:00:00+00:00','2026-10-01T00:00:00+00:00','unit')",('甲'*1001,)).lastrowid
        conn.execute('INSERT INTO goal_sources(goal_id,message_id) VALUES(?,?)',(gid,mid))
        row={**_row(conn,gid),'expected':{'decision':'merge'},'reason':'label','category':'label'}
        value=material(conn,row)
    assert set(value)=={'id','content','people','deadline','entry_kind','sources','sources_truncated'}
    assert len(value['sources'][0]['text'])==1000 and value['sources'][0]['truncated']
    assert 'origin' not in value and 'expected' not in value and 'reason' not in value


def test_goal_purpose_settings_require_auth_csrf_and_bound_the_product_budget(store):
    from fastapi.testclient import TestClient
    from iris.api import create_app
    from conftest import login_admin
    with TestClient(create_app(store=store),base_url='http://127.0.0.1',client=('127.0.0.1',12345)) as client:
        assert client.patch('/admin/api/settings/goal-dedup-judge',json={'enabled':False}).status_code==409
        login_admin(client)
        response=client.patch('/admin/api/settings/goal-dedup-judge',json={'enabled':False,'budget_seconds':4,'concurrency':2})
        assert response.status_code==200,response.text
        options=response.json()['goal_dedup_judge']
        assert options['budget_seconds']==4 and options['concurrency']==2 and options['enabled'] is False
        assert response.json()['models']['goal_dedup_judge']['inherited']
        for fields in ({'budget_seconds':11},{'budget_seconds':True},{'concurrency':0},{'method':'B'},{'unknown':1}):
            assert client.patch('/admin/api/settings/goal-dedup-judge',json=fields).status_code==400
        del client.headers['X-Iris-CSRF']
        assert client.patch('/admin/api/settings/goal-dedup-judge',json={'enabled':True}).status_code==403
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_operations WHERE action='goal_dedup_judge_saved'").fetchone()[0]==1


@pytest.mark.parametrize('wrapper',[lambda s:s+'`',lambda s:'```json\n'+s+'\n```'])
def test_only_outer_markdown_delimiters_are_normalized(wrapper):
    from iris.goal_dedup_judge import validate_decisions
    raw='{"decisions":[{"id":1,"verdict":"same"}]}'
    assert validate_decisions(wrapper(raw),[1])=={1:'same'}
    for invalid in (raw+' extra',raw+raw,raw.replace('same','merge'),'{"decisions":[{"id":1,"verdict":"same",}]}'):
        with pytest.raises(ValueError):
            validate_decisions(wrapper(invalid),[1])


def test_goal_health_probe_uses_product_budget_without_measurement_override(store):
    observed=[]
    def respond(request):
        observed.append(request.extensions['timeout']['read'])
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}]})
    store.set_setting('goal_dedup_judge',{'budget_seconds':2})
    model=gateway(store,respond)
    try:
        model._call('goal_dedup_judge','health_probe',{'messages':[]},probe=True)
        assert 0<observed[0]<=2
    finally:
        model.close()
