"""Probe real goal creation paths against a labeled public/external corpus.

No expected labels are passed to goal creation or model input. Databases,
requests, model content (never reasoning), and results stay in --out.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx

from iris.db import Store
from iris.goals import Goals, _deadline, write_learning_goal
from iris.model_health import ModelHealth
from iris.models import Gateway, ModelConfig, ModelError, load_test_models
from iris.query_analysis import SubjectNames
from iris.state import role_zone

# Also supports loading this script as a module in deterministic unit tests.
sys.path.insert(0,str(Path(__file__).resolve().parent))
from scoring import report, score_case

ROOT=Path(__file__).resolve().parents[2]


def save(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def utc(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def sources(conn, items, entry_kinds):
    result=[]
    for item in items:
        eid=item['entry_id']
        conn.execute("INSERT OR IGNORE INTO entries(id,name,platform,kind) VALUES(?,?,'goal-probe',?)",
                     (eid,eid,entry_kinds.get(eid,'private')))
        mid=conn.execute('''INSERT INTO messages(entry_id,kind,sender_subject_id,content,occurred_at,
            received_at,dedupe_key,learning_state) VALUES(?,?,?,?,?,?,?,'learned')''',
            (eid,item['kind'],item['sender'],item['text'],utc(item['at']).isoformat(),utc(item['at']).isoformat(),
             'source-'+str(conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM messages').fetchone()[0]))).lastrowid
        result.append(mid)
    return result


def seed_case(store, case, corpus):
    """Install historical facts literally; never deduplicate the existing rows."""
    store.set_setting('timezone',corpus['timezone'])
    store.set_setting('role_name',corpus['role_name'])
    material=[*case['existing'],case['incoming']]
    entry_kinds={goal['entry_id']:goal['entry_kind'] for goal in material if goal.get('entry_id')}
    keys={}
    with store.write() as conn:
        for subject in case['subjects']:
            conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)",
                         (subject['id'],subject['name'],utc(case['now']).isoformat()))
            conn.executemany('INSERT INTO subject_aliases(subject_id,alias) VALUES(?,?)',
                             [(subject['id'],alias) for alias in subject.get('aliases',[])])
        for eid,kind in entry_kinds.items():
            conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES(?,?,'goal-probe',?)",(eid,eid,kind))
        for goal in case['existing']:
            evidence=sources(conn,goal.get('sources',[]),entry_kinds)
            deadline=_deadline(goal.get('deadline'),role_zone(conn))
            created=utc(goal['created_at']).isoformat()
            gid=conn.execute('''INSERT INTO goals(content,kind,origin,state,deadline,deadline_at,entry_id,
                created_at,updated_at,schedule_initialized) VALUES(?,?,?,?,?,?,?,?,?,1)''',
                (goal['content'],goal['kind'],goal['origin'],goal['state'],deadline,deadline,
                 goal.get('entry_id'),created,created)).lastrowid
            keys[gid]=goal['key']
            people=goal.get('people')
            if people is None:
                people=set(SubjectNames(conn).mentioned(goal['content']))-{'self','scene'}
                people.update(item['sender'] for item in goal.get('sources',[]) if item['sender']!='self')
            conn.executemany('INSERT INTO goal_people(goal_id,subject_id) VALUES(?,?)',[(gid,p) for p in sorted(set(people))])
            conn.executemany('INSERT INTO goal_sources(goal_id,message_id) VALUES(?,?)',[(gid,m) for m in evidence])
        incoming_evidence=sources(conn,case['incoming'].get('sources',[]),entry_kinds)
    return keys,incoming_evidence


class RecordingGateway(Gateway):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.records=[]
        self.raw_contents=[]
        self.client.event_hooks['response'].append(self._capture_content)

    def _capture_content(self,response):
        # Keep invalid model JSON as well. Never retain headers, provider error
        # prose, or reasoning fields; only successful HTTP completion content.
        if response.status_code!=200:
            return
        response.read()
        try:
            data=response.json()
            choice=data['choices'][0]
            content=choice['message'].get('content')
            if isinstance(content,str):
                self.raw_contents.append({'content':content,'finish_reason':choice.get('finish_reason')})
        except (ValueError,KeyError,IndexError,TypeError,AttributeError):
            pass

    def _call(self,kind,purpose,payload,**kwargs):
        # Admission calls _call recursively: capture only the outer invocation.
        if kwargs.get('_lease') is not None or kind!='goal_dedup_judge':
            return super()._call(kind,purpose,payload,**kwargs)
        record={'messages':payload.get('messages'),'purpose':purpose}
        self.records.append(record)
        try:
            result=super()._call(kind,purpose,payload,**kwargs)
            record.update(content=result['choices'][0]['message']['content'],
                          finish_reason=result['choices'][0].get('finish_reason'),usage=result.get('usage'),
                          queue_ms=result.get('_iris_queue_ms'),network_ms=result.get('_iris_network_ms'))
            return result
        except ModelError as exc:
            # Never persist provider error text, headers, configuration or reasoning.
            record.update(error=exc.category,reason=exc.reason)
            raise


def fake_transport(verdict):
    """Offline plumbing check; ignores labels and cannot access the network."""
    def respond(request):
        if verdict=='timeout':
            raise httpx.ReadTimeout('simulated goal timeout',request=request)
        material=json.loads(json.loads(request.content)['messages'][-1]['content'])
        content=json.dumps({'decisions':[{'id':row['id'],'verdict':verdict} for row in material['candidates']]})
        if verdict=='invalid':
            content='invalid unit response'
        return httpx.Response(200,json={'choices':[{'message':{'content':content},'finish_reason':'stop'}],
                                      'usage':{'prompt_tokens':1,'completion_tokens':1}})
    return httpx.MockTransport(respond)


def run_case(case,corpus,out,method,configs,budget,*,fake=None):
    directory=out/case['id']
    directory.mkdir()
    store=Store(directory/'iris.db')
    gateway=None
    client=None
    current=utc(case['now'])
    try:
        keys,evidence=seed_case(store,case,corpus)
        store.set_setting('goal_dedup_judge',{'enabled':method!='A','method':method,
                                             'budget_seconds':budget,'concurrency':1,'queue_limit':8})
        if method!='A':
            health=ModelHealth(store,configs,clock=lambda:current)
            client=httpx.Client(transport=fake_transport(fake)) if fake else None
            gateway=RecordingGateway(configs,store,health=health,clock=lambda:current,client=client)
            goals=Goals(store,gateway=gateway,clock=lambda:current)
        else:
            goals=Goals(store,clock=lambda:current)
        incoming=case['incoming']
        fields={name:incoming[name] for name in ('content','kind','deadline','people','entry_id') if name in incoming}
        started=time.monotonic()
        if incoming['origin']=='internal':
            with store.write() as conn:
                result=write_learning_goal(conn,content=incoming['content'],kind=incoming['kind'],
                    deadline=incoming.get('deadline'),entry_id=incoming.get('entry_id'),evidence=evidence,current=current)
            if method!='A':
                result=goals.review(result['submitted_id'])
        else:
            result=goals.create(**fields,origin=incoming['origin'],actor=incoming['origin'],evidence=evidence)
        duration_ms=(time.monotonic()-started)*1000
        submitted=result['submitted_id']
        with store.read() as conn:
            operations=[dict(r) for r in conn.execute("SELECT * FROM admin_operations WHERE action='goal_merge' ORDER BY id")]
            merges=[{'target':keys.get(int(row['object_id']),'incoming'),
                     'source':keys.get(json.loads(row['details_json'])['merged_id'],'incoming')}
                    for row in operations]
            possible=[keys.get(r[0],'incoming') for r in conn.execute('''SELECT CASE WHEN goal_a=? THEN goal_b ELSE goal_a END
                FROM goal_duplicates WHERE status='possible' AND (goal_a=? OR goal_b=?)''',(submitted,submitted,submitted))]
            calls=[dict(r) for r in conn.execute("SELECT * FROM model_calls WHERE model_kind='goal_dedup_judge' ORDER BY id")]
            snapshot=[dict(r) for r in conn.execute('SELECT id,content,state,deadline,merged_into,revision FROM goals ORDER BY id')]
        degraded=(result['dedup']['status']=='pending' or any(c['result_category']!='success' for c in calls))
        item={'origin':incoming['origin'],'decision':result['dedup'],'merges':merges,'possible_targets':possible,
              'duration_ms':duration_ms,'degraded':degraded,'model_calls':len(calls),
              'prompt_tokens':sum(c['prompt_tokens'] or 0 for c in calls),
              'completion_tokens':sum(c['completion_tokens'] or 0 for c in calls),
              'reasoning_tokens':sum(c['reasoning_tokens'] or 0 for c in calls)}
        save(directory/'result.json',{'receipt':result,'goals':snapshot,'operations':operations,'calls':calls})
        save(directory/'requests.json',gateway.records if gateway else [])
        save(directory/'model-contents.json',gateway.raw_contents if gateway else [])
        return score_case(case,item)
    finally:
        if gateway:
            gateway.close()
        if client:
            client.close()
        store.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus',type=Path,default=ROOT/'evals/goal_dedup_v1.json')
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--method',choices=('A','B','C','C2'),required=True)
    parser.add_argument('--budget-seconds',type=float,default=60)
    parser.add_argument('--fake',choices=('same','different','uncertain','timeout','invalid'),
                        help='offline transport self-check; no configuration read or network calls; excluded from selection')
    args=parser.parse_args()
    out=args.out.resolve()
    if out.is_relative_to(ROOT):
        parser.error('--out must be outside the repository')
    if not 0<args.budget_seconds<=60:
        parser.error('--budget-seconds must be >0 and <=60')
    out.mkdir(parents=True,exist_ok=False)
    corpus_bytes=args.corpus.read_bytes()
    corpus=json.loads(corpus_bytes)
    if corpus.get('format_version')!=1:
        parser.error('unsupported corpus format')
    configs=None
    if args.method!='A':
        configs=({'chat':ModelConfig('https://goal-probe.invalid/v1','','fake-goal-dedup')}
                 if args.fake else load_test_models())
    if configs:
        configs['goal_dedup_judge']=replace(configs['chat'],dimensions=None,reasoning_effort='high')
    metadata={'method':args.method,'simulation':bool(args.fake),'fake_verdict':args.fake,'budget_seconds':args.budget_seconds,'corpus_sha256':hashlib.sha256(corpus_bytes).hexdigest(),
              'prompt_sha256':hashlib.sha256((ROOT/'src/iris/prompts/goal_dedup_judge_v1.md').read_bytes()).hexdigest(),
              'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'scoring_sha256':hashlib.sha256(Path(__file__).with_name('scoring.py').read_bytes()).hexdigest(),
              'source_sha256':hashlib.sha256(b''.join(path.relative_to(ROOT).as_posix().encode()+path.read_bytes()
                    for path in sorted((ROOT/'src/iris').rglob('*.py')))).hexdigest(),
              'expected_cases':len(corpus['cases']),'timezone':corpus['timezone'],
              'model':configs['chat'].model if configs else None,'reasoning_effort':'high' if configs else None}
    save(out/'metadata.json',metadata)
    rows=[]
    for case in corpus['cases']:
        row=run_case(case,corpus,out,args.method,configs,args.budget_seconds,fake=args.fake)
        rows.append(row)
        print(json.dumps({'case':case['id'],'method':args.method,'duration_ms':round(row['duration_ms'],1),
                          'degraded':row['degraded']},ensure_ascii=False),flush=True)
        if row['degraded']:
            break  # Preserve invalid run; a retry must use a fresh output directory.
    summary={**metadata,**report(rows),'complete':len(rows)==len(corpus['cases'])}
    summary['valid']=summary['valid'] and summary['complete']
    save(out/'report.json',summary)
    print(json.dumps({k:v for k,v in summary.items() if k not in ('results','categories','classes')},ensure_ascii=False),flush=True)
    return 0 if summary['valid'] else 2


if __name__=='__main__':
    raise SystemExit(main())
