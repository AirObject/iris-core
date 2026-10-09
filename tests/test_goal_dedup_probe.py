"""Scoring tests are small hand-written unit inputs, not evaluation corpora."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1]/'evals/goal_dedup_probe/scoring.py'
spec = spec_from_file_location('goal_probe_scoring',PATH)
scoring = module_from_spec(spec)
spec.loader.exec_module(scoring)


def sample(expected, *, merges=(), possible=(), degraded=False):
    case = {'id':'unit','category':'unit','expected':expected}
    result = {'origin':'host','merges':[{'target':target} for target in merges],
              'possible_targets':list(possible),'decision':{'status':'pending' if degraded else 'created'},
              'degraded':degraded,'model_calls':0,'prompt_tokens':0,'completion_tokens':0,
              'reasoning_tokens':0,'duration_ms':1}
    return scoring.score_case(case,result)


def test_zero_merges_is_undefined_and_infeasible_even_with_recognition():
    result=scoring.report([sample({'decision':'merge','target':'g1'},possible=['g1'])])
    assert result['wrong_merge_rate']=={'numerator':0,'denominator':0,'rate':None}
    assert result['merge_recognition']['rate']==1
    assert not result['feasible']


def test_wrong_target_separate_uncertain_and_each_actual_merge_count():
    rows=[sample({'decision':'merge','target':'g1'},merges=['g2','g1']),
          sample({'decision':'separate'},merges=['g1']),sample({'decision':'uncertain'},merges=['g1'])]
    result=scoring.report(rows)
    assert result['wrong_merge_rate']=={'numerator':3,'denominator':4,'rate':.75}
    assert result['merge_recognition']['rate']==1


def test_possible_duplicates_on_negative_cases_do_not_count_as_merges():
    rows=[sample({'decision':'separate'},possible=['g1']),
          sample({'decision':'uncertain','candidates':['g1','g2']},possible=['g2','g3'])]
    result=scoring.report(rows)
    assert result['separate_possible_rate']['rate']==1
    assert result['uncertain_possible_rate']['rate']==1
    assert result['uncertain_candidate_hit_rate']['rate']==1
    assert result['uncertain_candidate_outside_cases']==1


def test_any_degradation_invalidates_the_whole_run():
    result=scoring.report([sample({'decision':'merge','target':'g1'},merges=['g1']),
                           sample({'decision':'uncertain'},degraded=True)])
    assert not result['valid'] and not result['feasible']
    assert result['pending_cases']==result['degraded_cases']==1


def run(wrong=0,recognition=1,calls=1,p95=1):
    return {'valid':True,'feasible':wrong is not None and wrong<=.05,
            'wrong_merge_rate':{'rate':wrong},'merge_recognition':{'rate':recognition},
            'model_calls':calls,'latency_ms':{'p95':p95}}


def test_select_uses_worse_repeat_and_global_recognition_tie():
    reports={'A':[run(),run(.06)],'B':[run(recognition=.98,calls=2)]*2,
             'C':[run(recognition=1,calls=3)]*2}
    assert scoring.select_method(reports)=='B'
    reports['B'][1]=run(recognition=.96,calls=2)
    assert scoring.select_method(reports)=='C'


def test_no_feasible_prefers_observed_lowest_rate_over_zero_merges():
    reports={'A':[run(None)]*2,'B':[run(.1)]*2,'C':[run(.2)]*2}
    assert scoring.select_method(reports)=='B'
    reports['B'][0]['valid']=False
    with pytest.raises(ValueError):
        scoring.select_method(reports)


@pytest.mark.parametrize('origin',['host','internal','admin'])
def test_runner_seeds_history_and_calls_real_creation_path(tmp_path,origin):
    spec=spec_from_file_location('goal_probe_run',PATH.with_name('run.py'))
    runner=module_from_spec(spec)
    spec.loader.exec_module(runner)
    historical={'key':'g1','content':'整理手册','kind':'normal','origin':'host','state':'open',
                'entry_id':'old','entry_kind':'group','deadline':None,'people':[],
                'created_at':'2026-10-01T00:00:00+08:00','sources':[]}
    incoming={'content':'整理手册','kind':'normal','origin':origin,'entry_id':'new',
              'entry_kind':'private','sources':[{'entry_id':'new','sender':'self','kind':'self_output',
                                                'at':'2026-10-02T00:00:00+08:00','text':'我会整理手册。'}]}
    case={'id':'unit','category':'unit','now':'2026-10-02T00:01:00+08:00',
          'subjects':[],'existing':[historical],'incoming':incoming,
          'expected':{'decision':'merge','target':'g1'}}
    result=runner.run_case(case,{'timezone':'Asia/Shanghai','role_name':'Iris'},tmp_path,'A',None,60)
    assert result['recognized'] and result['model_calls']==0
    assert len(result['merges'])==(0 if origin=='admin' else 1)
    assert result['possible_targets']==(['g1'] if origin=='admin' else [])
