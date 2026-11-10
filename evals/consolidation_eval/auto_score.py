"""Deterministic scoring of resolution=auto_v1 runs (spec sections 2, 4, 5 and 10).

Handling, protection and write damage are computed from the run; only the semantic
must_keep / must_not_add check comes from external judgment rounds.

uv run python evals/consolidation_eval/auto_score.py --materials <run>/materials --out <dir>
uv run python evals/consolidation_eval/auto_score.py --materials <run>/materials \\
    --detail-judgments <round1> --detail-judgments <round2> --judge-model <name> --out <dir>
"""
from __future__ import annotations

import argparse
from collections import Counter
import itertools
import json
from pathlib import Path

from run import AUTO_DETAIL_SCORING, AUTO_DETAIL_VERSION, ROOT, auto_detail_items, run_validity
from iris.consolidation import (INFERENCE_CEILING, claim_numbers, lower_tier, negation_scopes, qualifier_details,
                                time_expressions)
from iris.evaluation import _json_sha256, _read_json, _verified_material_file, _write_json
from score import combine, exact, ratio, require, validate_payload

CORRECT, MISSED, WRONG, UNPROCESSED = 'correct', 'missed', 'wrong', 'unprocessed'
# Work that reached a valid judgment (or protection). Anything else is reported as unprocessed.
PROCESSED_OUTCOMES = {'auto_conflict', 'auto_dependency', 'merged', 'mutual_merge_rejected', 'protected', 'checked'}
BUDGET_REASONS = {'call_budget', 'usage_limit', 'paused', 'temporary_unavailable', 'interrupted'}
CLAIM_CHECKS = (('numbers', claim_numbers), ('negation', negation_scopes),
                ('qualifiers', lambda text: set(qualifier_details(text))), ('time', time_expressions))
JUDGMENT_FIELDS = ('format_version', 'case_id', 'merge_detail_results')


def _new_annotations(before, after):
    known = {a['id'] for a in before['annotations']}
    return [a for a in after['annotations'] if a['id'] not in known]


def _kinds(before, after):
    return [a['kind'] for a in _new_annotations(before, after)]


def _unchanged(before, after):
    return (after['merged_into'] is None and
            all(after[key] == before[key] for key in ('content', 'belief', 'retention', 'lifecycle')))


def _moved(before, after):
    """Body, score or lifecycle changed; annotations alone are not a write to the fact."""
    return after['merged_into'] is not None or any(after[key] != before[key] for key in ('content', 'belief', 'lifecycle'))


def _forgotten_by_rule(before, after, threshold):
    return (after['content'] == before['content'] and after['belief'] == min(before['belief'], INFERENCE_CEILING)
            and after['lifecycle'] == 'forgotten' and after['retention'] == min(before['retention'], threshold-1))


def _lowered_by_rule(before, after):
    return (after['content'] == before['content'] and after['belief'] == lower_tier(before['belief'])
            and after['lifecycle'] == before['lifecycle'] and after['retention'] == before['retention'])


def judge_contradiction(label, memory, threshold):
    (old_before, old_after), (new_before, new_after) = memory(label['old']), memory(label['new'])
    relation = label['relation']
    if relation == 'supersedes':
        pointed = any(a['kind'] == 'superseded' and a['superseded_by'] == new_after['id']
                      for a in _new_annotations(old_before, old_after))
        if _forgotten_by_rule(old_before, old_after, threshold) and pointed and _unchanged(new_before, new_after) and not _kinds(new_before, new_after):
            return CORRECT
        # The newer statement must stay; touching it, or the older body, is backwards.
        wrong = _moved(new_before, new_after) or old_after['content'] != old_before['content'] or old_after['merged_into'] is not None
        return WRONG if wrong else MISSED
    if relation == 'reported_vs_self':
        if _lowered_by_rule(old_before, old_after) and 'disputed' in _kinds(old_before, old_after) and _unchanged(new_before, new_after) and not _kinds(new_before, new_after):
            return CORRECT
        wrong = (_moved(new_before, new_after) or old_after['lifecycle'] != old_before['lifecycle'] or old_after['content'] != old_before['content']
                 or old_after['merged_into'] is not None or (old_after['belief'] != old_before['belief'] and old_after['belief'] != lower_tier(old_before['belief'])))
        return WRONG if wrong else MISSED
    pair = [memory(key) for key in label['keys']]
    if any(_moved(before, after) for before, after in pair):
        return WRONG
    if relation == 'coexist':
        return CORRECT if all(_unchanged(b, a) and not _kinds(b, a) for b, a in pair) else MISSED
    return CORRECT if all(_unchanged(b, a) and 'disputed' in _kinds(b, a) for b, a in pair) else MISSED


def judge_dependency(label, memory, threshold):
    before, after = memory(label['derived'])
    annotated = 'insufficient_support' in _kinds(before, after)
    if label['expected'] == 'forget':
        if _forgotten_by_rule(before, after, threshold) and annotated:
            return CORRECT
        wrong = after['content'] != before['content'] or after['merged_into'] is not None or after['lifecycle'] == 'deleted' or after['belief'] > before['belief']
        return WRONG if wrong else MISSED
    if _unchanged(before, after) and not annotated:
        return CORRECT
    return WRONG if _moved(before, after) or annotated else MISSED


def work_status(work):
    if work['outcome'] in PROCESSED_OUTCOMES:
        return 'processed'
    if work['outcome'] == 'failed':
        return {'invalid_output': 'invalid_output', 'unsafe_write': 'invalid_output', 'model_error': 'call_failed'}.get(work['reason'], 'execution_failed')
    if work['outcome'] is None or work['reason'] in BUDGET_REASONS:
        return 'budget'
    return work['reason'] or 'skipped'


def label_status(row, kind, ids):
    """(status, work) for the work that judged these memories; not_planned when none exists."""
    works = [w for w in row['work_results'] if w['kind'] == kind and set(w['memory_ids']) == set(ids)]
    if not works:
        return 'not_planned', None
    ranked = sorted(works, key=lambda w: work_status(w) != 'processed')
    return work_status(ranked[0]), ranked[0]


def rewrite_check(merge):
    """Deterministic claims of a rewritten body against the union of its inputs."""
    inputs = [m['content'] for m in merge['before_memories']]
    body = merge['result']['content']
    return [name for name, extract in CLAIM_CHECKS if extract(body) != set().union(*(extract(t) for t in inputs))]


def _destination(payload, key):
    after = {m['id']: m for m in payload['after']['memories']}
    current, seen = payload['before']['key_map'][key], set()
    while after[current]['merged_into'] is not None:
        if current in seen:
            return None
        seen.add(current)
        current = after[current]['merged_into']
    return current


def judge_merge_label(pair, expected, memory, payload):
    joined = _destination(payload, pair[0]) == _destination(payload, pair[1]) is not None
    if expected == 'keep_both':
        if joined:
            return WRONG
        bodies = [memory(key) for key in pair]
        return CORRECT if all(after['content'] == before['content'] and after['merged_into'] is None for before, after in bodies) else WRONG
    return CORRECT if joined else MISSED


def _merge_integrity(merge):
    """Mechanics the merge write must satisfy, independent of labels."""
    problems = []
    result = merge['result']
    if result['lifecycle'] != 'active' or result['is_placeholder']:
        problems.append('result_not_active')
    absorbed = merge['absorbed_memories']
    if {x['id'] for x in absorbed} != set(merge['input_ids'])-{result['id']} or any(
            x['lifecycle'] != 'deleted' or x['merged_into'] != result['id'] for x in absorbed):
        problems.append('invalid_placeholder')
    if result['belief'] > max(x['belief'] for x in merge['before_memories']):
        problems.append('belief_raised')
    source_keys = {s['key'] for x in merge['before_memories'] for s in x['sources']}
    if not source_keys <= {s['key'] for s in result['sources']}:
        problems.append('sources_not_inherited')
    return problems


def score_case(row):
    payload = row['input']
    case, expected = payload['case'], payload['case']['expected']
    threshold = payload['forget_threshold']
    keymap = payload['before']['key_map']
    before = {m['id']: m for m in payload['before']['memories']}
    after = {m['id']: m for m in payload['after']['memories']}

    def memory(key):
        return before[keymap[key]], after[keymap[key]]

    reports = {i['item_key']: i for i in row['report']['items'] if i['phase'] == 'consolidation'}

    def settle(item, result, kind, keys):
        # Only a valid judgment (or protection) can be correct; a wrong state is wrong regardless.
        status, work = label_status(row, kind, [keymap[k] for k in keys])
        if result != WRONG and status != 'processed':
            result = UNPROCESSED
            item['unprocessed'] = status
        elif result == CORRECT and kind == 'dependency' and work['outcome'] == 'auto_dependency':
            if reports.get(work['check_id'][3:], {}).get('details', {}).get('verdict') == 'undecided':
                result = MISSED
        item['result'] = result
        return item

    items, covered = [], set()
    for index, label in enumerate(expected.get('auto_contradictions', [])):
        items.append(settle({'kind': 'contradiction', 'index': index, 'keys': label['keys'], 'relation': label['relation']},
                            judge_contradiction(label, memory, threshold), 'pair', label['keys']))
        covered.add(label['old'])
    for index, label in enumerate(expected.get('auto_dependencies', [])):
        items.append(settle({'kind': 'dependency', 'index': index, 'derived': label['derived'], 'expected': label['expected']},
                            judge_dependency(label, memory, threshold), 'dependency', [label['derived']]))
        covered.add(label['derived'])
    merge_labels = [(tuple(label['pair']), label['expected']) for label in expected.get('mutual_merges', [])]
    labelled = {frozenset(pair) for pair, _ in merge_labels}
    merge_labels += [(tuple(pair), 'merge') for pair in expected.get('merges', []) if frozenset(pair) not in labelled]
    for pair, wanted in merge_labels:
        items.append(settle({'kind': 'merge', 'pair': list(pair), 'expected': wanted},
                            judge_merge_label(pair, wanted, memory, payload), 'pair', pair))
    # Union-find over expected merges: a chain may legitimately pass through several keys.
    component = {key: key for key in keymap}
    def root(key):
        while component[key] != key:
            key = component[key]
        return key
    for pair, wanted in merge_labels:
        if wanted == 'merge':
            component[root(pair[1])] = root(pair[0])
    unexpected = []
    for merge in payload['actual_merges']:
        if any(root(a) != root(b) for a, b in itertools.combinations(merge['input_keys'], 2)):
            unexpected.append({'kind': 'unexpected_merge', 'action_id': merge['action_id'], 'keys': merge['input_keys']})
    merged_keys = {key for merge in payload['actual_merges'] for key in merge['input_keys']}
    for key, mid in keymap.items():
        if key not in covered and key not in merged_keys and _moved(before[mid], after[mid]):
            unexpected.append({'kind': 'unexpected_change', 'key': key})
    protection = [{'key': key, 'passed': _unchanged(*memory(key)) and not _kinds(*memory(key)) and
                   after[keymap[key]]['pinned'] == before[keymap[key]]['pinned'] and
                   after[keymap[key]]['last_edit'] == before[keymap[key]]['last_edit']}
                  for key in expected.get('protected', [])]
    damage = []
    results = {memory_id for merge in payload['actual_merges'] for memory_id in merge['input_ids']}
    for mid, old in before.items():
        new = after[mid]
        if mid in results:
            continue
        if new['content'] != old['content']:
            damage.append({'reason': 'content_rewritten', 'memory_id': mid})
        if new['lifecycle'] == 'deleted' and old['lifecycle'] != 'deleted':
            damage.append({'reason': 'unexpected_deletion', 'memory_id': mid})
        if new['belief'] > old['belief']:
            damage.append({'reason': 'belief_raised', 'memory_id': mid})
        if new['lifecycle'] == 'forgotten' and old['lifecycle'] != 'forgotten' and new['retention'] >= threshold:
            damage.append({'reason': 'forgotten_above_threshold', 'memory_id': mid})
    rewrites = []
    labelled = [frozenset(label['pair']) for label in expected.get('mutual_merges', []) if label['expected'] == 'merge']
    for merge in payload['actual_merges']:
        damage += [{'reason': reason, 'action_id': merge['action_id']} for reason in _merge_integrity(merge)]
        if merge['result']['content'] in [m['content'] for m in merge['before_memories']]:
            continue
        failed = rewrite_check(merge)
        rewrites.append({'action_id': merge['action_id'], 'keys': merge['input_keys'], 'failed_checks': failed,
                         'labelled': any(pair <= set(merge['input_keys']) for pair in labelled)})
        if failed:
            damage.append({'reason': 'rewrite_claims_changed', 'action_id': merge['action_id'], 'checks': failed})
    for mid, new in after.items():
        for annotation in _new_annotations(before[mid], new):
            target = annotation['superseded_by']
            if annotation['kind'] == 'superseded' and (target == mid or target not in after or after[target]['lifecycle'] == 'deleted'):
                damage.append({'reason': 'invalid_superseded_target', 'memory_id': mid})
    return {'case_id': case['id'], 'items': items, 'unexpected_actions': unexpected, 'protection': protection,
            'write_damage': damage, 'rewritten_merges': rewrites}


def validate_detail_judgment(judgment, items, case_id):
    exact(judgment, JUDGMENT_FIELDS)
    require(type(judgment['format_version']) is int and judgment['format_version'] == 1 and judgment['case_id'] == case_id, 'detail judgment case mismatch')
    results = judgment['merge_detail_results']
    require(isinstance(results, list) and len(results) == len(items), 'detail result length mismatch')
    for result, item in zip(results, items, strict=True):
        exact(result, ('action_id', 'must_keep_present', 'must_not_add_absent', 'reason'))
        require(result['action_id'] == item['action_id'], 'detail action mismatch')
        for field, labels in (('must_keep_present', item['must_keep']), ('must_not_add_absent', item['must_not_add'])):
            require(isinstance(result[field], list) and len(result[field]) == len(labels), 'detail label length mismatch')
            require(all(type(value) is bool for value in result[field]), 'expected a JSON boolean')
        require(isinstance(result['reason'], str) and bool(result['reason'].strip()), 'detail reason required')
    return judgment


def load_materials(directory):
    manifest = _read_json(directory/'manifest.json')
    require(manifest.get('format_version') == 1 and manifest.get('evaluation') == 'consolidation_auto', 'invalid auto materials')
    require(manifest['materials_sha256'] == _json_sha256({k: v for k, v in manifest.items() if k != 'materials_sha256'}), 'manifest digest mismatch')
    rows = _read_json(_verified_material_file(directory, 'run.json', manifest['run_sha256']))['rows']
    instructions = _verified_material_file(directory, 'merge-details-scoring.md', manifest['merge_details_scoring_sha256'])
    require(instructions.read_text(encoding='utf-8') == AUTO_DETAIL_SCORING, 'merge detail scoring differs')
    require(len(rows) == len(manifest['cases']), 'case count mismatch')
    for index, (row, entry) in enumerate(zip(rows, manifest['cases'], strict=True)):
        exact(entry, ('case_id', 'file', 'sha256', 'judgment_file'))
        require(entry['file'] == f'cases/{index:04}.json' and entry['judgment_file'] == f'{index:04}.json', 'invalid case path/order')
        document = _read_json(_verified_material_file(directory, entry['file'], entry['sha256']))
        require(document['case_id'] == entry['case_id'] == row['input']['case']['id'], 'case mismatch')
        require(document['merges'] == auto_detail_items(row['input']), 'case merge items mismatch')
        require(document['source_sha256'] == manifest['run']['source_sha256'] and document['scoring_version'] == AUTO_DETAIL_VERSION, 'run binding mismatch')
        validate_payload(row['input'])
    return manifest, rows


def load_detail_rounds(manifest, rows, directories):
    rounds = []
    for directory in directories:
        binding = _read_json(directory/'manifest.json')
        exact(binding, ('materials_sha256',))
        require(binding['materials_sha256'] == manifest['materials_sha256'], 'detail material mismatch')
        expected = {'manifest.json', *(entry['judgment_file'] for entry in manifest['cases'])}
        require({p.name for p in directory.iterdir() if p.is_file()} == expected, 'detail files missing or unexpected')
        rounds.append([validate_detail_judgment(_read_json(directory/entry['judgment_file']), auto_detail_items(row['input']), entry['case_id'])
                       for entry, row in zip(manifest['cases'], rows, strict=True)])
    return rounds


def call_counts(rows):
    """Same validity vocabulary as the other evaluations: timeouts, 429, truncation, invalid calls."""
    calls = [c for row in rows for c in row['calls']]
    error_text = lambda call: (call.get('error') or '').lower()
    return {'calls': len(calls),
            'timeouts': sum('timeout' in error_text(c) for c in calls),
            'rate_limited': sum('429' in error_text(c) or 'rate' in error_text(c) for c in calls),
            'truncated': sum(c['finish_reason'] not in (None, 'stop') for c in calls),
            'invalid_calls': sum(c['result'] in ('invalid_output', 'item_error') for c in calls)}


def score(materials, out, *, detail_judgments=(), judge_model=None):
    require(len(detail_judgments) <= 2, 'at most two independent detail rounds')
    require(not detail_judgments or (judge_model and judge_model.strip()), 'judge model required with detail judgments')
    manifest, rows = load_materials(materials)
    rounds = load_detail_rounds(manifest, rows, detail_judgments)
    cases = [score_case(row) for row in rows]
    disagreements, details, detail_cases = [], [], []
    if rounds:
        for index, row in enumerate(rows):
            judgment = rounds[0][index]
            if len(rounds) == 2:
                judgment = combine(judgment, rounds[1][index], row['input']['case']['id'], disagreements)
            results = judgment['merge_detail_results']
            lost = [r['action_id'] for r in results if not all(r['must_keep_present']) or not all(r['must_not_add_absent'])]
            details.append(ratio(len(lost), len(results)))
            detail_cases.append({'case_id': row['input']['case']['id'], 'lost_action_ids': lost, 'judgment': judgment})
    items = [item for case in cases for item in case['items']]
    unexpected = sum(len(case['unexpected_actions']) for case in cases)
    wrong = sum(item['result'] == WRONG for item in items)+unexpected
    protection = [check for case in cases for check in case['protection']]
    validity = [run_validity(row) for row in rows]
    report = {'format_version': 1, 'evaluation': 'consolidation_auto', 'resolution': manifest['run']['resolution'],
              'corpus_sha256': manifest['run']['corpus']['sha256'], 'source_sha256': manifest['run']['source_sha256'],
              'materials_sha256': manifest['materials_sha256'], 'valid': all(v['valid'] for v in validity), 'run_validity': validity,
              'call_counts': call_counts(rows),
              'mishandling': ratio(wrong, len(items)+unexpected), 'correct': ratio(sum(item['result'] == CORRECT for item in items), len(items)),
              'unprocessed': {'count': sum(item['result'] == UNPROCESSED for item in items),
                              'by_category': dict(sorted(Counter(item['unprocessed'] for item in items if item['result'] == UNPROCESSED).items()))},
              'rewritten_merges': {'count': sum(len(case['rewritten_merges']) for case in cases),
                                   'unlabelled': sum(not r['labelled'] for case in cases for r in case['rewritten_merges']),
                                   'items': [{'case_id': case['case_id'], **r} for case in cases for r in case['rewritten_merges']]},
              'protection': ratio(sum(check['passed'] for check in protection), len(protection)),
              'write_damage': ratio(sum(bool(case['write_damage']) for case in cases), len(cases)),
              'detail_loss': ratio(sum(d['numerator'] for d in details), sum(d['denominator'] for d in details)) if rounds else None,
              'detail_judge_rounds': len(rounds), 'judge_model': judge_model, 'detail_disagreements': disagreements,
              'detail_cases': detail_cases, 'cases': cases}
    report['gates'] = {
        'mishandling_at_most_5_percent': report['mishandling']['rate'] is not None and report['mishandling']['rate'] <= .05,
        'write_damage_zero': report['write_damage']['numerator'] == 0,
        'protection_full': report['protection']['rate'] in (None, 1),
        'detail_loss_zero': None if report['detail_loss'] is None or report['detail_loss']['rate'] is None else report['detail_loss']['numerator'] == 0,
        'correct_at_least_80_percent': report['correct']['rate'] is not None and report['correct']['rate'] >= .8}
    out.mkdir(parents=True, exist_ok=False)
    _write_json(out/'report.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--materials', type=Path, required=True)
    parser.add_argument('--detail-judgments', type=Path, action='append', default=[])
    parser.add_argument('--judge-model')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.resolve().is_relative_to(ROOT):
        parser.error('full artifacts must be outside repository')
    report = score(args.materials, args.out, detail_judgments=args.detail_judgments, judge_model=args.judge_model)
    print(json.dumps({k: v for k, v in report.items() if k not in ('cases', 'detail_cases', 'run_validity')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
