"""Frozen visibility corpus runner. No learning, generation or corpus rewriting.

--offline exercises the default full-text fallback without loading credentials.
Normal runs use configured embeddings; --judge opts into real recall judgments.
The recent-window adapter keeps historical source rows out of recent messages.
--http-search sends every search through the real ASGI app and Bearer auth;
only the retrieval clock and background scheduler are controlled by the fixture.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from contextlib import ExitStack, contextmanager
from unittest.mock import patch
from datetime import datetime
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from iris.api import create_app, HostRetrieval

from iris.consolidation import Consolidation, DEFAULTS as CONSOLIDATION_DEFAULTS, merge_exclusion, snapshot
from iris.db import Store, dumps
from iris.memory_ops import Visibility, set_entry_visibility, setup_role
from iris.models import Gateway, load_test_models
from iris.recall_evaluation import cache_embeddings
from iris.retrieval import Retrieval, DEFAULTS

ROOT = Path(__file__).resolve().parents[2]


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def validate(data):
    if data.get('format_version') != 1 or not data.get('cases'):
        raise ValueError('expected nonempty visibility format_version=1')
    seen = set()
    for case in data['cases']:
        if case['id'] in seen:
            raise ValueError('duplicate case ID')
        seen.add(case['id'])
        entries = {e['id'] for e in case['entries']}
        keys = {m['key'] for m in case['memories']} | {g['key'] for g in case.get('goals', [])}
        if len(keys) != len(case['memories'])+len(case.get('goals', [])):
            raise ValueError('duplicate object key')
        for q in case['queries']:
            if q['entry_id'] not in entries or q['mode'] not in ('prepare','search'):
                raise ValueError('invalid query entry/mode')
            if not set(q.get('forbidden', [])+q.get('expected', [])+q.get('expected_goals', [])) <= keys:
                raise ValueError('unknown annotation key')
            if q['mode']=='search' and q.get('text') is None:
                raise ValueError('search text cannot be null')
        if not case['queries']:
            raise ValueError('empty case')


def seed(store, case):
    setup_role(store, 'Iris', current=datetime.fromisoformat(case['now']))
    mids, gids, sources = {}, {}, {}
    with store.write() as conn:
        for e in case['entries']:
            conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES(?,?,'visibility_eval',?)", (e['id'],e['name'],e['kind']))
        for s in case.get('subjects', []):
            conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)", (s['id'],s['name'],case['now']))
            conn.executemany('INSERT INTO subject_aliases(subject_id,alias) VALUES(?,?)', [(s['id'],a) for a in s.get('aliases',[])])
        for obj in [*case['memories'], *case.get('goals', [])]:
            for source in obj.get('sources', []):
                old = sources.get(source['key'])
                if old:
                    if old[1] != source:
                        raise ValueError('inconsistent source key')
                    continue
                mid = conn.execute("""INSERT INTO messages(entry_id,kind,sender_subject_id,content,occurred_at,received_at,dedupe_key,learning_state)
                    VALUES(?,?,?,?,?,?,?,'learned')""", (source['entry_id'],source['kind'],source['sender'],source['text'],source['at'],source['at'],source['key'])).lastrowid
                sources[source['key']] = (mid,source)
        for m in case['memories']:
            stamp = m['created_at']
            mid = conn.execute("""INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,retention,
                event_time,lifecycle,entry_id,created_at,updated_at,first_confirmed_at,last_confirmed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (m['content'],m['type'],m['speaker'],m['stance'],m['belief'],m['importance'],
                round(30+m['importance']*.4),m.get('event_time'),m.get('lifecycle','active'),m.get('entry_id',m['sources'][0]['entry_id'] if m['sources'] else None),stamp,stamp,stamp,stamp)).lastrowid
            mids[m['key']] = mid
            conn.executemany('INSERT INTO memory_subjects VALUES(?,?)', [(mid,s) for s in m['about']])
            conn.executemany("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,?)",
                             [(mid,sources[s['key']][0],stamp) for s in m['sources']])
        for m in case['memories']:
            conn.executemany("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,1,?)",
                             [(mids[m['key']],mids[parent],m['created_at']) for parent in m.get('derived_from',[])])
        # Fixed snapshots bypass create-time goal deduplication; preserving every
        # annotated object is essential, especially equal-text public/private pairs.
        for g in case.get('goals', []):
            gid = conn.execute("""INSERT INTO goals(content,kind,origin,state,entry_id,deadline,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""", (g['content'],g['kind'],g['origin'],g['state'],g.get('entry_id'),g.get('deadline'),g['created_at'],g['created_at'])).lastrowid
            gids[g['key']] = gid
            conn.executemany('INSERT INTO goal_people VALUES(?,?)', [(gid,s) for s in g.get('people',[])])
            conn.executemany('INSERT INTO goal_sources VALUES(?,?)', [(gid,sources[s['key']][0]) for s in g.get('sources',[])])
    for e in case['entries']:
        if 'visibility' in e:
            set_entry_visibility(store,e['id'],e['visibility'],visible_in=e.get('visible_in',[]))
    return mids,gids,{mid: key for key,(mid,_) in sources.items()}


class WindowRetrieval(Retrieval):
    """Use the corpus's exact recent window with the production adaptive_6 logic."""
    window = ()
    ranked = ()
    selected = ()

    def _rank(self, *args, **kwargs):
        self.ranked = super()._rank(*args, **kwargs)
        return self.ranked

    def _select(self, candidates, **kwargs):
        result = super()._select(candidates, **kwargs)
        chosen = {m['id'] for m in result}
        self.selected = [m for m in candidates if m['id'] in chosen]
        return result

    def missing_reason(self, mid, response):
        if mid in response.get('judgment',{}).get('removed_memory_ids',[]):
            return 'recall_judgment'
        candidate = next((m for m in self.ranked if m['id']==mid),None)
        if candidate is None:
            return 'retrieval_relevance_or_candidate_cap'
        if any(self._duplicate(candidate,m) for m in self.selected):
            return 'dedup'
        return 'selection_budget_or_highlight_limit'

    def _recent(self, conn, entry_id, limit):
        return list(self.window[-limit:]) if limit else []

    def _prepare_context(self, entry_id, text):
        with self.store.read() as conn:
            kind = conn.execute('SELECT kind FROM entries WHERE id=?',(entry_id,)).fetchone()[0]
            if text is not None:
                return text, kind, None
            other = [m for m in reversed(self.window) if m['kind']=='message' and m['sender_subject_id'] not in ('self','scene')][:20]
            selected = self._conversation_suffix(conn,other,kind)
            return '\n'.join(m['content'] for m in reversed(selected)),kind,other[0]['content'] if other else ''


def window(case, query):
    names = {s['id']:s['name'] for s in case['subjects']} | {'self':'Iris','scene':'场景'}
    return [dict(id=-(i+1),entry_id=query['entry_id'],kind=m['kind'],sender_subject_id=m['sender'],
                 sender_name=names[m['sender']],content=m['text'],occurred_at=m['at'],unlearned=True)
            for i,m in enumerate(query.get('recent_messages', []))]


@contextmanager
def http_search_client(store, gateway, clock):
    observed = []

    class ClockedHostRetrieval(HostRetrieval, WindowRetrieval):
        # Inherit the real host goal authorization and retrieval implementation.
        # WindowRetrieval only observes rank/select here: search has no window.
        def __init__(self, store, gateway, scope):
            super().__init__(store, gateway, scope)
            self.clock = lambda: clock[0]
            observed.append(self)

    with patch('iris.api.HostRetrieval', ClockedHostRetrieval), patch('iris.api.Scheduler.start'):
        with TestClient(create_app(store=store, gateway=gateway, configs={} if gateway is None else None),
                        base_url='http://127.0.0.1', client=('127.0.0.1', 12345)) as client:
            credential = client.app.state.tokens.create(host='visibility-eval', scope={'kind': 'all'}, actor='local_cli')
            client.headers['Authorization'] = 'Bearer ' + credential['token']
            yield client, observed


def run_case(case, out, *, configs=None, judge=False, http_search=False):
    out.mkdir(parents=True, exist_ok=False)
    clock = [datetime.fromisoformat(case['now'])]
    store = Store(out/'case.db')
    gateway = None
    resources = ExitStack()
    try:
        mids,gids,sources = seed(store,case)
        r = WindowRetrieval(store, clock=lambda: clock[0])
        if configs:
            texts = [m['content'] for m in case['memories']]
            for q in case['queries']:
                r.window = window(case,q)
                text,kind = r.prepare_query(q['entry_id'],q.get('text')) if q['mode']=='prepare' else (q['text'],None)
                if text.strip():
                    texts.append(r.embedding_text(text,entry_kind=kind))
            cached,_ = cache_embeddings(configs,texts,store,out.parent/'embeddings.db')
            gateway = Gateway(configs,store)
            gateway.embedding = cached.embedding
            config = configs['embedding']
            # Default calibration must match the configured model and dimension.
            if config.model != DEFAULTS['embedding_model'] or config.dimensions not in (None,DEFAULTS['embedding_dimensions']):
                raise ValueError('configured embedding does not match default calibration')
            with store.write() as conn:
                for m in case['memories']:
                    conn.execute('UPDATE memories SET embedding=?,embedding_model=? WHERE id=?',
                        (np.asarray(cached.vectors[m['content']],dtype=np.float32).tobytes(),config.model,mids[m['key']]))
            r = WindowRetrieval(store,gateway,clock=lambda:clock[0])
        http = resources.enter_context(http_search_client(store,gateway,clock)) if http_search and any(q['mode']=='search' for q in case['queries']) else None
        reverse_m,reverse_g = {v:k for k,v in mids.items()},{v:k for k,v in gids.items()}
        no_merge = []
        with store.read() as conn:
            get = lambda mid: snapshot(conn,mid)
            for pair in case.get('consolidation',{}).get('no_merge',[]):
                a,b = (get(mids[key]) for key in pair)
                reason = merge_exclusion(a,b)
                planned = Consolidation(store)._plan_pair(get,a,b,CONSOLIDATION_DEFAULTS)
                no_merge.append({'pair':pair,'exclusion':reason,'planned':planned is not None,
                                 'leak':reason!='visibility' or planned is not None})
        changes = sorted(case.get('visibility_changes',[]),key=lambda c:datetime.fromisoformat(c['at']))
        applied,rows = [],[]
        for q in sorted(case['queries'],key=lambda q:datetime.fromisoformat(q.get('at',case['now']))):
            clock[0] = datetime.fromisoformat(q.get('at',case['now']))
            while changes and datetime.fromisoformat(changes[0]['at']) <= clock[0]:
                change = changes.pop(0)
                set_entry_visibility(store,change['entry_id'],change['visibility'],visible_in=change.get('visible_in',[]))
                applied.append(change)
            r.window = window(case,q)
            current_retrieval,transport,http_status = r,'direct',None
            if q['mode']=='search':
                payload = dict(entry_id=q['entry_id'],text=q['text'],include_goals=q.get('include_goals',False),
                               include_forgotten=q.get('include_forgotten',False))
                if http is None:
                    response = r.search(**payload)
                else:
                    client,observed = http
                    result = client.post('/api/v1/memories/search',json=payload)
                    if result.status_code != 200:
                        raise ValueError(f'HTTP search failed: {result.status_code}')
                    response = result.json()
                    current_retrieval,transport,http_status = observed[-1],'http',result.status_code
            else:
                response = r.prepare(q['entry_id'],text=q.get('text'),participants=q.get('participants',[]),
                                     recent_limit=len(r.window),judge=judge)
            valid = response.get('judgment',{}).get('status') != 'degraded'
            if configs and any(h['code'] in ('embedding_fallback','embedding_uncalibrated','embedding_unconfigured') for h in response['hints']):
                valid = False
            partitions = {}
            for m in response['memories']:
                partitions.setdefault(m['reason'],[]).append(reverse_m[m['id']])
                for source in m['sources']:
                    if source['message_id'] not in sources or set(source)-{'message_id','entry_id','entry_name','occurred_at'}:
                        raise ValueError('incomplete provenance or source prose returned')
            partitions['goals'] = [reverse_g[g['id']] for g in response.get('goals',[])]
            for g in response.get('goals',[]):
                for gid in g.get('possible_duplicate_ids',[]):
                    partitions.setdefault('goal_duplicate_references',[]).append(reverse_g[gid])
                for a in g.get('basis_annotations',[]):
                    for key in ('memory_id','observed_merged_into'):
                        if a.get(key) is not None:
                            partitions.setdefault('goal_basis_references',[]).append(reverse_m[a[key]])

            leaks = [{'key':key,'partition':part} for part,keys in partitions.items() for key in keys if key in q.get('forbidden',[])]
            found = {key for keys in partitions.values() for key in keys}
            missing=[]
            with store.read() as conn:
                v=Visibility(conn)
                for key in q.get('expected',[])+q.get('expected_goals',[]):
                    if key not in found:
                        visible = v.memory_visible(mids[key],q['entry_id']) if key in mids else v.goal_visible(gids[key],q['entry_id'])
                        reason = 'visibility' if not visible else current_retrieval.missing_reason(mids[key],response) if key in mids else 'goal_partition_budget'
                        missing.append({'key':key,'reason':reason})
            rows.append({'query':q['key'],'at':clock[0].isoformat(),'transport':transport,'http_status':http_status,'valid':valid,'partitions':partitions,'leaks':leaks,
                'forbidden_count':len(q.get('forbidden',[])), 'expected_count':len(q.get('expected',[])),
                'expected_hits':sum(k in found for k in q.get('expected',[])),
                'expected_goals_count':len(q.get('expected_goals',[])), 'expected_goals_hits':sum(k in found for k in q.get('expected_goals',[])),
                'missing':missing, 'include_goals_violation':q['mode']=='search' and not q.get('include_goals',False) and bool(response.get('goals')),
                'response':response})
        return {'id':case['id'],'queries':rows,'no_merge':no_merge,'changes':applied,
                'mapping':{'memories':mids,'goals':gids,'sources':sources}}
    finally:
        resources.close()
        if gateway:
            gateway.close()
        store.close()


def run(corpus, out, *, configs=None, judge=False, http_search=False, embedding_cache=None):
    if out.resolve().is_relative_to(ROOT):
        raise ValueError('full artifacts must be outside the repository')
    data=json.loads(corpus.read_text(encoding='utf-8'))
    validate(data)
    out.mkdir(parents=True,exist_ok=False)
    if embedding_cache:
        shutil.copyfile(embedding_cache,out/'embeddings.db')
    cases=[]
    for i,case in enumerate(data['cases']):
        try:
            result=run_case(case,out/f'case-{i+1:03}',configs=configs,judge=judge,http_search=http_search)
        except Exception as exc:
            result={'id':case['id'],'error_type':type(exc).__name__,'queries':[],'no_merge':[],'changes':[]}
        cases.append(result)
        write(out/f'result-{i+1:03}.json',result)
        print(f'{case["id"]}: {len(result["queries"])}/{len(case["queries"])} queries',flush=True)
    rows=[q for c in cases for q in c['queries']]
    merge=[m for c in cases for m in c['no_merge']]
    count=sum(len(c['queries']) for c in data['cases'])
    report={'corpus_sha256':hashlib.sha256(corpus.read_bytes()).hexdigest(),
        'source_sha256':hashlib.sha256(b''.join(p.relative_to(ROOT).as_posix().encode()+p.read_bytes() for p in sorted((ROOT/'src/iris').rglob('*')) if p.suffix in ('.py','.sql','.md','.json'))).hexdigest(),
        'mode':'default_hybrid' if configs else 'default_fulltext_fallback','judge':judge,
        'http_search':http_search,'http_search_queries':sum(q['transport']=='http' for q in rows),
        'embedding_cache_reused':embedding_cache is not None,
        'valid':len(rows)==count and all(q['valid'] for q in rows) and not any('error_type' in c for c in cases),
        'cases':len(cases),'queries_expected':count,'queries_executed':len(rows),
        'leakage_count':sum(len(q['leaks']) for q in rows)+sum(m['leak'] for m in merge),
        'leaking_queries':sum(bool(q['leaks']) for q in rows),'merge_pairs':len(merge),
        'merge_leaks':sum(m['leak'] for m in merge),'changes_applied':sum(len(c['changes']) for c in cases),
        'include_goals_violations':sum(q['include_goals_violation'] for q in rows),
        **{key:sum(q[key] for q in rows) for key in ('forbidden_count','expected_count','expected_hits','expected_goals_count','expected_goals_hits')},
        'errors':[{'id':c['id'],'error_type':c['error_type']} for c in cases if 'error_type' in c],
        'consolidation_method':'merge_exclusion plus _plan_pair on the independently seeded snapshot; no generation',
        'details':cases}
    for label in ('expected','expected_goals'):
        report[label+'_hit_rate']=report[label+'_hits']/report[label+'_count'] if report[label+'_count'] else None
    write(out/'report.json',report)
    print(dumps({k:v for k,v in report.items() if k!='details'}),flush=True)
    return report


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--corpus',type=Path,default=ROOT/'evals/visibility_v1.json')
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--offline',action='store_true')
    parser.add_argument('--judge',action='store_true')
    parser.add_argument('--http-search',action='store_true',help='send search queries through the authenticated HTTP API')
    parser.add_argument('--embedding-cache',type=Path,help='copy a completed public embedding cache; judgments are always fresh')
    args=parser.parse_args()
    if args.offline and args.judge:
        parser.error('--judge requires configured models')
    report=run(args.corpus,args.out,configs=None if args.offline else load_test_models(),judge=args.judge,
               http_search=args.http_search,embedding_cache=args.embedding_cache)
    raise SystemExit(0 if report['valid'] and not report['leakage_count'] and not report['include_goals_violations'] else 1)


if __name__=='__main__':
    main()
