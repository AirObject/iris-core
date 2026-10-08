"""Experiment integrity tests; these are not quality-evaluation corpus samples."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import httpx
import pytest
sys.path.insert(0,str(Path(__file__).parent))
from probe import (choose,validate_scores,filtered,apply_judge,model_payload,one_call,
                   deterministic,gate_accept,regressions)
from iris.recall_evaluation import recall_metrics


def row():
    return {'id':'unit','corpus':'recall_v1','split':'dev','categories':[], 'mode':'prepare',
        'relevant':{'a':3},'returned':['a','b','h'],
        'reasons':{'a':'relevant','b':'relevant','h':'person_highlight'},'local_ms':1,
        'conversation':False,'features':{
            'a':dict(cosine=.6,coverage=.2,lexical=False,vector=True,top_cosine=.7,gap=.1,cosine_ratio=.85,rrf_ratio=.8),
            'b':dict(cosine=.7,coverage=.1,lexical=False,vector=True,top_cosine=.7,gap=.1,cosine_ratio=1,rrf_ratio=1),
            'h':dict(cosine=.1,coverage=0,lexical=False,vector=False,top_cosine=.7,gap=.1,cosine_ratio=.14,rrf_ratio=0)}}


def trial(name,r,n,f,p):
    return {'id':name,'metrics':{'recall_at_8':r,'ndcg_at_8':n,'irrelevant_return_rate':f,'relevant_precision':p}}


def test_m2_feasibility_precedes_quality():
    assert choose([trial('best_q_but_false',1,1,.2,1),trial('feasible',.85,.75,.1,.5)])['id']=='feasible'


def test_global_band_is_strict_and_not_chained():
    candidates=[trial('top',.98,.98,.1,.7),trial('tied',.971,.971,.1,.8),trial('outside',.962,.962,.1,1)]
    assert choose(candidates)['id']=='tied'
    assert choose([trial('top',.98,.98,.1,.7),trial('boundary',.97,.97,.1,1)])['id']=='top'


def test_final_tie_uses_frozen_order_not_quality():
    assert choose([trial('earlier',.971,.971,.1,.8),trial('later',.98,.98,.1,.8)])['id']=='earlier'


def test_no_feasible_minimum_false_requires_recall_ndcg():
    assert choose([trial('reject_all',0,0,0,0),trial('near',.9,.9,.2,.9),trial('worse',1,1,.3,.9)])['id']=='near'


@pytest.mark.parametrize('scores',[
    [{'id':'a','support':True}], [{'id':'b','support':80}],[],
    [{'id':'a','support':101}], [{'id':'a','support':70.0}],
    [{'id':'a','support':80},{'id':'a','support':90}],
])
def test_invalid_model_schema_fails(scores):
    with pytest.raises(ValueError):validate_scores(json.dumps({'scores':scores}),['a'])


def test_score_order_must_match():
    with pytest.raises(ValueError):validate_scores('{"scores":[{"id":"b","support":80},{"id":"a","support":90}]}',['a','b'])
    assert validate_scores('{"scores":[{"id":"a","support":80}]}',['a'])=={'a':80}


def test_filter_preserves_highlights_never_relabels_rejected_items():
    actual=filtered(row(),[])
    assert actual['returned']==['h'] and actual['reasons']=={'h':'person_highlight'}
    actual['relevant']={}
    assert recall_metrics([actual])['irrelevant_return_rate']==0
    assert recall_metrics([actual])['average_returned']==1


def test_timeout_and_error_return_exact_baseline():
    for call in ({'status':'success','duration_ms':2001,'scores':{'a':99}},
                 {'status':'error','duration_ms':1,'scores':{}}):
        actual,fail=apply_judge(row(),call,75,2)
        assert fail and actual['returned']==row()['returned'] and actual['reasons']==row()['reasons']
    actual,fail=apply_judge(row(),{'status':'success','duration_ms':1999,'scores':{'a':80}},75,2)
    assert not fail and actual['returned']==['a','h']


def test_no_label_or_category_leak_to_model():
    r=row();r['query']={'text':'显式问题','categories':['无答案'],'relevant':{'a':3}}
    r['retrieval_query']='组合文本';r['subject_aliases']=[]
    r['response']={'memories':[{'id':1,'reason':'relevant','content':'记忆','speaker_name':'Iris','about':[], 'kind':'事实','stance':'亲历'},
                             {'id':2,'reason':'relevant','content':'另一记忆','speaker_name':'甲','about':[],'kind':'事实','stance':'亲历'},
                             {'id':3,'reason':'person_highlight','content':'背景','speaker_name':'乙','about':[],'kind':'事实','stance':'亲历'}]}
    body=model_payload(r,1)
    assert [m['id'] for m in body['candidates']]==['a']
    assert '无答案' not in json.dumps(body,ensure_ascii=False)
    assert not {'relevant','categories','split','corpus','features'} & set(body)


def test_deterministic_uses_signals_only_and_gate_disjunction():
    assert deterministic(row(),{'family':'cosine','floor':.65})['returned']==['b','h']
    assert gate_accept(row(),[.8,.15])
    assert not gate_accept(row(),[.8,.5])


def test_http_single_call_and_redacted_output():
    def handler(request):
        payload=json.loads(request.content)
        assert payload['reasoning_effort']=='low' and payload['max_tokens']==4096
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"scores":[{"id":"a","support":95}]}','reasoning_content':'not persisted'}}],
             'usage':{'prompt_tokens':20,'completion_tokens':10,'completion_tokens_details':{'reasoning_tokens':4}}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await one_call(client,SimpleNamespace(model='unit-model',base_url='https://example.invalid',api_key='unit-secret'),{'candidates':[{'id':'a'}]},'low')
    result=asyncio.run(run())
    assert result['status']=='success' and result['scores']=={'a':95}
    assert result['reasoning_chars']==13 and result['usage']['reasoning_tokens']==4
    assert 'not persisted' not in json.dumps(result)


def test_actual_deadline_cancels_without_retry():
    count=0
    async def handler(request):
        nonlocal count
        count+=1;await asyncio.sleep(.1)
        return httpx.Response(200,json={})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await one_call(client,SimpleNamespace(model='unit',base_url='https://example.invalid',api_key='unit-secret'),{'candidates':[{'id':'a'}]},'high',deadline=.01)
    result=asyncio.run(run())
    assert result['error']=='timeout' and count==1


def test_protocol_roundtrip(tmp_path):
    from probe import freeze_protocol
    assert freeze_protocol(tmp_path)==freeze_protocol(tmp_path)


def test_live_check_records_safe_error_code_not_provider_message():
    from live_check import ResponseMetadata
    def handler(request):
        return httpx.Response(429,headers={'Retry-After':'3'},json={'error':{
            'code':'ModelAccountTpmRateLimitExceeded','message':'untrusted echoed credentials'}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            proxy=ResponseMetadata(client)
            result=await one_call(proxy,SimpleNamespace(model='unit',base_url='https://example.invalid',api_key='unit-secret'),{'candidates':[]},'high')
            result.update(proxy.metadata)
            return result
    result=asyncio.run(run())
    assert result['error']=='http_429' and result['error_code']=='ModelAccountTpmRateLimitExceeded'
    assert result['retry_after_seconds']=='3'
    assert 'untrusted echoed credentials' not in json.dumps(result)


def test_live_protocol_rejects_mixed_run_settings(tmp_path):
    from live_check import freeze_run_protocol
    path=tmp_path/'protocol.json'
    original={'concurrency':1,'interval_seconds':1,'deadline':10}
    freeze_run_protocol(path,original)
    freeze_run_protocol(path,original)
    with pytest.raises(ValueError,match='choose a new label'):
        freeze_run_protocol(path,{**original,'concurrency':2})
    assert json.loads(path.read_text())==original
