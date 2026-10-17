"""Frozen-corpus consolidation runner. No product CLI integration; no model judging.

uv run python evals/consolidation_eval/run.py --method broad --out /external/run
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))
from iris.consolidation import METHODS, RESOLUTIONS, MERGE_PROMPT_VERSION, Consolidation, snapshot
from iris.db import Store, dumps
from iris.evaluation import _json_sha256, _read_json, _write_json
from iris.maintenance import Maintenance
from iris.memory_ops import delete_memory, edit_memory, manage_memory
from iris.models import Gateway, load_test_models
from iris.model_health import ModelHealth

SOURCE_FIELDS=('key','entry_id','entry_kind','sender','kind','at','text')
SCORING_VERSION='consolidation_scoring_v1'
SCORING=(ROOT/'src/iris/prompts'/f'{SCORING_VERSION}.md').read_text(encoding='utf-8')
REPORT_ONLY_SCORING=(Path(__file__).parent/'report_only_scoring.md').read_text(encoding='utf-8')
EVALUATION_POLICY='merge_scoring_v2'
DETAIL_SCORING_V1=(Path(__file__).parent/'merge_details_scoring_v1.md').read_text(encoding='utf-8')
DETAIL_SCORING=(Path(__file__).parent/'merge_details_scoring_v2.md').read_text(encoding='utf-8')
WRITE_AUDIT=(Path(__file__).parent/'write_audit_v2.md').read_text(encoding='utf-8')
# Completed model refusals / malformed output are quality observations. Anything
# else in the call ledger (including unfinished calls) is an execution failure.
QUALITY_CALL_RESULTS={'success','invalid_output','item_error','content_rejection'}


def source_hash():
    paths=sorted([*ROOT.joinpath('src/iris').rglob('*.py'),*ROOT.joinpath('src/iris').rglob('*.sql'),
                  *ROOT.joinpath('src/iris').rglob('*.md'),*ROOT.joinpath('src/iris').rglob('*.json'),
                  *Path(__file__).parent.glob('*.py'),*Path(__file__).parent.glob('*.json'),
                  *Path(__file__).parent.glob('*.md')])
    return hashlib.sha256(b''.join(str(p.relative_to(ROOT)).encode()+p.read_bytes() for p in paths)).hexdigest()


def load_corpus(path):
    try:
        data=_read_json(path)
    except ValueError:
        data=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    cases=data['cases'] if isinstance(data,dict) else data
    if not isinstance(cases,list) or not cases or len({c['id'] for c in cases})!=len(cases):
        raise ValueError('invalid corpus cases')
    for c in cases:
        if not datetime.fromisoformat(c['now']).tzinfo:
            raise ValueError('now requires timezone')
        keys={m['key'] for m in c['memories']}
        if len(keys)!=len(c['memories']):
            raise ValueError('duplicate memory key')
        mutual_pairs(c['expected'],keys)
        for field in ('merges','no_merge'):
            if any(len(p)!=2 or not set(p)<=keys for p in c['expected'].get(field,[])):
                raise ValueError('invalid pair label')
    return cases


def mutual_pairs(expected, keys):
    """Optional labels are unordered, distinct memory-key pairs, never judgments."""
    pairs=expected.get('mutual_details',[])
    if not isinstance(pairs,list):
        raise ValueError('invalid mutual_details labels')
    seen=set()
    for pair in pairs:
        if (not isinstance(pair,list) or len(pair)!=2 or
            any(not isinstance(key,str) or key not in keys for key in pair) or len(set(pair))!=2):
            raise ValueError('invalid mutual_details pair')
        frozen=frozenset(pair)
        if frozen in seen:
            raise ValueError('duplicate mutual_details pair')
        seen.add(frozen)
    return pairs


def protected_merge_error(check):
    return (check['merge_allowed'] is False and check['outcome']=='failed'
            and check.get('failure_code')=='merge_forbidden'
            and (check.get('result') or {}).get('decision')=='merge')


def run_validity(row):
    """MG2-S: keep model quality outcomes; reject incomplete/infrastructure runs.

    Read execution receipts rather than treating every deferred work item as a
    dropped attempt. Blocked decisions and malformed completed answers retain
    their full quality denominator. Unknown execution failures remain invalid.
    """
    phase=row['report']['consolidation']
    checks={c['check_id']:c for c in row['input']['merge_checks']}
    works={w['check_id']:w for w in row['work_results']}
    failures=[i for i in row['report']['items'] if i['outcome']=='failed']
    def quality_failure(item):
        if item['phase']!='consolidation':
            return False
        if item['reason'] in ('unsafe_write','invalid_output'):
            return True
        cid='co-'+item['item_key']
        if cid in checks and protected_merge_error(checks[cid]):
            return True
        calls=[c for c in row['calls'] if 'co-'+str(c['work_id'])==cid]
        return (item['reason']=='model_error' and bool(calls)
                and all(c['result'] in QUALITY_CALL_RESULTS for c in calls))
    quality_ids={'co-'+i['item_key'] for i in failures if quality_failure(i)}
    blocked_ids={cid for cid,c in checks.items() if c['outcome']=='skipped'
                 and c['reason']=='unresolved_conflict' and c.get('blocked_by')}
    # A normal CAS rejection is an observed no-write result too (e.g. an
    # earlier merge changed the input). It is not a missing model attempt.
    safe_skip_ids=blocked_ids | {cid for cid,w in works.items() if w['outcome']=='skipped'
                                and w['reason']=='revision_conflict' and w['state']=='obsolete'}
    reasons=[]
    if any(c['result'] not in QUALITY_CALL_RESULTS for c in row['calls']):
        reasons.append('model_call_failed')
    formats=[i for i in failures if i['phase']=='consolidation' and i['reason']=='invalid_output']
    if formats and not row['calls']:
        reasons.append('output_failure_without_call')
    if phase['skip_reason']:
        reasons.append('model_phase_skipped')
    if any(not quality_failure(i) for i in failures):
        reasons.append('execution_failure')
    if any(a['status']=='skipped' and a.get('check_id') not in safe_skip_ids for a in row['input']['actual_actions']):
        reasons.append('work_skipped')
    def deferred(w):
        return (w['outcome'] in (None,'failed','skipped') and w['reason']!='unsafe_write'
                and not w['details'].get('terminal',False))
    accounted=sum(deferred(w) and cid in quality_ids|safe_skip_ids for cid,w in works.items())
    if phase['deferred']>accounted or any(w['outcome'] is None for w in works.values()):
        reasons.append('unattempted_work')
    if not row.get('valid',True) and not reasons:
        reasons.append('unexplained_original_invalid')
    return {'case_id':row['input']['case']['id'],'original_valid':row.get('valid',not reasons),
            'valid':not reasons,'format_failures':len(formats),'invalid_reasons':reasons}


def blocker_type(work):
    result=work['result'] or {}
    outcome=result.get('merge_detail_check',{}).get('outcome')
    if outcome in ('mutual_details','invalid_details'):
        return outcome
    if result.get('decision')=='conflict':
        return 'conflict'
    return work['failure_code'] or work['reason'] or 'unattempted'


def seed(store,case):
    key_map={}
    source_map={}
    with store.write() as conn:
        for s in case['subjects']:
            if s['id']=='self':
                continue
            conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)",(s['id'],s['name'],case['now']))
            conn.executemany('INSERT INTO subject_aliases(subject_id,alias) VALUES(?,?)',[(s['id'],a) for a in s.get('aliases',[])])
        def source(s):
            s={key:s[key] for key in SOURCE_FIELDS}
            if s['key'] in source_map:
                if source_map[s['key']][1]!=s:
                    raise ValueError('inconsistent source key')
                return source_map[s['key']][0]
            conn.execute("INSERT OR IGNORE INTO entries(id,name,platform,kind) VALUES(?,?,'eval',?)",(s['entry_id'],s['entry_id'],s['entry_kind']))
            mid=conn.execute('''INSERT INTO messages(entry_id,kind,sender_subject_id,content,occurred_at,received_at,dedupe_key,learning_state)
                VALUES(?,?,?,?,?,?,?,'learned')''',(s['entry_id'],s['kind'],s['sender'],s['text'],s['at'],s['at'],s['key'])).lastrowid
            source_map[s['key']]=(mid,s)
            return mid
        for m in case['memories']:
            stamp=m['created_at']
            sources=[source(s) for s in m.get('sources',[])]
            mid=conn.execute('''INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,retention,
                lifecycle,pinned,event_time,created_at,updated_at,first_confirmed_at,last_confirmed_at,forgotten_at,world)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (m['content'],m['type'],m['speaker'],m['stance'],m['belief'],m['importance'],m['retention'],m['lifecycle'],int(m['pinned']),
                 m.get('event_time'),stamp,stamp,stamp,stamp,stamp if m['lifecycle']=='forgotten' else None,m.get('world','real'))).lastrowid
            key_map[m['key']]=mid
            conn.executemany('INSERT INTO memory_subjects VALUES(?,?)',[(mid,s) for s in m['about']])
            for sid in sources:
                conn.execute("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,?)",(mid,sid,stamp))
            if m.get('last_edit')=='admin':
                conn.execute('''INSERT INTO memory_revisions(memory_id,revision_before,revision_after,before_json,after_json,reason,actor,created_at)
                    VALUES(?,0,1,?,?,'manual edit','admin',?)''',(mid,dumps({'content':None}),dumps({'content':m['content']}),stamp))
        for m in case['memories']:
            for base in m.get('derived_from',[]):
                conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,1,?)",
                    (key_map[m['key']],key_map[base],m['created_at']))
        # Include new event evidence in the message map, but attach only at its event.
        for event in case.get('events',[]):
            for s in event.get('sources',[]):
                source(s)
    return key_map,source_map


def export_memory(m,key_map,members):
    reverse={v:k for k,v in key_map.items()}
    return {**{k:m[k] for k in ('id','content','type','belief','retention','importance','lifecycle','pinned','last_edit',
              'event_time','created_at','speaker','stance','about','revision','merged_into')},
            'original_keys':[k for k in key_map if k in members.get(m['id'],[reverse[m['id']]])],
            'sources':[{k:s[k] for k in ('key','entry_id','entry_kind','sender','kind','at','text')} for s in m['sources']],
            'derived_from':list(dict.fromkeys(reverse[s['id']] for s in m['derived_from'])),
            'annotations':[dict(a) for a in m['annotations']],
            'is_placeholder':m['merged_into'] is not None}


def capture(store,key_map,members):
    with store.read() as conn:
        return {'key_map':key_map.copy(),'memories':[export_memory(snapshot(conn,mid),key_map,members) for mid in key_map.values()]}


def case_run(case,dbpath,gateway_factory,method,resolution='report_only_v1'):
    if resolution not in RESOLUTIONS or (method!='broad'):
        raise ValueError('resolution comparison fixes the selected merge method to broad')
    started=time.monotonic()
    store=Store(dbpath)
    gateway=None
    try:
        store.set_setting('consolidation',{'enabled':True,'method':method,'max_calls':50,'resolution':resolution,'entry_profile_enabled':False})
        key_map,source_map=seed(store,case)
        members={v:[k] for k,v in key_map.items()}
        retention=[]
        event_actions=[]
        def change(mid,before,after,cause,reason,at,dep=(),events=(),action=None):
            if before==after:
                return
            retention.append({'index':len(retention),'at':at,'memory_id':mid,'original_keys':members[mid].copy(),
                'before':before,'after':after,'delta':after-before,'cause':cause,'dependency_keys':list(dep),
                'event_indexes':list(events),'action_id':action,'reason':reason})
        for index,event in enumerate(case.get('events',[])):
            mid=key_map[event['key']]
            before=capture(store,key_map,members)
            value=next(m for m in before['memories'] if m['id']==mid)
            at=event['at']
            with patch('iris.memory_ops.now',lambda:at):
                if event['op']=='delete':
                    assert delete_memory(store,mid,value['revision'],current=datetime.fromisoformat(at))
                elif event['op']=='forget':
                    assert manage_memory(store,mid,value['revision'],action='forget')
                elif event['op']=='edit':
                    actor=event.get('actor',event.get('last_edit','learning'))
                    assert edit_memory(store,mid,value['revision'],content=event['content'],actor=actor)
                    with store.write() as conn:
                        for s in event.get('sources',[]):
                            sid=source_map[s['key']][0]
                            if not conn.execute("SELECT 1 FROM sources WHERE memory_id=? AND message_id=?",(mid,sid)).fetchone():
                                conn.execute("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,?)",(mid,sid,at))
                else:
                    raise ValueError('unsupported event operation')
            after=capture(store,key_map,members)
            end=next(m for m in after['memories'] if m['id']==mid)
            action=f'event-{index}'
            event_actions.append({'action_id':action,'kind':'event_'+event['op'],'keys':[event['key']],'status':'committed',
                'before_memories':[value],'after_memories':[end],'report':'语料事件：'+event['op']})
            change(mid,value['retention'],end['retention'],'model_consolidation','语料 forget 事件的人工生命周期操作，非模型或依赖扣减',at,events=[index],action=action)
        clock=lambda:datetime.fromisoformat(case['now'])
        gateway=gateway_factory(store,clock)
        maintenance=Maintenance(store,gateway=gateway,clock=clock)
        rid=maintenance.request()
        # Stop precisely at the model phase, after all M2 deterministic operations.
        def reached_model():
            with store.read() as conn:
                return conn.execute('SELECT phase FROM maintenance_runs WHERE id=?',(rid,)).fetchone()[0]>=5
        maintenance.run(rid,stop=reached_model)
        before=capture(store,key_map,members)
        initial_report=maintenance.report(rid)
        reverse={v:k for k,v in key_map.items()}
        for item in initial_report['items']:
            d=item['details']
            if 'before' in d and 'after' in d:
                dep=[reverse[d['source_memory_id']]] if 'source_memory_id' in d else []
                events=[i for i,e in enumerate(case.get('events',[])) if e['key'] in dep]
                change(item['memory_id'],d['before'],d['after'],'m2_dependency' if item['phase']=='dependency' else 'daily_decay',
                    item['outcome'],item['created_at'],dep,events)
        # Observe the actual apply-time blocker set and a single allowlisted
        # exception code. Do not change decisions, transactions or error handling.
        observations={}
        original_apply=Consolidation._apply_work
        def observe_apply(engine,work,payload,result,config,blocked):
            ids=set(payload.get('pair_ids',[payload.get('target_id')]))
            observed={'blocked_pairs':sorted(sorted(pair) for pair in blocked if ids & pair),
                      'failure_code':None}
            observations[work['id']]=observed
            try:
                return original_apply(engine,work,payload,result,config,blocked)
            except ValueError as exc:
                if type(exc) is ValueError and exc.args==('merge_forbidden',):
                    observed['failure_code']='merge_forbidden'
                raise
        with patch.object(Consolidation,'_apply_work',observe_apply):
            maintenance.run(rid)
        report=maintenance.report(rid)
        merges=[]
        actions=event_actions.copy()
        for item in report['items']:
            if item['phase']!='consolidation':
                continue
            d=item['details']
            if not d.get('before_memories'):
                actions.append({'action_id':'failed-'+item['item_key'],'kind':item['outcome'],
                    'keys':[reverse[item['memory_id']]] if item['memory_id'] in reverse else [],
                    'status':'failed','before_memories':[],'after_memories':[],'report':(item['reason'] or '')+': '+d.get('rejection','')})
                continue
            old=[export_memory(m,key_map,members) for m in d['before_memories']]
            if item['outcome']=='merged':
                ids=[d['result_id'],*d['absorbed_ids']]
                original={k for mid in ids for k in members[mid]}
                members[d['result_id']]=[k for k in key_map if k in original]
                post=[export_memory(m,key_map,members) for m in d['after_memories']]
                merges.append({'action_id':d['action_id'],'input_keys':[k for k in key_map if k in original],
                    'input_ids':ids,'before_memories':[m for m in old if m['id'] in ids],
                    'result':next(m for m in post if m['id']==d['result_id']),
                    'absorbed_memories':[m for m in post if m['id'] in d['absorbed_ids']]})
            else:
                post=[export_memory(m,key_map,members) for m in d['after_memories']]
                actions.append({'action_id':d['action_id'],'kind':item['outcome'],'keys':[k for k in key_map if key_map[k] in [m['id'] for m in old]],
                    'status':'committed','before_memories':old,'after_memories':post,'report':d['report'],
                    'decision':d['decision'],'evidence':d['evidence'],'source_excerpts':d['source_excerpts'],
                    'conclusions':d['conclusions']})
            for old_m,new_m in zip(old,post,strict=True):
                change(old_m['id'],old_m['retention'],new_m['retention'],'model_consolidation',d['report'],item['created_at'],action=d['action_id'])
        with store.read() as conn:
            calls=[dict(r) for r in conn.execute('SELECT * FROM consolidation_calls WHERE run_id=? ORDER BY id',(rid,))]
            skipped=[dict(r) for r in conn.execute("SELECT * FROM consolidation_run_work WHERE run_id=? AND (outcome='skipped' OR outcome IS NULL)",(rid,))]
            for skip in skipped:
                work=conn.execute('SELECT payload_json FROM consolidation_work WHERE id=?',(skip['work_id'],)).fetchone()
                data=json.loads(work[0])
                old=[export_memory(m,key_map,members) for m in data['memories']]
                actions.append({'action_id':f'skip-{skip["work_id"]}','kind':'skipped','keys':[reverse[m['id']] for m in old],
                    'status':'skipped','before_memories':old,'after_memories':[], 'report':skip['reason'] or 'deferred',
                    'check_id':f'co-{skip["work_id"]}','reason':skip['reason']})
        checks=[]
        works=[]
        with store.read() as conn:
            for work in conn.execute('''SELECT w.*,rw.outcome,rw.reason,rw.details_json FROM consolidation_work w
                JOIN consolidation_run_work rw ON rw.work_id=w.id WHERE rw.run_id=? ORDER BY w.id''',(rid,)):
                data=json.loads(work['payload_json'])
                result=json.loads(work['result_json']) if work['result_json'] else None
                observation=observations.get(work['id'],{})
                item={'check_id':f'co-{work["id"]}','kind':work['kind'],'state':work['state'],
                      'outcome':work['outcome'],'reason':work['reason'],'details':json.loads(work['details_json']),
                      'failure_code':observation.get('failure_code'),
                      'memory_ids':data.get('pair_ids',[data.get('target_id')]),'result':result}
                works.append(item)
                if work['kind']=='pair':
                    checks.append({'check_id':item['check_id'],'pair_ids':data['pair_ids'],
                        'pair_keys':[reverse[mid] for mid in data['pair_ids']],
                        'merge_allowed':data['merge_allowed'],'merge_exclusion':data['merge_exclusion'],
                        'before_memories':[export_memory(m,key_map,{}) for m in data['memories'] if m['id'] in data['pair_ids']],
                        'result':result,'outcome':work['outcome'],'reason':work['reason'],
                        'failure_code':item['failure_code'],'blocked_by':[]})
        for check in checks:
            if check['reason']!='unresolved_conflict':
                continue
            observed=observations.get(int(check['check_id'][3:]),{})
            for pair in observed.get('blocked_pairs',[]):
                blockers=[w for w in works if set(w['memory_ids'])==set(pair)]
                # Planning failures are recorded in the maintenance report, not
                # in the pair queue. They still invalidate execution below.
                if not blockers:
                    blockers=[{'check_id':None,'failure_code':'planning_error','reason':None,'result':None}]
                for blocker in blockers:
                    check['blocked_by'].append({'check_id':blocker['check_id'],'pair_ids':pair,
                        'pair_keys':[reverse[mid] for mid in pair],'type':blocker_type(blocker)})
        for action in actions:
            check=next((c for c in checks if c['check_id']==action.get('check_id')),None)
            if check:
                action['blocked_by']=check['blocked_by']
        after=capture(store,key_map,members)
        after['report']='\n'.join(a['report'] for a in actions if a['status']=='committed' and not a['kind'].startswith('event_'))
        enriched=copy.deepcopy(case)
        for m in enriched['memories']:
            m.update(annotations=[],is_placeholder=False,merged_into=None)
        payload={'role':{'name':'Iris','self_subject_id':'self'},'case':enriched,'before':before,'actual_merges':merges,
            'actual_actions':actions,'after':after,'retention_changes':retention,
            'merge_checks':checks,'merge_version':MERGE_PROMPT_VERSION,'evaluation_policy':EVALUATION_POLICY}
        failures=[i for i in report['items'] if i['outcome']=='failed']
        safety_rejections=sum(i['reason']=='unsafe_write' for i in failures)
        row={'input':payload,'calls':calls,'report':report,'work_results':works,
             'safety_rejections':safety_rejections,'elapsed_seconds':time.monotonic()-started}
        row['valid']=run_validity(row)['valid']
        return row
    finally:
        if gateway and callable(getattr(gateway,'close',None)):
            gateway.close()
        store.close()


def export(rows,out,metadata):
    if out.exists() and any(out.iterdir()):
        raise ValueError('materials directory must be empty')
    out.mkdir(parents=True,exist_ok=True)
    policies={row['input'].get('evaluation_policy') for row in rows}
    if len(policies)!=1 or policies-{None,EVALUATION_POLICY}:
        raise ValueError('mixed or unknown evaluation policy')
    policy=next(iter(policies))
    manifest={'format_version':1,'evaluation':'consolidation','run':metadata,
              'run_sha256':_write_json(out/'run.json',{'rows':rows}),'cases':[]}
    if policy:
        manifest['evaluation_policy']=policy
    (out/'scoring.md').write_text(SCORING,encoding='utf-8')
    manifest['scoring_sha256']=hashlib.sha256(SCORING.encode()).hexdigest()
    for i,row in enumerate(rows):
        document={'format_version':1,'evaluation':'consolidation','case_id':row['input']['case']['id'],
            'corpus_sha256':metadata['corpus']['sha256'],'source_sha256':metadata['source_sha256'],
            'scoring_version':SCORING_VERSION,'input':row['input']}
        filename=f'cases/{i:04}.json'
        manifest['cases'].append({'case_id':document['case_id'],'file':filename,'sha256':_write_json(out/filename,document),'judgment_file':f'{i:04}.json'})
    audit=WRITE_AUDIT if policy else (Path(__file__).parent/'write_audit.md').read_text(encoding='utf-8')
    (out/'write-audit.md').write_text(audit,encoding='utf-8')
    manifest['write_audit_sha256']=hashlib.sha256(audit.encode()).hexdigest()
    if metadata.get('resolution')=='report_only_v1':
        (out/'report-only-scoring.md').write_text(REPORT_ONLY_SCORING,encoding='utf-8')
        manifest['report_only_scoring_sha256']=hashlib.sha256(REPORT_ONLY_SCORING.encode()).hexdigest()
    if all('merge_checks' in row['input'] for row in rows):
        detail_scoring=DETAIL_SCORING if policy else DETAIL_SCORING_V1
        (out/'merge-details-scoring.md').write_text(detail_scoring,encoding='utf-8')
        manifest['merge_details_scoring_sha256']=hashlib.sha256(detail_scoring.encode()).hexdigest()
    manifest['materials_sha256']=_json_sha256(manifest)
    _write_json(out/'manifest.json',manifest)
    _write_json(out/'round-template.json',{'materials_sha256':manifest['materials_sha256']})
    _write_json(out/'write-audit-template.json',{'materials_sha256':manifest['materials_sha256'],
        'cases':[{'case_id':e['case_id'],'has_damage':None,'reason':''} for e in manifest['cases']]})
    if 'merge_details_scoring_sha256' in manifest:
        _write_json(out/'detail-judgment-template'/'manifest.json',{'materials_sha256':manifest['materials_sha256']})
        for entry,row in zip(manifest['cases'],rows,strict=True):
            p=row['input']
            _write_json(out/'detail-judgment-template'/entry['judgment_file'],{
                'format_version':1,'case_id':p['case']['id'],
                'merge_detail_results':[{'action_id':m['action_id'],'all_facts_preserved':None,'reason':''} for m in p['actual_merges']],
                'refusal_results':[{'index':i,'pair':pair,'no_false_refusal':None,'reason':''}
                                   for i,pair in enumerate(p['case']['expected'].get('merges',[]))]})
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus',type=Path,default=ROOT/'evals/consolidation_v1.json')
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--method',choices=METHODS,required=True)
    parser.add_argument('--resolution',choices=RESOLUTIONS,default='report_only_v1')
    parser.add_argument('--case',action='append',default=[])
    args=parser.parse_args()
    if args.method!='broad':
        parser.error('resolution comparison requires --method broad')
    cases=load_corpus(args.corpus)
    if args.case:
        cases=[c for c in cases if c['id'] in args.case]
    if not cases:
        parser.error('no cases')
    out=args.out.resolve()
    if out.is_relative_to(ROOT):
        parser.error('full artifacts must be outside repository')
    out.mkdir(parents=True,exist_ok=False)
    configs=load_test_models()
    chat=configs['chat']
    if chat.reasoning_effort!='high':
        raise ValueError('CO probe requires explicit high reasoning effort')
    def factory(store,clock):
        return Gateway(configs,store,health=ModelHealth(store,configs,clock=clock),clock=clock)
    rows=[]
    source=source_hash()
    for case in cases:
        row=case_run(case,out/(case['id']+'.db'),factory,args.method,args.resolution)
        rows.append(row)
        _write_json(out/(case['id']+'.json'),row)
        print(json.dumps({'case':case['id'],'valid':row['valid'],'calls':len(row['calls']),
            'merges':len(row['input']['actual_merges']),'seconds':round(row['elapsed_seconds'],2)}),flush=True)
    if source_hash()!=source:
        raise ValueError('source changed during model evaluation')
    metadata={'corpus':{'sha256':_json_sha256(cases),'file_sha256':hashlib.sha256(args.corpus.read_bytes()).hexdigest(),'cases':len(cases)},
        'source_sha256':source,'scoring_version':SCORING_VERSION,'method':args.method,'resolution':args.resolution,
        'merge_version':MERGE_PROMPT_VERSION,'evaluation_policy':EVALUATION_POLICY,'chat_model':chat.model,
        'chat_reasoning_effort':chat.reasoning_effort,'endpoint_sha256':hashlib.sha256(chat.base_url.encode()).hexdigest(),
        'calls':sum(len(r['calls']) for r in rows),'elapsed_seconds':sum(r['elapsed_seconds'] for r in rows),'valid':all(r['valid'] for r in rows)}
    export(rows,out/'materials',metadata)
    _write_json(out/'summary.json',metadata)
    print(json.dumps(metadata,ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
