"""Infrastructure failures stay invalid; malformed check fields fail conservatively."""
import copy
import json

import pytest

from iris.db import dumps
from iris.persona_evaluation import run_persona_eval, _metrics, _invalid_model_calls
from test_persona_evaluation import FakeGateway, TIMELINE, tmp_path


@pytest.mark.parametrize('failure', ['length', 'missing_verdict'])
def test_incomplete_checks_are_exported_but_never_reused_as_completed(tmp_path, failure):
    class Incomplete(FakeGateway):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            if purpose == 'persona_check':
                if failure == 'length':
                    reply.finish_reason = 'length'
                else:
                    value = json.loads(reply.content)
                    value.pop('change_degree')
                    reply.content = dumps(value)
            return reply
    corpus = tmp_path/'small.jsonl'; case = copy.deepcopy(TIMELINE)
    case['events'] = case['events'][:2]; corpus.write_text(dumps(case), encoding='utf-8')
    scoring = tmp_path/'scoring.md'; scoring.write_text('格式测试', encoding='utf-8')
    for _ in range(2):
        materials, report = run_persona_eval({}, tmp_path, corpus=corpus, out=tmp_path/'out',
            judge_mode='external', gateway_factory=Incomplete, runner_identity=failure, scoring_path=scoring)
        if failure == 'missing_verdict':
            assert report['metrics']['run_valid'] and report['metrics']['incomplete_check_count'] == 0
            assert report['rows'][0]['complete'] and report['resumed_cases'] == _
            assert report['metrics']['check_structure']['conservative_batch_count'] >= 1
            continue
        assert report['resumed_cases'] == 0
        assert not report['metrics']['run_valid']
        assert report['metrics']['incomplete_check_count'] == 1
        assert 'incomplete_model_check' in report['metrics']['run_invalid_reasons']
        assert report['metrics']['incomplete_timelines'] == 1
        assert not report['rows'][0]['complete']
        assert len(json.loads(materials.read_text())['cases']) == 1
        # Old reports may say complete=True: offline scoring must still recompute validity.
        old_rows = copy.deepcopy(report['rows']); old_rows[0]['complete'] = True
        assert not _metrics(old_rows)['run_valid']


def test_any_output_limit_invalidates_the_run_even_after_safe_deletion():
    calls = [{'id': 1, 'purpose': 'persona_sentence_repair', 'finish_reason': 'length',
              'duration_ms': 20000, 'timed_out': 0, 'status_code': 200, 'result_category': 'success'}]
    assert _invalid_model_calls(calls)[0]['reason'] == 'output_limit'


def test_a_complete_deterministically_rejected_candidate_is_a_valid_quality_result(tmp_path):
    class Unsupported(FakeGateway):
        def chat(self, messages, purpose, **kwargs):
            reply = super().chat(messages, purpose, **kwargs)
            if purpose == 'persona_generate':
                value = json.loads(reply.content)
                value['sentences'][0]['basis'] = ['M404']
                reply.content = dumps(value)
            return reply
    corpus = tmp_path/'small.jsonl'; case = copy.deepcopy(TIMELINE)
    case['events'] = case['events'][:2]; corpus.write_text(dumps(case), encoding='utf-8')
    scoring = tmp_path/'scoring.md'; scoring.write_text('格式测试', encoding='utf-8')
    _, report = run_persona_eval({}, tmp_path, corpus=corpus, out=tmp_path/'out', judge_mode='external',
        gateway_factory=Unsupported, runner_identity='unsupported', scoring_path=scoring)
    assert report['metrics']['run_valid'] and report['rows'][0]['complete']
    assert report['metrics']['check_rejected_ratio'] == 1
    assert report['metrics']['incomplete_check_count'] == 0
