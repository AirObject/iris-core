import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

DIR=Path(__file__).resolve().parents[1]/'evals/consolidation_eval'
spec=importlib.util.spec_from_file_location('co_eval_run',DIR/'run.py')
runner=importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
sys.modules['run']=runner
spec=importlib.util.spec_from_file_location('co_eval_score',DIR/'score.py')
scorer=importlib.util.module_from_spec(spec)
spec.loader.exec_module(scorer)
from test_consolidation import Model,merge_answer


def test_frozen_corpus_fake_run_and_material_integrity(tmp_path):
    cases=runner.load_corpus(runner.ROOT/'evals/consolidation_v1.json')
    rows=[]
    for c in cases:
        row=runner.case_run(c,tmp_path/(c['id']+'.db'),lambda store,clock:Model(store), 'balanced')
        scorer.validate_payload(row['input'])
        rows.append(row)
    metadata={'source_sha256':runner.source_hash(),'scoring_version':runner.SCORING_VERSION,'method':'balanced',
              'corpus':{'sha256':runner._json_sha256(cases)}}
    manifest=runner.export(rows,tmp_path/'materials',metadata)
    loaded,actual=scorer.load_materials(tmp_path/'materials')
    assert len(actual)==40 and loaded==manifest
    path=tmp_path/'materials/cases/0000.json'
    path.write_text(path.read_text(encoding='utf-8')+' ',encoding='utf-8')
    with pytest.raises(ValueError,match='fingerprint'):
        scorer.load_materials(tmp_path/'materials')


def test_scoring_semantic_failure_counts_as_mismerge_and_belief_cap_separate(tmp_path):
    case=runner.load_corpus(runner.ROOT/'evals/consolidation_v1.json')[0]
    row=runner.case_run(case,tmp_path/'merge.db',lambda store,clock:Model(store,merge_answer),'broad')
    p=row['input']
    scorer.validate_payload(p)
    m=p['actual_merges'][0]
    j={'format_version':1,'case_id':case['id'],'merge_results':[{'action_id':m['action_id'],
        'key_facts_preserved':False,'source_grounded':True,'attribution_correct':True,'reason':'漏事实'}],
       'contradiction_results':[],'dependency_results':[],'protection_results':[]}
    scorer.validate_judgment(j,p)
    report=scorer.score_case(p,j)
    assert report['mismerge']['rate']==1 and report['recognition']['rate']==1
    j['merge_results'][0]['key_facts_preserved']=True
    m['result']['belief']=100
    report=scorer.score_case(p,j)
    assert report['mismerge']['rate']==0 and not report['belief_cap'][0]['passed']
    bad=copy.deepcopy(j);bad['merge_results'][0]['source_grounded']=1
    with pytest.raises(ValueError,match='boolean'):
        scorer.validate_judgment(bad,p)
    bad=copy.deepcopy(j);bad['merge_results'][0]['extra']='no'
    with pytest.raises(ValueError,match='fields'):
        scorer.validate_judgment(bad,p)


def test_edit_events_sources_actor_and_protection_loaded(tmp_path):
    c=runner.load_corpus(runner.ROOT/'evals/consolidation_v1.json')[-1]
    row=runner.case_run(c,tmp_path/'case.db',lambda store,clock:Model(store),'balanced')
    before=row['input']['before']
    a=before['memories'][0]
    assert a['last_edit']=='admin' and len(a['sources'])==2
    assert a['content']==c['events'][-1]['content']
    assert len(scorer.protected(row['input']))==2
    assert row['input']['case']['expected']==c['expected']


def test_double_judging_AND_lists_and_disagreements():
    a={'must_keep':[True,True],'source_grounded':True,'reason':'甲'}
    b={'must_keep':[False,True],'source_grounded':False,'reason':'乙'}
    disagreements=[]
    result=scorer.combine(a,b,disagreements=disagreements)
    assert result=={'must_keep':[False,True],'source_grounded':False}
    assert len(disagreements)==2
