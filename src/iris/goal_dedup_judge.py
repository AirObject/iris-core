"""Conservative goal candidate retrieval and replaceable model judgment.

This module performs no database writes. The caller snapshots inputs before the
network call, then checks revisions in a separate short transaction.
"""
from __future__ import annotations

import difflib
import json
import math
import re
import time
from importlib.resources import files

from .claim_sequences import normalize_claim, same_claim_sequences
from .models import ModelError
from .state import local_time, role_zone

PROMPT=files('iris').joinpath('prompts/goal_dedup_judge_v1.md').read_text(encoding='utf-8')
# Selected by the frozen public dev comparison: batch judgment (candidate C).
# Explicit method overrides are internal to the probe, never an admin option.
DEFAULT_METHOD='C'
DEFAULTS={'enabled':True,'budget_seconds':5.0,'concurrency':1,'queue_limit':8}
MAX_SECONDS=60.0  # Measurement override; the administrator API is capped at 10 s.
MAX_CANDIDATES=8


def settings(store=None):
    return {**DEFAULTS,**(store.setting('goal_dedup_judge',{}) if store else {})}


def connection_settings(conn):
    row=conn.execute("SELECT value_json FROM runtime_settings WHERE key='goal_dedup_judge'").fetchone()
    return {**DEFAULTS,**(json.loads(row[0]) if row else {})}


def method(options):
    return options.get('method',DEFAULT_METHOD) if options['enabled'] else 'A'


def validate_budget(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<value<=MAX_SECONDS:
        raise ValueError('goal judgment budget must be >0 and <=60 seconds')
    return float(value)


def compatible(a,b):
    left,right=set(a.get('people',[])),set(b.get('people',[]))
    return (a['kind']==b['kind'] and (not left or not right or left==right)
            and (not a.get('deadline') or not b.get('deadline') or a['deadline']==b['deadline'])
            and same_claim_sequences(a['content'],b['content']))


def select_candidates(candidates,proposed,*,dismissed=(),limit=MAX_CANDIDATES):
    ranked=[]
    for row in candidates:
        if (row['id']==proposed['id'] or row['id'] in dismissed or row.get('state','open')!='open'
                or row.get('merged_into') is not None or not compatible(row,proposed)):
            continue
        score=difflib.SequenceMatcher(None,normalize_claim(row['content']),normalize_claim(proposed['content'])).ratio()
        if score>=.25 or set(row.get('people',[])) & set(proposed.get('people',[])):
            ranked.append((score,row))
    ranked.sort(key=lambda item:(-item[0],item[1]['created_at'],item[1]['id']))
    return [row for _,row in ranked[:limit]]


def validate_decisions(raw,ids):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:
                raise ValueError('duplicate JSON field')
            result[key]=value
        return result
    # Accept only Markdown delimiter noise outside an otherwise complete JSON
    # object. No prose extraction, body repair, duplicate-key loss, or retry.
    raw=re.sub(r'^```(?:json)?\s*','',raw.strip(),flags=re.I).rstrip('` \t\r\n')
    parsed=json.loads(raw,object_pairs_hook=unique)
    if not isinstance(parsed,dict) or set(parsed)!={'decisions'} or not isinstance(parsed['decisions'],list):
        raise ValueError('invalid goal judgment envelope')
    rows=parsed['decisions']
    if len(rows)!=len(ids):
        raise ValueError('missing goal judgment')
    for row,gid in zip(rows,ids):
        if (not isinstance(row,dict) or set(row)!={'id','verdict'} or type(row['id']) is not int
                or row['id']!=gid or row['verdict'] not in ('same','different','uncertain')):
            raise ValueError('invalid goal judgment item')
    return {row['id']:row['verdict'] for row in rows}


def decision_from_verdicts(verdicts,*,origin,overflow=False):
    same=[gid for gid,verdict in verdicts.items() if verdict=='same']
    possible=[gid for gid,verdict in verdicts.items() if verdict in ('same','uncertain')]
    if len(same)==1 and len(possible)==1 and origin!='admin' and not overflow:
        return {'status':'merged','target_id':same[0]}
    if possible:
        return {'status':'possible_duplicate','target_id':possible[0],'target_ids':possible}
    return {'status':'created','target_id':None}


def _person(conn,sid):
    row=conn.execute('SELECT name FROM subjects WHERE id=?',(sid,)).fetchone()
    return {'id':sid,'name':row[0] if row else sid,
            'aliases':[r[0] for r in conn.execute('SELECT alias FROM subject_aliases WHERE subject_id=? ORDER BY alias',(sid,))]}


def _local_time(value,zone):
    # Legacy learning deadlines may be unresolved prose; preserve it as data.
    try:
        return local_time(value,zone)
    except (ValueError,OverflowError):
        return value


def material(conn,row):
    zone=role_zone(conn)
    entry=conn.execute('SELECT kind FROM entries WHERE id=?',(row['entry_id'],)).fetchone()
    sources=conn.execute('''SELECT m.sender_subject_id,m.content,m.occurred_at,e.kind AS entry_kind
        FROM goal_sources gs JOIN messages m ON m.id=gs.message_id JOIN entries e ON e.id=m.entry_id
        WHERE gs.goal_id=? ORDER BY m.id DESC LIMIT 9''',(row['id'],)).fetchall()
    return {'id':row['id'],'content':{'text':row['content'],'at':_local_time(row['created_at'],zone)},
            'people':[_person(conn,sid) for sid in row['people']],
            'deadline':_local_time(row['deadline'],zone),'entry_kind':entry[0] if entry else None,
            'sources':[{'sender':_person(conn,s['sender_subject_id']),'text':s['content'][:1000],
                        'at':_local_time(s['occurred_at'],zone),'entry_kind':s['entry_kind'],'truncated':len(s['content'])>1000}
                       for s in reversed(sources[:8])], 'sources_truncated':len(sources)>8}


def judge(gateway,incoming,candidates,*,method,budget_seconds):
    started=time.monotonic()
    budget=validate_budget(budget_seconds)
    if method not in ('B','C'):
        raise ValueError('unknown model goal deduplication method')
    if not gateway or not hasattr(gateway,'goal_dedup_judge'):
        raise ModelError('configuration','goal judgment unavailable',reason='unconfigured')
    groups=[[row] for row in candidates] if method=='B' else [candidates]
    verdicts={}
    for group in groups:
        remaining=budget-(time.monotonic()-started)
        if remaining<=0:
            raise ModelError('retryable','goal judgment total timeout',reason='timeout')
        payload={'incoming':incoming,'candidates':group}
        messages=[{'role':'system','content':PROMPT},{'role':'user','content':json.dumps(payload,ensure_ascii=False)}]
        response=gateway.goal_dedup_judge(messages,[row['id'] for row in group],budget_seconds=remaining)
        verdicts.update(response['decisions'])
    if time.monotonic()-started>budget:
        raise ModelError('retryable','goal judgment total timeout',reason='timeout')
    return verdicts
