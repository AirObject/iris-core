"""Isolated explicit-input execution and immutable external judging bundles.

Run from a checkout with `uv run python -m evals.entry_profile_eval`.
No default corpus, corpus globbing, embedded grading model, or score-based reuse.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from iris.db import Store, dumps
from iris.entry_profile import EntryProfileEngine, VIOLATIONS, admin_edit, _instant
from iris.evaluation import _json_sha256, _read_json, _write_json, _verified_material_file, _percentile
from iris.models import Gateway, ModelReply, load_test_models

FORMAT_VERSION=1
SCORING_VERSION='entry_profile_scoring_v1'
HERE=Path(__file__).resolve().parent


def _texts(value, name):
    if not isinstance(value,list) or any(not isinstance(x,str) or not x.strip() for x in value):
        raise ValueError(name+' must be an array of nonempty strings')


def _group_cases(document):
    """Frozen groups format: each target gets a fresh DB containing all entries."""
    groups=document.get('groups')
    if not isinstance(groups,list) or not groups or 'cases' in document:
        raise ValueError('expected nonempty groups or cases, exclusively')
    entries=[]; messages=[]; ids=set()
    for group in groups:
        key=group['id']
        if not isinstance(key,str) or not key or key in ids:
            raise ValueError('duplicate or invalid group id')
        ids.add(key)
        entries.append({'id':key,'kind':group['entry_kind'],'name':group.get('name',key)})
        previous={}
        for message in group['messages']:
            mid=message['id']; sender=message['sender']
            if not isinstance(mid,str) or not mid or mid in previous:
                raise ValueError('duplicate or invalid message key')
            if (sender=='self') != (message['kind']=='self_output'):
                raise ValueError('self messages must be self_output')
            quote=message.get('quote')
            if quote is not None:
                original=previous.get(quote.get('message_id'))
                if (not original or quote.get('sender')!=original['sender'] or quote.get('text')!=original['text']
                        or _instant(original['at'])>_instant(message['at'])):
                    raise ValueError('quote must match an earlier message in the same entry')
            messages.append({**message,'id':key+':'+mid,'entry_id':key,
                **({'quote':{**quote,'message_id':key+':'+quote['message_id']}} if quote else {})})
            previous[mid]=message
    messages.sort(key=lambda m:(_instant(m['at']),m['entry_id'],m['id']))
    return [{**group,'entry':entry,'other_entries':[e for e in entries if e['id']!=entry['id']],
             'messages':messages,'as_of':group['now'],'timezone':document.get('timezone','Asia/Shanghai'),
             'role_name':document.get('role_name','Iris')}
            for group,entry in zip(groups,entries,strict=True)]


def load_corpus(path):
    """An explicit file only; annotations are never loaded as facts or prompts."""
    document=_read_json(Path(path))
    if (not isinstance(document,dict) or type(document.get('format_version')) is not int
            or document['format_version']!=1):
        raise ValueError('expected format_version=1')
    cases=_group_cases(document) if 'groups' in document else document.get('cases')
    if not isinstance(cases,list) or not cases:
        raise ValueError('expected nonempty cases')
    ids=set()
    for case in cases:
        if not isinstance(case,dict) or not isinstance(case.get('id'),str) or not case['id'] or case['id'] in ids:
            raise ValueError('duplicate or invalid case id')
        ids.add(case['id']); current=_instant(case['as_of']); ZoneInfo(case.get('timezone','Asia/Shanghai'))
        entries=[case['entry'],*case.get('other_entries',[])]
        entry_ids={e['id'] for e in entries}
        if len(entry_ids)!=len(entries) or any(not isinstance(e['id'],str) or not e['id'] or e['kind'] not in ('group','live','private') for e in entries):
            raise ValueError('invalid entry declarations')
        if not isinstance(case.get('messages'),list):
            raise ValueError('messages must be an array')
        message_ids=set()
        for message in case['messages']:
            key=message.get('id')
            if not isinstance(key,str) or not key or key in message_ids:
                raise ValueError('duplicate or invalid message key')
            message_ids.add(key)
            if (not isinstance(message.get('text'),str) or message.get('entry_id',case['entry']['id']) not in entry_ids
                    or _instant(message['at'])>current or message.get('kind','message') not in ('message','self_output','action_result','event')):
                raise ValueError('invalid message content, entry, kind or time')
        for memory in case.get('memories',[]):
            if (not isinstance(memory.get('content'),str) or not isinstance(memory.get('sources'),list)
                    or any(source not in message_ids for source in memory['sources'])):
                raise ValueError('invalid memory sources')
        _texts(case.get('must_cover',[]),'must_cover'); _texts(case.get('forbidden',[]),'forbidden')
        if 'initial_profile' in case and not isinstance(case['initial_profile'],str):
            raise ValueError('initial_profile must be text')
    return cases


def _load_case(store, case):
    stamp=case['as_of']; entry_id=case['entry']['id']; mids={}
    store.set_setting('timezone',case.get('timezone','Asia/Shanghai'))
    store.set_setting('role_name',case.get('role_name','Iris'))
    with store.write() as conn:
        for entry in [case['entry'],*case.get('other_entries',[])]:
            visibility=entry.get('visibility','shared'); visible_in=entry.get('visible_in',[])
            if visibility not in ('shared','entry_only','entries') or not isinstance(visible_in,list):
                raise ValueError('invalid entry visibility')
            conn.execute('INSERT INTO entries(id,name,platform,kind,visibility,visible_in_json) VALUES(?,?,?,?,?,?)',
                         (entry['id'],entry.get('name',entry['id']),'eval',entry['kind'],visibility,dumps(visible_in)))
        for message in case['messages']:
            sender=message.get('sender','speaker')
            if not isinstance(sender,str) or not sender:
                raise ValueError('invalid sender')
            conn.execute('INSERT OR IGNORE INTO subjects(id,kind,name,created_at) VALUES(?,?,?,?)',
                         (sender,'self' if sender=='self' else 'scene' if sender=='scene' else 'person',sender,stamp))
            mids[message['id']]=conn.execute('''INSERT INTO messages(entry_id,kind,sender_subject_id,content,occurred_at,
                received_at,dedupe_key,quote_author_subject_id,quote_content) VALUES(?,?,?,?,?,?,?,?,?)''',
                (message.get('entry_id',entry_id),message.get('kind','message'),sender,message['text'],message['at'],
                 message['at'],message['id'],message.get('quote',{}).get('sender'),message.get('quote',{}).get('text'))).lastrowid
        for memory in case.get('memories',[]):
            lifecycle=memory.get('lifecycle','active')
            if lifecycle not in ('active','forgotten','deleted') or type(memory.get('importance',50)) is not int or not 0<=memory.get('importance',50)<=100:
                raise ValueError('invalid memory metadata')
            mid=conn.execute('''INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,retention,
                lifecycle,entry_id,created_at,updated_at,first_confirmed_at,last_confirmed_at)
                VALUES(?,'事实','self',?,90,?,80,?,?,?,?,?,?)''',(memory['content'],memory.get('stance','亲历'),
                memory.get('importance',50),lifecycle,entry_id,stamp,stamp,stamp,stamp)).lastrowid
            for key in dict.fromkeys(memory['sources']):
                conn.execute("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,?)",(mid,mids[key],stamp))
    if case.get('initial_profile'):
        admin_edit(store,entry_id,case['initial_profile'],expected_version=0,clock=lambda:_instant(stamp))


def _source_hash():
    iris=Path(__import__('iris').__file__).parent
    paths=sorted([*iris.rglob('*.py'),*iris.joinpath('migrations').glob('*.sql'),
                  *iris.joinpath('prompts').glob('*.md'),*HERE.glob('*.py'),HERE/'scoring.md'])
    # Deliberately excludes evals corpora, credentials and external material directories.
    return _json_sha256([(str(p.relative_to(iris)) if p.is_relative_to(iris) else 'entry_profile_eval/'+p.name,
                          hashlib.sha256(p.read_bytes()).hexdigest()) for p in paths])


def _model_metadata(configs):
    return {kind:{'model':config.model,'endpoint_sha256':hashlib.sha256(config.base_url.encode()).hexdigest(),
                  'reasoning_effort':config.reasoning_effort,'dimensions':config.dimensions} for kind,config in configs.items()}


def _redacted(value, configs):
    encoded=dumps(value)
    for config in configs.values():
        if config.api_key:
            encoded=encoded.replace(config.api_key,'[REDACTED]')
    return json.loads(encoded)


def _payload(row):
    profile=row['result'].get('version')
    return {'entry':row['case']['entry'],'as_of':row['case']['as_of'],
            'profile':profile,'evidence':profile['material']['evidence'] if profile else None,
            'previous':profile['material'].get('previous','') if profile else '',
            'must_cover':row['case'].get('must_cover',[]),'forbidden':row['case'].get('forbidden',[]),
            'status':row['result']['status'],'reason':row['result'].get('reason')}


def _document(row, metadata):
    return {'format_version':1,'evaluation':'entry_profile','case_id':row['case']['id'],
            'source_sha256':metadata['source_sha256'],'corpus_sha256':metadata['corpus_sha256'],
            'scoring_version':SCORING_VERSION,'input':_payload(row)}


def run_eval(corpus, out, *, configs=None, gateway_factory=Gateway, extra_input=None):
    cases=load_corpus(corpus); configs=configs or {}; root=Path(out)
    root.mkdir(parents=True,exist_ok=True)
    directory=root/('entry-profile-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid4().hex[:8])
    directory.mkdir()
    scoring=(HERE/'scoring.md').read_text(encoding='utf-8')
    metadata={'source_sha256':_source_hash(),'corpus_sha256':_json_sha256(cases),
        'corpus_file_sha256':hashlib.sha256(Path(corpus).read_bytes()).hexdigest(),
        'models':_model_metadata(configs),'extra_input_sha256':_json_sha256(extra_input),
        'prompt_versions':['entry_profile_generate_v1','entry_profile_check_v1'],
        'scoring_version':SCORING_VERSION,'scoring_sha256':hashlib.sha256(scoring.encode()).hexdigest(),
        'timeouts_seconds':{'generate':120,'check':120}}
    metadata['input_sha256']=_json_sha256(metadata)
    rows=[]; started=time.monotonic()
    # Always a fresh DB/run: no completed result is selected or reused by score.
    for index,case in enumerate(cases):
        store=Store(directory/'databases'/f'{index:04}.db'); gateway=None
        try:
            _load_case(store,case)
            gateway=gateway_factory(configs,store)
            engine=EntryProfileEngine(store,gateway,clock=lambda:_instant(case['as_of']))
            with store.read() as conn:
                saved=conn.execute('SELECT current_version FROM entry_profile_settings WHERE entry_id=?',(case['entry']['id'],)).fetchone()
            result=engine.regenerate(case['entry']['id'],expected_version=saved[0] if saved else 0)
            with store.read() as conn:
                attempts=[dict(r) for r in conn.execute('SELECT * FROM entry_profile_attempts ORDER BY id')]
                calls=[dict(r) for r in conn.execute('SELECT * FROM model_calls ORDER BY id')]
            rows.append({'case':case,'result':result,'attempts':attempts,'calls':calls,'complete':result['status']!='failed' and result.get('reason')!='timeout'})
        except Exception as error:
            # Never echo exception text from providers, payloads or configuration.
            with store.read() as conn:
                attempts=[dict(r) for r in conn.execute('SELECT * FROM entry_profile_attempts ORDER BY id')]
                calls=[dict(r) for r in conn.execute('SELECT * FROM model_calls ORDER BY id')]
            rows.append({'case':case,'result':{'status':'failed','reason':type(error).__name__,'version':None},
                         'attempts':attempts,'calls':calls,'complete':False})
        finally:
            if gateway and callable(getattr(gateway,'close',None)):
                gateway.close()
            store.close()
    rows=_redacted(rows,configs)
    materials=directory/'judging-materials'; materials.mkdir()
    manifest={'format_version':1,'evaluation':'entry_profile','run':metadata,
              'run_sha256':_write_json(materials/'run.json',{'rows':rows}),'cases':[]}
    (materials/'scoring.md').write_text(scoring,encoding='utf-8')
    manifest['scoring_sha256']=hashlib.sha256((materials/'scoring.md').read_bytes()).hexdigest()
    for index,row in enumerate(rows):
        name=f'cases/{index:04}.json'
        digest=_write_json(materials/name,_document(row,metadata))
        manifest['cases'].append({'case_id':row['case']['id'],'file':name,'sha256':digest,'judgment_file':f'{index:04}.json'})
    manifest['materials_sha256']=_json_sha256(manifest)
    _write_json(materials/'manifest.json',manifest)
    _write_json(materials/'round-template.json',{'materials_sha256':manifest['materials_sha256']})
    report={**metadata,'rows':rows,'materials':str(materials.resolve()),'elapsed_seconds':time.monotonic()-started,
            'metrics':_metrics(rows)}
    _write_json(directory/'report.json',report)
    return report


def _load_materials(path):
    path=Path(path); manifest=_read_json(path/'manifest.json')
    try:
        if manifest['format_version']!=1 or manifest['evaluation']!='entry_profile':
            raise ValueError('unsupported materials format')
        if manifest['materials_sha256']!=_json_sha256({k:v for k,v in manifest.items() if k!='materials_sha256'}):
            raise ValueError('manifest fingerprint mismatch')
        rows=_read_json(_verified_material_file(path,'run.json',manifest['run_sha256']))['rows']
        scoring=_verified_material_file(path,'scoring.md',manifest['scoring_sha256'])
        meta=manifest['run']
        if (meta['corpus_sha256']!=_json_sha256([r['case'] for r in rows]) or meta['scoring_version']!=SCORING_VERSION
                or meta['scoring_sha256']!=hashlib.sha256(scoring.read_bytes()).hexdigest()
                or meta['input_sha256']!=_json_sha256({k:v for k,v in meta.items() if k!='input_sha256'})):
            raise ValueError('run input fingerprint mismatch')
        if len(rows)!=len(manifest['cases']):
            raise ValueError('material case count mismatch')
        for index,(entry,row) in enumerate(zip(manifest['cases'],rows,strict=True)):
            if entry['file']!=f'cases/{index:04}.json' or entry['judgment_file']!=f'{index:04}.json':
                raise ValueError('invalid material filename')
            document=_read_json(_verified_material_file(path,entry['file'],entry['sha256']))
            if entry['case_id']!=row['case']['id'] or document!=_document(row,meta):
                raise ValueError('material input differs from run')
    except (KeyError,TypeError) as error:
        raise ValueError('invalid materials structure') from error
    return manifest,rows


def _validate(vote,row):
    if not isinstance(vote,dict) or set(vote)!={'sentence_results','must_cover','forbidden'}:
        raise ValueError('invalid judgment fields')
    profile=row['result'].get('version'); sentences=profile['sentences'] if profile else []
    if not isinstance(vote['sentence_results'],list) or len(vote['sentence_results'])!=len(sentences):
        raise ValueError('judgment sentence count mismatch')
    for index,result in enumerate(vote['sentence_results'],1):
        if (not isinstance(result,dict) or set(result)!={'index','supported','violations','reason'}
                or type(result['index']) is not int or result['index']!=index or type(result['supported']) is not bool
                or not isinstance(result['reason'],str) or not result['reason'].strip()
                or not isinstance(result['violations'],list)
                or result['violations']!=[v for v in VIOLATIONS if v in result['violations']]
                or ('无依据' in result['violations'] and result['supported'])):
            raise ValueError('invalid sentence judgment')
    for name in ('must_cover','forbidden'):
        if not isinstance(vote[name],list) or len(vote[name])!=len(row['case'].get(name,[])) or any(type(x) is not bool for x in vote[name]):
            raise ValueError('invalid reference judgments')
    if profile is None and any(vote['must_cover']):
        raise ValueError('missing profiles cannot cover reference points')
    return vote


def _combine(votes):
    if len(votes)==1:
        return votes[0],[]
    a,b=votes; combined={'sentence_results':[],'must_cover':[],'forbidden':[]}; differences=[]
    for index,(left,right) in enumerate(zip(a['sentence_results'],b['sentence_results'],strict=True),1):
        combined['sentence_results'].append({'index':index,'supported':left['supported'] and right['supported'],
            'violations':[v for v in VIOLATIONS if v in left['violations'] or v in right['violations']],
            'reason':'round 1: '+left['reason']+'; round 2: '+right['reason']})
        for key in ('supported','violations'):
            if left[key]!=right[key]:
                differences.append({'array':'sentence_results','index':index,'field':key,'round_1':left[key],'round_2':right[key]})
    for key in ('must_cover','forbidden'):
        for index,(left,right) in enumerate(zip(a[key],b[key],strict=True),1):
            combined[key].append(left and right if key=='must_cover' else left or right)
            if left!=right:
                differences.append({'array':key,'index':index,'round_1':left,'round_2':right})
    return combined,differences


def _metrics(rows, votes=None):
    candidates=[r['result']['version'] for r in rows if r['result'].get('version')]
    calls=[c for r in rows for c in r['calls']]
    if not calls:
        calls=[{'duration_ms':c['duration_ms'],'prompt_tokens':c['usage'].get('prompt_tokens'),
                'completion_tokens':c['usage'].get('completion_tokens'),'reasoning_tokens':c['usage'].get('reasoning_tokens'),
                'timed_out':c.get('reason')=='timeout'} for r in rows for a in r['attempts'] for c in json.loads(a['calls_json'])]
    metrics={'cases':len(rows),'incomplete_cases':sum(not r['complete'] for r in rows),
        'generated_candidates':len(candidates),'rejected_candidates':sum(v['status']=='rejected' for v in candidates),
        'published':sum(v['status']=='published' for v in candidates),'candidate':sum(v['status']=='candidate' for v in candidates),
        'deleted_sentences':sum(len(v['checks'].get('deleted_sentences',[])) for v in candidates),
        'original_sentences':sum(v['checks'].get('original_sentence_count',0) for v in candidates),
        'calls':{'count':len(calls),'duration_ms_p50':_percentile([c['duration_ms'] for c in calls],50),
                 'duration_ms_p95':_percentile([c['duration_ms'] for c in calls],95),
                 'prompt_tokens':sum(c.get('prompt_tokens') or 0 for c in calls),
                 'completion_tokens':sum(c.get('completion_tokens') or 0 for c in calls),
                 'reasoning_tokens':sum(c.get('reasoning_tokens') or 0 for c in calls),
                 'timed_out':sum(bool(c.get('timed_out')) for c in calls)},
        'grounded_sentence_ratio':None,'must_cover_coverage':None,'forbidden_occurrences':None,'meets_threshold':False}
    metrics['rejected_ratio']=metrics['rejected_candidates']/len(candidates) if candidates else None
    metrics['deleted_ratio']=metrics['deleted_sentences']/metrics['original_sentences'] if metrics['original_sentences'] else None
    if votes is not None:
        total=supported=admin=covered=expected=forbidden=0; violations=Counter(); rejected=Counter()
        for row,vote in zip(rows,votes,strict=True):
            profile=row['result'].get('version')
            expected+=len(vote['must_cover'])
            if not profile or profile['status']=='rejected':
                rejected.update(v for s in vote['sentence_results'] for v in s['violations'])
                continue
            covered+=sum(vote['must_cover']); forbidden+=sum(vote['forbidden'])
            for sentence,result in zip(profile['sentences'],vote['sentence_results'],strict=True):
                violations.update(result['violations'])
                if sentence['author']=='admin':
                    admin+=1
                else:
                    total+=1; supported+=result['supported']
        ratio=supported/total if total else None
        prohibited={key:value for key,value in violations.items() if key!='无依据'}
        metrics.update(grounded_sentence_ratio=ratio,grounded_sentences=supported,model_sentences=total,admin_sentences=admin,
            violations=dict(violations),prohibited_violations=prohibited,rejected_violations=dict(rejected),must_cover_covered=covered,must_cover_total=expected,
            must_cover_coverage=covered/expected if expected else None,forbidden_occurrences=forbidden,
            meets_threshold=ratio is not None and ratio>=.9 and not prohibited and not forbidden and not metrics['incomplete_cases'])
    return metrics


def score(materials, judgments, *, judge_model, out):
    paths=list(map(Path,judgments))
    if len(paths) not in (1,2) or len({p.resolve() for p in paths})!=len(paths) or not judge_model.strip():
        raise ValueError('provide one or two independent rounds and a judge model name')
    manifest,rows=_load_materials(materials)
    rounds=[]
    for path in paths:
        expected={'manifest.json',*(e['judgment_file'] for e in manifest['cases'])}
        if {p.name for p in path.glob('*.json')}!=expected:
            raise ValueError('missing or unexpected judgment files')
        if any(not (path/name).resolve().is_relative_to(path.resolve()) for name in expected):
            raise ValueError('judgment files must stay within their directory')
        if _read_json(path/'manifest.json').get('materials_sha256')!=manifest['materials_sha256']:
            raise ValueError('judgment material fingerprint mismatch')
        rounds.append([_validate(_read_json(path/e['judgment_file']),row) for e,row in zip(manifest['cases'],rows,strict=True)])
    combined=[]; differences=[]
    for index,row in enumerate(rows):
        vote,items=_combine([r[index] for r in rounds]);combined.append(vote)
        differences.extend({'case_id':row['case']['id'],**item} for item in items)
    report={**manifest['run'],'materials_sha256':manifest['materials_sha256'],'judge_model':judge_model.strip(),
        'judge_runs':len(rounds),'judgment_rounds':[{'round':i+1,'sha256':_json_sha256(v)} for i,v in enumerate(rounds)],
        'original_judgments':rounds,'judgments':combined,'disagreements':differences,'rows':rows,'metrics':_metrics(rows,combined)}
    out=Path(out)
    if any(out.resolve().is_relative_to(p.resolve()) for p in [Path(materials),*paths]):
        raise ValueError('score output must not overwrite materials or rounds')
    name='entry-profile-score-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid4().hex[:8]+'.json'
    _write_json(out/name,report)
    return report


class _Responses:
    def __init__(self, values):
        self.values=iter(values)
    def chat(self, messages, purpose, max_tokens=16000, **kwargs):
        return ModelReply(dumps(next(self.values)),'stop',{'prompt_tokens':0,'completion_tokens':0})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    run=commands.add_parser('run');run.add_argument('--corpus',required=True);run.add_argument('--out',required=True)
    run.add_argument('--fake-responses',help='Explicit JSON array of fake responses; never loads model configuration')
    grade=commands.add_parser('score');grade.add_argument('--materials',required=True);grade.add_argument('--judgments',action='append',required=True)
    grade.add_argument('--judge-model',required=True);grade.add_argument('--out',required=True)
    args=parser.parse_args()
    if args.command=='run':
        if args.fake_responses:
            values=_read_json(Path(args.fake_responses))
            if not isinstance(values,list):
                parser.error('fake responses must be a JSON array')
            gateway=_Responses(values)
            result=run_eval(args.corpus,args.out,configs={},gateway_factory=lambda configs,store:gateway,extra_input=values)
        else:
            result=run_eval(args.corpus,args.out,configs=load_test_models())
        print('Materials: '+result['materials'])
        print(dumps(result['metrics']))
    else:
        result=score(args.materials,args.judgments,judge_model=args.judge_model,out=args.out)
        print(dumps(result['metrics']))


if __name__=='__main__':
    main()
