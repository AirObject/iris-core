"""Select a frozen method from at least two complete, valid, externally scored runs."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from run import METHODS
from iris.evaluation import _read_json, _write_json


def choose(reports):
    if any(r.get('judge_rounds')!=2 for r in reports):
        raise ValueError('final selection requires independent double judging')
    values=[]
    for method in METHODS:
        runs=[r for r in reports if r['method']==method]
        if len(runs)<2 or any(not r['valid'] for r in runs):
            raise ValueError('each frozen candidate needs two valid complete runs')
        if len({r['cases'][i]['case_id'] for r in runs for i in range(len(r['cases']))})!=len(runs[0]['cases']) or any(len(r['cases'])!=40 for r in runs):
            raise ValueError('selection requires all 40 consolidation_v1 cases')
        rates=[r['mismerge']['rate'] for r in runs]
        error=max(rates) if all(rate is not None for rate in rates) else None
        recognition=min(r['recognition']['rate'] for r in runs)
        values.append({'method':method,'feasible':all(r['feasible'] for r in runs),'worst_mismerge':error,
            'worst_recognition':recognition,'calls':max(r['calls']['count'] for r in runs),
            'p95_seconds':max(r['case_p95_seconds'] for r in runs)})
    feasible=[v for v in values if v['feasible']]
    if feasible:
        best=max(v['worst_recognition'] for v in feasible)
        tied=[v for v in feasible if best-v['worst_recognition']<.03]
        selected=min(tied,key=lambda v:(v['calls'],v['p95_seconds'],list(METHODS).index(v['method'])))
        reason='feasible; recognition within <0.03; calls, case P95, frozen order'
    else:
        selected=min(values,key=lambda v:(v['worst_mismerge'] if v['worst_mismerge'] is not None else float('inf'),list(METHODS).index(v['method'])))
        reason='no feasible candidate; lowest worst mismerge; requires planner review'
    return {'selected':selected['method'],'reason':reason,'candidates':values}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,action='append',required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    result=choose([_read_json(p) for p in args.report])
    _write_json(args.out,result)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
