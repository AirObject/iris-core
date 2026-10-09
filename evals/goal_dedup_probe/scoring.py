"""Automatic scoring for frozen goal deduplication labels; no model grading."""
from __future__ import annotations

import math


def ratio(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator,
            'rate': numerator / denominator if denominator else None}


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    position = (len(values)-1)*fraction
    left = math.floor(position)
    return values[left] + (values[min(left+1,len(values)-1)]-values[left])*(position-left)


def score_case(case, result):
    expected = case['expected']
    merges = result['merges']
    possible = set(result['possible_targets'])
    target = expected.get('target')
    wrong = sum(expected['decision'] != 'merge' or merge['target'] != target for merge in merges)
    correct = expected['decision'] == 'merge' and (
        any(merge['target'] == target for merge in merges) or target in possible)
    allowed = expected.get('candidates')
    return {**result, 'id': case['id'], 'category': case['category'], 'expected': expected,
            'wrong_merges': wrong, 'recognized': bool(correct),
            'possible_candidate_hit': bool(possible & set(allowed)) if allowed is not None else None,
            'possible_outside_candidates': sorted(possible-set(allowed)) if allowed is not None else None}


def summarize(rows):
    merge_cases = [r for r in rows if r['expected']['decision'] == 'merge']
    separate = [r for r in rows if r['expected']['decision'] == 'separate']
    uncertain = [r for r in rows if r['expected']['decision'] == 'uncertain']
    candidates = [r for r in uncertain if r['possible_candidate_hit'] is not None]
    degraded = sum(bool(r['degraded']) for r in rows)
    result = {
        'cases': len(rows), 'valid': degraded == 0,
        'invalid_reason': 'degraded_run_must_be_repeated' if degraded else None,
        'wrong_merge_rate': ratio(sum(r['wrong_merges'] for r in rows), sum(len(r['merges']) for r in rows)),
        'merge_recognition': ratio(sum(r['recognized'] for r in merge_cases), len(merge_cases)),
        'separate_possible_rate': ratio(sum(bool(r['possible_targets']) for r in separate), len(separate)),
        'uncertain_possible_rate': ratio(sum(bool(r['possible_targets']) for r in uncertain), len(uncertain)),
        'uncertain_candidate_hit_rate': ratio(sum(r['possible_candidate_hit'] for r in candidates), len(candidates)),
        'uncertain_candidate_outside_cases': sum(bool(r['possible_outside_candidates']) for r in candidates),
        'degraded_cases': degraded, 'pending_cases': sum(r['decision']['status'] == 'pending' for r in rows),
        'model_calls': sum(r['model_calls'] for r in rows),
        'prompt_tokens': sum(r['prompt_tokens'] for r in rows),
        'completion_tokens': sum(r['completion_tokens'] for r in rows),
        'reasoning_tokens': sum(r['reasoning_tokens'] for r in rows),
        'latency_ms': {'p50': percentile([r['duration_ms'] for r in rows],.5),
                       'p95': percentile([r['duration_ms'] for r in rows],.95)},
        'host_latency_ms': {'p50': percentile([r['duration_ms'] for r in rows if r['origin']=='host'],.5),
                            'p95': percentile([r['duration_ms'] for r in rows if r['origin']=='host'],.95)},
    }
    wrong = result['wrong_merge_rate']['rate']
    result['feasible'] = result['valid'] and wrong is not None and wrong <= .05
    return result


def report(rows):
    return {**summarize(rows), 'categories': {category:summarize([r for r in rows if r['category']==category])
            for category in sorted({r['category'] for r in rows})},
            'classes': {label:summarize([r for r in rows if r['expected']['decision']==label])
                       for label in ('merge','separate','uncertain')}, 'results': rows}


def select_method(reports, order=('A','B','C')):
    """Conservative across repeats; compare ties to the global best, not chained."""
    summaries = {}
    for method in order:
        runs = reports[method]
        if len(runs) < 2 or any(not run['valid'] for run in runs):
            raise ValueError('each candidate needs at least two complete valid runs')
        rates = [run['wrong_merge_rate']['rate'] for run in runs]
        summaries[method] = {'feasible': all(run['feasible'] for run in runs),
            'wrong': max(rate if rate is not None else math.inf for rate in rates),
            'recognition': min(run['merge_recognition']['rate'] or 0 for run in runs),
            'calls': max(run['model_calls'] for run in runs),
            'p95': max(run['latency_ms']['p95'] for run in runs)}
    eligible = [method for method in order if summaries[method]['feasible']]
    if not eligible:
        return min(order, key=lambda method:(summaries[method]['wrong'],order.index(method)))
    best = max(summaries[method]['recognition'] for method in eligible)
    tied = [method for method in eligible if best-summaries[method]['recognition'] < .03]
    return min(tied, key=lambda method:(summaries[method]['calls'],summaries[method]['p95'],order.index(method)))
