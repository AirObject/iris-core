"""Runner accounting uses the frozen public corpus, never a generated quality set."""
import importlib.util
import json
import tempfile
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('visibility_runner',ROOT/'evals/visibility_eval/run.py')
runner=importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.fixture
def tmp_path():
    with tempfile.TemporaryDirectory(prefix='iris-visibility-test-') as directory:
        yield Path(directory)


@pytest.mark.parametrize('http_search', [False, True])
def test_frozen_visibility_offline_all_annotations_and_changes(tmp_path, http_search):
    report=runner.run(ROOT/'evals/visibility_v1.json',tmp_path/'run',http_search=http_search)
    assert report['valid'] and report['leakage_count']==0
    assert (report['cases'],report['queries_executed'],report['forbidden_count'])==(32,99,74)
    assert (report['merge_pairs'],report['merge_leaks'],report['changes_applied'])==(6,0,4)
    assert (report['expected_hits'],report['expected_goals_hits'])==(94,18)
    assert not report['include_goals_violations']
    assert report['http_search'] is http_search
    expected_http=sum(q['mode']=='search' for c in json.loads((ROOT/'evals/visibility_v1.json').read_text())['cases'] for q in c['queries']) if http_search else 0
    assert report['http_search_queries']==expected_http
    assert sum(q['transport']=='http' for c in report['details'] for q in c['queries'])==expected_http


def test_failed_query_cannot_be_scored_as_zero_leakage_success(tmp_path, monkeypatch):
    def fail(*args,**kwargs):
        raise RuntimeError('simulated infrastructure error')
    monkeypatch.setattr(runner,'run_case',fail)
    report=runner.run(ROOT/'evals/visibility_v1.json',tmp_path/'run')
    assert not report['valid'] and report['queries_executed']==0 and len(report['errors'])==32


def test_degraded_judgment_invalidates_run(tmp_path,monkeypatch):
    from iris.retrieval import Retrieval
    original=Retrieval.prepare
    def degrade(*args,**kwargs):
        result=original(*args,**kwargs)
        result['judgment']['status']='degraded'
        return result
    monkeypatch.setattr(Retrieval,'prepare',degrade)
    report=runner.run(ROOT/'evals/visibility_v1.json',tmp_path/'run')
    assert not report['valid'] and report['queries_executed']==99


def test_only_corpus_recent_window_is_returned(tmp_path):
    case=json.loads((ROOT/'evals/visibility_v1.json').read_text())['cases'][0]
    row=runner.run_case(case,tmp_path/'case')
    returned=row['queries'][0]['response']['recent_messages']
    assert [m['content'] for m in returned]==[m['text'] for m in case['queries'][0]['recent_messages']]


def test_http_error_invalidates_visibility_run(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from iris.api import HostRetrieval
    def unavailable(*args, **kwargs):
        raise HTTPException(status_code=503, detail='simulated infrastructure error')
    monkeypatch.setattr(HostRetrieval, 'search', unavailable)
    report=runner.run(ROOT/'evals/visibility_v1.json', tmp_path/'run', http_search=True)
    assert not report['valid'] and report['queries_executed'] < report['queries_expected']
    assert report['errors']
