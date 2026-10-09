"""The C2 adoption rule uses offline reports, never a model or changed labels."""
from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from test_goal_dedup_probe import sample, scoring


@pytest.fixture
def compare(monkeypatch):
    path=Path(__file__).parents[1]/'evals/goal_dedup_probe/compare_possible.py'
    monkeypatch.syspath_prepend(str(path.parent))
    spec=spec_from_file_location('compare_goal_possible',path)
    module=module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.compare


def reports(method,*,improved=False):
    value=scoring.report([
        sample({'decision':'merge','target':'g1'},merges=['g1']),
        sample({'decision':'merge','target':'g2'},possible=['g2'] if improved else []),
        sample({'decision':'separate'},possible=['g1'] if improved else []),
        sample({'decision':'uncertain'},possible=['g1']),
    ])
    value.update(method=method,complete=True,simulation=False,expected_cases=4,
        corpus_sha256='unit-corpus',prompt_sha256='unit-prompt',model='unit',reasoning_effort='high',budget_seconds=60)
    return [deepcopy(value),deepcopy(value)]


def test_adopt_higher_recognition_without_imposing_unrequested_possible_threshold(compare):
    result=compare(reports('C'),reports('C2',improved=True))
    assert result['selected']=='C2'
    assert result['summaries']['C2']['runs'][0]['nonmerge_possible_rate']['rate']==1
    assert result['summaries']['C']['runs'][0]['nonmerge_possible_rate']['rate']==.5


def test_worse_repeat_and_zero_merge_reject_adoption(compare):
    candidate=reports('C2',improved=True)
    candidate[1]['merge_recognition']['rate']=.5
    assert compare(reports('C'),candidate)['selected']=='C'
    candidate=reports('C2',improved=True)
    candidate[1]['wrong_merge_rate']['rate']=.01
    assert compare(reports('C'),candidate)['selected']=='C'
    candidate[1]['wrong_merge_rate']={'numerator':0,'denominator':0,'rate':None}
    assert compare(reports('C'),candidate)['selected']=='C'


@pytest.mark.parametrize('change',[{'simulation':True},{'valid':False},{'complete':False},
    {'degraded_cases':1},{'pending_cases':1},{'method':'C'},{'cases':3},
    {'corpus_sha256':'changed'},{'prompt_sha256':'changed'},{'budget_seconds':5},
    {'model':'changed'},{'reasoning_effort':'low'}])
def test_mixed_invalid_or_fake_reports_never_select_c2(compare,change):
    candidate=reports('C2',improved=True)
    candidate[1].update(change)
    with pytest.raises(ValueError):
        compare(reports('C'),candidate)


def test_require_two_runs_of_both_methods(compare):
    with pytest.raises(ValueError):
        compare(reports('C'),reports('C2')[:1])


@pytest.mark.parametrize('origin',['host','internal','admin'])
def test_probe_only_numeric_pair_has_no_model_requests(tmp_path,origin,monkeypatch):
    path=Path(__file__).parents[1]/'evals/goal_dedup_probe/run.py'
    spec=spec_from_file_location('possible_probe',path)
    runner=module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setattr(runner,'load_test_models',lambda:pytest.fail('must never read configuration'))
    historical={'key':'old','content':'周五20点检查服务器','kind':'normal','origin':'host','state':'open',
        'entry_id':'old','entry_kind':'group','deadline':None,'people':[],
        'created_at':'2026-10-01T00:00:00+08:00','sources':[]}
    incoming={'content':'周五晚上检查服务器','kind':'normal','origin':origin,'entry_id':'new',
        'entry_kind':'private','sources':[]}
    case={'id':'unit','category':'unit','subjects':[],'now':'2026-10-02T00:00:00+08:00',
        'existing':[historical],'incoming':incoming,'expected':{'decision':'merge','target':'old'}}
    from iris.models import ModelConfig
    result=runner.run_case(case,{'timezone':'Asia/Shanghai','role_name':'Iris'},tmp_path,'C2',
        {'chat':ModelConfig('https://unit.invalid','','unit')},60,fake='same')
    assert result['decision']['status']=='possible_duplicate'
    assert result['possible_targets']==['old']
    assert result['model_calls']==0 and result['merges']==[] and not result['degraded']
    assert result['recognized']
