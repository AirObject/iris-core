"""Versioned summaries of people, derived exclusively from globally visible memories.

This module has no HTTP or scheduler call site. Host projections are read-only and
recheck current visibility; model calls run outside all database transactions.
"""
from __future__ import annotations

import copy
import json
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from importlib.resources import files
from threading import Lock
from zoneinfo import ZoneInfo

from .db import dumps
from .memory_ops import Visibility, operation
from .models import Gateway, ModelError
from .persona import split_sentences
from .profile_common import ChatHealthGate, evidence_hash, excerpt, json_object, setting, whitespace
from .query_analysis import SubjectNames
from .queue import estimate_tokens

DEFAULTS = dict(enabled=True, publish_mode='check_auto', memory_tokens=3000,
                min_memories=8, min_changed=5, update_days=14, max_subjects_per_run=10,
                max_items=5, max_chars=200, max_tokens=250,
                generate_timeout_seconds=120, check_timeout_seconds=120)
KINDS = {'relationship':'与我的关系', 'fact':'稳定事实', 'preference':'偏好与习惯', 'recent':'近期重要事项'}
PUBLISH_MODES = ('check_auto','all_manual')
DEGREES = ('small','medium','large')
VIOLATIONS = ('无依据','单次互动泛化','性别推断','个人评价','敏感个人信息','保密内容','指令','他人的事情','转述未标明','过期事项','虚构')
_ACTIVE_LOCK = Lock()
_ACTIVE_ATTEMPTS = set()


def utc_now():
    return datetime.now(timezone.utc)


def _instant(value):
    result=datetime.fromisoformat(value) if isinstance(value,str) else value
    if not isinstance(result,datetime) or result.tzinfo is None:
        raise ValueError('person summary time requires a timezone')
    return result


class PersonSummaryConflict(ValueError):
    """The current version, settings, subject or evidence changed."""


class PersonSummaryBusy(ValueError):
    """A generation is already admitted for this person."""


def _runtime(conn):
    config={**DEFAULTS,**setting(conn,'person_summary',{})}
    if type(config['enabled']) is not bool or config['publish_mode'] not in PUBLISH_MODES:
        raise ValueError('invalid person summary settings')
    for key in DEFAULTS.keys()-{'enabled','publish_mode'}:
        if type(config[key]) is not int or config[key]<1:
            raise ValueError('invalid person summary settings')
    if (any(config[key]>DEFAULTS[key] for key in ('memory_tokens','max_items','max_chars','max_tokens',
                                                 'generate_timeout_seconds','check_timeout_seconds'))
            or config['min_memories']<8 or config['min_changed']<5 or config['update_days']<14
            or config['max_subjects_per_run']>100):
        raise ValueError('person summary settings exceed specification')
    return config


def _subject(conn, subject_id, *, writable=False):
    row=conn.execute('SELECT * FROM subjects WHERE id=?',(subject_id,)).fetchone()
    if row is None:
        raise KeyError(subject_id)
    if writable and (row['kind']!='person' or row['id'] in ('self','scene') or row['merged_into'] is not None):
        raise ValueError('subject_unavailable')
    return dict(row)


def _settings(conn, subject_id):
    _subject(conn,subject_id); defaults=_runtime(conn)
    row=conn.execute('SELECT * FROM person_summary_settings WHERE subject_id=?',(subject_id,)).fetchone()
    return (dict(row)|{'enabled':bool(row['enabled'])}) if row else dict(subject_id=subject_id,enabled=True,
        publish_mode=defaults['publish_mode'],revision=0,current_version=0,needs_regeneration=0,updated_at=None)


def summary_settings(store, subject_id):
    with store.read() as conn:
        return _settings(conn,subject_id)


def _ensure(conn, subject_id, stamp):
    value=_settings(conn,subject_id)
    if value['revision']==0:
        conn.execute('INSERT INTO person_summary_settings(subject_id,publish_mode,updated_at) VALUES(?,?,?)',
                     (subject_id,value['publish_mode'],stamp))
    return _settings(conn,subject_id)


def _decode(row):
    if row is None:
        return None
    value=dict(row)
    for key in ('sentences','checks','material','memory_state'):
        value[key]=json.loads(value.pop(key+'_json'))
    return value


def _version(conn, subject_id, version):
    row=conn.execute('SELECT * FROM person_summary_versions WHERE subject_id=? AND version=?',(subject_id,version)).fetchone()
    if row is None:
        raise KeyError((subject_id,version))
    return _decode(row)


def _current(conn, subject_id):
    version=_settings(conn,subject_id)['current_version']
    return _version(conn,subject_id,version) if version else None


def _expect(conn, subject_id, expected_version):
    if type(expected_version) is not int or expected_version<0:
        raise ValueError('expected_version must be a nonnegative integer')
    current=_current(conn,subject_id)
    if (current['version'] if current else 0)!=expected_version:
        raise PersonSummaryConflict('current_version_changed')
    return current


def _about(conn, memory_id):
    return [dict(r) for r in conn.execute('''SELECT s.id,s.name FROM memory_subjects ms
        JOIN subjects s ON s.id=ms.subject_id WHERE ms.memory_id=? ORDER BY s.id''',(memory_id,))]


def _related(row, about, subject_id, names):
    if row['speaker_subject_id']==subject_id or any(s['id']==subject_id for s in about):
        return True
    # Reuse longest-label and word boundaries. An ambiguous name alone does not
    # identify a particular person; explicit about/speaker still works normally.
    return any(names.labels[m.group().casefold()]=={subject_id} for m in names.pattern.finditer(row['content']))


def _memory_hash(row, about):
    keys=('id','revision','content','kind','speaker_subject_id','stance','belief','event_time','world','last_confirmed_at')
    return evidence_hash({**{key:row[key] for key in keys},'about':[s['id'] for s in about],'tags':row.get('tags',[])})


def _restricted_memories(conn):
    """Declared restrictions, including preserved roots and all descendants.

    Unlike effective Visibility, this never treats a restricted declaration as
    global merely because its allowed set happens to contain every current entry.
    The UNION recursion terminates on cycles and reads no message prose.
    """
    return {r[0] for r in conn.execute("""WITH RECURSIVE restricted(id) AS (
        SELECT s.memory_id FROM sources s JOIN messages x ON x.id=s.message_id
            JOIN entries e ON e.id=x.entry_id WHERE s.kind='message' AND e.visibility!='shared'
        UNION SELECT v.memory_id FROM memory_visibility_roots v JOIN entries e ON e.id=v.entry_id
            WHERE e.visibility!='shared'
        UNION SELECT s.memory_id FROM restricted r JOIN sources s ON s.source_memory_id=r.id
            WHERE s.kind='memory'
    ) SELECT id FROM restricted""")}


def _dependencies(conn, memory_ids):
    return {r[0] for r in conn.execute("""WITH RECURSIVE ancestry(id) AS (
        SELECT value FROM json_each(?) UNION SELECT s.source_memory_id FROM ancestry a
        JOIN sources s ON s.memory_id=a.id WHERE s.kind='memory' AND s.source_memory_id IS NOT NULL
    ) SELECT id FROM ancestry""",(dumps(sorted(set(memory_ids))),))}


def _eligible(conn, subject_id, current):
    visibility=Visibility(conn); names=SubjectNames(conn)
    where=visibility.memory_sql(None)
    restricted=dumps(sorted(_restricted_memories(conn)))
    rows=[]
    for row in conn.execute(f'''SELECT m.* FROM memories m WHERE m.lifecycle='active' AND m.purged_at IS NULL
        AND julianday(m.created_at)<=julianday(?) AND {where}
        AND m.id NOT IN (SELECT value FROM json_each(?))
        ORDER BY m.importance DESC,m.retention DESC,
                 MAX(julianday(m.updated_at),julianday(m.last_confirmed_at)) DESC,m.id''',(current.isoformat(),restricted)):
        row=dict(row); row['tags']=[r[0] for r in conn.execute('SELECT tag FROM memory_tags WHERE memory_id=? ORDER BY tag',(row['id'],))]
        about=_about(conn,row['id'])
        if _related(row,about,subject_id,names):
            rows.append((dict(row),about))
    return rows


def _select(conn, subject_id, current):
    subject=_subject(conn,subject_id,writable=True); config=_runtime(conn)
    rows=_eligible(conn,subject_id,current); selected=[]
    state={str(row['id']):_memory_hash(row,about) for row,about in rows}
    for row,about in rows:
        speaker=_subject(conn,row['speaker_subject_id'])
        item={key:row[key] for key in ('revision','kind','stance','belief','importance','retention','event_time',
                                      'world','created_at','updated_at','first_confirmed_at','last_confirmed_at','tags')}
        item.update(ref=f'M{len(selected)+1}',memory_id=row['id'],content=excerpt(row['content'],600),
                    speaker={'id':speaker['id'],'name':speaker['name']},about=about,sha256=state[str(row['id'])])
        item['truncated']=item['content']!=row['content']
        if estimate_tokens(dumps([*selected,item]))<=config['memory_tokens']:
            selected.append(item)
    aliases=[r[0] for r in conn.execute("SELECT a.alias FROM subject_aliases a LEFT JOIN messages x ON x.id=a.source_message_id LEFT JOIN entries e ON e.id=x.entry_id WHERE a.subject_id=? AND a.folded_into IS NULL AND (a.source_message_id IS NULL OR e.visibility='shared') ORDER BY a.alias",(subject_id,))]
    return {'subject':{key:subject[key] for key in ('id','name','revision')}|{'aliases':aliases},
            'as_of':current.isoformat(),'timezone':setting(conn,'timezone','Asia/Shanghai'),
            'role_name':setting(conn,'role_name','Iris'),'memories':selected,
            'memory_count':len(rows),'memory_tokens':estimate_tokens(dumps(selected)),'memory_state':state}


def select_material(store, subject_id, *, clock=utc_now):
    with store.read() as conn:
        return _select(conn,subject_id,_instant(clock()))


def _basis(item):
    return {key:item[key] for key in ('memory_id','revision','sha256')}


def _stale(conn, subject_id, sentences, *, current=None):
    result=[]; seen=set(); visibility=Visibility(conn); names=SubjectNames(conn)
    restricted=_restricted_memories(conn)
    today=current.astimezone(ZoneInfo(setting(conn,'timezone','Asia/Shanghai'))).date() if current else None
    for sentence in sentences:
        if sentence.get('valid_until') and current and date.fromisoformat(sentence['valid_until'])<today:
            result.append({'reason':'time_range_expired','valid_until':sentence['valid_until']})
        for basis in sentence['basis']:
            key=dumps(basis)
            if key in seen:
                continue
            seen.add(key)
            row=conn.execute('SELECT * FROM memories WHERE id=?',(basis['memory_id'],)).fetchone()
            reason=None
            if row is None or row['purged_at']:
                reason='memory_purged'
            elif row['lifecycle']!='active':
                reason='memory_unavailable'
            elif row['id'] in restricted or visibility.memory(row['id']) is not None:
                reason='visibility_changed'
            else:
                row=dict(row); row['tags']=[r[0] for r in conn.execute('SELECT tag FROM memory_tags WHERE memory_id=? ORDER BY tag',(row['id'],))]
                about=_about(conn,row['id'])
                if not _related(row,about,subject_id,names):
                    reason='no_longer_related'
                elif row['revision']!=basis['revision'] or _memory_hash(row,about)!=basis.get('sha256'):
                    reason='memory_changed'
            if reason:
                result.append({**basis,'reason':reason})
    return result


def _view(conn, value, *, current=None):
    if value is None:
        return None
    value={**value,'stale_basis':_stale(conn,value['subject_id'],value['sentences'],current=current)}
    subject=_subject(conn,value['subject_id']); settings=_settings(conn,value['subject_id'])
    if value['invalidated_reason'] or value['cleared_at']:
        value['stale_basis'].append({'reason':value['invalidated_reason'] or 'memory_purged'})
    if subject['revision']!=value['subject_revision'] or settings['needs_regeneration']:
        value['stale_basis'].append({'reason':'subject_changed'})
    value['possibly_stale']=bool(value['stale_basis'])
    value['is_current']=settings['current_version']==value['version']
    return value


def _visible(conn, value, entry_id):
    visibility=Visibility(conn); visibility.entry(entry_id)
    restricted=_restricted_memories(conn)
    ids={b['memory_id'] for s in value['sentences'] for b in s['basis']}
    unavailable={r[0] for r in conn.execute('SELECT value FROM json_each(?) WHERE value NOT IN (SELECT id FROM memories WHERE purged_at IS NULL)',(dumps(sorted(ids)),))}
    return not (ids & restricted or unavailable) and all(visibility.memory_visible(mid,entry_id) for mid in ids)


def current_summary(store, subject_id, *, entry_id=None, clock=utc_now):
    """Admin detail without an entry; otherwise only the safe host projection."""
    with store.read() as conn:
        if entry_id is not None:
            return summary_context(conn,subject_id,entry_id,clock=clock)
        return _view(conn,_current(conn,subject_id),current=_instant(clock()))


def summary_context(conn, subject_id, entry_id, *, clock=utc_now):
    """Future prepare item. No writes/calls; callers supply an authorized entry."""
    subject=_subject(conn,subject_id); settings=_settings(conn,subject_id)
    if (subject['kind']!='person' or subject_id in ('self','scene') or subject['merged_into'] is not None or not settings['enabled'] or not _runtime(conn)['enabled']):
        return None
    value=_view(conn,_current(conn,subject_id),current=_instant(clock()))
    if (value is None or not value['content'] or value['status']!='published' or value['invalidated_reason']
            or value['cleared_at'] or not _visible(conn,value,entry_id)
            or any(s['reason']=='time_range_expired' for s in value['stale_basis'])):
        return None
    return {'subject_id':subject_id,'name':subject['name'],'text':value['content'],'version':value['version'],
            'generated_at':value['generated_at'],'possibly_stale':value['possibly_stale']}


def get_version(store, subject_id, version, *, clock=utc_now):
    with store.read() as conn:
        return _view(conn,_version(conn,subject_id,version),current=_instant(clock()))


def list_versions(store, subject_id, *, limit=50, offset=0, clock=utc_now):
    if type(limit) is not int or not 1<=limit<=200 or type(offset) is not int or offset<0:
        raise ValueError('invalid pagination')
    with store.read() as conn:
        _subject(conn,subject_id)
        return [_view(conn,_decode(row),current=_instant(clock())) for row in conn.execute(
            'SELECT * FROM person_summary_versions WHERE subject_id=? ORDER BY version DESC LIMIT ? OFFSET ?',
            (subject_id,limit,offset))]


def _due(conn, subject_id, current):
    subject=_subject(conn,subject_id); config=_runtime(conn); settings=_settings(conn,subject_id)
    previous=_current(conn,subject_id)
    rows=_eligible(conn,subject_id,current) if subject['kind']=='person' and subject_id not in ('self','scene') and not subject['merged_into'] else []
    state={str(row['id']):_memory_hash(row,about) for row,about in rows}
    old=previous['memory_state'] if previous else {}
    changed=sum(state.get(mid)!=old.get(mid) for mid in state.keys()|old.keys())
    stale=bool(previous and _view(conn,previous,current=current)['possibly_stale'])
    reason=('subject_kind' if subject['kind']!='person' or subject_id in ('self','scene') else 'subject_merged' if subject['merged_into'] else
            'disabled' if not config['enabled'] or not settings['enabled'] else
            'insufficient_memories' if len(rows)<config['min_memories'] else None)
    if reason is None and previous and not (stale or settings['needs_regeneration'] or changed>=config['min_changed'] or
            changed and current-_instant(previous['generated_at'])>=timedelta(days=config['update_days'])):
        reason='not_due'
    return {'due':reason is None,'reason':reason,'global_memories':len(rows),'changed_memories':changed,
            'current_version':previous['version'] if previous else 0,'possibly_stale':stale}


def summary_due(store, subject_id, *, clock=utc_now):
    with store.read() as conn:
        return _due(conn,subject_id,_instant(clock()))


def _audit(conn, action, subject_id, stamp, **details):
    operation(conn,'person_summary_'+action,'person_summary',subject_id,details,
              actor='person_summary' if action.startswith('generation') else 'admin',stamp=stamp)


def set_settings(store, subject_id, *, expected_version, enabled=None, publish_mode=None, clock=utc_now):
    if enabled is not None and type(enabled) is not bool:
        raise ValueError('enabled must be boolean')
    if publish_mode is not None and publish_mode not in PUBLISH_MODES:
        raise ValueError('invalid publish mode')
    stamp=_instant(clock()).isoformat()
    with store.write() as conn:
        _subject(conn,subject_id,writable=True); _expect(conn,subject_id,expected_version)
        settings=_ensure(conn,subject_id,stamp)
        values={'enabled':settings['enabled'] if enabled is None else enabled,
                'publish_mode':settings['publish_mode'] if publish_mode is None else publish_mode}
        conn.execute('''UPDATE person_summary_settings SET enabled=?,publish_mode=?,revision=revision+1,
            updated_at=? WHERE subject_id=?''',(values['enabled'],values['publish_mode'],stamp,subject_id))
        _audit(conn,'settings',subject_id,stamp,expected_version=expected_version,**values)
        return _settings(conn,subject_id)


def _length_errors(content, sentences, config):
    return (['empty'] if not content.strip() else ['too_many_items'] if len(sentences)>config['max_items'] else
            ['too_long'] if len(content)>config['max_chars'] else
            ['token_budget'] if estimate_tokens(content)>config['max_tokens'] else [])


def _insert(conn, subject_id, *, previous, content, sentences, checks, degree, status, stamp,
            author, source, material, memory_state, settings_revision, subject_revision):
    version=conn.execute('SELECT COALESCE(MAX(version),0)+1 FROM person_summary_versions WHERE subject_id=?',(subject_id,)).fetchone()[0]
    for row in conn.execute("SELECT version,checks_json FROM person_summary_versions WHERE subject_id=? AND status='candidate'",(subject_id,)).fetchall():
        old=json.loads(row['checks_json']); old.update(reason='superseded',superseded_by=version)
        conn.execute("UPDATE person_summary_versions SET status='rejected',checks_json=? WHERE subject_id=? AND version=?",(dumps(old),subject_id,row['version']))
    vid=conn.execute('''INSERT INTO person_summary_versions(subject_id,version,base_version,content,sentences_json,
        checks_json,change_degree,status,author,source,generated_at,published_at,settings_revision,subject_revision,
        material_json,memory_state_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (subject_id,version,previous,content,dumps(sentences),dumps(checks),degree,status,author,source,stamp,
         stamp if status=='published' else None,settings_revision,subject_revision,dumps(material),dumps(memory_state))).lastrowid
    ids={b['memory_id'] for s in sentences for b in s['basis']}
    ids.update(m['memory_id'] for m in material.get('evidence',{}).get('memories',[]))
    ids.update(material.get('dependency_ids',[]))
    ids=_dependencies(conn,ids)
    conn.executemany('INSERT INTO person_summary_version_memories(version_id,memory_id) VALUES(?,?)',((vid,mid) for mid in sorted(ids)))
    if status=='published':
        conn.execute('UPDATE person_summary_settings SET current_version=?,needs_regeneration=0 WHERE subject_id=?',(version,subject_id))
    _audit(conn,'generation_'+status if author=='model' else source,subject_id,stamp,version=version,base_version=previous)
    return _view(conn,_version(conn,subject_id,version),current=_instant(stamp))


def _edit_segments(content, previous):
    # Keep an unchanged structured line with its evidence even if its text has punctuation.
    import re
    known=sorted({s['text'] for s in previous['sentences'] if s.get('kind') in KINDS} if previous else [],key=len,reverse=True)
    if not known:
        return split_sentences(content)
    parts=[]; start=0
    for match in re.finditer('^(?:'+'|'.join(re.escape(text) for text in known)+')',content,re.MULTILINE):
        parts.extend(split_sentences(content[start:match.start()])); parts.append(match.group()); start=match.end()
    parts.extend(split_sentences(content[start:]))
    return parts


def admin_edit(store, subject_id, content, *, expected_version, clock=utc_now):
    stamp=_instant(clock()).isoformat()
    with store.write() as conn:
        subject=_subject(conn,subject_id,writable=True); previous=_expect(conn,subject_id,expected_version)
        if not isinstance(content,str):
            raise ValueError('summary must be text')
        old=defaultdict(list)
        for item in previous['sentences'] if previous else []:
            old[item['text']].append(item)
        sentences=[old[text].pop(0) if old[text] else {'text':text,'basis':[],'author':'admin'}
                   for text in _edit_segments(content,previous)]
        if _length_errors(content,sentences,_runtime(conn)):
            raise ValueError('summary must be nonempty, at most 5 items / 200 characters / 250 tokens')
        settings=_ensure(conn,subject_id,stamp); selected=_select(conn,subject_id,_instant(stamp))
        return _insert(conn,subject_id,previous=expected_version,content=content,sentences=sentences,
            checks={'passed':True,'deterministic':{'passed':True},'model':None,'admin_edit':True},degree='large',
            status='published',stamp=stamp,author='admin',source='edit',material={},memory_state=selected['memory_state'],
            settings_revision=settings['revision'],subject_revision=subject['revision'])


def version_diff(store, subject_id, before, after):
    with store.read() as conn:
        left=_version(conn,subject_id,before)['sentences']; right=_version(conn,subject_id,after)['sentences']
        return {'subject_id':subject_id,'before':before,'after':after,'changed':left!=right,
                'sentences':[{'op':op,'before':left[a:b],'after':right[c:d]} for op,a,b,c,d in
                             SequenceMatcher(a=[s['text'] for s in left],b=[s['text'] for s in right],autojunk=False).get_opcodes()]}


def rollback(store, subject_id, version, *, expected_version, clock=utc_now):
    stamp=_instant(clock()).isoformat()
    with store.write() as conn:
        subject=_subject(conn,subject_id,writable=True); _expect(conn,subject_id,expected_version)
        target=_version(conn,subject_id,version)
        if (target['status']!='published' or target['cleared_at'] or target['invalidated_reason'] or
                _length_errors(target['content'],target['sentences'],_runtime(conn))):
            raise ValueError('rollback requires a previously published valid summary')
        settings=_ensure(conn,subject_id,stamp)
        return _insert(conn,subject_id,previous=expected_version,content=target['content'],sentences=target['sentences'],
            checks={**target['checks'],'rollback_from':version},degree='large',status='published',stamp=stamp,
            author='admin',source='rollback',material=target['material'],memory_state=target['memory_state'],
            settings_revision=settings['revision'],subject_revision=subject['revision'])


def confirm_candidate(store, subject_id, version, *, expected_version, clock=utc_now):
    stamp=_instant(clock()).isoformat()
    with store.write() as conn:
        subject=_subject(conn,subject_id,writable=True); _expect(conn,subject_id,expected_version)
        value=_version(conn,subject_id,version)
        if value['status']!='candidate' or value['base_version']!=expected_version:
            raise PersonSummaryConflict('candidate_changed')
        if (not value['checks']['passed'] or value['cleared_at'] or value['invalidated_reason'] or
                subject['revision']!=value['subject_revision'] or _stale(conn,subject_id,value['sentences'],current=_instant(stamp))):
            raise PersonSummaryConflict('evidence_changed')
        conn.execute("UPDATE person_summary_versions SET status='published',published_at=? WHERE subject_id=? AND version=?",(stamp,subject_id,version))
        conn.execute('UPDATE person_summary_settings SET current_version=?,needs_regeneration=0 WHERE subject_id=?',(version,subject_id))
        _audit(conn,'confirm',subject_id,stamp,version=version,base_version=expected_version)
        return _view(conn,_version(conn,subject_id,version),current=_instant(stamp))


def reject_candidate(store, subject_id, version, *, expected_version, reason='administrator_rejected', clock=utc_now):
    stamp=_instant(clock()).isoformat()
    with store.write() as conn:
        _subject(conn,subject_id,writable=True); _expect(conn,subject_id,expected_version)
        value=_version(conn,subject_id,version)
        if value['status']!='candidate':
            raise PersonSummaryConflict('candidate_changed')
        conn.execute("UPDATE person_summary_versions SET status='rejected',checks_json=? WHERE subject_id=? AND version=?",
                     (dumps({**value['checks'],'reason':reason}),subject_id,version))
        _audit(conn,'reject',subject_id,stamp,version=version)
        return _view(conn,_version(conn,subject_id,version),current=_instant(stamp))


def _render(item):
    scope='（'+item['time_range']+'）' if item.get('time_range') else ''
    return KINDS[item['kind']]+'：'+item['body'].rstrip('。')+scope+'。\n'


def _resolve(generated, evidence, previous, current):
    if set(generated)!={'items'} or not isinstance(generated['items'],list):
        raise ValueError('invalid_output')
    protected=copy.deepcopy([s for s in previous['sentences'] if s['author']=='admin'] if previous else [])
    manual={s['text'].strip() for s in protected}; refs={m['ref']:m for m in evidence['memories']}
    sentences=[]; deleted=[]; count=len(generated['items']); relationships=0
    for index,item in enumerate(generated['items'],1):
        reason=None; valid=None
        if (not isinstance(item,dict) or not {'kind','text','memory_refs'}<=set(item)
                or set(item)-{'kind','text','memory_refs','time_range','valid_until'}
                or not isinstance(item.get('kind'),str) or item['kind'] not in KINDS or not isinstance(item.get('text'),str) or not item['text'].strip()
                or not isinstance(item.get('memory_refs'),list) or not 1<=len(item['memory_refs'])<=3
                or any(not isinstance(ref,str) or ref not in refs for ref in item['memory_refs'])
                or len(set(item['memory_refs']))!=len(item['memory_refs'])):
            reason='invalid_item_or_references'
        else:
            valid={'kind':item['kind'],'body':whitespace(item['text']),
                   'basis':[_basis(refs[ref]) for ref in item['memory_refs']], 'author':'model'}
            if item['kind']=='recent':
                if not isinstance(item.get('time_range'),str) or not item['time_range'].strip() or len(item['time_range'])>40:
                    reason='missing_time_range'
                else:
                    valid['time_range']=whitespace(item['time_range'])
                if 'valid_until' in item:
                    try:
                        until=date.fromisoformat(item['valid_until'])
                        if until.isoformat()!=item['valid_until'] or until<current.date():
                            raise ValueError('expired')
                        valid['valid_until']=until.isoformat()
                    except (TypeError,ValueError):
                        reason='invalid_or_expired_time_range'
            elif 'time_range' in item or 'valid_until' in item:
                reason='unexpected_time_range'
            if not reason:
                valid['text']=_render(valid)
                if valid['text'].strip() in manual or item['text'].strip() in manual:
                    count-=1
                    continue
                if item['kind']=='relationship':
                    relationships+=1
                    if relationships>1:
                        reason='too_many_relationships'
        if reason:
            deleted.append({'index':index,'item':item,'reason':reason,'stage':'references'})
        else:
            sentences.append(valid)
    return [*sentences,*protected],deleted,count


def _check_input(material, sentences):
    memories={m['memory_id']:m for m in material['evidence']['memories']}
    return {'subject':material['evidence']['subject'],'as_of':material['evidence']['as_of'],
            'timezone':material['evidence']['timezone'],
            'previous':material['previous'],'candidate':[
                {**{k:v for k,v in s.items() if k!='basis'},'index':i+1,
                 'evidence':[{k:v for k,v in memories[b['memory_id']].items() if k!='sha256'} for b in s['basis']]}
                for i,s in enumerate(sentences)]}


def _check(output, sentences):
    if (set(output)!={'items','change_degree','reason'} or not isinstance(output.get('items'),list)
            or len(output['items'])!=len(sentences) or output.get('change_degree') not in DEGREES
            or not isinstance(output.get('reason'),str) or not output['reason'].strip()):
        raise ValueError('invalid_check_output')
    kept=[]; deleted=[]; warnings=[]
    for index,(item,sentence) in enumerate(zip(output['items'],sentences,strict=True),1):
        if (not isinstance(item,dict) or set(item)!={'index','supported','scene_qualified','violations','reason'}
                or type(item['index']) is not int or item['index']!=index or type(item['supported']) is not bool
                or type(item['scene_qualified']) is not bool or not isinstance(item['reason'],str) or not item['reason'].strip()
                or not isinstance(item['violations'],list) or item['violations']!=[v for v in VIOLATIONS if v in item['violations']]):
            raise ValueError('invalid_check_item')
        if sentence['author']=='admin':
            kept.append(sentence)
            if item['violations']:
                warnings.append({'index':index,'violations':item['violations'],'reason':item['reason']})
        elif not item['supported'] or not item['scene_qualified'] or item['violations']:
            deleted.append({'index':index,'sentence':sentence,'reason':item['reason'],
                            'violations':item['violations'],'stage':'check'})
        else:
            kept.append(sentence)
    return kept,deleted,warnings,output['change_degree']


def _safe_previous(conn, subject_id, previous, current):
    if previous is None or previous['cleared_at'] or previous['invalidated_reason']:
        return '',[],[]
    safe=[s for s in previous['sentences'] if s['author']=='admin' or not _stale(conn,subject_id,[s],current=current)]
    return ''.join(s['text'] for s in safe),[s for s in safe if s['author']=='admin'],[b for s in safe for b in s['basis']]


def _recover_interrupted(conn, store, subject_id, stamp):
    for row in conn.execute("SELECT id FROM person_summary_attempts WHERE subject_id=? AND state='running'",(subject_id,)).fetchall():
        with _ACTIVE_LOCK:
            active=(store.path,row['id']) in _ACTIVE_ATTEMPTS
        if not active:
            conn.execute("UPDATE person_summary_attempts SET state='failed',reason='interrupted',finished_at=? WHERE id=?",(stamp,row['id']))
            _audit(conn,'generation_interrupted',subject_id,stamp,attempt_id=row['id'])


class PersonSummaryEngine:
    def __init__(self, store, gateway, *, clock=utc_now):
        self.store,self.gateway,self.clock=store,gateway,clock

    def update(self, subject_id, *, expected_version, run_id=None):
        return self._run(subject_id,expected_version,'periodic',run_id=run_id)

    def regenerate(self, subject_id, *, expected_version):
        return self._run(subject_id,expected_version,'regenerate')

    def _gateway(self):
        gateway=self.gateway
        if gateway is None or not callable(getattr(gateway,'chat',None)):
            raise ModelError('paused','unconfigured',paused=True,reason='unconfigured')
        if isinstance(gateway,Gateway):
            health=gateway.health
            if health is None:
                from .model_health import ModelHealth
                health=ModelHealth(self.store,gateway._raw_configs,clock=self.clock)
            gateway=copy.copy(gateway); gateway.health=ChatHealthGate(health)
        return gateway

    def _call(self, gateway, kind, payload, outputs, calls, config):
        if getattr(gateway,'health',None):
            gateway.health.check('chat','learning')
        purpose='consolidation_person_summary_'+kind
        prompt=files('iris').joinpath(f'prompts/person_summary_{kind}_v1.md').read_text(encoding='utf-8')
        messages=[{'role':'system','content':prompt},{'role':'user','content':dumps(payload)}]
        monotonic=getattr(gateway,'monotonic',time.monotonic); started=monotonic()
        try:
            reply=gateway.chat(messages,purpose,max_tokens=16000,_deadline=started+config[kind+'_timeout_seconds'])
        except ModelError as error:
            calls.append({'purpose':purpose,'result':error.category,'reason':error.reason or error.category,
                          'duration_ms':round((monotonic()-started)*1000),'usage':{}})
            raise
        outputs[purpose]=reply.content
        calls.append({'purpose':purpose,'result':'success','duration_ms':round((monotonic()-started)*1000),
                      'usage':reply.usage,'finish_reason':reply.finish_reason})
        if reply.finish_reason!='stop':
            raise ValueError('incomplete_output')
        return json_object(reply.content)

    def _finish(self, attempt, state, reason, outputs, calls):
        with self.store.write() as conn:
            purged=[r[0] for r in conn.execute('''SELECT d.memory_id FROM person_summary_attempt_memories d
                LEFT JOIN memories m ON m.id=d.memory_id WHERE d.attempt_id=? AND (m.id IS NULL OR m.purged_at IS NOT NULL)''',(attempt,))]
            if purged:
                purge_memory_references(conn,purged,clock=self.clock)
                state,reason='conflict','memory_purged'
            conn.execute('''UPDATE person_summary_attempts SET state=?,reason=?,
                outputs_json=CASE WHEN cleared_at IS NULL THEN ? ELSE '{}' END,calls_json=?,finished_at=?
                WHERE id=? AND state='running' ''',
                (state,reason,dumps(outputs),dumps(calls),_instant(self.clock()).isoformat(),attempt))
            saved=conn.execute('SELECT state,reason FROM person_summary_attempts WHERE id=?',(attempt,)).fetchone()
            return dict(saved)

    def _skip(self, subject_id, expected, source, due, reason, run_id, stamp):
        with self.store.write() as conn:
            _expect(conn,subject_id,expected)
            _audit(conn,'generation_skipped',subject_id,stamp,base_version=expected,reason=reason,source=source)
            inserted=conn.execute('''INSERT OR IGNORE INTO person_summary_attempts(subject_id,run_id,base_version,state,
                reason,started_at,finished_at) VALUES(?,?,?,'skipped',?,?,?)''',(subject_id,run_id,expected,reason,stamp,stamp))
            attempt=(conn.execute('SELECT id FROM person_summary_attempts WHERE subject_id=? AND run_id=?',
                                  (subject_id,run_id)).fetchone()[0] if run_id is not None else inserted.lastrowid)
        return {'status':'skipped','reason':reason,'version':None,'attempt_id':attempt,'due':due}

    def _validate_snapshot(self, conn, subject_id, expected, config, settings, evidence, attempt=None):
        _expect(conn,subject_id,expected)
        subject=_subject(conn,subject_id)
        if subject['merged_into'] or subject['revision']!=evidence['subject']['revision']:
            raise PersonSummaryConflict('subject_changed')
        if _runtime(conn)!=config or _settings(conn,subject_id)['revision']!=settings['revision']:
            raise PersonSummaryConflict('settings_changed')
        if evidence['memory_count']<config['min_memories'] or _stale(conn,subject_id,[{'basis':[_basis(m) for m in evidence['memories']]+evidence.get('previous_basis',[])}]):
            raise PersonSummaryConflict('evidence_changed')
        if attempt is not None:
            row=conn.execute('SELECT state,cleared_at FROM person_summary_attempts WHERE id=?',(attempt,)).fetchone()
            if row is None or row['state']!='running' or row['cleared_at']:
                raise PersonSummaryConflict('attempt_changed')
            if conn.execute('''SELECT 1 FROM person_summary_attempt_memories d LEFT JOIN memories m ON m.id=d.memory_id
                WHERE d.attempt_id=? AND (m.id IS NULL OR m.purged_at IS NOT NULL) LIMIT 1''',(attempt,)).fetchone():
                raise PersonSummaryConflict('evidence_purged')

    def _run(self, subject_id, expected, source, *, run_id=None):
        current=_instant(self.clock()); stamp=current.isoformat()
        with self.store.write() as conn:
            _recover_interrupted(conn,self.store,subject_id,stamp)
            if run_id is not None:
                saved=conn.execute('SELECT * FROM person_summary_attempts WHERE subject_id=? AND run_id=?',(subject_id,run_id)).fetchone()
                if saved and saved['state']!='running':
                    return {'status':saved['state'],'reason':saved['reason'],'attempt_id':saved['id'],
                            'version':_view(conn,_version(conn,subject_id,saved['version'])) if saved['version'] else None}
            if conn.execute("SELECT 1 FROM person_summary_attempts WHERE subject_id=? AND state='running'",(subject_id,)).fetchone():
                raise PersonSummaryBusy('generation_in_progress')
            previous=_expect(conn,subject_id,expected); due=_due(conn,subject_id,current)
            config=_runtime(conn); settings=_settings(conn,subject_id)
        reason=None if source=='regenerate' and due['reason']=='not_due' else due['reason']
        if reason:
            return self._skip(subject_id,expected,source,due,reason,run_id,stamp)
        try:
            gateway=self._gateway()
            if getattr(gateway,'health',None):
                gateway.health.check('chat','learning')
        except ModelError as error:
            return self._skip(subject_id,expected,source,due,error.reason or error.category,run_id,stamp)
        with self.store.read() as conn:
            selected=_select(conn,subject_id,current)
            previous_text,admin_sentences,previous_basis=_safe_previous(conn,subject_id,previous,current)
        state=selected['memory_state']; evidence={key:value for key,value in selected.items() if key!='memory_state'}
        evidence['previous_basis']=previous_basis
        if not evidence['memories']:
            return self._skip(subject_id,expected,source,due,'no_evidence',run_id,stamp)
        with self.store.write() as conn:
            self._validate_snapshot(conn,subject_id,expected,config,settings,evidence)
            if conn.execute("SELECT 1 FROM person_summary_attempts WHERE subject_id=? AND state='running'",(subject_id,)).fetchone():
                raise PersonSummaryBusy('generation_in_progress')
            settings=_ensure(conn,subject_id,stamp)
            material={'format_version':1,'evidence':evidence,'previous':previous_text,'admin_sentences':admin_sentences,
                      'output_limits':{k:config[k] for k in ('max_items','max_chars','max_tokens')},
                      'dependency_ids':sorted(_dependencies(conn,[m['memory_id'] for m in evidence['memories']]+[b['memory_id'] for b in previous_basis]))}
            attempt=conn.execute('''INSERT INTO person_summary_attempts(subject_id,run_id,base_version,state,started_at,
                material_json) VALUES(?,?,?,'running',?,?)''',(subject_id,run_id,expected,stamp,dumps(material))).lastrowid
            conn.executemany('INSERT INTO person_summary_attempt_memories(attempt_id,memory_id) VALUES(?,?)',
                             ((attempt,mid) for mid in material['dependency_ids']))
            _audit(conn,'regenerate_requested' if source=='regenerate' else 'generation_requested',subject_id,stamp,
                   attempt_id=attempt,base_version=expected,source=source)
            with _ACTIVE_LOCK:
                _ACTIVE_ATTEMPTS.add((self.store.path,attempt))
        return self._execute(subject_id,expected,source,stamp,previous,due,config,settings,gateway,evidence,material,state,attempt)

    def _execute(self, subject_id, expected, source, stamp, previous, due, config, settings,
                 gateway, evidence, material, state, attempt):
        outputs={}; calls=[]; sentences=None
        checks={'passed':False,'errors':[],'deleted_sentences':[],'model':None}; degree='large'
        try:
            try:
                generated=self._call(gateway,'generate',material,outputs,calls,config)
                sentences,deleted,count=_resolve(generated,evidence,{'sentences':material['admin_sentences']},_instant(stamp).astimezone(ZoneInfo(evidence['timezone'])))
                checks.update(deleted_sentences=deleted,original_item_count=count,original_sentences=generated['items'])
                content=''.join(s['text'] for s in sentences)
                if len(deleted)*2>count:
                    checks['errors'].append('too_many_deleted')
                checks['errors']+=_length_errors(content,sentences,config)
                if not checks['errors']:
                    with self.store.write() as conn:
                        self._validate_snapshot(conn,subject_id,expected,config,settings,evidence,attempt)
                        conn.execute('UPDATE person_summary_attempts SET outputs_json=?,calls_json=? WHERE id=?',
                                     (dumps(outputs),dumps(calls),attempt))
                    checks['checked_sentences']=sentences; checks['model_input']=_check_input(material,sentences)
                    output=self._call(gateway,'check',checks['model_input'],outputs,calls,config); checks['model']=output
                    sentences,removed,warnings,degree=_check(output,sentences)
                    checks['deleted_sentences']+=removed; checks['admin_warnings']=warnings
                    content=''.join(s['text'] for s in sentences)
                    if len(checks['deleted_sentences'])*2>count:
                        checks['errors'].append('too_many_deleted')
                    checks['errors']+=_length_errors(content,sentences,config)
                    if removed:
                        degree='large'
                checks['passed']=not checks['errors']
                checks['deterministic']={'passed':checks['passed'],'errors':list(checks['errors']),
                    'characters':len(content),'tokens':estimate_tokens(content),'deleted_count':len(checks['deleted_sentences'])}
            except (ModelError,ValueError) as error:
                if isinstance(error,PersonSummaryConflict):
                    raise
                reason=(error.reason or error.category) if isinstance(error,ModelError) else 'invalid_output'
                checks.update(reason=reason,errors=[*checks['errors'],reason],passed=False)
                if sentences is None:
                    saved=self._finish(attempt,'failed',reason,outputs,calls)
                    return {'status':saved['state'],'reason':saved['reason'],'version':None,'attempt_id':attempt,'due':due}
            content=''.join(s['text'] for s in sentences)
            if not checks['passed'] and not checks.get('reason'):
                checks['reason']=checks['errors'][0]
            status=('rejected' if not checks['passed'] else 'candidate' if
                    settings['publish_mode']=='all_manual' or checks.get('admin_warnings') else 'published')
            with self.store.write() as conn:
                self._validate_snapshot(conn,subject_id,expected,config,settings,evidence,attempt)
                version=_insert(conn,subject_id,previous=expected,content=content,sentences=sentences,checks=checks,
                    degree=degree,status=status,stamp=stamp,author='model',source=source,material=material,
                    memory_state=state,settings_revision=settings['revision'],subject_revision=evidence['subject']['revision'])
                conn.execute('''UPDATE person_summary_attempts SET state=?,reason=?,version=?,outputs_json=?,calls_json=?,
                    finished_at=? WHERE id=?''',(status,checks.get('reason'),version['version'],dumps(outputs),dumps(calls),
                                              _instant(self.clock()).isoformat(),attempt))
            return {'status':status,'reason':checks.get('reason'),'version':version,'attempt_id':attempt,'due':due}
        except PersonSummaryConflict as error:
            self._finish(attempt,'conflict',str(error),outputs,calls)
            raise
        except Exception:
            self._finish(attempt,'failed','unexpected_error',outputs,calls)
            raise
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE_ATTEMPTS.discard((self.store.path,attempt))


def purge_memory_references(conn, memory_ids, *, clock=utc_now):
    """Clear whole prose fields in the caller's purge transaction (CF phase 2).

    Associations include selected material, previous-summary evidence, and known
    ancestors; they survive deleted source links. No text replacement is used.
    IDs, categories, authors, timestamps and numeric call metadata remain.
    """
    if not conn.in_transaction:
        raise ValueError('purge_memory_references requires the caller write transaction')
    ids=list(memory_ids)
    if any(type(mid) is not int or mid<1 for mid in ids):
        raise ValueError('memory_ids must contain positive integers')
    stamp=_instant(clock()).isoformat(); counts={'versions':0,'attempts':0}
    # The shared gateway also stores raw output in the consolidation ledger.
    # That ledger has no per-person attempt identity, so clear the entire person-
    # summary purpose for affected runs, preserving other consolidation phases.
    # Include already-cleared attempts: a network response can arrive after purge
    # and the engine's final conflict handler must clear that late ledger copy.
    conn.execute('''UPDATE consolidation_calls SET raw_output=NULL,error='因彻底清除而清空'
        WHERE purpose IN ('consolidation_person_summary_generate','consolidation_person_summary_check')
        AND run_id IN (SELECT a.run_id FROM person_summary_attempts a
            JOIN person_summary_attempt_memories d ON d.attempt_id=a.id
            WHERE d.memory_id IN (SELECT value FROM json_each(?)))''',(dumps(ids),))
    for row in conn.execute('''SELECT * FROM person_summary_versions WHERE cleared_at IS NULL AND id IN (
        SELECT version_id FROM person_summary_version_memories WHERE memory_id IN (SELECT value FROM json_each(?)))''',
        (dumps(ids),)).fetchall():
        sentences=[]
        for item in json.loads(row['sentences_json']):
            # Whitelist structure: model explanations/time_range/body may all copy prose.
            kept={key:item[key] for key in ('kind','author','valid_until') if key in item}
            kept.update(text='',basis=[{key:b[key] for key in ('memory_id','revision') if key in b}
                                      for b in item.get('basis',[])])
            sentences.append(kept)
        conn.execute('''UPDATE person_summary_versions SET content='',sentences_json=?,checks_json=?,
            material_json='{}',invalidated_reason='memory_purged',cleared_at=? WHERE id=?''',
            (dumps(sentences),dumps({'passed':False,'reason':'memory_purged','cleared_reason':'因彻底清除而清空'}),stamp,row['id']))
        conn.execute('''UPDATE person_summary_settings SET needs_regeneration=1
            WHERE subject_id=? AND current_version=?''',(row['subject_id'],row['version']))
        counts['versions']+=1
    for row in conn.execute('''SELECT id,subject_id FROM person_summary_attempts WHERE cleared_at IS NULL AND id IN (
        SELECT attempt_id FROM person_summary_attempt_memories WHERE memory_id IN (SELECT value FROM json_each(?)))''',
        (dumps(ids),)).fetchall():
        conn.execute('''UPDATE person_summary_attempts SET material_json='{}',outputs_json='{}',cleared_at=?,
            reason='因彻底清除而清空',finished_at=COALESCE(finished_at,?),
            state=CASE WHEN state='running' THEN 'conflict' ELSE state END WHERE id=?''',(stamp,stamp,row['id']))
        counts['attempts']+=1
    return counts


def regenerate_summary(store, gateway, subject_id, *, expected_version, clock=utc_now):
    """Future admin regenerate endpoint; synchronous, CAS protected and audited."""
    return PersonSummaryEngine(store,gateway,clock=clock).regenerate(subject_id,expected_version=expected_version)


def update_summaries_for_run(store, gateway, run, config, *, clock=utc_now, stop=None):
    """Future consolidation step. Supply its existing budget-accounting gateway.

    The gateway must admit every HTTP attempt into consolidation_calls and apply
    its daily-token gate, just as the existing entry-profile/persona adapter does.
    This function only preflights two calls; retries still consume shared budget.
    Durable attempts bound both repeat runs and the configured person count.
    """
    if not config.get('person_summary_enabled',True) or not config.get('enabled',True):
        return True
    current=_instant(clock()); run_id=run['id']
    with store.read() as conn:
        runtime=_runtime(conn)
        if not runtime['enabled']:
            return True
        # Message metadata orders recent participants; no raw message text is read.
        subjects=[r[0] for r in conn.execute("""SELECT s.id FROM subjects s WHERE s.kind='person'
            AND s.id NOT IN ('self','scene') AND s.merged_into IS NULL ORDER BY
            COALESCE((SELECT MAX(julianday(x.occurred_at)) FROM messages x WHERE x.sender_subject_id=s.id),
                     (SELECT MAX(julianday(m.last_confirmed_at)) FROM memories m WHERE m.speaker_subject_id=s.id),
                     julianday(s.created_at)) DESC,s.id""")]
    for subject_id in subjects:
        if stop and stop():
            return False
        with store.write() as conn:
            _recover_interrupted(conn,store,subject_id,current.isoformat())
            saved=conn.execute('SELECT * FROM person_summary_attempts WHERE subject_id=? AND run_id=?',(subject_id,run_id)).fetchone()
            admitted=conn.execute('SELECT COUNT(*) FROM person_summary_attempts WHERE run_id=?',(run_id,)).fetchone()[0]
            expected=_settings(conn,subject_id)['current_version']
            count=conn.execute('SELECT COUNT(*) FROM consolidation_calls WHERE run_id=?',(run_id,)).fetchone()[0]
            due=_due(conn,subject_id,current)
        if saved and saved['state']=='running':
            return False
        if saved:
            result={'status':saved['state'],'reason':saved['reason']}
        elif admitted>=runtime['max_subjects_per_run']:
            continue  # Still recover report rows for already-admitted people later in the order.
        elif not due['due']:
            continue
        else:
            engine=PersonSummaryEngine(store,gateway,clock=clock)
            if count+2>config['max_calls']:
                engine._skip(subject_id,expected,'periodic',due,'call_budget',run_id,current.isoformat())
                result={'status':'skipped','reason':'call_budget'}
            else:
                try:
                    result=engine.update(subject_id,expected_version=expected,run_id=run_id)
                except PersonSummaryBusy:
                    return False
                except PersonSummaryConflict as error:
                    result={'status':'conflict','reason':str(error)}
        with store.write() as conn:
            attempt=conn.execute('SELECT * FROM person_summary_attempts WHERE subject_id=? AND run_id=?',(subject_id,run_id)).fetchone()
            if attempt:
                conn.execute('''INSERT OR IGNORE INTO maintenance_items(run_id,phase,item_key,object_id,outcome,reason,details_json,created_at)
                    VALUES(?,'consolidation',?,?,'person_summary',?,?,?)''',
                    (run_id,'person_summary:'+subject_id,attempt['id'],result.get('reason'),
                     dumps({'subject_id':subject_id,'status':result['status'],'version':attempt['version'],'attempt_id':attempt['id']}),current.isoformat()))
        if result.get('reason')=='interrupted':
            return False
        if result.get('reason') in ('call_budget','usage_limit','paused','temporary_unavailable'):
            break
    return True
