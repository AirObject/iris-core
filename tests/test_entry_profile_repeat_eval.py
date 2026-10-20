"""Repeated replay and paired descriptive statistics, with fake votes only."""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evals.entry_profile_eval import memory_runner as runner
from iris.db import dumps
from iris.models import ModelError
from test_entry_profile_incremental import Gateway, propose, source
from test_entry_profile_incremental_eval import corpus


def factory(case, store):
    def observe(payload):
        if payload['profiles']:
            return {'observations': [dict(op='confirm', memory_id=payload['profiles'][0]['memory_id'],
                source_refs=[source(m) for m in payload['messages']])]}
        return propose(payload)
    return Gateway(store, observe)


def scores(series_dir, coverages, violations=None):
    series = json.loads((series_dir / 'series.json').read_text())
    paths = []
    for record, coverage in zip(series['runs'], coverages, strict=True):
        root = series_dir / record['directory']; materials = root / 'judging-materials'
        manifest = json.loads((materials / 'manifest.json').read_text())
        rounds = []
        for name in ('format-a', 'format-b'):
            out = root / name; out.mkdir()
            (out / 'manifest.json').write_bytes((materials / 'round-template.json').read_bytes())
            for case in manifest['cases']:
                value = json.loads((materials / case['file']).read_text())['input']
                bad = bool(violations and violations[record['index'] - 1])
                vote = dict(memory_results=[dict(index=i, memory_id=m['memory_id'], revision=m['revision'],
                    supported=not bad, violations=['无依据', '个人评价'] if bad else [], reason='假票只验证汇总结构')
                    for i, m in enumerate(value['memories'], 1)], projection_violations=[],
                    must_cover=[coverage] * len(value['must_cover']), forbidden=[bad] * len(value['forbidden']),
                    stage_coverage={stage: [coverage] * len(value['must_cover']) for stage in runner.STAGES})
                (out / case['judgment_file']).write_text(dumps(vote))
            rounds.append(out)
        report = runner.score_draft(materials, rounds, judge_model='fake-format-only')
        path = root / 'score.json'; path.write_text(dumps(report)); paths.append(path)
    return paths


def series(tmp_path, name='series', *, runs=2, corpus_path=None):
    path = corpus_path or corpus(tmp_path)
    root = tmp_path / name
    runner.run_repeated(path, root, runs=runs, gateway_factory=factory)
    return root


def test_repeat_uses_independent_directories_and_databases(tmp_path):
    stores = []
    def record(case, store):
        stores.append(store.path)
        return factory(case, store)
    root = tmp_path / 'series'
    result = runner.run_repeated(corpus(tmp_path), root, runs=3, gateway_factory=record)
    assert result['requested_runs'] == 3 and len(result['runs']) == 3
    assert len(stores) == len(set(map(str, stores))) == 3
    assert [r['index'] for r in result['runs']] == [1, 2, 3]
    assert all((root / r['directory'] / 'judging-materials/manifest.json').exists() for r in result['runs'])
    with pytest.raises(FileExistsError):
        runner.run_repeated(corpus(tmp_path), root, runs=3, gateway_factory=record)


@pytest.mark.parametrize('count', [0, -1, True, 1.5])
def test_invalid_repeat_count_is_rejected_before_creating_output(tmp_path, count):
    out = tmp_path / 'out'
    with pytest.raises(ValueError):
        runner.run_repeated(corpus(tmp_path), out, runs=count, gateway_factory=factory)
    assert not out.exists()


def test_repeat_stops_after_invalid_transport_instead_of_silently_rerunning(tmp_path):
    calls = []
    def bad(case, store):
        calls.append(case)
        return Gateway(store, lambda p: (_ for _ in ()).throw(ModelError('retryable', 'late', reason='timeout')))
    result = runner.run_repeated(corpus(tmp_path), tmp_path / 'series', runs=3, gateway_factory=bad)
    assert len(calls) == len(result['runs']) == 1
    assert result['invalidated']['reason'] == 'timeout'
    assert not result['gate_eligible']


def test_summary_preserves_each_run_and_per_case_coverage_distribution(tmp_path):
    root = series(tmp_path)
    summary = runner.summarize_repeated(root, scores(root, [True, False]))
    assert [r['coverage'] for r in summary['runs']] == [1, 0]
    assert summary['means']['coverage'] == .5
    assert summary['per_case'][0]['coverage_counts'] == [1, 0]
    assert summary['per_case'][0]['coverage_mean'] == .5
    assert not summary['gate_eligible'] and summary['descriptive_only']


def test_pairing_is_by_run_index_and_case_and_sums_violations_separately(tmp_path):
    path = corpus(tmp_path)
    value = json.loads(path.read_text()); value['cases'][0]['forbidden'] = ['禁止个人评价']
    path.write_text(dumps(value))
    left_root = series(tmp_path, 'left', corpus_path=path)
    right_root = series(tmp_path, 'right', corpus_path=path)
    left = runner.summarize_repeated(left_root, scores(left_root, [False, True]))
    right = runner.summarize_repeated(right_root, scores(right_root, [True, True], [True, False]))
    compared = runner.compare_repeated(left, right)
    assert [p['coverage_delta'] for p in compared['pairs']] == [1, 0]
    assert compared['means']['coverage_delta'] == .5
    assert compared['per_case'][0]['coverage_deltas'] == [1, 0]
    assert compared['totals']['right']['violation_memory_items'] == 1
    assert compared['totals']['right']['forbidden_occurrences'] == 1
    assert compared['totals']['right']['calls'] == compared['totals']['left']['calls']
    assert compared['means']['calls_delta'] == 0
    assert compared['descriptive_only'] and not compared['gate_eligible']


def test_missing_or_foreign_score_is_not_averaged_away(tmp_path):
    root = series(tmp_path)
    paths = scores(root, [True, False])
    with pytest.raises(ValueError): runner.summarize_repeated(root, paths[:1])
    value = json.loads(paths[1].read_text()); value['materials_sha256'] = '0' * 64
    paths[1].write_text(dumps(value))
    with pytest.raises(ValueError, match='fingerprint'):
        runner.summarize_repeated(root, paths)


def test_comparison_rejects_different_corpus_or_tampered_summary(tmp_path):
    root = series(tmp_path)
    summary = runner.summarize_repeated(root, scores(root, [True, False]))
    tampered = copy.deepcopy(summary); tampered['runs'][0]['coverage'] = 100
    with pytest.raises(ValueError, match='fingerprint'): runner.compare_repeated(summary, tampered)
    other_path = tmp_path / 'other.json'
    value = json.loads(corpus(tmp_path).read_text()); value['cases'][0]['must_cover'] = ['其他格式标签']
    other_path.write_text(dumps(value))
    other_root = series(tmp_path, 'other', corpus_path=other_path)
    other = runner.summarize_repeated(other_root, scores(other_root, [True, True]))
    with pytest.raises(ValueError, match='corpus'): runner.compare_repeated(summary, other)


def test_offline_commands_do_not_load_any_model_configuration(tmp_path, monkeypatch):
    root = series(tmp_path)
    paths = scores(root, [True, False]); output = tmp_path / 'summary.json'
    import iris.models
    monkeypatch.setattr(iris.models, 'load_test_models', lambda: (_ for _ in ()).throw(AssertionError('config read')))
    monkeypatch.setattr(sys, 'argv', ['memory_runner', 'summarize', '--series', str(root),
        '--score', str(paths[0]), '--score', str(paths[1]), '--out', str(output)])
    runner.main()
    assert json.loads(output.read_text())['means']['coverage'] == .5
    comparison = tmp_path / 'comparison.json'
    monkeypatch.setattr(sys, 'argv', ['memory_runner', 'compare', '--left', str(output),
        '--right', str(output), '--out', str(comparison)])
    runner.main()
    assert json.loads(comparison.read_text())['means']['coverage_delta'] == 0


def test_series_detects_input_changes_during_the_final_run(tmp_path, monkeypatch):
    path = corpus(tmp_path); original = runner.run_sqlite
    def mutate(*args, **kwargs):
        manifest = original(*args, **kwargs)
        path.write_text(path.read_text() + '\n')
        return manifest
    monkeypatch.setattr(runner, 'run_sqlite', mutate)
    result = runner.run_repeated(path, tmp_path / 'series', runs=1, gateway_factory=factory)
    assert result['invalidated']['reason'] == 'input_changed'
    assert not result['gate_eligible']


def test_pairing_matches_case_ids_even_if_summary_case_order_differs(tmp_path):
    path = corpus(tmp_path); value = json.loads(path.read_text())
    second = copy.deepcopy(value['cases'][0]); second['id'] = 'unit-second'
    second['must_cover'] = ['第二例格式标签']; value['cases'].append(second)
    path.write_text(dumps(value))
    root = series(tmp_path, runs=1, corpus_path=path)
    summary = runner.summarize_repeated(root, scores(root, [True]))
    reordered = copy.deepcopy(summary)
    reordered['runs'][0]['cases'].reverse()
    reordered.pop('summary_sha256')
    reordered = runner._sealed(reordered, 'summary_sha256')
    comparison = runner.compare_repeated(summary, reordered)
    assert [r['case_id'] for r in comparison['pairs']] == ['unit-daily', 'unit-second']
    assert all(r['coverage_delta'] == 0 for r in comparison['pairs'])


def test_repeat_default_requests_match_single_replay_without_runtime_overrides(tmp_path):
    path = corpus(tmp_path)
    baseline, repeated = [], []
    def capture(target):
        def make(case, store):
            gateway = factory(case, store)
            original = gateway.chat
            def chat(messages, purpose, max_tokens=16000, **kwargs):
                target.append(dict(messages=copy.deepcopy(messages), purpose=purpose,
                                   max_tokens=max_tokens, deadline=kwargs.get('_deadline')))
                return original(messages, purpose, max_tokens, **kwargs)
            gateway.chat = chat
            return gateway
        return make
    runner.run_sqlite(path, tmp_path / 'single', gateway_factory=capture(baseline))
    result = runner.run_repeated(path, tmp_path / 'repeat', runs=1,
                                 gateway_factory=capture(repeated))
    assert baseline == repeated and len(repeated) == 3
    assert result['settings'] == {}
    assert result['runs'][0]['usage']['calls'] == 3
    assert result['runs'][0]['usage']['provider_recorded'] is False


@pytest.mark.parametrize('command', ['run', 'repeat'])
def test_cli_has_no_experimental_runtime_switch(tmp_path, monkeypatch, command):
    import iris.models
    monkeypatch.setattr(iris.models, 'load_test_models',
                        lambda: (_ for _ in ()).throw(AssertionError('config read')))
    args = ['memory_runner', command, '--corpus', str(corpus(tmp_path)),
            '--out', str(tmp_path / 'out'), '--union-nomination']
    if command == 'repeat':
        args.extend(['--runs', '3'])
    monkeypatch.setattr(sys, 'argv', args)
    with pytest.raises(SystemExit) as error:
        runner.main()
    assert error.value.code == 2


def test_repeat_cli_passes_only_standard_run_arguments(tmp_path, monkeypatch):
    import iris.models
    configs = object()
    called = []
    monkeypatch.setattr(iris.models, 'load_test_models', lambda: configs)
    def repeated(corpus_path, output, **kwargs):
        called.append((corpus_path, output, kwargs))
        return {'runs': [{}, {}, {}], 'invalidated': None}
    monkeypatch.setattr(runner, 'run_repeated', repeated)
    path = corpus(tmp_path); out = tmp_path / 'out'
    monkeypatch.setattr(sys, 'argv', ['memory_runner', 'repeat', '--corpus', str(path),
                                    '--runs', '3', '--out', str(out)])
    runner.main()
    assert called == [(str(path), str(out), {'runs': 3, 'configs': configs})]


def test_provider_call_usage_counts_observe_admit_and_json_repair(tmp_path):
    (tmp_path / 'run.json').write_text(dumps({'cases': [{'model_calls': [
        {'purpose': 'consolidation_entry_profile_observe', 'duration_ms': 100,
         'prompt_tokens': 2, 'completion_tokens': 3},
        {'purpose': 'consolidation_entry_profile_observe_repair', 'duration_ms': 250,
         'prompt_tokens': 5, 'completion_tokens': 7},
        {'purpose': 'consolidation_entry_profile_admit', 'duration_ms': 400,
         'prompt_tokens': 11, 'completion_tokens': 13},
    ]}]}))
    usage = runner._repeat_usage(tmp_path)
    assert usage == dict(calls=3, provider_recorded=True, observe_calls=1,
                        admit_calls=1, repair_calls=1, request_seconds=.75,
                        prompt_tokens=18, completion_tokens=23)
