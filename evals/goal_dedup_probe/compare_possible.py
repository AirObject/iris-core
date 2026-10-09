"""Compare frozen C2 to #47 C reports without rerunning the baseline model."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from scoring import ratio


def compare(baseline,candidate):
    runs=[*baseline,*candidate]
    if len(baseline)!=2 or len(candidate)!=2:
        raise ValueError('exactly two valid runs are required for each method')
    for label,group in (('C',baseline),('C2',candidate)):
        for run in group:
            if (run.get('simulation') or not run.get('valid') or not run.get('complete')
                    or run.get('method')!=label or run.get('degraded_cases')!=0
                    or run.get('pending_cases')!=0 or run['cases']!=run['expected_cases']):
                raise ValueError('only complete, nondegraded real runs of C and C2 may select a method')
    for key in ('corpus_sha256','prompt_sha256','model','reasoning_effort','budget_seconds','expected_cases'):
        if not runs[0].get(key) or any(run.get(key)!=runs[0][key] for run in runs[1:]):
            raise ValueError('inconsistent comparison inputs: '+key)
    summaries={}
    for label,group in (('C',baseline),('C2',candidate)):
        wrong=[run['wrong_merge_rate']['rate'] for run in group]
        summaries[label]={
            'feasible':all(value is not None and value<=.05 for value in wrong),
            'worst_wrong_merge_rate':max(wrong) if all(value is not None for value in wrong) else None,
            'worst_merge_recognition':min(run['merge_recognition']['rate'] or 0 for run in group),
            'runs':[{key:run[key] for key in ('wrong_merge_rate','merge_recognition','separate_possible_rate',
                'uncertain_possible_rate','uncertain_candidate_hit_rate','uncertain_candidate_outside_cases',
                'model_calls','latency_ms','host_latency_ms','prompt_tokens','completion_tokens','reasoning_tokens')}
                | {'nonmerge_possible_rate':ratio(
                    run['separate_possible_rate']['numerator']+run['uncertain_possible_rate']['numerator'],
                    run['separate_possible_rate']['denominator']+run['uncertain_possible_rate']['denominator'])}
                for run in group],
        }
    a,b=summaries['C'],summaries['C2']
    adopt=(a['feasible'] and b['feasible'] and b['worst_wrong_merge_rate']<=a['worst_wrong_merge_rate']
           and b['worst_merge_recognition']>a['worst_merge_recognition'])
    return {'selected':'C2' if adopt else 'C','summaries':summaries,
            'rule':'C2 requires strictly higher worst-run recognition with no higher wrong-merge rate; '
                   '0/0 is infeasible. Report nonmerge possible rate without introducing a threshold.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path,action='append',required=True,help='repeat for the two existing #47 C reports')
    parser.add_argument('--candidate',type=Path,action='append',required=True,help='repeat for two new valid C2 reports')
    parser.add_argument('--out',type=Path,required=True,help='new JSON file outside the repository')
    args=parser.parse_args()
    if args.out.resolve().is_relative_to(Path(__file__).resolve().parents[2]):
        parser.error('--out must be outside the repository')
    groups=[[json.loads(path.read_text(encoding='utf-8')) for path in paths]
            for paths in (args.baseline,args.candidate)]
    result=compare(*groups)
    with args.out.open('x',encoding='utf-8') as output:
        output.write(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'selected':result['selected'],'report':str(args.out)},ensure_ascii=False))


if __name__=='__main__':
    main()
