"""goal_review_eval: labels never reach the model; scoring is deterministic. Offline only."""
import json
import tempfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location('goal_review_eval_run', ROOT / 'evals/goal_review_eval/run.py')
runner = module_from_spec(spec)
spec.loader.exec_module(runner)
PUBLIC = json.loads((ROOT / 'evals/goal_review_v1.json').read_text(encoding='utf-8'))


def subset(*ids):
    return {**{k: v for k, v in PUBLIC.items() if k != 'cases'},
            'cases': [case for case in PUBLIC['cases'] if case['id'] in ids]}


@pytest.fixture
def outside():
    # Evaluation output must be outside the repository (pytest's basetemp is inside).
    with tempfile.TemporaryDirectory(prefix='g-c1-goal-review-eval-') as directory:
        yield Path(directory)


def run(outside, corpus, mode, name='run'):
    path = outside / 'corpus.json'
    path.write_text(json.dumps(corpus, ensure_ascii=False), encoding='utf-8')
    code = runner.main(['--corpus', str(path), '--out', str(outside / name), '--fake', mode])
    return code, json.loads((outside / name / 'report.json').read_text(encoding='utf-8'))


def test_product_input_drops_every_label():
    for case in PUBLIC['cases']:
        material = runner.product_input(case)
        assert set(material) == {'now', 'entries', 'subjects', 'goal', 'messages'}
        assert not set(runner.LABEL_FIELDS) & set(material)


def test_labels_never_reach_the_model_and_scoring_is_deterministic(outside):
    corpus = subset('GR001', 'GR005', 'GR009', 'GR012', 'GR017')
    code, report = run(outside, corpus, 'completed')
    assert code == 0 and report['valid'] and report['simulation'] is True and report['complete']
    requests = []
    for case in corpus['cases']:
        requests += json.loads((outside / 'run/cases' / case['id'] / 'requests.json').read_text(encoding='utf-8'))
    text = json.dumps(requests, ensure_ascii=False)
    assert requests and all(r['purpose'] == 'goal_auto_review' for r in requests)
    for case in corpus['cases']:
        assert case['reason'] not in text and case['expected'].get('notes', '\0') not in text
        for message in requests:
            payload = json.loads(message['messages'][-1]['content'])
            assert not {'expected', 'reason', 'category', 'split', 'id'} & set(payload)
            assert 'expected' not in payload['goal'] and 'key' not in payload['goal']
    # The host goal is never sent to the model and never closed.
    assert len(requests) == 4
    rows = {r['case_id']: r for r in report['results']}
    assert rows['GR001']['correct'] and not rows['GR001']['false_close']
    assert rows['GR005']['false_close'] and rows['GR009']['false_close']
    assert rows['GR012']['actual'] == 'open' and report['external_closed']['count'] == 0
    assert rows['GR017']['expected'] == 'abandoned' and rows['GR017']['false_close']  # direction reversed
    assert report['false_closures']['count'] == 3
    assert report['recognition'] == {'numerator': 1, 'denominator': 2, 'rate': .5}
    assert report['categories']['self_done'] == {'cases': 1, 'correct': 1, 'false_closures': 0, 'actual': {'completed': 1}}
    assert report['gates'] == {'false_closures_zero': False, 'external_closed_zero': True,
                               'recognition_at_least_80_percent': False}
    assert report['call_counts'] == {'calls': 4, 'timeouts': 0, 'rate_limited': 0, 'truncated': 0,
                                     'invalid_calls': 0, 'execution_errors': 0}
    assert set(report) >= {'corpus_sha256', 'prompt_sha256', 'prompt_version', 'source_sha256', 'valid'}


def test_open_answers_close_nothing(outside):
    code, report = run(outside, subset('GR001', 'GR005', 'GR012'), 'open')
    assert code == 0 and report['valid']
    assert [r['actual'] for r in report['results']] == ['open', 'open', 'open']
    assert report['false_closures']['count'] == 0 and report['recognition']['rate'] == 0


def test_invalid_output_is_a_quality_result_not_an_execution_failure(outside):
    code, report = run(outside, subset('GR001'), 'invalid')
    assert report['valid'] and report['results'][0]['actual'] == 'open'
    assert report['results'][0]['review_reason'] == 'invalid_output'
    assert report['call_counts']['calls'] == 2  # the answer plus one JSON repair


def test_timeout_stops_and_invalidates_the_run(outside):
    code, report = run(outside, subset('GR001', 'GR002'), 'timeout')
    assert code == 2 and not report['valid'] and not report['complete']
    assert report['cases'] == 1 and report['call_counts']['timeouts'] >= 1
    assert report['review_failures'][0]['case_id'] == 'GR001'


def test_output_inside_repository_and_bad_corpus_are_rejected(outside):
    with pytest.raises(SystemExit):
        runner.main(['--out', str(ROOT / 'evals' / 'goal-review-output'), '--fake', 'open'])
    broken = subset('GR001')
    broken['cases'][0]['expected'] = {'state': 'done'}
    path = outside / 'broken.json'
    path.write_text(json.dumps(broken, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(SystemExit):
        runner.main(['--corpus', str(path), '--out', str(outside / 'x'), '--fake', 'open'])
    assert not (outside / 'x').exists() and not (ROOT / 'evals' / 'goal-review-output').exists()


@pytest.mark.parametrize('expected,actual,false_close,correct', [
    ('open', 'open', False, True), ('open', 'completed', True, False), ('open', 'abandoned', True, False),
    ('completed', 'completed', False, True), ('completed', 'abandoned', True, False),
    ('completed', 'open', False, False), ('abandoned', 'completed', True, False),
])
def test_score_case_directions(expected, actual, false_close, correct):
    case = {'id': 'u', 'category': 'unit', 'goal': {'origin': 'internal'}, 'expected': {'state': expected}}
    row = runner.score_case(case, {'state': actual})
    assert (row['false_close'], row['correct']) == (false_close, correct)


def test_external_closure_counts_separately():
    case = {'id': 'u', 'category': 'host_admin', 'goal': {'origin': 'host'}, 'expected': {'state': 'open'}}
    row = runner.score_case(case, {'state': 'completed'})
    result = runner.report([row], [], expected_cases=1)
    assert result['external_closed'] == {'count': 1, 'cases': ['u']} and not result['gates']['external_closed_zero']
