"""Offline parameter plumbing for the GD2 grid; no quality corpus is generated."""
import json
from contextlib import closing
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from iris.models import ModelConfig
from test_goal_dedup_probe import sample, scoring


def test_nonmerge_diagnostic_includes_separate_and_uncertain_without_changing_merge_score():
    rows = [sample({'decision':'separate'},possible=['a']),sample({'decision':'separate'}),
            sample({'decision':'uncertain'},possible=['b']),sample({'decision':'merge','target':'c'},merges=['c'])]
    summary = scoring.report(rows)
    assert summary['nonmerge_possible_rate'] == {'numerator':2,'denominator':3,'rate':2/3}
    assert summary['wrong_merge_rate'] == {'numerator':0,'denominator':1,'rate':0}
    assert summary['merge_recognition']['rate'] == 1
    assert scoring.report([])['nonmerge_possible_rate']['rate'] is None


@pytest.mark.parametrize('threshold,marked', [(0,True),(.99,False)])
def test_runner_uses_requested_threshold_and_preserves_it_in_database(tmp_path,monkeypatch,threshold,marked):
    path=Path(__file__).parents[1]/'evals/goal_dedup_probe/run.py'
    spec=spec_from_file_location('gd2_probe',path)
    runner=module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setattr(runner,'load_test_models',lambda:pytest.fail('must not read model configuration'))
    case={'id':'unit','category':'unit','subjects':[],'now':'2026-10-02T00:00:00+08:00',
        'existing':[{'key':'old','content':'周五20点检查服务器','kind':'normal','origin':'host','state':'open',
            'created_at':'2026-10-01T00:00:00+08:00','deadline':None,'people':[],'sources':[]}],
        'incoming':{'content':'周五晚上检查服务器','kind':'normal','origin':'host','sources':[]},
        'expected':{'decision':'merge','target':'old'}}
    result=runner.run_case(case,{'timezone':'Asia/Shanghai','role_name':'Iris'},tmp_path,'C2',
        {'chat':ModelConfig('https://test.invalid/v1','','fake')},1,fake='different',possible_min_similarity=threshold)
    assert bool(result['possible_targets']) == marked
    assert result['model_calls'] == 0 and not result['degraded']
    with closing(runner.Store(tmp_path/'unit/iris.db')) as reopened, reopened.read() as conn:
        setting=json.loads(conn.execute("SELECT value_json FROM runtime_settings WHERE key='goal_dedup_judge'").fetchone()[0])
    assert setting['possible_min_similarity'] == threshold
