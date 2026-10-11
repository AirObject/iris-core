"""Isolated explicit-input execution and immutable external judging bundles.

Run from a checkout with `uv run python -m evals.person_summary_eval`.
No default corpus, corpus globbing, embedded grading model, or score-based reuse.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from iris.db import Store, dumps
from iris.person_summary import (
    PersonSummaryEngine, VIOLATIONS, admin_edit, select_material, summary_due, _instant,
)
from iris.evaluation import _json_sha256, _read_json, _write_json, _verified_material_file, _percentile
from iris.models import Gateway, ModelReply, load_test_models

FORMAT_VERSION=1
SCORING_VERSION='person_summary_scoring_v1'
HERE=Path(__file__).resolve().parent


def _texts(value, name):
    if not isinstance(value,list) or any(not isinstance(x,str) or not x.strip() for x in value):
        raise ValueError(name+' must be an array of nonempty strings')


def _restricted(case):
    restricted_entries={e['id'] for e in case['entries'] if e.get('visibility','shared')!='shared'}
    private={m['id'] for m in case['memories'] if restricted_entries.intersection(m.get('source_entries',[]))}
    while True:
        extended=private|{m['id'] for m in case['memories'] if private.intersection(m.get('derived_from',[]))}
        if extended==private:
            return private
        private=extended


def load_corpus(path):
    """Explicit file only. Annotations/probes are never passed to either model."""
    document=_read_json(Path(path))
    if not isinstance(document,dict) or type(document.get('format_version')) is not int or document['format_version']!=1:
        raise ValueError('expected format_version=1')
    cases=document.get('cases'); ids=set()
    if not isinstance(cases,list) or not cases:
        raise ValueError('expected nonempty cases')
    for case in cases:
        if not isinstance(case,dict) or not isinstance(case.get('id'),str) or not case['id'] or case['id'] in ids:
            raise ValueError('duplicate or invalid case id')
        ids.add(case['id']); current=_instant(case['as_of']); ZoneInfo(case.get('timezone','Asia/Shanghai'))
        entries=case['entries']; subjects=case['subjects']; memories=case['memories']
        if not all(isinstance(values,list) and values for values in (entries,subjects)) or not isinstance(memories,list):
            raise ValueError('entries and subjects must be nonempty arrays; memories must be an array')
        def identities(values):
            result=[v['id'] for v in values]
            if any(not isinstance(v,str) or not v for v in result) or len(set(result))!=len(result):
                raise ValueError('duplicate or invalid identity')
            return set(result)
        eids=identities(entries); sids=identities(subjects); mids=identities(memories)
        if case['subject_id'] not in sids or {'self','scene'}&sids:
            raise ValueError('subject must be a declared person; self/scene are implicit')
        for e in entries:
            if e.get('kind','group') not in ('group','live','private') or e.get('visibility','shared') not in ('shared','entry_only','entries'):
                raise ValueError('invalid entry kind or visibility')
            _texts(e.get('visible_in',[]),'visible_in')
            if not set(e.get('visible_in',[]))<=eids:
                raise ValueError('unknown visible entry')
        for subject in subjects:
            if not isinstance(subject.get('name'),str) or not subject['name'].strip():
                raise ValueError('invalid subject name')
            _texts(subject.get('aliases',[]),'aliases')
            origins=subject.get('alias_source_entries',{})
            if (not isinstance(origins,dict) or not set(origins)<=set(subject.get('aliases',[]))
                    or any(value is not None and (not isinstance(value,str) or value not in eids) for value in origins.values())):
                raise ValueError('alias_source_entries must map declared aliases to declared entries or null')
        for m in memories:
            if (not isinstance(m.get('content'),str) or (not m['content'].strip() and m.get('lifecycle')!='purged') or m.get('speaker') not in sids|{'self','scene'}
                    or m.get('lifecycle','active') not in ('active','forgotten','deleted','purged')):
                raise ValueError('invalid memory content, speaker or lifecycle')
            for key in ('about','source_entries','derived_from','tags'):
                _texts(m.get(key,[]),key)
            if not set(m.get('about',[]))<=sids|{'self','scene'} or not set(m.get('source_entries',[]))<=eids or not set(m.get('derived_from',[]))<=mids:
                raise ValueError('unknown memory relationship')
            for key,default in (('importance',50),('retention',80),('belief',90),('revision',1)):
                if type(m.get(key,default)) is not int or not (1<=m.get(key,default) if key=='revision' else 0<=m.get(key,default)<=100):
                    raise ValueError('invalid numeric metadata')
            for key in ('created_at','updated_at','first_confirmed_at','last_confirmed_at'):
                if _instant(m.get(key,m['created_at']))>current:
                    raise ValueError('future memory metadata')
        _texts(case.get('must_cover',[]),'must_cover'); _texts(case.get('forbidden',[]),'forbidden')
        if type(case.get('expect_none',False)) is not bool:
            raise ValueError('expect_none must be a boolean')
        if case.get('expect_none',False) and case.get('must_cover',[]):
            raise ValueError('expect_none requires an empty must_cover array')
        private=_restricted(case); by_id={m['id']:m for m in memories}
        probes=case.get('leak_probes',[])
        if not isinstance(probes,list):
            raise ValueError('invalid leak probes')
        for probe in probes:
            if (not isinstance(probe,dict) or set(probe)!={'memory_id','text'} or probe['memory_id'] not in private
                    or not isinstance(probe['text'],str) or not probe['text'].strip()
                    or probe['text'] not in by_id[probe['memory_id']]['content']
                    or any(probe['text'] in m['content'] for m in memories if m['id'] not in private)):
                raise ValueError('leak probe must uniquely identify restricted content')
        if 'initial_summary' in case and not isinstance(case['initial_summary'],str):
            raise ValueError('initial_summary must be text')
    return cases


def _load_case(store, case):
    stamp=case['as_of']; mids={}
    store.set_setting('timezone',case.get('timezone','Asia/Shanghai'))
    store.set_setting('role_name',case.get('role_name','Iris'))
    with store.write() as conn:
        for e in case['entries']:
            conn.execute('INSERT INTO entries(id,name,platform,kind,visibility,visible_in_json) VALUES(?,?,?,?,?,?)',
                         (e['id'],e.get('name',e['id']),'eval',e.get('kind','group'),e.get('visibility','shared'),dumps(e.get('visible_in',[]))))
        for subject in case['subjects']:
            conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)",(subject['id'],subject['name'],stamp))
            for index,alias in enumerate(subject.get('aliases',[])):
                entry=subject.get('alias_source_entries',{}).get(alias)
                message=None
                if entry is not None:
                    # Provenance metadata only: the corpus supplies no raw utterance.
                    message=conn.execute("""INSERT INTO messages(entry_id,kind,sender_subject_id,content,occurred_at,received_at,dedupe_key)
                        VALUES(?,'message',?,'',?,?,?)""",(entry,subject['id'],stamp,stamp,'alias:'+subject['id']+':'+str(index))).lastrowid
                conn.execute('INSERT INTO subject_aliases(subject_id,alias,source_message_id) VALUES(?,?,?)',
                             (subject['id'],alias,message))
        for m in case['memories']:
            mids[m['id']]=conn.execute("""INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,
                retention,lifecycle,revision,event_time,world,created_at,updated_at,first_confirmed_at,last_confirmed_at,purged_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",('' if m.get('lifecycle')=='purged' else m['content'],m.get('kind','事实'),m['speaker'],m.get('stance','亲历'),
                m.get('belief',90),m.get('importance',50),m.get('retention',80),
                'deleted' if m.get('lifecycle')=='purged' else m.get('lifecycle','active'),m.get('revision',1),
                m.get('event_time'),m.get('world','real'),m['created_at'],m.get('updated_at',m['created_at']),
                m.get('first_confirmed_at',m['created_at']),m.get('last_confirmed_at',m['created_at']),
                stamp if m.get('lifecycle')=='purged' else None)).lastrowid
        for m in case['memories']:
            mid=mids[m['id']]
            conn.executemany('INSERT INTO memory_subjects(memory_id,subject_id) VALUES(?,?)',((mid,s) for s in set(m.get('about',[]))))
            conn.executemany('INSERT INTO memory_tags(memory_id,tag) VALUES(?,?)',((mid,t) for t in set(m.get('tags',[]))))
            conn.executemany('INSERT INTO memory_visibility_roots(memory_id,entry_id) VALUES(?,?)',((mid,e) for e in set(m.get('source_entries',[]))))
            conn.executemany("INSERT INTO sources(memory_id,kind,source_memory_id,created_at) VALUES(?,'memory',?,?)",
                             ((mid,mids[parent],stamp) for parent in set(m.get('derived_from',[]))))
    if case.get('initial_summary'):
        admin_edit(store,case['subject_id'],case['initial_summary'],expected_version=0,clock=lambda:_instant(stamp))
    return mids


def _source_hash():
    iris=Path(__import__('iris').__file__).parent
    paths=sorted([*iris.rglob('*.py'),*iris.joinpath('migrations').glob('*.sql'),
                  *iris.joinpath('prompts').glob('*.md'),*HERE.glob('*.py'),*HERE.glob('scoring*.md')])
    # Deliberately excludes evals corpora, credentials and external material directories.
    return _json_sha256([(str(p.relative_to(iris)) if p.is_relative_to(iris) else 'person_summary_eval/'+p.name,
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


def _strings(value):
    if isinstance(value,str):
        yield value
    elif isinstance(value,dict):
        for key,item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value,(list,tuple)):
        for item in value:
            yield from _strings(item)


def _output_texts(outputs):
    for raw in outputs.values():
        yield raw
        try:
            # Preserve duplicate keys too: even invalid JSON schema can contain
            # restricted text which must not vanish during the independent audit.
            decoded=json.loads(raw,object_pairs_hook=list)
        except (ValueError,TypeError,RecursionError):
            continue
        yield from _strings(decoded)


def _private_identity(case):
    """Independent corpus-side audit, not the product relatedness matcher."""
    entries={e['id']:e.get('visibility','shared') for e in case['entries']}
    labels={}; private=[]; target=case['subject_id']
    def add(label, subject_id):
        if label and label not in ('我','self','scene'):
            labels.setdefault(label.casefold(),set()).add(subject_id)
    add(case.get('role_name','Iris'),'self')
    for subject in case['subjects']:
        public=[subject['name']]
        for alias in subject.get('aliases',[]):
            entry=subject.get('alias_source_entries',{}).get(alias)
            if entry is None or entries[entry]=='shared':
                public.append(alias)
            elif subject['id']==target:
                private.append(alias)
        for label in public:
            add(label,subject['id'])
    def pattern(values):
        return re.compile('|'.join(re.escape(v) if re.search('[\u4e00-\u9fff]',v)
            else r'(?<!\w)'+re.escape(v)+r'(?!\w)' for v in sorted(values,key=lambda v:(-len(v),v))) or r'(?!x)x',re.I)
    safe=pattern(labels); restricted=pattern(private); ids=set()
    for memory in case['memories']:
        if memory['speaker']==target or target in memory.get('about',[]):
            continue
        if any(labels[match.group().casefold()]=={target} for match in safe.finditer(memory['content'])):
            continue
        if restricted.search(memory['content']):
            ids.add(memory['id'])
    return ids,private


def _completed(result, attempts, calls):
    if result['status'] not in ('published','candidate','rejected') and result.get('reason')!='invalid_output':
        return False
    if result.get('version') and result['version']['checks'].get('model_error'):
        return False
    if any(c.get('timed_out') for c in calls):
        return False
    traces=[c for a in attempts for c in json.loads(a['calls_json'])]
    # A provider/gate failure is not a judgment, even if generation already made
    # a candidate which was persisted as rejected. Invalid actual output counts.
    return bool(any(json.loads(a['outputs_json']) for a in attempts)) and all(c['result']=='success' for c in traces)


def _expect_none_check(row):
    """A skipped audit row is allowed; any evidence of generation is a failure."""
    if not row['case'].get('expect_none',False):
        return None
    violations=[]; versions={v['version']:v['status'] for v in row.get('generated_versions',[])}
    profile=row['result'].get('version')
    if profile:
        versions[profile['version']]=profile['status']
    for version,status in sorted(versions.items()):
        violations.append({'kind':'unexpected_version','version':version,'status':status})
    if not versions and row['result']['status'] in ('published','candidate','rejected'):
        violations.append({'kind':'unexpected_generation_result','status':row['result']['status']})
    for attempt in row['attempts']:
        artifacts=[key for key in ('material_json','outputs_json','calls_json') if json.loads(attempt[key])]
        if attempt['state']!='skipped' or artifacts or attempt.get('version') is not None or attempt.get('cleared_at'):
            violations.append({'kind':'unexpected_attempt','attempt_id':attempt['id'],'state':attempt['state'],
                               'artifacts':artifacts,'version':attempt.get('version')})
    if row['calls']:
        violations.append({'kind':'unexpected_model_calls','count':len(row['calls'])})
    expected_skip=row['result']['status']=='skipped' and row['result'].get('reason')=='insufficient_memories'
    return {'status':'failed' if violations else 'passed' if expected_skip else 'incomplete',
            'global_memories':row.get('due',row['result'].get('due',{})).get('global_memories'),
            'violations':violations}


def _row_complete(row):
    check=_expect_none_check(row)
    # An unexpected attempt is already a decisive failure of an absence case,
    # including a provider failure with no output. It needs no semantic judgment.
    if check is not None:
        return check['status']!='incomplete'
    return _completed(row['result'],row['attempts'],row['calls'])


def _material_diagnostics(selected, attempts, mapping):
    if selected is None:
        return None
    available={int(mid) for mid in selected['memory_state']}
    actual={m['memory_id'] for m in selected['memories']}
    source='pre_generation_snapshot'; attempt_id=None
    # A run makes one regenerate request per isolated case. Prefer its recorded
    # material; the pre-call snapshot also diagnoses no_evidence/health skips.
    for attempt in attempts:
        material=json.loads(attempt['material_json'])
        if 'evidence' in material:
            available={int(mid) for mid in json.loads(attempt['memory_state_json'])}
            actual={m['memory_id'] for m in material['evidence'].get('memories',[])}
            source='attempt'; attempt_id=attempt['id']
    excluded=available-actual
    return {'selection_source':source,'attempt_id':attempt_id,'global_memories':len(available),
            'selected_memories':len(actual),'material_truncated':len(actual)<len(available),
            'excluded_memory_ids':[key for key,mid in mapping.items() if mid in excluded],
            'excluded_database_memory_ids':sorted(excluded)}


def _material_memory_ids(material):
    evidence=material.get('evidence',{})
    return set(material.get('dependency_ids',[])) | {m['memory_id'] for m in evidence.get('memories',[])} | {
        b['memory_id'] for b in evidence.get('previous_basis',[])} | {
        b['memory_id'] for s in material.get('admin_sentences',[]) for b in s.get('basis',[])}


def _non_active(row):
    # Compute eligibility from immutable corpus annotations, not DB lifecycle,
    # the product selector, model checks or an external grader's decision.
    inactive={m['id']:m.get('lifecycle','active') for m in row['case']['memories']
              if m.get('lifecycle','active')!='active'}
    mapping=row['memory_ids']; violations=[]
    if set(mapping)!={m['id'] for m in row['case']['memories']}:
        return {'non_active_memories':len(inactive),'violations':[{'kind':'incomplete_seed_mapping'}]}
    by_id={mapping[key]:{'corpus_memory_id':key,'lifecycle':state} for key,state in inactive.items()}
    def audit(ids, kind, **location):
        for mid in sorted(ids & by_id.keys()):
            violations.append({'kind':kind,'memory_id':mid,**by_id[mid],**location})
    for attempt in row['attempts']:
        audit(_material_memory_ids(json.loads(attempt['material_json'])),'non_active_material',attempt_id=attempt['id'])
    profile=row['result'].get('version')
    if profile:
        audit(_material_memory_ids(profile.get('material',{})),'non_active_material',location='version_material')
        audit({b['memory_id'] for s in profile['sentences'] for b in s['basis']},'non_active_reference')
    return {'non_active_memories':len(inactive),'violations':violations}


def _leaks(row):
    private=_restricted(row['case']); mapping=row['memory_ids']; violations=[]
    if set(mapping)!={m['id'] for m in row['case']['memories']}:
        return {'restricted_memories':len(private),'probe_count':len(row['case'].get('leak_probes',[])),
                'violations':[{'kind':'incomplete_seed_mapping'}]}
    restricted_ids={mapping[key] for key in private}
    identity,private_aliases=_private_identity(row['case']); identity_ids={mapping[key] for key in identity}
    for attempt in row['attempts']:
        material=json.loads(attempt['material_json'])
        actual={m['memory_id'] for m in material.get('evidence',{}).get('memories',[])}
        for mid in sorted(actual&identity_ids):
            violations.append({'kind':'restricted_identity_material','attempt_id':attempt['id'],'memory_id':mid})
        for alias in material.get('evidence',{}).get('subject',{}).get('aliases',[]):
            if alias in private_aliases:
                violations.append({'kind':'restricted_alias','attempt_id':attempt['id'],'alias':alias})
        for mid in sorted(actual&restricted_ids):
            violations.append({'kind':'restricted_material','attempt_id':attempt['id'],'memory_id':mid})
        # All raw outputs count, including failed/rejected generations. A semantic
        # grader cannot override this independently computed exact-substring gate.
        for location,texts in (('material',list(_strings(material))),
                               ('raw_outputs',list(_output_texts(json.loads(attempt['outputs_json']))))):
            for probe in row['case'].get('leak_probes',[]):
                if any(probe['text'] in text for text in texts):
                    violations.append({'kind':'restricted_text','location':location,'attempt_id':attempt['id'],**probe})
    profile=row['result'].get('version')
    if profile:
        for probe in row['case'].get('leak_probes',[]):
            if probe['text'] in profile['content']:
                violations.append({'kind':'restricted_text','location':'rendered_summary',**probe})
        for sentence in profile['sentences']:
            for basis in sentence['basis']:
                if basis['memory_id'] in identity_ids:
                    violations.append({'kind':'restricted_identity_reference','memory_id':basis['memory_id']})
                if basis['memory_id'] in restricted_ids:
                    violations.append({'kind':'restricted_reference','memory_id':basis['memory_id']})
    return {'restricted_memories':len(private),'probe_count':len(row['case'].get('leak_probes',[])),'violations':violations}


def _payload(row):
    profile=row['result'].get('version'); expect_none=row['case'].get('expect_none',False)
    payload={'subject_id':row['case']['subject_id'],'as_of':row['case']['as_of'],
            'profile':profile,'evidence':profile['material'].get('evidence') if profile else None,
            'previous':profile['material'].get('previous','') if profile else '',
            'source_case':row['case'],'memory_ids':row['memory_ids'],'leak_check':_leaks(row),
            'must_cover':[] if expect_none else row['case'].get('must_cover',[]),
            'forbidden':[] if expect_none else row['case'].get('forbidden',[]),
            'status':row['result']['status'],'reason':row['result'].get('reason')}
    # Preserve the document shape of older immutable v1 bundles when scoring.
    if 'expect_none_check' in row:
        payload.update(expect_none=expect_none,judgment_mode='empty' if expect_none else 'semantic',
                       expect_none_check=_expect_none_check(row),due=row['due'],
                       material_diagnostics=row['material_diagnostics'],non_active_check=_non_active(row))
    return payload


def _document(row, metadata):
    return {'format_version':1,'evaluation':'person_summary','case_id':row['case']['id'],
            'source_sha256':metadata['source_sha256'],'corpus_sha256':metadata['corpus_sha256'],
            'scoring_version':metadata['scoring_version'],'input':_payload(row)}


def run_eval(corpus, out, *, configs=None, gateway_factory=Gateway, extra_input=None):
    cases=load_corpus(corpus); configs=configs or {}; root=Path(out)
    corpus_file_sha256=hashlib.sha256(Path(corpus).read_bytes()).hexdigest()
    root.mkdir(parents=True,exist_ok=True)
    directory=root/('person-summary-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid4().hex[:8])
    directory.mkdir()
    scoring=(HERE/'scoring.md').read_text(encoding='utf-8')
    metadata={'source_sha256':_source_hash(),'corpus_sha256':_json_sha256(cases),
        'corpus_file_sha256':corpus_file_sha256,
        'fake_only':not bool(configs),'models':_model_metadata(configs),'extra_input_sha256':_json_sha256(extra_input),
        'prompt_versions':['person_summary_generate_v1','person_summary_check_v1'],
        'scoring_version':SCORING_VERSION,'scoring_sha256':hashlib.sha256(scoring.encode()).hexdigest(),
        'timeouts_seconds':{'generate':120,'check':120}}
    metadata['input_sha256']=_json_sha256(metadata)
    rows=[]; started=time.monotonic()
    # Always a fresh DB/run: no completed result is selected or reused by score.
    for index,case in enumerate(cases):
        store=Store(directory/'databases'/f'{index:04}.db'); gateway=None; mapping={}
        due={}; selected=None; base_version=0
        try:
            try:
                mapping=_load_case(store,case)
                with store.read() as conn:
                    saved=conn.execute('SELECT current_version FROM person_summary_settings WHERE subject_id=?',(case['subject_id'],)).fetchone()
                    base_version=saved[0] if saved else 0
                gateway=gateway_factory(configs,store)
                clock=lambda:_instant(case['as_of'])
                due=summary_due(store,case['subject_id'],clock=clock)
                if not case.get('expect_none',False):
                    selected=select_material(store,case['subject_id'],clock=clock)
                engine=PersonSummaryEngine(store,gateway,clock=clock)
                result=engine.regenerate(case['subject_id'],expected_version=base_version)
            except Exception as error:
                # Never echo exception text from providers, payloads or configuration.
                result={'status':'failed','reason':type(error).__name__,'version':None}
            with store.read() as conn:
                attempts=[dict(r) for r in conn.execute('SELECT * FROM person_summary_attempts ORDER BY id')]
                calls=[dict(r) for r in conn.execute('SELECT * FROM model_calls ORDER BY id')]
                versions=[dict(r) for r in conn.execute("""SELECT version,status,author,source FROM person_summary_versions
                    WHERE subject_id=? AND version>? ORDER BY version""",(case['subject_id'],base_version))]
            row={'case':case,'memory_ids':mapping,'result':result,'attempts':attempts,'calls':calls,
                 'due':due,'generated_versions':versions}
            row.update(complete=_row_complete(row),expect_none_check=_expect_none_check(row),
                       non_active_check=_non_active(row),
                       material_diagnostics=_material_diagnostics(selected,attempts,mapping))
            rows.append(row)
        finally:
            if gateway and callable(getattr(gateway,'close',None)):
                gateway.close()
            store.close()
    rows=_redacted(rows,configs)
    materials=directory/'judging-materials'; materials.mkdir()
    manifest={'format_version':1,'evaluation':'person_summary','run':metadata,
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
        if manifest['format_version']!=1 or manifest['evaluation']!='person_summary':
            raise ValueError('unsupported materials format')
        if manifest['materials_sha256']!=_json_sha256({k:v for k,v in manifest.items() if k!='materials_sha256'}):
            raise ValueError('manifest fingerprint mismatch')
        rows=_read_json(_verified_material_file(path,'run.json',manifest['run_sha256']))['rows']
        scoring=_verified_material_file(path,'scoring.md',manifest['scoring_sha256'])
        meta=manifest['run']
        if (meta['corpus_sha256']!=_json_sha256([r['case'] for r in rows]) or meta['scoring_version'] !=SCORING_VERSION
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
    if not isinstance(vote,dict) or set(vote)!={'item_results','must_cover','forbidden'}:
        raise ValueError('invalid judgment fields')
    if row['case'].get('expect_none',False):
        if any(vote[key]!=[] for key in ('item_results','must_cover','forbidden')):
            raise ValueError('expect_none judgments must contain only empty arrays')
        return vote
    profile=row['result'].get('version'); sentences=profile['sentences'] if profile else []
    if not isinstance(vote['item_results'],list) or len(vote['item_results'])!=len(sentences):
        raise ValueError('judgment item count mismatch')
    for index,result in enumerate(vote['item_results'],1):
        if (not isinstance(result,dict) or set(result)!={'index','supported','violations','reason'}
                or type(result['index']) is not int or result['index']!=index or type(result['supported']) is not bool
                or not isinstance(result['reason'],str) or not result['reason'].strip()
                or not isinstance(result['violations'],list)
                or result['violations']!=[v for v in VIOLATIONS if v in result['violations']]
                or ('无依据' in result['violations'] and result['supported'])):
            raise ValueError('invalid item judgment')
    for name in ('must_cover','forbidden'):
        if not isinstance(vote[name],list) or len(vote[name])!=len(_payload(row).get(name,[])) or any(type(x) is not bool for x in vote[name]):
            raise ValueError('invalid reference judgments')
    if profile is None and any(vote['must_cover']):
        raise ValueError('missing profiles cannot cover reference points')
    return vote


def _combine(votes):
    if len(votes)==1:
        return votes[0],[]
    a,b=votes; combined={'item_results':[],'must_cover':[],'forbidden':[]}; differences=[]
    for index,(left,right) in enumerate(zip(a['item_results'],b['item_results'],strict=True),1):
        combined['item_results'].append({'index':index,'supported':left['supported'] and right['supported'],
            'violations':[v for v in VIOLATIONS if v in left['violations'] or v in right['violations']],
            'reason':'round 1: '+left['reason']+'; round 2: '+right['reason']})
        for key in ('supported','violations'):
            if left[key]!=right[key]:
                differences.append({'array':'item_results','index':index,'field':key,'round_1':left[key],'round_2':right[key]})
    for key in ('must_cover','forbidden'):
        for index,(left,right) in enumerate(zip(a[key],b[key],strict=True),1):
            combined[key].append(left and right if key=='must_cover' else left or right)
            if left!=right:
                differences.append({'array':key,'index':index,'round_1':left,'round_2':right})
    return combined,differences


def _metrics(rows, votes=None):
    candidates=[r['result']['version'] for r in rows if r['result'].get('version')]
    checks=[_leaks(r) for r in rows]; calls=[c for r in rows for a in r['attempts'] for c in json.loads(a['calls_json'])]
    none_checks=[{'case_id':r['case']['id'],**check} for r in rows if (check:=_expect_none_check(r)) is not None]
    inactive_checks=[{'case_id':r['case']['id'],**_non_active(r)} for r in rows]
    truncations=[{'case_id':r['case']['id'],**r['material_diagnostics']} for r in rows
                 if r.get('material_diagnostics') and r['material_diagnostics']['material_truncated']]
    metrics={'cases':len(rows),'incomplete_cases':sum(not _row_complete(r) for r in rows),
        'expect_none_cases':len(none_checks),'expect_none_passed':sum(c['status']=='passed' for c in none_checks),
        'expect_none_failed':sum(c['status']=='failed' for c in none_checks),
        'expect_none_incomplete':sum(c['status']=='incomplete' for c in none_checks),
        'expect_none_violations':sum(len(c['violations']) for c in none_checks),'expect_none_results':none_checks,
        'material_truncated_cases':len(truncations),
        'material_truncated_memories':sum(len(c['excluded_memory_ids']) for c in truncations),
        'material_truncations':truncations,
        'material_diagnostics_unavailable':sum(not r['case'].get('expect_none',False) and not r.get('material_diagnostics') for r in rows),
        'non_active_violations':sum(len(c['violations']) for c in inactive_checks),
        'non_active_memories_checked':sum(c['non_active_memories'] for c in inactive_checks),
        'non_active_results':inactive_checks,
        'generated_candidates':len(candidates),'rejected_candidates':sum(v['status']=='rejected' for v in candidates),
        'published':sum(v['status']=='published' for v in candidates),'candidate':sum(v['status']=='candidate' for v in candidates),
        'deleted_items':sum(len(v['checks'].get('deleted_sentences',[])) for v in candidates),
        'original_items':sum(v['checks'].get('original_item_count',0) for v in candidates),
        'scoped_leaks':sum(len(c['violations']) for c in checks),
        'scoped_memories_checked':sum(c['restricted_memories'] for c in checks),
        'leak_probes_checked':sum(c['probe_count'] for c in checks),
        'calls':{'count':len(calls),'duration_ms_p50':_percentile([c['duration_ms'] for c in calls],50),
                 'duration_ms_p95':_percentile([c['duration_ms'] for c in calls],95),
                 'prompt_tokens':sum(c['usage'].get('prompt_tokens') or 0 for c in calls),
                 'completion_tokens':sum(c['usage'].get('completion_tokens') or 0 for c in calls),
                 'timed_out':sum(c.get('reason')=='timeout' for c in calls)},
        'grounded_item_ratio':None,'must_cover_coverage':None,'forbidden_occurrences':None,'meets_threshold':False}
    metrics['reference_validation_total']=metrics['original_items']
    metrics['reference_validation_passed']=metrics['original_items']-sum(
        d.get('stage')=='references' for v in candidates for d in v['checks'].get('deleted_sentences',[]))
    metrics['reference_validation_pass_rate']=(metrics['reference_validation_passed']/metrics['original_items'] if metrics['original_items'] else None)
    if votes is not None:
        total=supported=admin=covered=expected=forbidden=0; violations=Counter(); rejected=Counter()
        for row,vote in zip(rows,votes,strict=True):
            if row['case'].get('expect_none',False):
                continue
            profile=row['result'].get('version'); expected+=len(vote['must_cover'])
            if not profile or profile['status']=='rejected':
                rejected.update(v for s in vote['item_results'] for v in s['violations'])
                continue
            covered+=sum(vote['must_cover']); forbidden+=sum(vote['forbidden'])
            for sentence,result in zip(profile['sentences'],vote['item_results'],strict=True):
                violations.update(result['violations'])
                if sentence['author']=='admin': admin+=1
                else: total+=1; supported+=result['supported']
        ratio=supported/total if total else None
        prohibited={key:value for key,value in violations.items() if key!='无依据'}
        metrics.update(grounded_item_ratio=ratio,grounded_items=supported,model_items=total,admin_items=admin,
            violations=dict(violations),prohibited_violations=prohibited,rejected_violations=dict(rejected),
            must_cover_covered=covered,must_cover_total=expected,must_cover_coverage=covered/expected if expected else None,
            forbidden_occurrences=forbidden,meets_threshold=ratio is not None and ratio>=.9 and not prohibited
            and not forbidden and not metrics['scoped_leaks'] and not metrics['non_active_violations']
            and not metrics['expect_none_violations'] and not metrics['incomplete_cases'])
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
    name='person-summary-score-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid4().hex[:8]+'.json'
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
