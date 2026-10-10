"""R10 synthetic 5k/50k corpus with 100 entries, 50 entry_only.

Uses the existing benchmark's vector/prose generator and instant local judgment
transport. Every memory has a real source; each subject spans shared and private
entries. Includes persona, current state and goal partitions. No credentials.
"""
import argparse
import json
from pathlib import Path
import platform
import subprocess
import sys
import time

import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from benchmark_retrieval import build, Gateway, STAMP
from iris.db import Store
from iris.memory_ops import setup_role, set_entry_visibility
from iris.retrieval import Retrieval, DEFAULTS


def worker(out,size,repeats):
    path=out/f'perf-{size}.db'
    build(path,size,DEFAULTS['embedding_dimensions'])
    store=Store(path)
    try:
        setup_role(store,'Iris','我来自合成性能场景。')
        entries=['bench',*[f'perf-{i}' for i in range(1,100)]]
        with store.write() as conn:
            conn.executemany("INSERT INTO entries(id,name,platform,kind) VALUES(?,?,'perf','group')", [(e,e) for e in entries[1:]])
            sources=[]
            for e in entries:
                sources.append(conn.execute("""INSERT INTO messages(entry_id,kind,sender_subject_id,content,occurred_at,received_at,dedupe_key,learning_state)
                    VALUES(?,'self_output','self','合成性能依据',?,?,'source','learned')""", (e,STAMP,STAMP)).lastrowid)
            for i in range(size):
                bucket=(i//100)%100
                conn.execute("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,?)",(i+1,sources[bucket],STAMP))
                conn.execute('UPDATE memories SET entry_id=? WHERE id=?',(entries[bucket],i+1))
            for i in range(3):
                conn.execute("""INSERT INTO messages(entry_id,kind,sender_subject_id,content,occurred_at,received_at,dedupe_key,learning_state)
                    VALUES('bench','message',?,'今晚整理天文观測记录，继续聊聊观星。',?,?,?,'learned')""", (f'p{i}',STAMP,STAMP,f'recent-{i}'))
            for i in range(200):
                conn.execute("INSERT INTO goals(content,kind,origin,state,entry_id,created_at,updated_at) VALUES(?,'normal','host','open',?,?,?)",(f'合成性能目标{i}',entries[i%100],STAMP,STAMP))
            conn.execute("INSERT INTO current_state(id,activity,activity_updated_at,started_at,start_time_basis,updated_at) VALUES(1,'性能测量',?,?,'host',?)",(STAMP,STAMP,STAMP))
        for e in entries[1::2]:
            set_entry_visibility(store,e,'entry_only')
        store.set_setting('retrieval',{**DEFAULTS,'embedding_model':'perf-vector'})
        gateway=Gateway(DEFAULTS['embedding_dimensions'],store)
        try:
            r=Retrieval(store,gateway)
            results={}
            for name,query in [('unnamed',{}),('named',{'text':'参与者1的天文观測记录'})]:
                start=time.perf_counter();r.prepare('bench',**query);cold=(time.perf_counter()-start)*1000
                for _ in range(5):
                    r.prepare('bench',**query)
                samples=[]
                for _ in range(repeats):
                    start=time.perf_counter();response=r.prepare('bench',**query);samples.append((time.perf_counter()-start)*1000)
                    assert response['judgment']['status']!='degraded'
                results[name]={'prepare_p50_ms':float(np.percentile(samples,50)), 'prepare_p95_ms':float(np.percentile(samples,95)),
                    'cold_ms':cold,'samples_ms':samples,'returned':[m['id'] for m in response['memories']]}
            return {'memories':size,'entries':100,'entry_only':50,'source_backed_memories':size,'goals':200,
                    'queries':results,'python':platform.python_version(),'platform':platform.platform()}
        finally:
            gateway.close()
    finally:
        store.close()


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',required=True,type=Path);p.add_argument('--repeats',type=int,default=60)
    p.add_argument('--worker',type=int);args=p.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    if args.worker:
        result=worker(args.out,args.worker,args.repeats)
        (args.out/f'result-{args.worker}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        return
    results=[]
    for size in (5000,50000):
        subprocess.run([sys.executable,__file__,'--out',str(args.out),'--worker',str(size),'--repeats',str(args.repeats)],check=True)
        results.append(json.loads((args.out/f'result-{size}.json').read_text()))
    summary={'note':'Synthetic corpus; default retrieval settings; instant local judgment; model network excluded, local judgment overhead included; fresh worker per size; five warmups; 60 samples per group.',
             'results':results,'passed':all(q['prepare_p95_ms']<=500 for r in results for q in r['queries'].values())}
    (args.out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'passed':summary['passed'],'p95_ms':[q['prepare_p95_ms'] for r in results for q in r['queries'].values()]}))
    raise SystemExit(0 if summary['passed'] else 1)


if __name__=='__main__':
    main()
