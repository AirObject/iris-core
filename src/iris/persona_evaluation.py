"""Isolated persona timelines, immutable external materials and offline adverse scoring.

Accepts frozen persona_v1 JSON (days/ops/checkpoint) and compact JSONL timelines,
{id, role:{name,background,timezone}, start_at, events:[{at,op,...}]}.
Operations: add {memory:{key,content,speaker,about,stance,sources:[{content,
entry:{id,kind,name,platform},sender,kind,at}]}}, edit/delete/forget {memory_key},
persona_edit {content}, checkpoint {id,action:update|regenerate,must_reflect:[],
must_not:[],expected_change:small|medium|large,scenarios:[]}.
All timestamps include offsets. Memory keys are timeline-local; database IDs never
come from model output. Scoring.md is loaded from the separately frozen prompt.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from .db import Store, dumps
from .evaluation import _json_sha256, _read_json, _write_json, _verified_material_file, _percentile
from .memory_ops import setup_role, delete_memory, adjust_retention, lifecycle_settings
from .model_health import ModelHealth
from .models import Gateway, ModelError, parse_json_object_with_status
from .persona import (PersonaEngine, admin_edit, current_persona, pending_update, select_evidence,
                      confirm_candidate, reject_candidate, DEGREES, _trace, _stale, _settings, split_sentences)

FORMAT_VERSION = 1
SCORING_VERSION = 'persona_scoring_v1'
VIOLATIONS = ('虚构', '设定写成亲历', '他人评价写成特质', '单一日期泛化', '当前状态或待办', '指令', '保留失效依据')


def _instant(value):
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError('timeline timestamps must be ISO datetimes') from error
    if result.tzinfo is None:
        raise ValueError('timeline timestamps require timezone offsets')
    return result


def _texts(value, name):
    if not isinstance(value, list) or any(not isinstance(v,str) or not v.strip() for v in value):
        raise ValueError(f'{name} must be an array of nonempty strings')


def _normalize_frozen(document):
    if document.get('format_version') != 1 or not isinstance(document.get('timelines'), list):
        raise ValueError('unsupported persona corpus format')
    normalized = []
    for timeline in document['timelines']:
        zone = ZoneInfo(timeline.get('timezone', document.get('timezone', 'Asia/Shanghai')))
        entries = {e['id']:e for e in timeline['entries']}
        subjects = {s['id'] for s in timeline['subjects']} | {'self','scene'}
        if len(entries) != len(timeline['entries']) or len(subjects)-2 != len(timeline['subjects']):
            raise ValueError('duplicate or reserved entry/subject ID')
        events = []
        for day in timeline['days']:
            at = datetime.fromisoformat(day['date']+'T22:00:00').replace(tzinfo=zone).isoformat()
            for operation in day['ops']:
                op = dict(operation)
                if op['op'] in ('add','edit'):
                    if 'type' in op:
                        op['kind'] = op.pop('type')
                    if op.get('speaker','self') not in subjects or any(x not in subjects for x in op.get('about',[])):
                        raise ValueError('unknown memory subject')
                    sources = []
                    for source in op.get('sources',[]):
                        if source['entry_id'] not in entries or source['sender'] not in subjects:
                            raise ValueError('unknown source entry/subject')
                        if _instant(source['at']) > _instant(at):
                            raise ValueError('source occurs after its operation')
                        sources.append({**source,'entry':entries[source['entry_id']], 'content':source['text']})
                    op['sources'] = sources
                if op['op']=='add':
                    events.append({'op':'add','at':at,'memory':{k:v for k,v in op.items() if k!='op'}})
                else:
                    if op['op'] in ('edit','delete','forget'):
                        op['memory_key'] = op.pop('key')
                    events.append({**op,'at':at})
            checkpoint = day.get('checkpoint')
            if checkpoint is not None:
                expected = checkpoint['expect']
                for flag in ('generate','change'):
                    if type(expected.get(flag)) is not bool:
                        raise ValueError('checkpoint expectations require boolean generate/change')
                if 'needs_update' in expected and type(expected['needs_update']) is not bool:
                    raise ValueError('needs_update must be boolean')
                if expected.get('publication') not in ('published','pending_confirmation','unchanged'):
                    raise ValueError('invalid publication expectation')
                checkpoint_at = checkpoint.get('at',datetime.fromisoformat(day['date']+'T23:00:00').replace(tzinfo=zone).isoformat())
                if _instant(checkpoint_at) < _instant(at):
                    raise ValueError('checkpoint precedes operations')
                level = {'小':'small','中':'medium','大':'large'}.get(expected['level'],expected['level'])
                events.append({'op':'checkpoint','id':checkpoint.get('id',day['date']),'at':checkpoint_at,
                    'action':checkpoint['run'], 'expect':expected, 'expected_change':level,
                    'must_reflect':expected['must_reflect'],'must_not':expected['must_not'],
                    'scenarios':checkpoint.get('scenarios',[])})
        normalized.append({'id':timeline['id'], 'start_at':timeline['initialized_at'],
            'role':{'name':timeline['role_name'],'background':timeline['background'],'timezone':str(zone)},
            'subjects':timeline['subjects'], 'entries':timeline['entries'], 'events':events,
            'persona_goal':timeline.get('persona_goal'), 'persona_rules':timeline.get('persona_rules'),
            'raw_timeline':timeline})
    return normalized


def load_corpus(path):
    try:
        raw = Path(path).read_text(encoding='utf-8')
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            document = None
        cases = (_normalize_frozen(document) if isinstance(document,dict) and 'timelines' in document else
                 [json.loads(line) for line in raw.splitlines() if line.strip()])
        ids = set()
        for case in cases:
            if not isinstance(case['id'], str) or not case['id'] or case['id'] in ids:
                raise ValueError('duplicate or invalid timeline ID')
            ids.add(case['id'])
            role = case.get('role', {})
            if any(not isinstance(role.get(k,''),str) for k in ('name','background','timezone')):
                raise ValueError('role values must be strings')
            ZoneInfo(role.get('timezone','Asia/Shanghai'))
            latest = _instant(case['start_at'])
            if not isinstance(case['events'],list):
                raise ValueError('events must be an array')
            keys, checkpoints = set(), set()
            for event in case['events']:
                at = _instant(event['at'])
                if at < latest:
                    raise ValueError('timeline events must be chronological')
                latest = at
                op = event['op']
                if op == 'add':
                    memory = event['memory']
                    if not isinstance(memory['key'],str) or not memory['key'] or memory['key'] in keys:
                        raise ValueError('duplicate or invalid memory key')
                    keys.add(memory['key'])
                    _validate_memory(memory)
                elif op in ('edit','delete','forget'):
                    key = event['memory_key']
                    if key not in keys and not (isinstance(key,str) and key.startswith('initial:')):
                        raise ValueError('unknown memory key')
                    if op == 'edit':
                        _validate_memory(event, partial=True)
                elif op == 'persona_edit':
                    if ('content' in event)==('append' in event):
                        raise ValueError('persona_edit requires exactly one of content/append')
                    if 'content' in event:
                        if not isinstance(event['content'],str) or not event['content'].strip():
                            raise ValueError('persona_edit content required')
                    else:
                        _texts(event['append'],'persona_edit.append')
                        if not event['append'] or any(len(split_sentences(text))!=1 for text in event['append']):
                            raise ValueError('persona_edit.append must contain individual sentences')
                elif op in ('persona_confirm','persona_reject'):
                    pass
                elif op == 'checkpoint':
                    if not isinstance(event['id'],str) or not event['id'] or event['id'] in checkpoints:
                        raise ValueError('duplicate or invalid checkpoint ID')
                    checkpoints.add(event['id'])
                    if event.get('action','update') not in ('update','regenerate'):
                        raise ValueError('invalid checkpoint action')
                    for name in ('must_reflect','must_not','scenarios'):
                        _texts(event.get(name,[]),name)
                    if event.get('expected_change') not in (*DEGREES,None):
                        raise ValueError('invalid expected change degree')
                else:
                    raise ValueError('unknown persona timeline operation')
        if not cases:
            raise ValueError('empty persona corpus')
        return cases
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError('invalid persona timeline structure') from error


def _validate_memory(memory, *, partial=False):
    content = memory.get('content')
    if not isinstance(content,str) or not content.strip() or len(content)>1000:
        raise ValueError('memory content must contain 1..1000 characters')
    if 'about' in memory:
        _texts(memory['about'],'memory.about')
    if 'speaker' in memory and (not isinstance(memory['speaker'],str) or not memory['speaker']):
        raise ValueError('invalid memory speaker')
    if memory.get('stance','亲历') not in ('亲历','转述','观点','推断','设定'):
        raise ValueError('invalid memory stance')
    for score in ('importance','belief','retention'):
        if score in memory and (type(memory[score]) is not int or not 0 <= memory[score] <= 100):
            raise ValueError('invalid memory score')
    if 'pinned' in memory and type(memory['pinned']) is not bool:
        raise ValueError('pinned must be boolean')
    if not isinstance(memory.get('sources',[]),list):
        raise ValueError('sources must be an array')
    for source in memory.get('sources',[]):
        if not isinstance(source.get('content'),str) or not source['content']:
            raise ValueError('source message content required')
        if source.get('kind','self_output') not in ('message','self_output','action_result','event'):
            raise ValueError('invalid source message kind')
        if 'at' in source:
            _instant(source['at'])
        entry = source.get('entry',{})
        if not isinstance(entry,dict) or any(not isinstance(v,str) for v in entry.values()):
            raise ValueError('invalid source entry')


class TimelineClock:
    def __init__(self, value):
        self.value = _instant(value)

    def __call__(self):
        return self.value


def _subject(conn, name, stamp):
    if name in ('self','我','scene'):
        return 'self' if name=='我' else name
    sid = 'persona-eval:' + name
    conn.execute("INSERT OR IGNORE INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)", (sid,name,stamp))
    return sid


def _sources(conn, mid, memory, clock, keys):
    stamp = clock().isoformat()
    for source in memory.get('sources',[]):
        entry = source.get('entry',{})
        eid = entry.get('id','persona-eval')
        conn.execute('INSERT OR IGNORE INTO entries(id,name,platform,kind) VALUES(?,?,?,?)',
            (eid,entry.get('name',eid),entry.get('platform','eval'),entry.get('kind','private')))
        sender = _subject(conn,source.get('sender',memory.get('speaker','self')),stamp)
        existing = conn.execute('''SELECT id FROM messages WHERE entry_id=? AND sender_subject_id=?
            AND kind=? AND occurred_at=? AND content=? AND quote_author_subject_id IS ? AND quote_content IS ?''',
            (eid,sender,source.get('kind','self_output'),source.get('at',stamp),source['content'],
             _subject(conn,source['quote_author'],stamp) if source.get('quote_author') else None,source.get('quote_content'))).fetchone()
        msg = existing[0] if existing else conn.execute('''INSERT INTO messages(entry_id,kind,sender_subject_id,content,occurred_at,received_at,
            dedupe_key,scene_identity,quote_author_subject_id,quote_content,learning_state)
            VALUES(?,?,?,?,?,?,?,?,?,?,'learned')''', (eid,source.get('kind','self_output'),sender,source['content'],
            source.get('at',stamp),stamp,uuid4().hex,source.get('scene_identity'),
            _subject(conn,source['quote_author'],stamp) if source.get('quote_author') else None,source.get('quote_content'))).lastrowid
        if not conn.execute('SELECT 1 FROM sources WHERE memory_id=? AND message_id=?',(mid,msg)).fetchone():
            conn.execute("INSERT INTO sources(memory_id,kind,message_id,created_at) VALUES(?,'message',?,?)", (mid,msg,stamp))
        conn.execute('UPDATE memories SET entry_id=COALESCE(entry_id,?) WHERE id=?',(eid,mid))
    for key in memory.get('derived_from',[]):
        parent = conn.execute('SELECT revision FROM memories WHERE id=?',(keys[key],)).fetchone()
        conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,?,?)",
                     (mid,keys[key],parent[0],stamp))


def _mutate(store, event, keys, clock):
    op, stamp = event['op'],clock().isoformat()
    with store.write() as conn:
        if op == 'add':
            memory = event['memory']
            speaker = _subject(conn,memory.get('speaker','self'),stamp)
            importance = memory.get('importance',50)
            mid = conn.execute('''INSERT INTO memories(content,kind,speaker_subject_id,stance,belief,importance,retention,
                pinned,event_time,created_at,updated_at,first_confirmed_at,last_confirmed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''', (memory['content'],memory.get('kind','自我'),speaker,
                memory.get('stance','亲历'),memory.get('belief',80),importance,memory.get('retention',round(30+importance*.4)),
                int(memory.get('pinned',False)),memory.get('event_time'),stamp,stamp,stamp,stamp)).lastrowid
            for name in dict.fromkeys(memory.get('about',['self'])):
                conn.execute('INSERT INTO memory_subjects VALUES(?,?)',(mid,_subject(conn,name,stamp)))
            _sources(conn,mid,memory,clock,keys)
            keys[memory['key']] = mid
            return
        mid = keys[event['memory_key']]
        old = conn.execute('SELECT * FROM memories WHERE id=?',(mid,)).fetchone()
        if old is None or old['lifecycle']=='deleted':
            raise ValueError('timeline cannot mutate deleted memory')
        if op == 'delete':
            delete_memory(store,mid,old['revision'],_conn=conn,current=clock(),actor='persona_eval')
        elif op == 'forget':
            conn.execute('UPDATE memories SET pinned=0 WHERE id=?',(mid,))
            adjust_retention(store,mid,value=lifecycle_settings(conn)['forget_threshold']-1,_conn=conn,current=clock())
        else:
            fields = ('content','kind','stance','belief','importance','event_time')
            before = {k:old[k] for k in fields}
            after = {k:event.get(k,old[k]) for k in fields}
            conn.execute('''UPDATE memories SET content=?,kind=?,stance=?,belief=?,importance=?,event_time=?,
                revision=revision+1,updated_at=?,embedding=NULL,embedding_model=NULL WHERE id=? AND revision=?''',
                (*(after[k] for k in fields),stamp,mid,old['revision']))
            conn.execute('''INSERT INTO memory_revisions(memory_id,revision_before,revision_after,before_json,after_json,
                reason,actor,created_at) VALUES(?,?,?,?,?,'timeline edit',?,?)''',
                (mid,old['revision'],old['revision']+1,dumps(before),dumps(after),event.get('actor','persona_eval'),stamp))
            if 'speaker' in event:
                conn.execute('UPDATE memories SET speaker_subject_id=? WHERE id=?',(_subject(conn,event['speaker'],stamp),mid))
            if 'about' in event:
                conn.execute('DELETE FROM memory_subjects WHERE memory_id=?',(mid,))
                for name in dict.fromkeys(event['about']):
                    conn.execute('INSERT INTO memory_subjects VALUES(?,?)',(mid,_subject(conn,name,stamp)))
            _sources(conn,mid,event,clock,keys)


def _observation(store, case_id, checkpoint, previous, candidate):
    ids = {r['memory_id'] for v in (previous,candidate) if v for sentence in v['sentences'] for r in sentence['basis']}
    with store.read() as conn:
        zone = ZoneInfo(json.loads(conn.execute("SELECT value_json FROM runtime_settings WHERE key='timezone'").fetchone()[0]))
        memory_state = []
        for mid in sorted(ids):
            memory = conn.execute('SELECT * FROM memories WHERE id=?',(mid,)).fetchone()
            if memory is None:
                memory_state.append({'id':mid,'lifecycle':'missing'})
                continue
            item = {k:memory[k] for k in ('id','content','kind','speaker_subject_id','stance','belief','lifecycle',
                                          'revision','event_time','created_at','updated_at')}
            item['about'] = [r[0] for r in conn.execute('SELECT subject_id FROM memory_subjects WHERE memory_id=? ORDER BY subject_id',(mid,))]
            item['trace'] = _trace(conn,mid,zone)
            item['revisions'] = [dict(r) for r in conn.execute('SELECT * FROM memory_revisions WHERE memory_id=? ORDER BY id',(mid,))]
            memory_state.append(item)
        scenarios = set(checkpoint.get('scenarios',[]))
        if previous and _stale(conn,previous):
            scenarios.add('S17')
        if any(s['origin']=='memory' and not s['initial_setting'] and s['date_count']<2 for s in candidate['sentences']):
            scenarios.add('S18')
        if candidate['checks'].get('admin_content_removed_or_changed'):
            scenarios.add('S19')
        settings = _settings(conn)
        subjects = [dict(r) for r in conn.execute('SELECT id,name,kind FROM subjects ORDER BY id')]
        first = conn.execute("SELECT * FROM persona_versions WHERE source='initial_setting' ORDER BY id LIMIT 1").fetchone()
        role_name = json.loads(conn.execute("SELECT value_json FROM runtime_settings WHERE key='role_name'").fetchone()[0])
        role_settings = {'basis_kind':'role_settings','role_name':role_name,'source':'administrator_setup',
            'first_template':first['content'] if first else None, 'initialized_at':first['created_at'] if first else None,
            'first_template_sentences':json.loads(first['sentences_json']) if first else []}
    return {'timeline_id':case_id, 'checkpoint_id':checkpoint['id'], 'at':checkpoint['at'],
            'operation':checkpoint['op'], 'previous':previous, 'candidate':candidate,
            'must_reflect':checkpoint.get('must_reflect',[]), 'must_not':checkpoint.get('must_not',[]),
            'expected_change':checkpoint.get('expected_change'), 'scenarios':sorted(scenarios),
            'timezone':str(zone),'settings':settings,'subjects':subjects,'memory_state':memory_state,'role_settings':role_settings}


def _run_timeline(configs,case,directory,gateway_factory):
    clock = TimelineClock(case['start_at'])
    with tempfile.TemporaryDirectory(prefix='timeline-',dir=directory) as temporary:
        store = Store(Path(temporary)/'iris.db')
        gateway = None
        try:
            role = case.get('role',{})
            setup_role(store,role.get('name','Iris'),role.get('background',''),role.get('timezone','Asia/Shanghai'),current=clock())
            with store.write() as conn:
                stamp = clock().isoformat()
                for subject in case.get('subjects',[]):
                    sid = _subject(conn,subject['id'],stamp)
                    conn.execute('UPDATE subjects SET name=? WHERE id=?',(subject['name'],sid))
                    for alias in subject.get('aliases',[]):
                        conn.execute('INSERT OR IGNORE INTO subject_aliases(subject_id,alias) VALUES(?,?)',(sid,alias))
                for entry in case.get('entries',[]):
                    conn.execute('INSERT INTO entries(id,name,platform,kind) VALUES(?,?,?,?)',
                        (entry['id'],entry.get('name',entry['id']),entry.get('platform','eval'),entry['kind']))
                for name in ('persona_goal','persona_rules'):
                    if case.get(name) is not None:
                        conn.execute('INSERT OR REPLACE INTO runtime_settings VALUES(?,?)',(name,dumps(case[name])))
            initial = current_persona(store)
            observations = [_observation(store,case['id'],{'id':'initial','at':case['start_at'],'op':'initial'},None,initial)]
            with store.read() as conn:
                keys = {f'initial:{i+1}':r[0] for i,r in enumerate(conn.execute('SELECT id FROM memories ORDER BY id'))}
            health = ModelHealth(store,configs,clock=clock) if gateway_factory is Gateway else None
            gateway = gateway_factory(configs,store,health=health,clock=clock)
            engine, events = PersonaEngine(store,gateway,clock=clock),[]
            control_operations = []
            complete = True
            for ordinal,event in enumerate(case['events']):
                clock.value = _instant(event['at'])
                if event['op'] in ('add','edit','delete','forget'):
                    _mutate(store,event,keys,clock)
                    continue
                previous = current_persona(store)
                if event['op'] in ('persona_confirm','persona_reject'):
                    with store.read() as conn:
                        pending = conn.execute("SELECT id FROM persona_versions WHERE status='pending'").fetchone()
                    if pending is None:
                        control_operations.append({'event':event,'status':'noop','reason':'no_pending_candidate',
                                                   'current_version_id':previous['id'],'version':None})
                        continue
                    action = confirm_candidate if event['op']=='persona_confirm' else reject_candidate
                    version = action(store,pending[0],expected_version=previous['id'],clock=clock)
                    control_operations.append({'event':event,'status':'applied','version':version})
                    continue
                if event['op']=='persona_edit':
                    content = event['content'] if 'content' in event else previous['content']+''.join(event['append'])
                    candidate = admin_edit(store,content,expected_version=previous['id'],clock=clock)
                    observations.append(_observation(store,case['id'],{**event,'id':f'admin:{ordinal}'},previous,candidate))
                else:
                    result = (engine.update if event.get('action','update')=='update' else engine.regenerate)(expected_version=previous['id'])
                    events.append({'checkpoint':event,'result':result,'current':current_persona(store),
                                   'pending_update':pending_update(store)})
                    if result['version'] is not None:
                        observations.append(_observation(store,case['id'],event,previous,result['version']))
                    else:
                        # Retain execution evidence without fabricating a scoreable candidate.
                        observation = _observation(store,case['id'],{**event,'op':'checkpoint_current'},previous,current_persona(store))
                        observation['evidence'] = select_evidence(store)
                        observation['skip_reason'] = result['reason']
                        observations.append(observation)
                    if result['status'] in ('failed','conflict') or (result['status']=='skipped' and result['reason'] not in ('not_due','no_self_evidence')):
                        complete = False
            with store.read() as conn:
                attempts = [dict(r) for r in conn.execute('SELECT * FROM persona_attempts ORDER BY id')]
                calls = [dict(r) for r in conn.execute('SELECT * FROM model_calls ORDER BY id')]
            return {'case':case,'complete':complete,'observations':observations,'checkpoints':events,
                    'attempts':attempts,'calls':calls,'control_operations':control_operations}
        finally:
            if gateway:
                gateway.close()
            store.close()


def _payload(observation):
    candidate = observation['candidate']
    return {'timeline_id':observation['timeline_id'], 'checkpoint_id':observation['checkpoint_id'],
            'at':observation['at'], 'previous':observation['previous'], 'candidate':candidate,
            'evidence':observation.get('evidence',candidate['material'].get('evidence',{'memories':[]})),
            'settings':observation['settings'], 'timezone':observation['timezone'],
            'subjects':observation['subjects'],'memory_state':observation['memory_state'],'role_settings':observation['role_settings'],
            'must_reflect':observation['must_reflect'], 'must_not':observation['must_not'],
            'expected_change':observation['expected_change'], 'scenarios':observation['scenarios']}


def _observations(rows):
    return [observation for row in rows for observation in row['observations']
            if observation['operation']=='checkpoint' and observation['candidate']['source'] in ('periodic','regenerate')]


def _is_change(observation):
    candidate, previous = observation['candidate'],observation['previous']
    return (observation['operation'] == 'checkpoint' and candidate['status'] in ('current','pending') and candidate['checks'].get('passed') is True
            and previous is not None and candidate['content'] != previous['content'])


def _call_metrics(calls):
    return {'count':len(calls), 'duration_ms_p50':_percentile([c['duration_ms'] for c in calls],50),
            'duration_ms_p95':_percentile([c['duration_ms'] for c in calls],95),
            'prompt_tokens':sum(c['prompt_tokens'] or 0 for c in calls),
            'completion_tokens':sum(c['completion_tokens'] or 0 for c in calls),
            'reasoning_tokens':sum(c['reasoning_tokens'] or 0 for c in calls),
            'reasoning_usage_missing_calls':sum(c['reasoning_tokens'] is None for c in calls),
            'timings_by_purpose':{purpose:{'count':sum(c['purpose']==purpose for c in calls),
                'p50_ms':_percentile([c['duration_ms'] for c in calls if c['purpose']==purpose],50),
                'p95_ms':_percentile([c['duration_ms'] for c in calls if c['purpose']==purpose],95)}
                for purpose in dict.fromkeys(c['purpose'] for c in calls)},
            'usage_missing_calls':sum(c['prompt_tokens'] is None or c['completion_tokens'] is None for c in calls),
            'timed_out_calls':sum(bool(c.get('timed_out')) for c in calls),
            'by_purpose':dict(Counter(c['purpose'] for c in calls))}


def _metrics(rows, judgments=None):
    observations = _observations(rows)
    rejected = [o for o in observations if o['candidate']['status']=='rejected']
    changes = [i for i,o in enumerate(observations) if _is_change(o)]
    calls = [c for row in rows for c in row['calls']]
    outcomes = [item['result'] for row in rows for item in row['checkpoints']]
    all_versions = [o for row in rows for o in row['observations']]
    result = {'checkpoint_outcomes':dict(Counter(item['status'] for item in outcomes)),
              'skip_reasons':dict(Counter(item['reason'] for item in outcomes if item['status']=='skipped')),
              'changes':len(changes), 'changes_by_source':dict(Counter(observations[i]['candidate']['source'] for i in changes)),
              'change_degree_distribution':dict(Counter(observations[i]['candidate']['change_degree'] for i in changes)),
              'candidate_degree_distribution':dict(Counter(o['candidate']['change_degree'] for o in observations)),
              'generated_candidates':len(observations), 'check_rejected_candidates':len(rejected),
              'check_rejected_ratio':len(rejected)/len(observations) if observations else None,
              'initial_versions':sum(o['operation']=='initial' for o in all_versions),
              'admin_publications':sum(o['operation']=='persona_edit' for o in all_versions),
              'untriggered_checkpoints':sum(o['operation']=='checkpoint_current' for o in all_versions),
              'grounded_change_ratio':None, 'violations':{}, 'rejected_candidate_violations':{},
              'must_reflect_coverage':None,'must_not_occurrences':None,
              'calls':_call_metrics(calls), 'incomplete_timelines':sum(not r['complete'] for r in rows),
              'change_degree_comparison':[], 'checkpoint_expectations':[],
              'level_reasonable_ratio':None, 'scenarios':{key:[] for key in ('S17','S18','S19')}}
    for row in rows:
        for checkpoint in row['checkpoints']:
            expected = checkpoint['checkpoint'].get('expect')
            if expected is None:
                continue
            outcome = checkpoint['result']
            candidate = outcome['version']
            observation = next((o for o in row['observations'] if o['checkpoint_id']==checkpoint['checkpoint']['id']),None)
            actual = {'generate':outcome['reason'] not in ('not_due','no_self_evidence'),
                'change':bool(candidate and observation and _is_change(observation)),
                'publication':'published' if candidate and candidate['status']=='current' else
                              'pending_confirmation' if candidate and candidate['status']=='pending' else 'unchanged',
                'needs_update':checkpoint['pending_update']['pending'],
                'level':{'small':'小','medium':'中','large':'大'}.get(candidate['change_degree']) if candidate else None}
            compared = {key:actual[key]==expected[key] for key in actual if key in expected}
            result['checkpoint_expectations'].append({'timeline_id':row['case']['id'],
                'checkpoint_id':checkpoint['checkpoint']['id'],'expected':expected,'actual':actual,
                'fields':compared,'matches':all(compared.values())})
    for o in observations:
        candidate = o['candidate']
        if o['expected_change'] is not None:
            result['change_degree_comparison'].append({'timeline_id':o['timeline_id'],'checkpoint_id':o['checkpoint_id'],
                'expected':o['expected_change'],'actual':candidate['change_degree'],
                'matches':o['expected_change']==candidate['change_degree']})
        if 'S19' in o['scenarios'] or any(s['origin']=='admin' for s in o['previous']['sentences']):
            mode = candidate['material'].get('settings',{}).get('publish_mode','small_medium_auto')
            removed = candidate['checks'].get('admin_content_removed_or_changed',False)
            passed = (not removed or (candidate['change_degree']=='large' and
                candidate['status']==('rejected' if not candidate['checks']['passed'] else 'current' if mode=='all_auto' else 'pending')))
            result['scenarios']['S19'].append({'timeline_id':o['timeline_id'],'checkpoint_id':o['checkpoint_id'],
                'passed':passed,'manual_removed':removed,'status':candidate['status'],'publish_mode':mode})
    if judgments is not None:
        result['grounded_change_ratio'] = sum(judgments[i]['has_basis'] for i in changes)/len(changes) if changes else None
        violations, rejected_violations = Counter(),Counter()
        covered, total, forbidden, unsupported = 0,0,0,0
        for o,vote in zip(observations,judgments,strict=True):
            destination = rejected_violations if o['candidate']['status']=='rejected' else violations
            destination.update(v for sentence in vote['sentence_results'] for v in sentence['violations'])
            unsupported += sum(not sentence['supported'] for sentence in vote['sentence_results'])
            if o['candidate']['status']!='rejected':
                covered += sum(vote['must_reflect'])
                total += len(vote['must_reflect'])
                forbidden += sum(vote['must_not'])
            for scenario,violation in (('S17','保留失效依据'),('S18','单一日期泛化')):
                result['scenarios'][scenario].append({'timeline_id':o['timeline_id'],'checkpoint_id':o['checkpoint_id'],
                        'status':o['candidate']['status'],
                        'passed':all(violation not in sentence['violations'] for sentence in vote['sentence_results'])})
        result.update(violations=dict(violations),rejected_candidate_violations=dict(rejected_violations),
                      unsupported_sentences=unsupported, must_reflect_coverage=covered/total if total else None,
                      must_reflect_covered=covered, must_reflect_total=total, must_not_occurrences=forbidden,
                      level_reasonable_ratio=sum(v['level_reasonable'] for v in judgments)/len(judgments) if judgments else None)
    return result


def _load_scoring(path=None):
    path = path or files('iris').joinpath(f'prompts/{SCORING_VERSION}.md')
    if not path.is_file():
        raise ValueError('persona_scoring_v1.md is not available yet; freeze the PS scoring contract before model evaluation')
    return path.read_text(encoding='utf-8')


def _redacted(value, configs):
    encoded = dumps(value)
    for config in configs.values():
        if config.api_key:
            encoded = encoded.replace(config.api_key, '[REDACTED]')
    return json.loads(encoded)


def _export(rows, metadata, scoring, directory):
    directory.mkdir(parents=True,exist_ok=False)
    manifest = {'format_version':FORMAT_VERSION,'evaluation':'persona','run':metadata,
                'run_sha256':_write_json(directory/'run.json',{'rows':rows}),'cases':[]}
    (directory/'scoring.md').write_text(scoring,encoding='utf-8')
    manifest['scoring_sha256'] = hashlib.sha256((directory/'scoring.md').read_bytes()).hexdigest()
    for index,observation in enumerate(_observations(rows)):
        filename = f'cases/{index:04}.json'
        case_id = f'{observation["timeline_id"]}:{observation["checkpoint_id"]}'
        document = {'format_version':FORMAT_VERSION,'evaluation':'persona','case_id':case_id,
                    'source_sha256':metadata['source_sha256'],'corpus_sha256':metadata['corpus_sha256'],
                    'scoring_version':SCORING_VERSION,'input':_payload(observation)}
        digest = _write_json(directory/filename,document)
        manifest['cases'].append({'case_id':case_id,'file':filename,'sha256':digest,'judgment_file':f'{index:04}.json'})
    manifest['materials_sha256'] = _json_sha256(manifest)
    _write_json(directory/'manifest.json',manifest)
    _write_json(directory/'round-template.json',{'materials_sha256':manifest['materials_sha256']})
    return directory/'manifest.json',manifest


def _report(report,directory):
    directory.mkdir(parents=True,exist_ok=True)
    stem = 'persona-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid4().hex[:8]
    path = directory/(stem+'.json')
    _write_json(path,report)
    metrics = report['metrics']
    lines = ['# Persona evaluation', '', f"Judge mode: {report['judge_mode']}; rounds: {report.get('judge_runs',0)}.",
        'Public/dev diagnostics only; this report does not establish the M3 hidden gate.', '',
        '| Metric | Value |','| --- | --- |']
    for key in ('changes','grounded_change_ratio','generated_candidates','check_rejected_ratio',
                'must_reflect_coverage','must_not_occurrences','incomplete_timelines'):
        lines.append(f'| {key} | {metrics[key]} |')
    lines += ['', 'Call metrics: '+dumps(metrics['calls']), '', 'Violation distribution: '+dumps(metrics['violations']),
              '', 'Rejected candidate violations: '+dumps(metrics['rejected_candidate_violations']),
              '', 'Change degree distribution: '+dumps(metrics['change_degree_distribution']),
              '', 'Candidate degree distribution: '+dumps(metrics['candidate_degree_distribution']),
              '', 'Change degree comparison: '+dumps(metrics['change_degree_comparison']),
              '', 'S17 / S18 / S19: '+dumps(metrics['scenarios']),
              '', f"Disagreements: {len(report.get('disagreements',[]))}. Full details are in the adjacent JSON."]
    path.with_suffix('.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return path


def run_persona_eval(configs, root, *, corpus, out, judge_mode='model', gateway_factory=Gateway,
                     runner_identity=None, scoring_path=None):
    """Only complete timelines of the identical input fingerprint may be reused."""
    cases = load_corpus(corpus)
    if judge_mode not in ('model','external'):
        raise ValueError('invalid judge mode')
    scoring = _load_scoring(scoring_path)
    if gateway_factory is not Gateway and not runner_identity:
        raise ValueError('injected gateways require an explicit runner_identity')
    out = Path(out).resolve()
    repository = Path(__file__).resolve().parents[2]
    if out.is_relative_to(repository):
        raise ValueError('full persona artifacts must be outside the repository')
    source_dir = Path(__file__).parent
    sources = sorted(p for p in source_dir.rglob('*') if p.suffix in ('.py','.md','.sql','.json'))
    source_hash = hashlib.sha256(b''.join(str(p.relative_to(source_dir)).encode()+p.read_bytes() for p in sources)).hexdigest()
    identity = {'source_sha256':source_hash, 'cases':cases,
                'corpus_file_sha256':hashlib.sha256(Path(corpus).read_bytes()).hexdigest(), 'scoring_sha256':hashlib.sha256(scoring.encode()).hexdigest(),
                'judge_mode':judge_mode, 'runner_identity':runner_identity or 'gateway',
                'models':{kind: {'endpoint_sha256':hashlib.sha256(config.base_url.encode()).hexdigest(),
                    'model':config.model,'dimensions':config.dimensions,'reasoning_effort':config.reasoning_effort}
                    for kind,config in configs.items()}}
    signature = _json_sha256(identity)
    checkpoints = out/'.persona'/signature[:16]
    checkpoints.mkdir(parents=True,exist_ok=True)
    meta = checkpoints/'meta.json'
    if meta.exists() and _read_json(meta) != {'signature':signature}:
        raise ValueError('checkpoint fingerprint collision')
    _write_json(meta,{'signature':signature})
    rows,resumed,started = [],0,time.monotonic()
    for index,case in enumerate(cases):
        path = checkpoints/f'{index:04}.json'
        if path.exists():
            saved = _read_json(path)
            if (saved['signature'] != signature or saved['row_sha256'] != _json_sha256(saved['row'])
                    or saved['row']['case'] != case or saved['row']['complete'] is not True):
                raise ValueError('invalid or incomplete persona checkpoint')
            row,resumed = saved['row'],resumed+1
        else:
            row = _redacted(_run_timeline(configs,case,checkpoints,gateway_factory),configs)
            record = {'signature':signature,'row_sha256':_json_sha256(row),'row':row}
            if row['complete']:
                _write_json(path,record)
            else:
                _write_json(checkpoints/f'{index:04}-incomplete-{uuid4().hex[:8]}.json',record)
        rows.append(row)
    metadata = {'source_sha256':source_hash, 'corpus_sha256':_json_sha256(cases),
                'corpus_file_sha256':identity['corpus_file_sha256'], 'checkpoint_signature':signature,
                'scoring_version':SCORING_VERSION,'scoring_sha256':identity['scoring_sha256'],
                'models':identity['models'],'prompt_versions':['persona_generate_v1','persona_check_v1'],
                'timeouts_seconds':{'persona_generate':120,'persona_check':120,'judge':240},
                'resumed_cases':resumed,'elapsed_seconds':round(time.monotonic()-started,3)}
    directory = out/('judging-materials-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid4().hex[:8])
    materials,manifest = _export(rows,metadata,scoring,directory)
    report = {**metadata,'judge_mode':judge_mode,'judge_runs':0,'rows':rows,'metrics':_metrics(rows),
              'materials_sha256':manifest['materials_sha256']}
    if judge_mode=='external':
        _report(report,out)
        return materials,report
    judgments,judge_calls = _preview(configs,rows,scoring,out)
    report.update(judge_runs=1,judge_model=configs['chat'].model,judgments=judgments,
                  judge_calls=_call_metrics(judge_calls),metrics=_metrics(rows,judgments))
    return _report(_redacted(report,configs),out),report


def _has_basis(value):
    return bool(value['sentence_results']) and all(s['supported'] and not s['violations'] for s in value['sentence_results'])


def _validate_judgment(value,observation):
    fields = {'sentence_results','has_basis','must_reflect','must_not','level_reasonable','reason'}
    if not isinstance(value,dict) or set(value)!=fields:
        raise ValueError('persona judgment must have exactly the six frozen scoring fields')
    if (any(type(value[k]) is not bool for k in ('has_basis','level_reasonable'))
            or not isinstance(value['reason'],str) or not value['reason'].strip()):
        raise ValueError('invalid version verdict')
    sentences = observation['candidate']['sentences']
    rows = value['sentence_results']
    if not isinstance(rows,list) or len(rows)!=len(sentences):
        raise ValueError('judgment must include every sentence in order')
    for i,row in enumerate(rows,1):
        if (not isinstance(row,dict) or set(row)!={'index','supported','violations','reason'}
                or type(row.get('index')) is not int or row['index']!=i
                or type(row.get('supported')) is not bool
                or not isinstance(row.get('violations'),list)
                or any(v not in VIOLATIONS for v in row['violations'])
                or row['violations']!=[v for v in VIOLATIONS if v in row['violations']]
                or not isinstance(row.get('reason'),str) or not row['reason'].strip()):
            raise ValueError(f'invalid sentence judgment {i}')
    if value['has_basis'] != _has_basis(value):
        raise ValueError('has_basis contradicts sentence verdicts')
    for name in ('must_reflect','must_not'):
        if (not isinstance(value[name],list) or len(value[name])!=len(observation[name])
                or any(type(flag) is not bool for flag in value[name])):
            raise ValueError(f'invalid {name} boolean array')
    return value


def _preview(configs,rows,scoring,out):
    # Optional preview only; never shares its judgments with external materials.
    results = []
    with tempfile.TemporaryDirectory(prefix='persona-judge-',dir=out) as temporary:
        store = Store(Path(temporary)/'judge.db')
        gateway = Gateway(configs,store,health=ModelHealth(store,configs))
        try:
            for observation in _observations(rows):
                messages = [{'role':'system','content':scoring}, {'role':'user','content':dumps(_payload(observation))}]
                deadline = gateway.monotonic()+240
                for repair in (False,True):
                    gateway.health.check('chat','learning')
                    reply = gateway.chat(messages,'persona_judge_repair' if repair else 'persona_judge',
                                         max_tokens=16000,_deadline=deadline)
                    try:
                        if reply.finish_reason=='length':
                            raise ValueError('truncated judge output')
                        value,_ = parse_json_object_with_status(reply.content)
                        results.append(_validate_judgment(value,observation))
                        break
                    except ValueError:
                        if repair:
                            raise ValueError('invalid persona preview judgment after repair')
                        messages += [{'role':'assistant','content':reply.content},
                            {'role':'user','content':'输出不符合评分格式。请按评分说明输出完整 JSON，核对数组长度、顺序和真实布尔值。'}]
            with store.read() as conn:
                calls = [dict(r) for r in conn.execute('SELECT * FROM model_calls ORDER BY id')]
            return results,calls
        finally:
            gateway.close()
            store.close()


def _load_materials(directory):
    directory = Path(directory)
    manifest = _read_json(directory/'manifest.json')
    try:
        if manifest['format_version']!=FORMAT_VERSION or manifest['evaluation']!='persona':
            raise ValueError('unsupported persona materials format')
        if manifest['materials_sha256']!=_json_sha256({k:v for k,v in manifest.items() if k!='materials_sha256'}):
            raise ValueError('manifest fingerprint mismatch')
        rows = _read_json(_verified_material_file(directory,'run.json',manifest['run_sha256']))['rows']
        scoring = _verified_material_file(directory,'scoring.md',manifest['scoring_sha256'])
        run = manifest['run']
        if (run['corpus_sha256']!=_json_sha256([r['case'] for r in rows])
                or run['scoring_version']!=SCORING_VERSION or run['scoring_sha256']!=hashlib.sha256(scoring.read_bytes()).hexdigest()):
            raise ValueError('run inputs differ from bound materials')
        observations = _observations(rows)
        if len(manifest['cases'])!=len(observations):
            raise ValueError('material case count mismatch')
        for i,(entry,observation) in enumerate(zip(manifest['cases'],observations,strict=True)):
            if entry['file']!=f'cases/{i:04}.json' or entry['judgment_file']!=f'{i:04}.json':
                raise ValueError('invalid material filename')
            document = _read_json(_verified_material_file(directory,entry['file'],entry['sha256']))
            expected = {'format_version':FORMAT_VERSION,'evaluation':'persona',
                'case_id':f'{observation["timeline_id"]}:{observation["checkpoint_id"]}',
                'source_sha256':run['source_sha256'],'corpus_sha256':run['corpus_sha256'],
                'scoring_version':SCORING_VERSION,'input':_payload(observation)}
            if document!=expected or entry['case_id']!=expected['case_id']:
                raise ValueError('material input differs from run')
        return manifest,rows
    except (KeyError,TypeError) as error:
        raise ValueError('invalid persona materials structure') from error


def _combine(votes):
    if len(votes)==1:
        return votes[0],[]
    first,second = votes
    result,disagreements = {'sentence_results':[],'must_reflect':[],'must_not':[]},[]
    for i,(a,b) in enumerate(zip(first['sentence_results'],second['sentence_results'],strict=True),1):
        merged = {'index':i,'supported':a['supported'] and b['supported'],
            'violations':[v for v in VIOLATIONS if v in a['violations'] or v in b['violations']],
            'reason':'round 1: '+a['reason']+'; round 2: '+b['reason']}
        result['sentence_results'].append(merged)
        for field in ('supported','violations'):
            if a[field]!=b[field]:
                disagreements.append({'array':'sentence_results','index':i,'field':field,'round_1':a[field],'round_2':b[field]})
    for name in ('must_reflect','must_not'):
        for i,(a,b) in enumerate(zip(first[name],second[name],strict=True),1):
            result[name].append(a and b if name=='must_reflect' else a or b)
            if a!=b:
                disagreements.append({'array':name,'index':i,'round_1':a,'round_2':b})
    result['level_reasonable'] = first['level_reasonable'] and second['level_reasonable']
    if first['level_reasonable']!=second['level_reasonable']:
        disagreements.append({'field':'level_reasonable','round_1':first['level_reasonable'],'round_2':second['level_reasonable']})
    result['has_basis'] = _has_basis(result)
    result['reason'] = 'round 1: '+first['reason']+'; round 2: '+second['reason']
    return result,disagreements


def score_persona_judgments(materials,judgments,root,*,judge_model,out):
    if len(judgments) not in (1,2) or len({Path(p).resolve() for p in judgments})!=len(judgments):
        raise ValueError('supply one or two independent judgment directories')
    if not judge_model or not judge_model.strip():
        raise ValueError('--judge-model is required')
    manifest,rows = _load_materials(materials)
    observations = _observations(rows)
    rounds,records = [],[]
    for directory in map(Path,judgments):
        round_manifest = _read_json(directory/'manifest.json')
        if not isinstance(round_manifest,dict):
            raise ValueError('judgment manifest must be an object')
        if round_manifest.get('materials_sha256')!=manifest['materials_sha256']:
            raise ValueError('judgment material fingerprint mismatch')
        expected = {'manifest.json',*(e['judgment_file'] for e in manifest['cases'])}
        if {p.name for p in directory.glob('*.json')}!=expected:
            raise ValueError('missing or unexpected judgment files')
        votes = [_validate_judgment(_read_json(directory/e['judgment_file']),o)
                 for e,o in zip(manifest['cases'],observations,strict=True)]
        rounds.append(votes)
        records.append({'round':len(rounds),'sha256':_json_sha256(votes)})
    combined,disagreements = [],[]
    for i,observation in enumerate(observations):
        vote,differences = _combine([r[i] for r in rounds])
        combined.append(vote)
        disagreements.extend({'case_id':manifest['cases'][i]['case_id'],**d} for d in differences)
    report = {**manifest['run'],'judge_mode':'external','judge_model':judge_model.strip(),'judge_runs':len(rounds),
              'materials_sha256':manifest['materials_sha256'],'judgment_rounds':records,'rows':rows,
              'judgments':combined,'original_judgments':rounds,'disagreements':disagreements,'metrics':_metrics(rows,combined)}
    return _report(report,Path(out)),report
