"""Select conflict/dependency resolution with the already selected broad merger."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from iris.evaluation import _read_json, _write_json

ORDER = ('rewrite_v2', 'conservative_v2')
CORPUS_SHA256 = '6544ae854836a18305fb62c7d5b8965047aba7ea81e062ec2dd63a02f3f9db23'


def choose(reports):
    candidates = []
    fingerprints = set()
    for candidate in ORDER:
        runs = [r for r in reports if r.get('resolution') == candidate]
        if len(runs) != 2:
            raise ValueError('each frozen resolution requires two complete runs')
        for r in runs:
            if r['method'] != 'broad' or r.get('corpus_sha256') != CORPUS_SHA256:
                raise ValueError('comparison is restricted to broad on consolidation_v1')
            if not r['valid'] or r.get('judge_rounds') != 2 or r.get('write_audit_rounds') != 2:
                raise ValueError('valid runs with independent double scoring and write audit required')
            if [c['case_id'] for c in r['cases']] != [f'CS{i:03}' for i in range(1, 41)]:
                raise ValueError('all 40 cases in corpus order required')
            if r['materials_sha256'] in fingerprints:
                raise ValueError('cannot reuse the same run as two repetitions')
            fingerprints.add(r['materials_sha256'])
        candidates.append({'resolution': candidate,
            'worst_consistency': min(r['conflict_dependency_consistency']['rate'] for r in runs),
            'merge_and_protection_pass': all(r['mismerge']['rate'] == 0 and r['protection']['rate'] == 1 for r in runs),
            'write_damage': max(r['write_damage']['numerator'] for r in runs)})
    passing = [c for c in candidates if c['merge_and_protection_pass']]
    if not passing:
        selected, reason = None, 'no candidate preserves zero mismerges and all protections'
    elif max(c['worst_consistency'] for c in candidates) < .80:
        safe = next(c for c in candidates if c['resolution'] == 'conservative_v2')
        selected = safe['resolution'] if safe['merge_and_protection_pass'] and safe['write_damage'] == 0 else None
        reason = 'all below 0.80; conservative fallback only with no write damage and merge/protection gates'
    else:
        best = max(passing, key=lambda c: (c['worst_consistency'], -ORDER.index(c['resolution'])))
        selected = best['resolution'] if best['write_damage'] == 0 else None
        reason = 'highest worst combined consistency; exact ties use frozen order; damage requires planner review'
    return {'selected': selected, 'reason': reason, 'candidates': candidates}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, action='append', required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    result = choose([_read_json(p) for p in args.report])
    _write_json(args.out, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
