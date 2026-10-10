"""Compare seven public corpora with shared scopes, identical provider samples.

Baseline uses live embeddings and judgments. Candidate replays each judgment
only if its entire message input and candidate IDs match byte for byte; unseen
inputs fail the comparison. This isolates code changes from model sampling.
"""
from __future__ import annotations
import argparse
import hashlib
import itertools
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def worker(args):
    sys.path.insert(0,str(args.source/'src'))
    from iris.models import Gateway, load_test_models
    from iris.recall_evaluation import run_recall_eval
    from iris import queue
    # Corpus message senders otherwise receive random UUIDs, which change the
    # product's ID-ordered alias/about arrays even between two baseline runs.
    # Fix identities at ingestion; do not normalize any model input or output.
    identities = itertools.count(1)
    queue.uuid = SimpleNamespace(uuid4=lambda: uuid.UUID(int=next(identities)))
    records_path=args.out.parent/'judgments.json'
    records=json.loads(records_path.read_text(encoding='utf-8')) if args.mode=='replay' else {}
    consumed=[]
    class ControlledGateway(Gateway):
        def recall_judge(self,messages,ids,**kwargs):
            raw=json.dumps({'messages':messages,'ids':ids},ensure_ascii=False,sort_keys=True)
            key=hashlib.sha256(raw.encode()).hexdigest()
            if args.mode=='replay':
                if key not in records or records[key]['input'] != raw:
                    raise ValueError('candidate judgment input differs from baseline')
                consumed.append(key)
                return records[key]['result']
            result=super().recall_judge(messages,ids,**kwargs)
            records[key]={'input':raw,'result':result}
            write(records_path,records)
            consumed.append(key)
            return result
    args.out.mkdir(parents=True,exist_ok=True)
    cache=args.out.parent/'shared-embeddings.db'
    (args.out/'recall-embeddings.db').symlink_to(cache)
    _,report=run_recall_eval(load_test_models(),args.corpus_root,out=args.out,gateway_factory=ControlledGateway,
                             judge_interval=1 if args.mode=='record' else 0)
    write(args.out/'report.json',report)
    write(args.out/'sample-counts.json',{'judgment_calls':len(consumed),'unique_inputs':len(set(consumed)),
                                       'input_order_sha256':hashlib.sha256(json.dumps(consumed).encode()).hexdigest()})


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--baseline',type=Path)
    p.add_argument('--candidate',type=Path)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--embedding-cache',type=Path,help='completed public embedding cache to reuse')
    p.add_argument('--worker',action='store_true')
    p.add_argument('--source',type=Path)
    p.add_argument('--corpus-root',type=Path)
    p.add_argument('--mode',choices=['record','replay'])
    args=p.parse_args()
    if args.worker:
        worker(args)
        return
    args.out=args.out.resolve()
    args.out.mkdir(parents=True,exist_ok=False)
    if args.embedding_cache:
        shutil.copyfile(args.embedding_cache,args.out/'shared-embeddings.db')
    reports=[]
    for label,source,mode in [('baseline',args.baseline,'record'),('candidate',args.candidate,'replay')]:
        source=source.resolve()
        subprocess.run([sys.executable,__file__,'--worker','--source',str(source),'--corpus-root',str(args.candidate.resolve()),
                        '--mode',mode,'--out',str(args.out/label)],check=True,env=os.environ.copy())
        reports.append(json.loads((args.out/label/'report.json').read_text(encoding='utf-8')))
    diffs=[]
    for variant,original in reports[0]['variants'].items():
        current=reports[1]['variants'][variant]
        left={(q['corpus'],q['id']):q for q in original['queries']}
        right={(q['corpus'],q['id']):q for q in current['queries']}
        for key in sorted(left.keys()|right.keys()):
            a,b=left.get(key),right.get(key)
            if a is None or b is None or any(a[k]!=b[k] for k in ('returned','reasons')):
                diffs.append({'variant':variant,'corpus':key[0],'query':key[1]})
    counts=[json.loads((args.out/label/'sample-counts.json').read_text()) for label in ('baseline','candidate')]
    degraded=[r['variants']['default_judged']['judgment']['degraded'] for r in reports]
    result={'queries':reports[0]['corpus']['queries'],'datasets':reports[0]['corpus']['datasets'],
            'variants':list(reports[0]['variants']),'different_returns':len(diffs),'differences':diffs,
            'judgment_degraded':degraded,'judgment_samples':counts,'identical_model_inputs':counts[0]==counts[1],
            'valid':not any(degraded) and counts[0]==counts[1],
            'method':'fixed ingestion subject UUIDs; fresh baseline model samples; exact-input replay in candidate; all memory keys, order and reasons compared'}
    write(args.out/'summary.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['valid'] and not diffs else 1)


if __name__=='__main__':
    main()
