"""M2 public-dev answerability experiment. Never imported by product code."""
from __future__ import annotations
import argparse
import asyncio
from collections import Counter
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import subprocess
import time

import httpx
import numpy as np
from iris.db import Store
from iris.models import load_test_models, parse_json_object
from iris.query_analysis import analyze
from iris.recall_evaluation import load_corpus, seed_corpus, cache_embeddings, _run_queries, recall_metrics
from iris.retrieval import DEFAULTS, Retrieval

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CORPORA = ('recall_v1','recall_v2','recall_conversation_v1','recall_short_terms_v1',
           'recall_conversation_v2','recall_conversation_v3','recall_no_answer_v1')
GRID_COS = [0,.35,.4,.45,.5,.55,.6,.65,.7,.75,.8,.85,.9]
GRID_COVERAGE = [0,.1,.25,.35,.5,.65,.75,.9,1]
JUDGE_VARIANTS = [['low',4],['low',8],['high',8]]
SUPPORT = [1,25,50,75,90,100]
TIMEOUTS = [2,5,10,20,60]


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp = path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    temp.replace(path)


def source_fingerprint():
    paths = sorted(p for p in (ROOT/'src/iris').rglob('*') if p.suffix in ('.py','.json','.md','.sql'))
    return digest({str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})


def safe_configs(configs):
    return {p:{'base_url':c.base_url,'model':c.model,'dimensions':c.dimensions,'reasoning_effort':c.reasoning_effort}
            for p,c in configs.items()}


def freeze_protocol(out):
    protocol = {'version':1,'corpora':list(CORPORA),'all_public_splits_used_as_dev':True,
        'corpus_sha256':{name:hashlib.sha256((ROOT/'evals'/f'{name}.json').read_bytes()).hexdigest() for name in CORPORA},
        'source_sha256':source_fingerprint(), 'capture_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'probe_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'prompt_sha256':hashlib.sha256((HERE/'judgment_prompt.txt').read_bytes()).hexdigest(),
        'deterministic_candidates':deterministic_grid(),'judge_variants':JUDGE_VARIANTS,
        'support_thresholds':SUPPORT,'timeout_seconds':TIMEOUTS,'actual_judge_deadline_seconds':60,
        'max_output_tokens':4096,'temperature':0,'concurrency':2,
        'combination_gates':[[c,l] for c in (.45,.55,.65) for l in (.5,.75)],
        'candidate_order':'baseline; deterministic grid in listed order; low4,low8,high8 each support ascending and timeout ascending; gates listed then low8/high8 support ascending timeout ascending',
        'failure_policy':'return entire original result on HTTP/transport/timeout/parse/schema/refusal/length failure; no retry or repair',
        'selection':'R>=.85,N>=.75,F<=.10; global Q band strictly <.01 then precision then -F then frozen order. If none: R>=.85,N>=.75 then minimum F, frozen order for ties.',
        'post_selection_scope':'filter only existing reason=relevant returns, preserve order and all original person_highlight items, no refill or relabel. K takes first K relevant items; remaining relevant removed on success.',
        'combo_scope':'query gate (max cosine>=c OR max non-name coverage>=l), otherwise drop relevant without network; exact unchanged payload reuses matching real judge call',
        'timeout_scope':'actual 60s calls replayed at each budget; duration over budget falls back to full baseline; additional real short-budget check after selection',
        'created_at':datetime.now(timezone.utc).isoformat()}
    p=out/'protocol.json'
    if p.exists():
        old=json.loads(p.read_text()); protocol['created_at']=old['created_at']
        if old!=protocol: raise ValueError('protocol changed; choose a new output directory')
    else: write(p,protocol)
    return protocol


def deterministic_grid():
    result=[]
    def add(family, **params): result.append({'id':f'd{len(result):03d}','family':family,**params})
    for floor in GRID_COS: add('cosine',floor=floor)
    for floor in GRID_COVERAGE: add('coverage',floor=floor)
    for floor in (.35,.45,.55,.65,.75): add('agreement',floor=floor)
    for floor in (.35,.45,.55,.65,.75):
        for margin in (0,.02,.05,.1,.15): add('margin',floor=floor,margin=margin)
    for floor in (.35,.45,.55,.65,.75):
        for ratio in (.8,.9,.95,1): add('normalized',floor=floor,ratio=ratio)
    for floor in (.35,.45,.55,.65,.75):
        for coverage in (.25,.5,.75,1): add('cosine_or_coverage',floor=floor,coverage=coverage)
    for floor in (.3,.4,.5,.6,.7,.8,.9): add('blend',floor=floor)
    return result


class TracedRetrieval(Retrieval):
    def _rank(self, conn, text, vector, **kwargs):
        ranked=super()._rank(conn,text,vector,**kwargs)
        lexical=super()._rank(conn,text,None,**kwargs)
        lexical_ids={m['id'] for m in lexical if m['reason']=='relevant'}
        raw=self.index.scores(vector,include_forgotten=bool(kwargs.get('include_forgotten'))) if vector is not None else {}
        cosines=sorted((raw[m['id']][0] for m in ranked if m['id'] in raw),reverse=True)
        top=cosines[0] if cosines else 0
        gap=top-(cosines[1] if len(cosines)>1 else 0)
        analysis=analyze(conn,text,tokenizer=self.settings['tokenizer'],entry_kind=kwargs.get('entry_kind'))
        cutoff=max(self.settings['vector_min'],top*self.settings['vector_relative'])
        best_score=max((m['score'] for m in ranked),default=0)
        features={}
        for m in ranked:
            indexed=conn.execute('SELECT content,tags FROM memory_fts_trigram WHERE rowid=?',(m['id'],)).fetchone()
            haystack=(indexed['content']+' '+indexed['tags']).casefold()
            coverage=sum(t in haystack for t in analysis.coverage_tokens)/max(1,len(analysis.coverage_tokens))
            cosine=float(raw.get(m['id'],(0,0))[0])
            features[m['id']]={'cosine':cosine,'coverage':coverage,'lexical':m['id'] in lexical_ids,
                'vector':m['id'] in raw and cosine>=cutoff, 'top_cosine':float(top),'gap':float(gap),
                'cosine_ratio':cosine/top if top>0 else 0,'rrf_ratio':m['score']/best_score if best_score else 0}
        self.last_features=features
        return ranked


def import_exact_cache(config, texts, source, target):
    """Read only hashes for this experiment's public inputs from a named public cache."""
    if not source or not source.is_file(): return 0
    dest=sqlite3.connect(target); dest.execute('CREATE TABLE IF NOT EXISTS embeddings(key TEXT PRIMARY KEY,vector BLOB NOT NULL)')
    imported=0
    with sqlite3.connect(f'file:{source}?mode=ro',uri=True) as old:
        for text in dict.fromkeys(texts):
            key=hashlib.sha256((config.base_url+'\0'+config.model+'\0'+str(config.dimensions)+'\0'+text).encode()).hexdigest()
            row=old.execute('SELECT vector FROM embeddings WHERE key=?',(key,)).fetchone()
            if row:
                vector=np.frombuffer(row[0],dtype=np.float32)
                if len(vector)!=config.dimensions or not np.isfinite(vector).all() or not np.linalg.norm(vector):
                    raise ValueError('invalid imported vector')
                imported+=dest.execute('INSERT OR IGNORE INTO embeddings VALUES(?,?)',(key,row[0])).rowcount
    dest.commit();dest.close();return imported


def capture(out, cache_source=None):
    protocol=freeze_protocol(out)
    if (out/'capture.json').exists(): raise ValueError('capture already complete; reuse without overwrite')
    configs=load_test_models();configs['embedding']=replace(configs['embedding'],dimensions=2048)
    contexts=[];texts=[];started=time.perf_counter()
    with ExitStack() as stack:
        for name in CORPORA:
            data=load_corpus(ROOT/'evals'/f'{name}.json')
            folder=out/'databases'/name;folder.mkdir(parents=True,exist_ok=True)
            db=folder/'iris.db'
            if db.exists():
                # Incomplete captures restart isolated seeds, preserving external embedding cache.
                for suffix in ('','-wal','-shm'): Path(str(db)+suffix).unlink(missing_ok=True)
            store=Store(db);stack.callback(store.close)
            ids,entries=seed_corpus(store,data)
            # All settings are the actual seed defaults; no configuration writes.
            preparer=Retrieval(store)
            assert preparer.settings['embedding_model']==configs['embedding'].model
            text_by_id={}
            texts.extend(m['content'] for m in data['memories'])
            for q in data['queries']:
                text,kind=(q.get('text',''),None) if q.get('mode')=='search' else preparer.prepare_query(entries[q['id']],q.get('text',''))
                text_by_id[q['id']]=(text,kind)
                if text.strip():texts.append(preparer.embedding_text(text,entry_kind=kind))
            contexts.append((name,data,store,ids,entries,text_by_id))
        cache=out/'recall-embeddings.db'
        imported=import_exact_cache(configs['embedding'],texts,cache_source,cache)
        print(f'imported exact public cache inputs: {imported}',flush=True)
        gateway,misses=cache_embeddings(configs,texts,contexts[0][2],cache)
        rows=[];fts=[]
        for name,data,store,ids,entries,text_by_id in contexts:
            with store.write() as conn:
                for m in data['memories']:
                    conn.execute('UPDATE memories SET embedding=?,embedding_model=? WHERE id=?',
                        (np.asarray(gateway.vectors[m['content']],dtype=np.float32).tobytes(),configs['embedding'].model,ids[m['id']]))
            stamp=datetime.fromisoformat(data.get('as_of','2026-09-29T12:00:00+00:00'))
            plain=Retrieval(store,gateway,clock=lambda:stamp)
            trace=TracedRetrieval(store,gateway,clock=lambda:stamp)
            fallback=Retrieval(store,None,clock=lambda:stamp)
            baseline=_run_queries(plain,data['queries'],ids,entries,details=True)
            fts.extend(dict(r,corpus=name) for r in _run_queries(fallback,data['queries'],ids,entries,details=True))
            for q,row in zip(data['queries'],baseline):
                checked=_run_queries(trace,[q],ids,entries,details=False)[0]
                assert (row['returned'],row['reasons'])==(checked['returned'],checked['reasons'])
                row.update(corpus=name,conversation=q.get('text','') is None,
                           retrieval_query=text_by_id[q['id']][0],subject_aliases=data.get('subjects',[]),
                           features={key:trace.last_features[ids[key]] for key in row['returned']})
                rows.append(row)
            print('captured '+name+': '+str(len(baseline))+' queries',flush=True)
        with contexts[0][2].read() as conn:
            calls=[dict(r) for r in conn.execute('SELECT purpose,model,duration_ms,prompt_tokens,completion_tokens,result_category,finish_reason,timed_out FROM model_calls')]
        write(out/'capture.json',{'protocol_sha256':digest(protocol),'source_sha256':source_fingerprint(),
            'configuration':safe_configs(configs),'embedding_imported':imported,'embedding_cache_misses':misses,
            'embedding_calls':calls,'elapsed_seconds':time.perf_counter()-started,'rows':rows,'fulltext_rows':fts})
        print(json.dumps({'baseline':groups(rows)},ensure_ascii=False),flush=True)


def model_payload(row,k):
    items=[m for m in row['response']['memories'] if m['reason']=='relevant'][:k]
    reverse={m['id']:key for m,key in zip(row['response']['memories'],row['returned'])}
    q=row['query']
    body={'role_name':'Iris','query_hint':q.get('text',''), 'retrieval_query':row['retrieval_query'],
        'participants':q.get('participants',[]),'subject_aliases':row['subject_aliases'],
        'recent_messages':[{key:m[key] for key in ('kind','sender_name','content','occurred_at') if key in m}
                           for m in row['response'].get('recent_messages',[])],
        'candidates':[{'id':reverse[m['id']],'content':m['content'],'speaker':m['speaker_name'],
            'about':[p['name'] for p in m['about']],'kind':m['kind'],'stance':m['stance']} for m in items]}
    return body


def validate_scores(raw, ids):
    data=parse_json_object(raw)
    scores=data.get('scores')
    if not isinstance(scores,list) or len(scores)!=len(ids): raise ValueError('score length')
    if [s.get('id') if isinstance(s,dict) else None for s in scores]!=ids: raise ValueError('score ids/order')
    if any(type(s.get('support')) is not int or not 0<=s['support']<=100 for s in scores): raise ValueError('score type/range')
    return {s['id']:s['support'] for s in scores}


def integer_usage(data):
    usage=data.get('usage') or {}
    result={key:usage.get(key) for key in ('prompt_tokens','completion_tokens','total_tokens') if type(usage.get(key)) is int}
    for key,child in (('reasoning_tokens','completion_tokens_details'),('cached_tokens','prompt_tokens_details')):
        value=(usage.get(child) or {}).get(key)
        if type(value) is int:result[key]=value
    return result


async def one_call(client, config, body, effort, deadline=60):
    start=time.perf_counter()
    result={'status':'error','scores':{},'usage':{},'raw_content':None,'reasoning_effort':effort}
    payload={'model':config.model,'messages':[{'role':'system','content':(HERE/'judgment_prompt.txt').read_text(encoding='utf-8')},
             {'role':'user','content':json.dumps(body,ensure_ascii=False)}],
             'reasoning_effort':effort,'temperature':0,'max_tokens':4096,'response_format':{'type':'json_object'}}
    try:
        async with asyncio.timeout(deadline):
            response=await client.post(config.base_url+'/chat/completions',headers={'Authorization':'Bearer '+config.api_key},json=payload)
            result['http_status']=response.status_code
            if response.status_code!=200:
                result['error']='http_'+str(response.status_code)
            else:
                data=response.json();result['usage']=integer_usage(data)
                choice=data['choices'][0];msg=choice['message'];result['finish_reason']=choice.get('finish_reason')
                result['reasoning_present']=bool(msg.get('reasoning_content'))
                result['reasoning_chars']=len(msg.get('reasoning_content') or '')
                result['raw_content']=msg.get('content')
                if result['finish_reason']!='stop' or msg.get('refusal'):
                    result['error']='finish_or_refusal'
                else:
                    result['raw_content']=msg.get('content')
                    result['scores']=validate_scores(result['raw_content'],[m['id'] for m in body['candidates']])
                    result['status']='success'
    except (TimeoutError,httpx.TimeoutException): result['error']='timeout'
    except Exception as exc:result['error']=type(exc).__name__
    result['duration_ms']=(time.perf_counter()-start)*1000
    if result['duration_ms']>deadline*1000:
        result.update(status='error',scores={},error='timeout')
    if config.api_key:
        result=json.loads(json.dumps(result,ensure_ascii=False).replace(config.api_key,'[REDACTED]'))
    return result


async def judge(out):
    protocol=freeze_protocol(out)
    captured=json.loads((out/'capture.json').read_text())
    assert captured['protocol_sha256']==digest(protocol)
    config=load_test_models()['chat']
    assert config.model=='glm-5.3-flash'
    semaphore=asyncio.Semaphore(2);completed=0
    async with httpx.AsyncClient(timeout=60,follow_redirects=False) as client:
        async def job(row,effort,k):
            nonlocal completed
            body=model_payload(row,k)
            identity={'body':body,'model':config.model,'endpoint':config.base_url,'effort':effort,
                      'k':k,'protocol_sha256':digest(protocol)}
            fingerprint=digest(identity)
            target=out/'calls'/f'{effort}-k{k}'/f"{row['corpus']}--{row['id']}.json"
            if target.exists():
                old=json.loads(target.read_text())
                if old['fingerprint']!=fingerprint:raise ValueError('call fingerprint mismatch')
                return
            if not body['candidates']:
                result={'status':'skipped_empty','scores':{},'usage':{},'duration_ms':0}
            else:
                async with semaphore:
                    result=await one_call(client,config,body,effort)
                    await asyncio.sleep(.25)
            write(target,{'fingerprint':fingerprint,'input':body,**result})
            completed+=1
            if completed%10==0:print(f'judge completed {completed}; {effort}-k{k}; status={result["status"]}',flush=True)
        # Interleave efforts/query order; no independent model evaluations run in parallel.
        await asyncio.gather(*(job(row,effort,k) for row in captured['rows'] for effort,k in JUDGE_VARIANTS))


def filtered(row,keep):
    result={k:v for k,v in row.items() if k not in ('response','query','features','subject_aliases')}
    result['returned']=[mid for mid in row['returned'] if row['reasons'][mid]=='person_highlight' or mid in keep]
    result['reasons']={mid:row['reasons'][mid] for mid in result['returned']}
    return result


def deterministic(row,p):
    keep=[]
    for mid,f in row['features'].items():
        family=p['family'];cos=f['cosine'];cov=f['coverage'];floor=p['floor']
        yes={'cosine':lambda:cos>=floor,'coverage':lambda:cov>=floor,
            'agreement':lambda:cos>=floor and f['vector'] and f['lexical'],
            'margin':lambda:f['top_cosine']>=floor and f['gap']>=p['margin'],
            'normalized':lambda:cos>=floor and f['cosine_ratio']>=p['ratio'],
            'cosine_or_coverage':lambda:cos>=floor or cov>=p['coverage'],
            'blend':lambda:.7*cos+.25*cov+.05*int(f['lexical'] and f['vector'])>=floor}[family]()
        if yes:keep.append(mid)
    return filtered(row,keep)


def apply_judge(row,call,threshold,budget):
    if call['status'] not in ('success','skipped_empty') or call['duration_ms']>budget*1000:
        return filtered(row,row['returned']),True
    return filtered(row,[mid for mid,s in call['scores'].items() if s>=threshold]),False


def gate_accept(row,gate):
    features=[f for mid,f in row['features'].items() if row['reasons'][mid]=='relevant']
    return any(f['cosine']>=gate[0] or f['coverage']>=gate[1] for f in features)


def groups(rows):
    return {'all':recall_metrics(rows),
        'conversation':recall_metrics([r for r in rows if r.get('conversation', '对话中准备' in r['categories'])]),
        'explicit':recall_metrics([r for r in rows if not r.get('conversation','对话中准备' in r['categories'])]),
        **{name:recall_metrics([r for r in rows if r['corpus']==name]) for name in CORPORA}}


def choose(trials):
    eligible=[t for t in trials if t['metrics']['recall_at_8']>=.85 and t['metrics']['ndcg_at_8']>=.75]
    feasible=[t for t in eligible if t['metrics']['irrelevant_return_rate']<=.10]
    if not feasible:
        return min(eligible,key=lambda t:t['metrics']['irrelevant_return_rate']) if eligible else None
    def quality(t):return (Decimal(str(t['metrics']['recall_at_8']))+Decimal(str(t['metrics']['ndcg_at_8'])))/2
    top=max(quality(t) for t in feasible)
    tied=[t for t in feasible if top-quality(t)<Decimal('.01')]
    return max(tied,key=lambda t:(t['metrics']['relevant_precision'] or 0,-t['metrics']['irrelevant_return_rate']))


def regressions(rows,baseline):
    by_key={(r['corpus'],r['id']):r for r in baseline}
    before=groups(baseline);after=groups(rows);result={}
    for name in CORPORA:
        if before[name]['recall_at_8']-after[name]['recall_at_8']>.05+1e-12:
            lost=[]
            for row in rows:
                if row['corpus']!=name or not row['relevant']:continue
                old=by_key[(name,row['id'])]
                old_r=recall_metrics([old])['recall_at_8'];new_r=recall_metrics([row])['recall_at_8']
                if old_r>new_r:
                    lost.append({'id':row['id'],'before':old_r,'after':new_r,'lost':sorted(set(old['returned'])&set(row['relevant'])-set(row['returned']))})
            result[name]={'recall_drop':before[name]['recall_at_8']-after[name]['recall_at_8'],'queries':lost}
    return result


def summarize(out):
    capture=json.loads((out/'capture.json').read_text());rows=capture['rows'];trials=[]
    calls={}
    for effort,k in JUDGE_VARIANTS:
        for r in rows:
            p=out/'calls'/f'{effort}-k{k}'/f"{r['corpus']}--{r['id']}.json"
            calls[effort,k,r['corpus'],r['id']]=json.loads(p.read_text())
    def add(name,family,current,params,callset=(),failures=0):
        gs=groups(current)
        used=[c for c in callset if c['status']!='skipped_empty']
        durations=[c['duration_ms'] for c in used]
        usage={key:sum(c.get('usage',{}).get(key,0) for c in used) for key in ('prompt_tokens','completion_tokens','reasoning_tokens','cached_tokens')}
        t={'id':name,'family':family,'params':params,'metrics':gs['all'],'groups':gs,
           'regressions':regressions(current,rows),'calls':len(used),'fallbacks':failures,
           'p50_ms':float(np.percentile(durations,50)) if durations else None,
           'p95_ms':float(np.percentile(durations,95)) if durations else None,'usage':usage}
        trials.append(t)
        write(out/'results'/f'{name}.json',{'summary':t,'rows':current})
    add('baseline','baseline',[filtered(r,r['returned']) for r in rows],{})
    for p in deterministic_grid():add(p['id'],p['family'],[deterministic(r,p) for r in rows],p)
    def model_trial(effort,k,threshold,budget,gate=None):
        current=[];used=[];failed=0
        for row in rows:
            if gate and not gate_accept(row,gate):
                current.append(filtered(row,[]));continue
            call=calls[effort,k,row['corpus'],row['id']]
            new,fallback=apply_judge(row,call,threshold,budget);current.append(new)
            failed+=fallback;used.append(call)
        name=f'{effort}-k{k}-s{threshold}-t{budget}'
        if gate:name=f'gate-{gate[0]}-{gate[1]}-'+name
        add(name,'combination' if gate else f'glm-{effort}-k{k}',current,
            dict(effort=effort,k=k,threshold=threshold,budget=budget,gate=gate),used,failed)
    for effort,k in JUDGE_VARIANTS:
        for threshold in SUPPORT:
            for budget in TIMEOUTS:model_trial(effort,k,threshold,budget)
    for gate in ([c,l] for c in (.45,.55,.65) for l in (.5,.75)):
        for effort in ('low','high'):
            for threshold in SUPPORT:
                for budget in TIMEOUTS:model_trial(effort,8,threshold,budget,gate)
    chosen=choose(trials)
    representatives={family:choose([t for t in trials if t['family']==family]) for family in dict.fromkeys(t['family'] for t in trials)}
    # Family with no recall/ndcg-eligible candidate still has its highest-Q diagnostic.
    for family,t in representatives.items():
        if t is None:representatives[family]=max((t for t in trials if t['family']==family),key=lambda t:(t['metrics']['recall_at_8']+t['metrics']['ndcg_at_8'])/2)
    fulltext=groups(capture['fulltext_rows'])
    write(out/'summary.json',{'trials':trials,'selected':chosen,'representatives':representatives,'fulltext':fulltext,
        'feasible_count':sum(t['metrics']['recall_at_8']>=.85 and t['metrics']['ndcg_at_8']>=.75 and t['metrics']['irrelevant_return_rate']<=.1 for t in trials)})
    print(json.dumps({'selected':{k:chosen[k] for k in ('id','metrics','params')},
        'representatives':{f:{k:t[k] for k in ('id','metrics')} for f,t in representatives.items()}},ensure_ascii=False),flush=True)


def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=('freeze','capture','judge','summarize'))
    p.add_argument('--out',type=Path,required=True);p.add_argument('--cache-source',type=Path)
    args=p.parse_args();out=args.out.resolve()
    assert not out.is_relative_to(ROOT),'full artifacts must stay outside repository'
    if args.command=='freeze':print(digest(freeze_protocol(out)))
    elif args.command=='capture':capture(out,args.cache_source)
    elif args.command=='judge':asyncio.run(judge(out))
    else:summarize(out)

if __name__=='__main__':main()
