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


def test_unknown_optional_fields_do_not_break_external_materials(tmp_path):
    case=copy.deepcopy(runner.load_corpus(runner.ROOT/'evals/consolidation_v1.json')[0])
    case['future_option']={'ignored':True}
    case['memories'][0]['future_memory_option']='unused'
    case['memories'][0]['sources'][0]['future_source_option']='unused'
    row=runner.case_run(case,tmp_path/'external.db',lambda store,clock:Model(store),'balanced')
    scorer.validate_payload(row['input'])
    assert row['input']['case']['expected']==case['expected']


def test_selection_uses_worst_run_ties_and_requires_final_double_judging():
    spec=importlib.util.spec_from_file_location('co_selection',DIR/'selection.py')
    selection=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(selection)
    reports=[]
    for method,rate,calls in [('balanced',1.0,50),('strict',0.5,10),('broad',0.98,45)]:
        for _ in range(2):
            reports.append({'method':method,'valid':True,'judge_rounds':2,'feasible':True,
                'cases':[{'case_id':f'CS{i:03}'} for i in range(1,41)],
                'mismerge':{'rate':0.0},'recognition':{'rate':rate},'calls':{'count':calls},'case_p95_seconds':10.})
    assert selection.choose(reports)['selected']=='broad'
    reports[2]['mismerge']['rate']=None
    reports[2]['feasible']=False
    assert selection.choose(reports)['candidates'][1]['worst_mismerge'] is None
    reports[-1]['feasible']=False
    reports[-1]['mismerge']['rate']=0.1
    assert selection.choose(reports)['selected']=='balanced'
    reports[0]['judge_rounds']=1
    with pytest.raises(ValueError,match='double'):
        selection.choose(reports)


def test_safety_rejection_is_quality_result_not_invalid_model_run(tmp_path):
    case=runner.load_corpus(runner.ROOT/'evals/consolidation_v1.json')[0]
    def answer(p):
        return {'decision':'conflict','reason':'摘要范围有争议','evidence':[p['memories'][0]['sources'][0]['id']],
                'updates':[{'id':p['pair_ids'][0],'content':'她喜欢口琴','annotation':'待核对'}]}
    row=runner.case_run(case,tmp_path/'reject.db',lambda store,clock:Model(store,answer),'broad','rewrite_v2')
    assert row['valid'] and row['safety_rejections'] == 1
    assert any(a['status']=='failed' for a in row['input']['actual_actions'])
    assert len(row['calls']) == 1


def test_resolution_cannot_change_selected_merge_method(tmp_path):
    case=runner.load_corpus(runner.ROOT/'evals/consolidation_v1.json')[0]
    with pytest.raises(ValueError,match='broad'):
        runner.case_run(case,tmp_path/'wrong.db',lambda store,clock:Model(store),'strict','conservative_v2')


def test_write_audit_requires_bound_exact_cases_and_real_booleans(tmp_path):
    manifest={'materials_sha256':'same','cases':[{'case_id':'CS001'}]}
    a={'materials_sha256':'same','cases':[{'case_id':'CS001','has_damage':False,'reason':'未写回错误'}]}
    b=copy.deepcopy(a);b['cases'][0].update(has_damage=True,reason='标注丢失限定')
    paths=[]
    for index,document in enumerate((a,b)):
        path=tmp_path/f'{index}.json';runner._write_json(path,document);paths.append(path)
    result=scorer.audit_writes(manifest,paths)
    assert result['write_damage']['numerator']==1 and result['write_audit_rounds']==2
    assert result['write_audit_details'][0]['disagreement']
    b['cases'][0]['has_damage']=0
    runner._write_json(paths[1],b)
    with pytest.raises(ValueError,match='boolean'):
        scorer.audit_writes(manifest,paths)
    b['cases'][0]['has_damage']=False;b['materials_sha256']='different'
    runner._write_json(paths[1],b)
    with pytest.raises(ValueError,match='material'):
        scorer.audit_writes(manifest,paths)


def test_resolution_selection_worst_run_fallback_and_damage_gate():
    spec=importlib.util.spec_from_file_location('resolution_selection',DIR/'resolution_selection.py')
    selection=importlib.util.module_from_spec(spec);spec.loader.exec_module(selection)
    reports=[]
    for candidate in selection.ORDER:
        for i in range(2):
            reports.append({'resolution':candidate,'method':'broad','corpus_sha256':selection.CORPUS_SHA256,
                'valid':True,'judge_rounds':2,'write_audit_rounds':2,'materials_sha256':f'{candidate}-{i}',
                'cases':[{'case_id':f'CS{n:03}'} for n in range(1,41)],'mismerge':{'rate':0.},
                'protection':{'rate':1.},'conflict_dependency_consistency':{'rate':.75 if candidate=='rewrite_v2' else .7},
                'write_damage':{'numerator':0}})
    assert selection.choose(reports)['selected']=='conservative_v2'
    reports[0]['conflict_dependency_consistency']['rate']=.95
    assert selection.choose(reports)['selected']=='conservative_v2'  # Worse repetition still .75.
    reports[1]['conflict_dependency_consistency']['rate']=.9
    assert selection.choose(reports)['selected']=='rewrite_v2'
    reports[0]['write_damage']['numerator']=1
    assert selection.choose(reports)['selected'] is None
    reports[0]['write_damage']['numerator']=0
    reports[0]['mismerge']['rate']=.01
    assert selection.choose(reports)['selected']=='conservative_v2'
    reports[2]['protection']['rate']=.5
    assert selection.choose(reports)['selected'] is None
    reports[0]['write_audit_rounds']=1
    with pytest.raises(ValueError,match='double'):
        selection.choose(reports)


def test_score_exports_safety_audit_without_changing_frozen_judgment_schema(tmp_path):
    case=runner.load_corpus(runner.ROOT/'evals/consolidation_v1.json')[0]
    row=runner.case_run(case,tmp_path/'score.db',lambda store,clock:Model(store),'broad','conservative_v2')
    metadata={'source_sha256':runner.source_hash(),'scoring_version':runner.SCORING_VERSION,'method':'broad',
        'resolution':'conservative_v2','corpus':{'sha256':runner._json_sha256([case])}}
    materials=tmp_path/'materials'
    manifest=runner.export([row],materials,metadata)
    rounds=[];audits=[]
    for index in range(2):
        directory=tmp_path/f'judge-{index}';directory.mkdir();rounds.append(directory)
        runner._write_json(directory/'manifest.json',{'materials_sha256':manifest['materials_sha256']})
        runner._write_json(directory/'0000.json',{'format_version':1,'case_id':case['id'],
            'merge_results':[],'contradiction_results':[],'dependency_results':[],'protection_results':[]})
        path=tmp_path/f'audit-{index}.json';audits.append(path)
        runner._write_json(path,{'materials_sha256':manifest['materials_sha256'],
            'cases':[{'case_id':case['id'],'has_damage':False,'reason':'没有模型写回'}]})
    report=scorer.score(materials,rounds,tmp_path/'score','gpt-6-astra max',audits)
    assert report['resolution']=='conservative_v2' and report['write_damage']['numerator']==0
    assert report['judge_rounds']==report['write_audit_rounds']==2
    assert report['safety_rejections']==0


def test_resolution_freeze_binds_runtime_prompts_and_preserves_merge_instructions():
    import hashlib
    from iris.consolidation import DEFAULTS
    frozen=runner._read_json(DIR/'resolution_candidates_v2.json')
    assert frozen['frozen_order']==['rewrite_v2','conservative_v2'] and frozen['merge_method']=='broad'
    for name,expected in frozen['files'].items():
        assert hashlib.sha256((runner.ROOT/name).read_bytes()).hexdigest()==expected, name
    original=(runner.ROOT/'src/iris/prompts/consolidation_pair_v1.md').read_text(encoding='utf-8')
    common=original[:original.index('如果存在冲突')]
    for candidate in frozen['frozen_order']:
        assert (runner.ROOT/f'src/iris/prompts/consolidation_pair_{candidate}.md').read_text(encoding='utf-8').startswith(common)
    assert DEFAULTS['resolution']=='conservative_v2'
