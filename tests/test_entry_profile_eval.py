"""Synthetic serialization fixtures only: these are not entry-profile dev corpora."""
import copy
import json
from pathlib import Path

import pytest

from iris.db import dumps
import importlib.util
_spec=importlib.util.spec_from_file_location('entry_profile_eval_runner',Path(__file__).resolve().parents[1]/'evals/entry_profile_eval/runner.py')
_runner=importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_runner)
run_eval,score,load_corpus=_runner.run_eval,_runner.score,_runner.load_corpus
from test_entry_profile import Model, TEXTS


def corpus(tmp_path):
    value={'format_version':1,'cases':[{'id':'fixture','entry':{'id':'room','kind':'group','name':'格式测试'},
        'as_of':'2026-10-10T12:00:00+00:00','messages':[
            {'id':str(i),'at':'2026-10-10T10:00:00+00:00','sender':'self','kind':'self_output','text':'格式测试消息'+str(i)} for i in range(50)],
        'must_cover':['格式测试要点'],'forbidden':['格式测试禁义']}]}
    path=tmp_path/'fixture.json';path.write_text(dumps(value),encoding='utf-8')
    return path


def run(tmp_path):
    return run_eval(corpus(tmp_path),tmp_path/'run',configs={},gateway_factory=lambda configs,store:Model(store))


def judgments(materials,path,*,supported=True,forbidden=False):
    manifest=json.loads((materials/'manifest.json').read_text())
    path.mkdir()
    (path/'manifest.json').write_bytes((materials/'round-template.json').read_bytes())
    for case in manifest['cases']:
        data=json.loads((materials/case['file']).read_text())['input']
        sentences=data['profile']['sentences'] if data['profile'] else []
        vote={'sentence_results':[{'index':i+1,'supported':supported,'violations':[] if supported else ['无依据'],'reason':'格式测试判分'} for i,_ in enumerate(sentences)],
              'must_cover':[supported for _ in data['must_cover']], 'forbidden':[forbidden for _ in data['forbidden']]}
        (path/case['judgment_file']).write_text(dumps(vote),encoding='utf-8')
    return path


def test_runner_exports_bound_materials_and_adverse_double_scores(tmp_path):
    result=run(tmp_path)
    materials=Path(result['materials'])
    assert result['rows'][0]['result']['status']=='published'
    assert (materials/'run.json').exists() and (materials/'scoring.md').exists()
    a=judgments(materials,tmp_path/'a')
    b=judgments(materials,tmp_path/'b',supported=False,forbidden=True)
    report=score(materials,[a,b],judge_model='fake evaluator',out=tmp_path/'score')
    assert report['judge_runs']==2
    assert report['metrics']['grounded_sentence_ratio']==0
    assert report['metrics']['forbidden_occurrences']==1
    assert report['metrics']['must_cover_coverage']==0
    assert report['disagreements']
    assert report['original_judgments'][0][0]['sentence_results'][0]['supported'] is True


@pytest.mark.parametrize('target',['case','run','scoring','manifest'])
def test_tampered_materials_are_rejected(tmp_path,target):
    materials=Path(run(tmp_path)['materials']);a=judgments(materials,tmp_path/'a')
    paths={'case':materials/'cases/0000.json','run':materials/'run.json','scoring':materials/'scoring.md','manifest':materials/'manifest.json'}
    path=paths[target];path.write_text(path.read_text()+' ',encoding='utf-8')
    if target=='manifest':
        value=json.loads(path.read_text());value['run']['source_sha256']='changed';path.write_text(dumps(value))
    with pytest.raises(ValueError):
        score(materials,[a],judge_model='fake evaluator',out=tmp_path/'score')


@pytest.mark.parametrize('kind',['bool','index','count','foreign_round','extra_file','duplicate_round'])
def test_strict_vote_shape_and_fingerprint(tmp_path,kind):
    materials=Path(run(tmp_path)['materials']);a=judgments(materials,tmp_path/'a')
    path=a/'0000.json';v=json.loads(path.read_text())
    if kind=='bool': v['sentence_results'][0]['supported']=1
    elif kind=='index':v['sentence_results'][0]['index']=2
    elif kind=='count':v['must_cover']=[]
    elif kind=='foreign_round':(a/'manifest.json').write_text('{"materials_sha256":"other"}')
    elif kind=='extra_file':(a/'other.json').write_text('{}')
    path.write_text(dumps(v),encoding='utf-8')
    with pytest.raises(ValueError):
        score(materials,[a,a] if kind=='duplicate_round' else [a],judge_model='fake evaluator',out=tmp_path/'score')


def test_explicit_corpus_only_and_source_times_checked(tmp_path):
    path=corpus(tmp_path);v=json.loads(path.read_text())
    v['cases'][0]['messages'][0]['at']='2027-01-01T00:00:00Z'
    path.write_text(dumps(v),encoding='utf-8')
    with pytest.raises(ValueError):load_corpus(path)


def test_rejected_candidate_exported_and_not_scored_as_published(tmp_path):
    result=run_eval(corpus(tmp_path),tmp_path/'run',configs={},gateway_factory=lambda configs,store:Model(store,bad=(0,1,2)))
    materials=Path(result['materials'])
    assert result['rows'][0]['result']['status']=='rejected'
    a=judgments(materials,tmp_path/'a')
    report=score(materials,[a],judge_model='fake evaluator',out=tmp_path/'score')
    assert report['metrics']['rejected_candidates']==1
    assert report['metrics']['grounded_sentence_ratio'] is None
    assert not report['metrics']['meets_threshold']


def test_grounded_items_and_no_prohibited_content_meet_spec(tmp_path):
    texts=[f'格式测试说法{i+1}' for i in range(5)]
    result=run_eval(corpus(tmp_path),tmp_path/'run',configs={},gateway_factory=lambda configs,store:Model(store,texts=texts))
    materials=Path(result['materials']);a=judgments(materials,tmp_path/'a')
    path=a/'0000.json';vote=json.loads(path.read_text())
    path.write_text(dumps(vote))
    report=score(materials,[a],judge_model='fake evaluator',out=tmp_path/'score')
    assert report['metrics']['grounded_item_ratio']==1
    assert report['metrics']['meets_threshold']


def test_frozen_groups_adapter_keeps_entry_scope_quotes_and_labels_out_of_material(tmp_path):
    from iris.db import Store
    from iris.entry_profile import select_material
    value={'format_version':1,'name':'fixture','timezone':'Asia/Shanghai','role_name':'Iris','groups':[
        {'id':'A','entry_kind':'group','name':'甲','now':'2026-12-15T23:00:00+08:00',
         'messages':[{'id':'m001','sender':'共有账号','kind':'message','at':'2026-12-14T12:00:00+08:00','text':'甲原话'},
                     {'id':'m002','sender':'self','kind':'self_output','at':'2026-12-14T12:01:00+08:00','text':'接甲话',
                      'quote':{'message_id':'m001','sender':'共有账号','text':'甲原话'}}],
         'must_cover':['判分专用甲'],'forbidden':['禁止专用甲'],'notes':'不得注入材料'},
        {'id':'B','entry_kind':'live','name':'乙','now':'2026-12-15T23:00:00+08:00',
         'messages':[{'id':'m001','sender':'共有账号','kind':'message','at':'2026-12-14T12:00:00+08:00','text':'乙原话'}],
         'must_cover':['判分专用乙'],'forbidden':['禁止专用乙']}]}
    path=tmp_path/'groups.json';path.write_text(dumps(value))
    cases=load_corpus(path)
    assert len(cases)==2
    assert cases[0]['must_cover']==['判分专用甲']
    assert {m['id'] for m in cases[0]['messages']}=={'A:m001','A:m002','B:m001'}
    store=Store(tmp_path/'db')
    try:
        _runner._load_case(store,cases[0])
        material=select_material(store,'A',clock=lambda:_runner._instant(cases[0]['as_of']))
        encoded=dumps(material)
        assert '乙原话' not in encoded and '判分专用' not in encoded and '不得注入材料' not in encoded
        assert [m['source_id'] for m in material['messages']]==['A:m001','A:m002']
        assert material['messages'][1]['quote']=={'speaker':'共有账号','content':'甲原话'}
        with store.read() as conn:
            assert conn.execute('SELECT COUNT(*) FROM entries').fetchone()[0]==2
            assert conn.execute("SELECT COUNT(*) FROM subjects WHERE id='共有账号'").fetchone()[0]==1
    finally:
        store.close()
    value['groups'][0]['messages'][1]['quote']['text']='编造的引用'
    path.write_text(dumps(value))
    with pytest.raises(ValueError,match='quote'):
        load_corpus(path)


def test_call_percentiles_use_percent_not_fraction():
    rows=[{'complete':True,'result':{'version':None},'attempts':[],'calls':[
        {'duration_ms':n,'prompt_tokens':0,'completion_tokens':0} for n in (10,20,30)]}]
    stats=_runner._metrics(rows)['calls']
    assert stats['duration_ms_p50']==20
    assert stats['duration_ms_p95']==29


def test_recorded_check_timeout_is_incomplete_even_without_timeout_reason(tmp_path):
    from iris.models import ModelError
    def gateway(configs,store):
        model=Model(store)
        original=model.chat
        def chat(messages,purpose,*args,**kwargs):
            if purpose.endswith('check'):
                with store.write() as conn:
                    conn.execute("""INSERT INTO model_calls(purpose,model,duration_ms,result_category,created_at,timed_out)
                        VALUES(?,'fake',120000,'retryable','2026-10-10T12:00:00+00:00',1)""",(purpose,))
                raise ModelError('retryable','total timeout')
            return original(messages,purpose,*args,**kwargs)
        model.chat=chat
        return model
    result=run_eval(corpus(tmp_path),tmp_path/'run',configs={},gateway_factory=gateway)
    assert result['rows'][0]['result']['status']=='rejected'
    assert result['rows'][0]['result']['reason']=='retryable'
    assert result['rows'][0]['complete'] is False
    assert result['metrics']['incomplete_cases']==1
    assert result['metrics']['calls']['timed_out']==1
    # Older immutable runs may have set complete=True from the general reason.
    legacy=copy.deepcopy(result['rows']);legacy[0]['complete']=True
    assert _runner._metrics(legacy)['incomplete_cases']==1


def test_frozen_v2_coverage_proposal_is_exact_and_annotation_bound(tmp_path):
    import hashlib
    frozen=Path(__file__).resolve().parents[1]/'evals/entry_profile_v1.json'
    cases=load_corpus(frozen)
    policy=_runner.coverage_policy(cases,hashlib.sha256(frozen.read_bytes()).hexdigest())
    assert policy['status']=='proposed_pending_planner_confirmation'
    assert len(policy['not_applicable'])==9
    coverage=[_runner._coverage(c,policy) for c in cases]
    assert sum(len(c['must_cover_original']) for c in coverage)==40
    assert sum(len(c['must_cover']) for c in coverage)==31
    assert coverage[0]['must_cover_indices']==[1,2,4,5,6,7,8]
    original=copy.deepcopy(cases)
    cases[0]['must_cover'][2]='不同标注'
    with pytest.raises(ValueError,match='annotation mismatch'):
        _runner.coverage_policy(cases,policy['corpus_file_sha256'])
    assert _runner.coverage_policy(original,'unknown')['status']=='all_applicable'
    path=tmp_path/'policy.json';path.write_text(dumps(policy),encoding='utf-8')
    with pytest.raises(ValueError,match='fingerprint'):
        _runner.coverage_policy(original,'unknown',path)


def test_effective_coverage_scores_only_applicable_and_exports_reasons(tmp_path):
    import hashlib
    path=corpus(tmp_path)
    policy={'format_version':1,'corpus_file_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'status':'planner_confirmed','not_applicable':[{'case_id':'fixture','index':1,
            'text':'格式测试要点','reason':'机制测试：验证不适用项保持可见且不进入分母'}]}
    proposal=tmp_path/'policy.json';proposal.write_text(dumps(policy),encoding='utf-8')
    result=run_eval(path,tmp_path/'run',configs={},gateway_factory=lambda configs,store:Model(store),coverage_policy_path=proposal)
    materials=Path(result['materials']);vote_dir=judgments(materials,tmp_path/'a')
    document=json.loads((materials/'cases/0000.json').read_text())['input']
    assert document['must_cover']==[] and document['must_cover_original']==['格式测试要点']
    assert document['not_applicable']==policy['not_applicable']
    report=score(materials,[vote_dir],judge_model='fake evaluator',out=tmp_path/'score')
    assert report['metrics']['must_cover_total']==0 and report['metrics']['must_cover_coverage'] is None
    assert report['metrics']['not_applicable_count']==1
    assert report['metrics']['quote_validation_total']==5
    assert report['metrics']['quote_validation_pass_rate']==1


def test_quote_diagnostic_counts_rejected_draft_quotes():
    profile={'status':'rejected','checks':{'quote_validation':{'total':3,'passed':1}}}
    rows=[{'complete':True,'result':{'version':profile},'calls':[],'attempts':[]}]
    assert _runner._metrics(rows)['quote_validation_pass_rate']==1/3


# Historical document-path regression on the pre-switch schema.
from profile_legacy import legacy_schema
