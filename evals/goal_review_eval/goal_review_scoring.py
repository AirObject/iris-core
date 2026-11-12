"""Deterministic G1 scoring; reads labels only after every case has run."""
from __future__ import annotations

from collections import Counter

CLOSED = ('completed', 'abandoned')
# Completed model refusals / malformed output are quality observations; the goal
# simply stays open. Anything else in the call ledger is an execution failure.
QUALITY_CALL_RESULTS = {'success', 'invalid_output', 'item_error', 'content_rejection'}


def ratio(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator,
            'rate': numerator / denominator if denominator else None}


def score_case(case, result):
    """result: {'state', 'evidence_keys', 'origin', ...} from the product database."""
    expected = case['expected']['state']
    actual = result['state']
    false_close = (expected == 'open' and actual != 'open') or (
        expected in CLOSED and actual in CLOSED and actual != expected)
    external = case['goal']['origin'] in ('host', 'admin')
    row = {'case_id': case['id'], 'category': case['category'], 'origin': case['goal']['origin'],
           'expected': expected, 'actual': actual, 'correct': actual == expected,
           'false_close': false_close, 'external_closed': external and actual != 'open',
           'actual_evidence': result.get('evidence_keys', []),
           'expected_evidence': case['expected'].get('evidence', []),
           'review_reason': result.get('review_reason'), 'model_calls': result.get('model_calls', 0)}
    if actual in CLOSED and row['expected_evidence']:
        row['evidence_overlap'] = sorted(set(row['actual_evidence']) & set(row['expected_evidence']))
    return row


def call_counts(calls):
    """Same validity vocabulary as the other evaluations: timeouts, 429, truncation, invalid calls."""
    return {'calls': len(calls),
            'timeouts': sum(bool(c.get('timed_out')) for c in calls),
            'rate_limited': sum(c.get('status_code') == 429 for c in calls),
            'truncated': sum(c.get('finish_reason') not in (None, 'stop') for c in calls),
            'invalid_calls': sum(c.get('result_category') in ('invalid_output', 'item_error') for c in calls),
            'execution_errors': sum(c.get('result_category') not in QUALITY_CALL_RESULTS for c in calls)}


def report(rows, calls, *, expected_cases, failures=()):
    """Gates: false closures 0, host/admin closures 0, recognition >= 80%."""
    should_close = [r for r in rows if r['expected'] in CLOSED]
    categories = {}
    for name in sorted({r['category'] for r in rows}):
        members = [r for r in rows if r['category'] == name]
        categories[name] = {'cases': len(members), 'correct': sum(r['correct'] for r in members),
                            'false_closures': sum(r['false_close'] for r in members),
                            'actual': dict(Counter(r['actual'] for r in members))}
    counts = call_counts(calls)
    complete = len(rows) == expected_cases
    valid = (complete and not failures and counts['timeouts'] == 0 and counts['rate_limited'] == 0
             and counts['truncated'] == 0 and counts['execution_errors'] == 0)
    result = {'cases': len(rows), 'complete': complete, 'valid': valid, 'call_counts': counts,
              'review_failures': list(failures),
              'false_closures': {'count': sum(r['false_close'] for r in rows),
                                 'cases': [r['case_id'] for r in rows if r['false_close']]},
              'external_closed': {'count': sum(r['external_closed'] for r in rows),
                                  'cases': [r['case_id'] for r in rows if r['external_closed']]},
              'recognition': ratio(sum(r['correct'] for r in should_close), len(should_close)),
              'accuracy': ratio(sum(r['correct'] for r in rows), len(rows)),
              'categories': categories, 'results': rows}
    result['gates'] = {
        'false_closures_zero': result['false_closures']['count'] == 0,
        'external_closed_zero': result['external_closed']['count'] == 0,
        'recognition_at_least_80_percent': (result['recognition']['rate'] is not None
                                            and result['recognition']['rate'] >= .8)}
    return result
