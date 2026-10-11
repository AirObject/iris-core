"""MG2-S execution policy and diagnostics; all model replies are local fixtures."""
import copy
import json

import pytest

from iris.consolidation import Consolidation
from iris.models import ModelError
from test_consolidation import Model, merge_answer
from test_consolidation_eval import runner, scorer
from test_merge_details_eval import base_judgment, judge_dirs, sample


def blocked_case(tmp_path, kind):
    case = copy.deepcopy(runner.load_corpus(runner.ROOT / 'evals/consolidation_v1.json')[0])
    a, b = case['memories']
    a['content'] = '乔夏每周三在社区在线教口琴'
    b['content'] = '乔夏每周三在社区教口琴，面向初学者'
    for m in (a, b):
        m['sources'][0]['text'] = m['content']
    c = copy.deepcopy(a)
    c['key'] = 'c'
    c['sources'][0]['key'] = 's3'
    case['memories'].append(c)
    case['expected']['merges'] = [['a', 'c']]
    case['expected']['no_merge'] = [['a', 'b'], ['b', 'c']]

    def answer(p):
        if p['pair_ids'] == [1, 2]:
            if kind == 'conflict':
                return {'decision': 'conflict', 'reason': '假模型冲突', 'updates': [],
                        'evidence': [s['id'] for m in p['memories'] for s in m['sources']]}
            result = merge_answer(p)
            result['unique_details'] = {'a': ['在线'], 'b': ['面向初学者'] if kind == 'mutual_details' else ['不存在的原文']}
            return result
        if p['pair_ids'] == [1, 3]:
            return merge_answer(p)
        return {'decision': 'separate', 'reason': '假模型分开', 'evidence': [], 'updates': []}

    return runner.case_run(case, tmp_path / 'blocked.db', lambda store, clock: Model(store, answer), 'broad')


@pytest.mark.parametrize('kind', ['conflict', 'mutual_details', 'invalid_details'])
def test_blocked_expected_pair_is_a_valid_recognition_miss_with_causal_pair(tmp_path, kind):
    row = blocked_case(tmp_path, kind)
    assert row['valid']
    assert scorer.run_validity(row)['valid']
    check = next(c for c in row['input']['merge_checks'] if set(c['pair_keys']) == {'a', 'c'})
    assert check['outcome'] == 'skipped' and check['reason'] == 'unresolved_conflict'
    assert len(check['blocked_by']) == 1
    blocker = check['blocked_by'][0]
    assert blocker['pair_keys'] == ['a', 'b'] and blocker['type'] == kind
    assert blocker['check_id'] != check['check_id']
    scored = scorer.score_case(row['input'], base_judgment(row))
    assert scored['recognition'] == {'numerator': 0, 'denominator': 1, 'rate': 0.0}
    assert scored['blocked_merges'] == [{'index': 0, 'pair': ['a', 'c'], 'check_id': check['check_id'],
                                       'reason': 'unresolved_conflict', 'blocked_by': check['blocked_by']}]


@pytest.mark.parametrize('protection', ['pinned', 'admin'])
def test_forbidden_merge_is_model_error_and_does_not_invalidate(tmp_path, protection):
    case = copy.deepcopy(runner.load_corpus(runner.ROOT / 'evals/consolidation_v1.json')[0])
    case['memories'][0]['pinned'] = protection == 'pinned'
    case['memories'][0]['last_edit'] = 'admin' if protection == 'admin' else 'learning'
    row = runner.case_run(case, tmp_path / 'protected.db', lambda store, clock: Model(store, merge_answer), 'broad')
    assert row['valid'] and scorer.run_validity(row)['valid']
    check = row['input']['merge_checks'][0]
    assert check['outcome'] == 'failed' and check['failure_code'] == 'merge_forbidden'
    assert check['merge_allowed'] is False
    p = row['input']
    judgment = base_judgment(row)
    judgment['protection_results'] = [{'key': 'a', 'content_unchanged': True, 'belief_unchanged': True, 'reason': '未写回'}]
    scored = scorer.score_case(p, judgment)
    assert scored['protected_merge_errors']['count'] == 1
    assert scored['protected_merge_errors']['items'][0]['check_id'] == check['check_id']
    assert not p['actual_merges'] and scored['mismerge']['denominator'] == 0
    assert all(m['content'] == before['content'] for m, before in zip(p['after']['memories'], p['before']['memories']))


def test_other_value_error_is_not_misreported_as_forbidden_merge(tmp_path, monkeypatch):
    case = runner.load_corpus(runner.ROOT / 'evals/consolidation_v1.json')[0]
    def broken(*args):
        raise ValueError('unrelated_internal_failure')
    monkeypatch.setattr(Consolidation, 'apply', broken)
    row = runner.case_run(case, tmp_path / 'broken.db', lambda store, clock: Model(store, merge_answer), 'broad')
    assert not row['valid']
    assert row['input']['merge_checks'][0]['failure_code'] is None
    assert scorer.score_case(row['input'], base_judgment(row))['protected_merge_errors']['count'] == 0


@pytest.mark.parametrize('category,reason', [
    ('retryable', 'timeout'), ('retryable', 'rate_limited'), ('retryable', 'network_error'),
    ('paused', 'paused'), ('paused', 'usage_limit'), ('paused', 'call_budget'),
])
def test_infrastructure_failure_is_still_invalid_even_with_quality_block(tmp_path, category, reason):
    row = blocked_case(tmp_path, 'mutual_details')
    row['calls'][0]['result'] = category
    row['calls'][0]['error'] = reason
    assert not scorer.run_validity(row)['valid']


def test_unattempted_budget_is_not_excused_by_blocked_pair(tmp_path):
    row = blocked_case(tmp_path, 'invalid_details')
    row['report']['consolidation']['deferred'] += 1
    assert 'unattempted_work' in scorer.run_validity(row)['invalid_reasons']


@pytest.mark.parametrize('category', ['invalid_output', 'item_error', 'content_rejection'])
def test_completed_model_output_errors_are_quality_results(tmp_path, category):
    case = runner.load_corpus(runner.ROOT / 'evals/consolidation_v1.json')[0]
    row = runner.case_run(case, tmp_path / 'quality.db', lambda store, clock: Model(store, ModelError(category, 'fake')), 'broad')
    assert row['valid'] and scorer.run_validity(row)['valid']


def test_suggestion_false_positive_is_per_pair_not_per_annotation_and_not_gate(tmp_path):
    row, manifest, materials = sample(tmp_path, {'a': ['在线'], 'b': ['面向初学者']})
    base, detail = judge_dirs(tmp_path, manifest, row)
    report = scorer.score(materials, [base], tmp_path / 'score', 'fake', detail_judgments=[detail])
    assert report['suggestion_false_positives']['count'] == 1
    assert report['suggestion_false_positives']['items'][0]['pair'] == ['a', 'b']
    assert report['mutual_details']['denominator'] == 0 and report['mutual_details']['rate'] is None
    assert report['valid'] and report['mismerge']['denominator'] == 0  # no merges still fails gate
    assert not report['feasible']


def test_mutual_hit_uses_unordered_pairs_and_requires_committed_suggestion(tmp_path):
    row, _, _ = sample(tmp_path, {'a': ['在线'], 'b': ['面向初学者']})
    row['input']['case']['expected']['mutual_details'] = [['b', 'a']]
    scored = scorer.score_case(row['input'], base_judgment(row))
    assert scored['mutual_details']['numerator'] == scored['mutual_details']['denominator'] == 1
    assert scored['suggestion_false_positives']['count'] == 0
    assert scored['mutual_details']['misses_by_outcome'] == {}


@pytest.mark.parametrize('outcome', ['invalid_details', 'not_merge', 'one_sided', 'equivalent',
                                     'mutual_details', 'missing_detail_check', 'not_evaluated'])
def test_mutual_misses_are_classified_by_detail_outcome(tmp_path, outcome):
    row, _, _ = sample(tmp_path, {'a': ['在线'], 'b': ['面向初学者']})
    p = row['input']
    p['case']['expected']['mutual_details'] = [['b', 'a']]
    check = p['merge_checks'][0]
    check['outcome'] = 'skipped'
    if outcome == 'not_evaluated':
        p['merge_checks'] = []
    elif outcome == 'missing_detail_check':
        check['result'] = None
    else:
        check['result']['merge_detail_check']['outcome'] = outcome
    scored = scorer.score_case(p, base_judgment(row))
    assert scored['mutual_details']['numerator'] == 0
    assert scored['mutual_details']['denominator'] == 1
    assert scored['mutual_details']['misses_by_outcome'] == {outcome: 1}
    assert scored['suggestion_false_positives']['count'] == 0


@pytest.mark.parametrize('labels', [[['a']], [['a', 'a']], [['a', 'unknown']], [['a', 'b'], ['b', 'a']], 'a'])
def test_optional_mutual_label_validation(tmp_path, labels):
    cases = copy.deepcopy(runner.load_corpus(runner.ROOT / 'evals/consolidation_v1.json')[:1])
    cases[0]['expected']['mutual_details'] = labels
    path = tmp_path / 'test-corpus.json'
    path.write_text(json.dumps(cases, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='mutual_details'):
        runner.load_corpus(path)


def test_v2_scoring_and_audit_policy_are_versioned_and_fingerprint_bound(tmp_path):
    row, manifest, materials = sample(tmp_path, {'a': ['在线'], 'b': ['面向初学者']})
    assert row['input']['evaluation_policy'] == 'merge_scoring_v2'
    assert manifest['evaluation_policy'] == 'merge_scoring_v2'
    assert (materials / 'scoring.md').read_text() == runner.SCORING
    assert '2026-10-12' in (materials / 'merge-details-scoring.md').read_text()
    assert '合并待核对' in (materials / 'write-audit.md').read_text()
    assert scorer.load_materials(materials)[1] == [row]
    (materials / 'write-audit.md').write_text('changed', encoding='utf-8')
    with pytest.raises(ValueError, match='fingerprint'):
        scorer.load_materials(materials)


def test_legacy_materials_retain_v1_instructions_and_validity(tmp_path):
    case = runner.load_corpus(runner.ROOT / 'evals/consolidation_v1.json')[0]
    row = runner.case_run(case, tmp_path / 'legacy.db', lambda store, clock: Model(store), 'broad')
    row['input'].pop('evaluation_policy')
    row.pop('work_results')
    out = tmp_path / 'legacy'
    metadata = {'source_sha256': runner.source_hash(), 'method': 'broad',
                'corpus': {'sha256': runner._json_sha256([case])}}
    manifest = runner.export([row], out, metadata)
    assert 'evaluation_policy' not in manifest
    assert (out / 'merge-details-scoring.md').read_text() == runner.DETAIL_SCORING_V1
    assert (out / 'write-audit.md').read_text() == (runner.ROOT / 'evals/consolidation_eval/write_audit.md').read_text()
    assert scorer.load_materials(out)[1] == [row]
    row['input']['actual_actions'].append({'status': 'skipped'})
    assert not scorer.run_validity(row)['valid']  # old runs are not silently reinterpreted


def test_blocked_expected_pair_survives_export_and_offline_score(tmp_path):
    row = blocked_case(tmp_path, 'mutual_details')
    materials = tmp_path / 'materials'
    manifest = runner.export([row], materials, {'source_sha256': runner.source_hash(), 'method': 'broad',
        'resolution': 'report_only_v1', 'corpus': {'sha256': runner._json_sha256([row['input']['case']])}})
    base, detail = judge_dirs(tmp_path, manifest, row)
    report = scorer.score(materials, [base], tmp_path / 'score', 'fake', detail_judgments=[detail])
    assert report['valid'] and report['recognition']['numerator'] == 0
    assert report['blocked_merges'][0]['case_id'] == row['input']['case']['id']
    assert report['blocked_merges'][0]['blocked_by'][0]['type'] == 'mutual_details'
    assert report['suggestion_false_positives']['count'] == 1
    assert report['false_refusal']['numerator'] == 0  # blocking is not a semantic judgment


def test_suggestion_diagnostic_does_not_change_feasibility_with_an_actual_merge(tmp_path):
    first = tmp_path / 'first'; first.mkdir()
    second = tmp_path / 'second'; second.mkdir()
    merged, _, _ = sample(first, {'a': [], 'b': []})
    suggested, _, _ = sample(second, {'a': ['在线'], 'b': ['面向初学者']})
    suggested['input']['case']['id'] = 'fixture-suggestion'
    rows = [merged, suggested]
    metadata = {'source_sha256': runner.source_hash(), 'method': 'broad',
                'corpus': {'sha256': runner._json_sha256([r['input']['case'] for r in rows])}}
    materials = tmp_path / 'combined'
    manifest = runner.export(rows, materials, metadata)
    directory = tmp_path / 'judge'; directory.mkdir()
    runner._write_json(directory / 'manifest.json', {'materials_sha256': manifest['materials_sha256']})
    for entry, row in zip(manifest['cases'], rows):
        runner._write_json(directory / entry['judgment_file'], base_judgment(row))
    report = scorer.score(materials, [directory], tmp_path / 'score', 'fake mechanics only')
    assert report['valid'] and report['feasible']
    assert report['mismerge'] == {'numerator': 0, 'denominator': 1, 'rate': 0.0}
    assert report['suggestion_false_positives']['count'] == 1


def test_mutual_multiple_attempts_count_one_hit_or_one_miss_per_outcome(tmp_path):
    row, _, _ = sample(tmp_path, {'a': ['在线'], 'b': ['面向初学者']})
    p = row['input']
    p['case']['expected']['mutual_details'] = [['b', 'a']]
    extra = copy.deepcopy(p['merge_checks'][0])
    extra['check_id'] = 'co-another-attempt'
    extra['pair_keys'].reverse()
    p['merge_checks'].append(extra)
    scored = scorer.score_case(p, base_judgment(row))
    assert scored['mutual_details']['numerator'] == scored['mutual_details']['denominator'] == 1
    for check in p['merge_checks']:
        check['outcome'] = 'skipped'
    scored = scorer.score_case(p, base_judgment(row))
    assert scored['mutual_details']['misses_by_outcome'] == {'mutual_details': 1}
    extra['result']['merge_detail_check']['outcome'] = 'invalid_details'
    scored = scorer.score_case(p, base_judgment(row))
    assert scored['mutual_details']['denominator'] == 1
    assert scored['mutual_details']['misses_by_outcome'] == {'invalid_details': 1, 'mutual_details': 1}


def test_optional_mutual_labels_are_exported_and_aggregate_without_a_judge_field(tmp_path):
    row = blocked_case(tmp_path, 'mutual_details')
    row['input']['case']['expected']['mutual_details'] = [['b', 'a']]
    materials = tmp_path / 'materials'
    manifest = runner.export([row], materials, {'source_sha256': runner.source_hash(), 'method': 'broad',
        'resolution': 'report_only_v1', 'corpus': {'sha256': runner._json_sha256([row['input']['case']])}})
    base, detail = judge_dirs(tmp_path, manifest, row)
    report = scorer.score(materials, [base], tmp_path / 'score', 'fake', detail_judgments=[detail])
    assert report['mutual_details']['numerator'] == report['mutual_details']['denominator'] == 1
    assert report['suggestion_false_positives']['count'] == 0
    assert runner._read_json(materials / 'cases/0000.json')['input']['case']['expected']['mutual_details'] == [['b', 'a']]


def test_revision_skip_after_an_earlier_merge_is_a_quality_miss_not_infrastructure(tmp_path):
    case = copy.deepcopy(runner.load_corpus(runner.ROOT / 'evals/consolidation_v1.json')[0])
    c = copy.deepcopy(case['memories'][0])
    c['key'] = 'c'
    c['sources'][0]['key'] = 's3'
    case['memories'].append(c)
    case['expected']['merges'] = [['a', 'b'], ['a', 'c'], ['b', 'c']]
    row = runner.case_run(case, tmp_path / 'revision.db', lambda store, clock: Model(store, merge_answer), 'broad')
    assert row['valid'] and scorer.run_validity(row)['valid']
    assert sum(c['reason'] == 'revision_conflict' for c in row['input']['merge_checks']) == 2
    assert scorer.score_case(row['input'], base_judgment(row))['recognition'] == {
        'numerator': 1, 'denominator': 3, 'rate': 1 / 3}


def test_runner_budget_exhaustion_is_invalid_without_extra_calls(tmp_path):
    case = runner.load_corpus(runner.ROOT / 'evals/consolidation_v1.json')[0]
    def factory(store, clock):
        store.set_setting('consolidation', {'max_calls': 1})
        return Model(store, {'decision': 'separate', 'reason': 'fake', 'evidence': [], 'updates': [], 'unknown': 1})
    row = runner.case_run(case, tmp_path / 'budget.db', factory, 'broad')
    assert len(row['calls']) == 1
    assert not row['valid'] and not scorer.run_validity(row)['valid']
    assert row['report']['consolidation']['skip_reason'] == 'call_budget'
