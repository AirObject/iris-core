"""Deterministic shared-scope comparison of frozen consolidation/persona selection.

This checks product materials/eligibility, not semantic quality or stage gates.
"""
import argparse
import importlib.util
import itertools
import json
from pathlib import Path
import subprocess
import sys
import uuid


def worker(source,out,corpora):
    sys.path.insert(0,str(source/'src'))
    from iris.db import Store
    from iris.memory_ops import setup_role
    from iris.consolidation import Consolidation, DEFAULTS, snapshot, merge_exclusion, dependency_material, material
    from iris.persona import select_evidence
    from iris import persona_evaluation as pe
    spec=importlib.util.spec_from_file_location('consolidation_runner',source/'evals/consolidation_eval/run.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    out.mkdir(parents=True,exist_ok=False)
    rows=[]
    for case in module.load_corpus(corpora/'evals/consolidation_v1.json'):
        store=Store(out/(case['id']+'.db'))
        try:
            keys,_=module.seed(store,case)
            with store.read() as conn:
                memories={mid:snapshot(conn,mid) for mid in keys.values()}
                get=memories.get
                pairs=[{'ids':[a,b], 'exclusion':merge_exclusion(get(a),get(b)),
                        'plan':Consolidation(store)._plan_pair(get,get(a),get(b),DEFAULTS)} for a,b in itertools.combinations(memories,2)]
                deps=[dependency_material(conn,m) for m in memories.values()]
            rows.append({'corpus':'consolidation_v1','case':case['id'],'memories':[material(m) for m in memories.values()], 'pairs':pairs,'dependencies':deps})
        finally:
            store.close()
    for name in ('persona_v1','persona_v2'):
        for case in pe.load_corpus(corpora/'evals'/(name+'.json')):
            store=Store(out/(name+'-'+case['id']+'.db'))
            clock=pe.TimelineClock(case['start_at'])
            counter=itertools.count(1)
            pe.uuid4=lambda:uuid.UUID(int=next(counter))
            try:
                role=case['role'];setup_role(store,role['name'],role['background'],role['timezone'],current=clock())
                with store.write() as conn:
                    for entry in case.get('entries',[]):
                        conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES(?,?,'eval',?)",(entry['id'],entry['name'],entry['kind']))
                    for s in case.get('subjects',[]):
                        sid=pe._subject(conn,s['id'],clock().isoformat())
                        conn.execute('UPDATE subjects SET name=? WHERE id=?',(s['name'],sid))
                        conn.executemany('INSERT OR IGNORE INTO subject_aliases(subject_id,alias) VALUES(?,?)',[(sid,a) for a in s.get('aliases',[])])
                    keys={f'initial:{i+1}':r[0] for i,r in enumerate(conn.execute('SELECT id FROM memories ORDER BY id'))}
                evidence=[]
                for event in case['events']:
                    clock.value=pe._instant(event['at'])
                    if event['op'] in ('add','edit','delete','forget'):
                        pe._mutate(store,event,keys,clock)
                    if event['op']=='checkpoint':
                        evidence.append({'checkpoint':event['id'],'evidence':select_evidence(store)})
                rows.append({'corpus':name,'case':case['id'],'checkpoints':evidence})
            finally:
                store.close()
    (out/'materials.json').write_text(json.dumps(rows,ensure_ascii=False,sort_keys=True,indent=2)+'\n',encoding='utf-8')


def main():
    p=argparse.ArgumentParser();p.add_argument('--baseline',type=Path);p.add_argument('--candidate',type=Path)
    p.add_argument('--out',required=True,type=Path);p.add_argument('--worker',type=Path);p.add_argument('--corpus-root',type=Path)
    args=p.parse_args()
    if args.worker:
        worker(args.worker,args.out,args.corpus_root);return
    args.out.mkdir(parents=True,exist_ok=False)
    reports=[]
    for label,path in [('baseline',args.baseline),('candidate',args.candidate)]:
        subprocess.run([sys.executable,__file__,'--worker',str(path.resolve()),'--out',str(args.out/label),
                        '--corpus-root',str(args.candidate.resolve())],check=True)
        reports.append(json.loads((args.out/label/'materials.json').read_text()))
    diffs=[{'corpus':a['corpus'],'case':a['case']} for a,b in zip(*reports,strict=True) if a!=b]
    result={'cases':len(reports[0]),'consolidation_cases':sum(r['corpus']=='consolidation_v1' for r in reports[0]),
            'persona_checkpoints':sum(len(r.get('checkpoints',[])) for r in reports[0]), 'differences':diffs}
    (args.out/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False));raise SystemExit(bool(diffs))


if __name__=='__main__':
    main()
