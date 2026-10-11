"""Versioned summaries of people, derived exclusively from globally visible memories.

Host projections are read-only and
recheck current visibility; model calls run outside all database transactions.
"""
from __future__ import annotations

import copy
import json
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
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
from .search_text import match_query, query_terms

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
# Explicit projections never load vectors while checking or selecting evidence.
_MEMORY_COLUMNS = ('id','revision','content','kind','speaker_subject_id','stance','belief',
    'event_time','world','importance','retention','lifecycle','purged_at','created_at',
    'updated_at','first_confirmed_at','last_confirmed_at')
_MEMORY_SELECT = ','.join('m.' + key for key in _MEMORY_COLUMNS)
_VERSION_SELECT = ('id,subject_id,version,base_version,content,sentences_json,checks_json,change_degree,'
    'status,author,source,generated_at,published_at,settings_revision,subject_revision,material_json,'
    'memory_state_json,invalidated_reason,cleared_at')


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
    row=conn.execute('SELECT id,kind,name,revision,created_at,merged_into,merged_at FROM subjects WHERE id=?',(subject_id,)).fetchone()
    if row is None:
        raise KeyError(subject_id)
    if writable and (row['kind']!='person' or row['id'] in ('self','scene') or row['merged_into'] is not None):
        raise ValueError('subject_unavailable')
    return dict(row)


def _settings(conn, subject_id):
    _subject(conn,subject_id); defaults=_runtime(conn)
    row=conn.execute('SELECT subject_id,enabled,publish_mode,revision,current_version,needs_regeneration,updated_at FROM person_summary_settings WHERE subject_id=?',(subject_id,)).fetchone()
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
    row=conn.execute(f'SELECT {_VERSION_SELECT} FROM person_summary_versions WHERE subject_id=? AND version=?',(subject_id,version)).fetchone()
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


class _SummaryNames(SubjectNames):
    """Use the same label boundaries, with exclusively public identity links.

    Do not call SubjectNames.__init__: its unfiltered aliases belong to other
    features. Both material aliases and all relatedness checks use this view.
    """
    def __init__(self, conn):
        self.labels = {}; self.aliases = defaultdict(list)
        self.names = {r['id']:r['name'] for r in conn.execute(
            "SELECT id,name FROM subjects WHERE id!='scene' AND merged_into IS NULL")}
        for sid, name in self.names.items():
            if sid != 'self':
                self.add(name, sid)
        self.add(setting(conn, 'role_name', 'Iris'), 'self')
        for row in conn.execute("""SELECT a.subject_id,a.alias FROM subject_aliases a
            JOIN subjects s ON s.id=a.subject_id
            LEFT JOIN messages x ON x.id=a.source_message_id LEFT JOIN entries e ON e.id=x.entry_id
            WHERE s.id!='scene' AND s.merged_into IS NULL AND a.folded_into IS NULL
            AND (a.source_message_id IS NULL OR e.visibility='shared') ORDER BY a.alias"""):
            self.add(row['alias'], row['subject_id'])
            self.aliases[row['subject_id']].append(row['alias'])
        labels = sorted(self.labels, key=lambda value:(-len(value), value))
        pattern = '|'.join(re.escape(label) if re.search('[\u4e00-\u9fff]', label)
            else r'(?<!\w)' + re.escape(label) + r'(?!\w)' for label in labels)
        self.pattern = re.compile(pattern or r'(?!x)x', re.I)


def _related(row, about, subject_id, names):
    if row['speaker_subject_id']==subject_id or any(s['id']==subject_id for s in about):
        return True
    return any(names.labels[m.group().casefold()]=={subject_id} for m in names.pattern.finditer(row['content']))


def _memory_hash(row, about):
    # Confirmation and retention affect ordering, never facts or change counts.
    keys=('revision','content','speaker_subject_id','stance','belief','event_time','world')
    return evidence_hash({**{key:row[key] for key in keys},'about':[s['id'] for s in about],'tags':row.get('tags',[])})


def _scopes(conn, memory_ids):
    """Declared and effective visibility, walking only requested ancestors.

    This is the candidate-scoped equivalent of Visibility.memory. Its effective
    scope check and strict shared-declaration check are intentionally separate.
    No descendant closure, raw message body, or memory vector is read.
    """
    ids=sorted(set(memory_ids)); declared=set(); effective={mid:None for mid in ids}
    if not ids:
        return declared,effective
    universe=frozenset(r[0] for r in conn.execute('SELECT id FROM entries'))
    for row in conn.execute("""WITH RECURSIVE ancestry(root,id) AS (
        SELECT value,value FROM json_each(?) UNION SELECT a.root,s.source_memory_id
        FROM ancestry a JOIN sources s ON s.memory_id=a.id
        WHERE s.kind='memory' AND s.source_memory_id IS NOT NULL
    ), origins(root,entry_id) AS (
        SELECT a.root,x.entry_id FROM ancestry a JOIN sources s ON s.memory_id=a.id
        JOIN messages x ON x.id=s.message_id WHERE s.kind='message'
        UNION SELECT a.root,v.entry_id FROM ancestry a JOIN memory_visibility_roots v ON v.memory_id=a.id
    ) SELECT o.root,e.id,e.visibility,e.visible_in_json FROM origins o JOIN entries e ON e.id=o.entry_id""",
        (dumps(ids),)):
        mode=row['visibility']; root=row['root']
        if mode!='shared':
            declared.add(root)
        allowed=(universe if mode=='shared' else frozenset({row['id']}) if mode=='entry_only'
                 else frozenset([row['id'],*json.loads(row['visible_in_json'])]) & universe)
        scope=None if allowed==universe else allowed
        effective[root]=Visibility.intersect(effective[root],scope)
    return declared,effective


def _dependencies(conn, memory_ids):
    return {r[0] for r in conn.execute("""WITH RECURSIVE ancestry(id) AS (
        SELECT value FROM json_each(?) UNION SELECT s.source_memory_id FROM ancestry a
        JOIN sources s ON s.memory_id=a.id WHERE s.kind='memory' AND s.source_memory_id IS NOT NULL
    ) SELECT id FROM ancestry""",(dumps(sorted(set(memory_ids))),))}


def _direct_ids(conn, subject_id, current, *, limit=None):
    sql="""SELECT m.id FROM memories m INDEXED BY memories_dedupe
        WHERE m.speaker_subject_id=? AND m.lifecycle='active' AND m.purged_at IS NULL
        AND julianday(m.created_at)<=julianday(?) UNION
        SELECT m.id FROM memory_subjects ms INDEXED BY memory_subjects_by_subject
        JOIN memories m ON m.id=ms.memory_id WHERE ms.subject_id=? AND m.lifecycle='active'
        AND m.purged_at IS NULL AND julianday(m.created_at)<=julianday(?)"""
    args=[subject_id,current.isoformat(),subject_id,current.isoformat()]
    if limit is not None:
        sql+=' LIMIT ?'; args.append(limit)
    return {r[0] for r in conn.execute(sql,args)}


def _named_ids(conn, subject_id, names):
    found=set()
    for label,sids in names.labels.items():
        if sids!={subject_id}:
            continue
        tokenizer='trigram' if len(label)>=3 else 'jieba'
        tokens=[label] if tokenizer=='trigram' else query_terms(label,'jieba')
        if not tokens:
            continue
        table='memory_fts_'+tokenizer
        found.update(r[0] for r in conn.execute(
            f'SELECT rowid FROM {table} WHERE {table} MATCH ?', ('content : ('+match_query(tokens)+')',)))
    return found


def _enough_candidates(conn, subject_id, current, minimum, names):
    # The direct relationship indexes make obviously undersized subjects cheap.
    # Name-only subjects still get their FTS route; they are never rejected just
    # because they have fewer than eight speaker/about links.
    direct=_direct_ids(conn,subject_id,current,limit=minimum)
    return len(direct)>=minimum or len(direct|_named_ids(conn,subject_id,names))>=minimum


def _eligible(conn, subject_id, current, *, names=None):
    names=names or _SummaryNames(conn)
    ids=_direct_ids(conn,subject_id,current)|_named_ids(conn,subject_id,names)
    if not ids:
        return []
    restricted,scopes=_scopes(conn,ids); rows=[]
    for item in conn.execute(f"""SELECT {_MEMORY_SELECT} FROM memories m
        WHERE m.id IN (SELECT value FROM json_each(?)) AND m.lifecycle='active' AND m.purged_at IS NULL
        AND julianday(m.created_at)<=julianday(?) ORDER BY m.importance DESC,m.retention DESC,
        MAX(julianday(m.updated_at),julianday(m.last_confirmed_at)) DESC,m.id""",(dumps(sorted(ids)),current.isoformat())):
        if item['id'] in restricted or scopes[item['id']] is not None:
            continue
        row=dict(item); about=_about(conn,row['id'])
        if _related(row,about,subject_id,names):
            row['tags']=[r[0] for r in conn.execute('SELECT tag FROM memory_tags WHERE memory_id=? ORDER BY tag',(row['id'],))]
            rows.append((row,about))
    return rows


def _select(conn, subject_id, current, *, rows=None, names=None):
    subject=_subject(conn,subject_id,writable=True); config=_runtime(conn)
    names=names or _SummaryNames(conn)
    rows=_eligible(conn,subject_id,current,names=names) if rows is None else rows
    selected=[]; han=0; other=2  # JSON list brackets, then exactly one comma between items.
    state={str(row['id']):_memory_hash(row,about) for row,about in rows}
    for row,about in rows:
        speaker=_subject(conn,row['speaker_subject_id'])
        item={key:row[key] for key in ('revision','kind','stance','belief','importance','retention','event_time',
                                      'world','created_at','updated_at','first_confirmed_at','last_confirmed_at','tags')}
        item.update(ref=f'M{len(selected)+1}',memory_id=row['id'],content=excerpt(row['content'],600),
                    speaker={'id':speaker['id'],'name':speaker['name']},about=about,sha256=state[str(row['id'])])
        item['truncated']=item['content']!=row['content']
        encoded=dumps(item); added_han=sum('\u4e00'<=ch<='\u9fff' for ch in encoded)
        added_other=len(encoded)-added_han+bool(selected)
        if han+added_han+(other+added_other+2)//3>config['memory_tokens']:
            break
        selected.append(item); han+=added_han; other+=added_other
    return {'subject':{key:subject[key] for key in ('id','name','revision')}|{'aliases':names.aliases[subject_id]},
            'as_of':current.isoformat(),'timezone':setting(conn,'timezone','Asia/Shanghai'),
            'role_name':setting(conn,'role_name','Iris'),'memories':selected,
            'memory_count':len(rows),'memory_tokens':han+(other+2)//3,'memory_state':state}


def select_material(store, subject_id, *, clock=utc_now):
    with store.read() as conn:
        return _select(conn,subject_id,_instant(clock()))


def _basis(item):
    return {key:item[key] for key in ('memory_id','revision','sha256')}


def _recent_deadline(memories):
    dates=[]
    for row in memories:
        try:
            value=datetime.fromisoformat(row['event_time']) if row.get('event_time') else None
        except (ValueError,TypeError):
            value=None
        if value is None:
            value=_instant(row['created_at'])
        elif value.tzinfo is None:
            value=value.replace(tzinfo=timezone.utc)
        dates.append(value)
    return max(dates)+timedelta(days=30) if dates else None


def _stale(conn, subject_id, sentences, *, current=None, names=None):
    result=[]; seen=set()
    ids={b['memory_id'] for s in sentences for b in s['basis']}
    restricted,scopes=_scopes(conn,ids)
    rows={r['id']:dict(r) for r in conn.execute(
        f'SELECT {_MEMORY_SELECT} FROM memories m WHERE m.id IN (SELECT value FROM json_each(?))',(dumps(sorted(ids)),))}
    today=current.astimezone(ZoneInfo(setting(conn,'timezone','Asia/Shanghai'))).date() if current else None
    for index,sentence in enumerate(sentences):
        expired=False
        if current and sentence.get('valid_until'):
            expired=date.fromisoformat(sentence['valid_until'])<today
        elif current and sentence.get('kind')=='recent':
            deadline=(_instant(sentence['expires_at']) if sentence.get('expires_at') else
                      _recent_deadline([rows[b['memory_id']] for b in sentence['basis'] if b['memory_id'] in rows]))
            expired=deadline is not None and current>=deadline
        if expired:
            result.append({'reason':'time_range_expired','sentence_index':index})
        for basis in sentence['basis']:
            key=dumps(basis)
            if key in seen:
                continue
            seen.add(key); row=rows.get(basis['memory_id']); reason=None
            if row is None or row['purged_at']:
                reason='memory_purged'
            elif row['lifecycle']!='active':
                reason='memory_unavailable'
            elif row['id'] in restricted or scopes[row['id']] is not None:
                reason='visibility_changed'
            else:
                row['tags']=[r[0] for r in conn.execute('SELECT tag FROM memory_tags WHERE memory_id=? ORDER BY tag',(row['id'],))]
                about=_about(conn,row['id'])
                if names is None and row['speaker_subject_id']!=subject_id and not any(s['id']==subject_id for s in about):
                    names=_SummaryNames(conn)
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



def current_summary(store, subject_id, *, entry_id=None, clock=utc_now):
    """Admin detail without an entry; otherwise only the safe host projection."""
    with store.read() as conn:
        if entry_id is not None:
            return summary_context(conn,subject_id,entry_id,clock=clock)
        return _view(conn,_current(conn,subject_id),current=_instant(clock()))


def summary_context(conn, subject_id, entry_id, *, clock=utc_now):
    """Host item, with only cited ancestors checked on this snapshot."""
    if entry_id is not None and not conn.execute('SELECT 1 FROM entries WHERE id=?',(entry_id,)).fetchone():
        raise KeyError(entry_id)
    subject=_subject(conn,subject_id); settings=_settings(conn,subject_id)
    if (subject['kind']!='person' or subject_id in ('self','scene') or subject['merged_into'] is not None or not settings['enabled'] or not _runtime(conn)['enabled']):
        return None
    value=_view(conn,_current(conn,subject_id),current=_instant(clock()))
    if (value is None or not value['content'] or value['status']!='published' or value['invalidated_reason']
            or value['cleared_at'] or any(s['reason'] in ('visibility_changed','no_longer_related') for s in value['stale_basis'])):
        return None
    # _stale checks effective global scope as well as the stricter declaration;
    # a globally valid basis is visible at this authorized entry too.
    unavailable={s['memory_id'] for s in value['stale_basis'] if s['reason'] in ('memory_unavailable','memory_purged') and 'memory_id' in s}
    expired={s['sentence_index'] for s in value['stale_basis'] if s['reason']=='time_range_expired'}
    kept=[s for i,s in enumerate(value['sentences']) if i not in expired and not any(b['memory_id'] in unavailable for b in s['basis'])]
    if not kept:
        return None
    content=value['content'] if len(kept)==len(value['sentences']) else ''.join(s['text'] for s in kept)
    return {'subject_id':subject_id,'name':subject['name'],'text':content,'version':value['version'],
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
            f'SELECT {_VERSION_SELECT} FROM person_summary_versions WHERE subject_id=? ORDER BY version DESC LIMIT ? OFFSET ?',
            (subject_id,limit,offset))]


def _due(conn, subject_id, current, *, rows=None, names=None):
    subject=_subject(conn,subject_id); config=_runtime(conn); settings=_settings(conn,subject_id)
    previous=_current(conn,subject_id)
    available=subject['kind']=='person' and subject_id not in ('self','scene') and not subject['merged_into']
    rows=(_eligible(conn,subject_id,current,names=names) if available else []) if rows is None else rows
    state={str(row['id']):_memory_hash(row,about) for row,about in rows}
    old=previous['memory_state'] if previous else {}
    changed=sum(state.get(mid)!=old.get(mid) for mid in state.keys()|old.keys())
    stale=bool(previous and _view(conn,previous,current=current)['possibly_stale'])
    reason=('subject_kind' if subject['kind']!='person' or subject_id in ('self','scene') else 'subject_merged' if subject['merged_into'] else
            'disabled' if not config['enabled'] or not settings['enabled'] else
            'insufficient_memories' if len(rows)<config['min_memories'] else None)
    last=conn.execute("""SELECT state,memory_state_json FROM person_summary_attempts
        WHERE subject_id=? AND state!='skipped' ORDER BY id DESC LIMIT 1""",(subject_id,)).fetchone()
    if reason is None and last and last['state'] in ('failed','rejected') and json.loads(last['memory_state_json'])==state:
        reason='no_changes_since_attempt'
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
    if not isinstance(content,str):
        raise ValueError('summary must be text')
    with store.read() as conn:
        subject=_subject(conn,subject_id,writable=True); previous=_expect(conn,subject_id,expected_version)
        config=_runtime(conn); snapshot_settings=_settings(conn,subject_id)
        selected=_select(conn,subject_id,_instant(stamp))
    old=defaultdict(list)
    for item in previous['sentences'] if previous else []:
        old[item['text']].append(item)
    sentences=[old[text].pop(0) if old[text] else {'text':text,'basis':[],'author':'admin'}
               for text in _edit_segments(content,previous)]
    if _length_errors(content,sentences,config):
        raise ValueError('summary must be nonempty, at most 5 items / 200 characters / 250 tokens')
    with store.write() as conn:
        current_subject=_subject(conn,subject_id,writable=True); latest=_expect(conn,subject_id,expected_version)
        if (current_subject['revision']!=subject['revision'] or _runtime(conn)!=config
                or _settings(conn,subject_id)['revision']!=snapshot_settings['revision']):
            raise PersonSummaryConflict('settings_or_subject_changed')
        # Purge mutates a version in place without advancing current_version.
        # Retained model lines must not restore prose cleared after our snapshot.
        if ((previous and any(latest[key]!=previous[key] for key in ('cleared_at','invalidated_reason')))
                or _stale(conn,subject_id,sentences,current=_instant(stamp))):
            raise PersonSummaryConflict('evidence_changed')
        settings=_ensure(conn,subject_id,stamp)
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
        conn.execute("""UPDATE person_summary_attempts SET state='rejected',reason=?
            WHERE subject_id=? AND version=? AND state='candidate'""",(reason,subject_id,version))
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
                else:
                    deadline=_recent_deadline([refs[ref] for ref in item['memory_refs']])
                    valid['expires_at']=deadline.isoformat()
                    if current>=deadline:
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


def _check_run_admission(conn, subject_id, run_id):
    if run_id is None:
        return
    if conn.execute('SELECT 1 FROM person_summary_attempts WHERE subject_id=? AND run_id=?',(subject_id,run_id)).fetchone():
        raise PersonSummaryConflict('attempt_changed')
    count=conn.execute('SELECT COUNT(*) FROM person_summary_attempts WHERE run_id=?',(run_id,)).fetchone()[0]
    if count>=_runtime(conn)['max_subjects_per_run']:
        raise PersonSummaryConflict('run_limit')


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
            _check_run_admission(conn,subject_id,run_id)
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
        reserved=self._reserve(subject_id,expected,source,run_id=run_id)
        return self._execute(*reserved['context']) if 'context' in reserved else reserved

    def _reserve(self, subject_id, expected, source, *, run_id=None):
        current=_instant(self.clock()); stamp=current.isoformat()
        with self.store.write() as conn:
            _recover_interrupted(conn,self.store,subject_id,stamp)
        with self.store.read() as conn:
            if run_id is not None:
                saved=conn.execute('SELECT id,state,reason,version FROM person_summary_attempts WHERE subject_id=? AND run_id=?',(subject_id,run_id)).fetchone()
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
            _check_run_admission(conn,subject_id,run_id)
            self._validate_snapshot(conn,subject_id,expected,config,settings,evidence)
            if conn.execute("SELECT 1 FROM person_summary_attempts WHERE subject_id=? AND state='running'",(subject_id,)).fetchone():
                raise PersonSummaryBusy('generation_in_progress')
            settings=_ensure(conn,subject_id,stamp)
            material={'format_version':1,'evidence':evidence,'previous':previous_text,'admin_sentences':admin_sentences,
                      'output_limits':{k:config[k] for k in ('max_items','max_chars','max_tokens')},
                      'dependency_ids':sorted(_dependencies(conn,[m['memory_id'] for m in evidence['memories']]+[b['memory_id'] for b in previous_basis]))}
            attempt=conn.execute('''INSERT INTO person_summary_attempts(subject_id,run_id,base_version,state,started_at,
                material_json,memory_state_json) VALUES(?,?,?,'running',?,?,?)''',(subject_id,run_id,expected,stamp,dumps(material),dumps(state))).lastrowid
            conn.executemany('INSERT INTO person_summary_attempt_memories(attempt_id,memory_id) VALUES(?,?)',
                             ((attempt,mid) for mid in material['dependency_ids']))
            _audit(conn,'regenerate_requested' if source=='regenerate' else 'generation_requested',subject_id,stamp,
                   attempt_id=attempt,base_version=expected,source=source)
            with _ACTIVE_LOCK:
                _ACTIVE_ATTEMPTS.add((self.store.path,attempt))
        return {'attempt_id':attempt,'context':(subject_id,expected,source,stamp,previous,due,config,settings,gateway,evidence,material,state,attempt)}

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
                checks.update(reason=reason,errors=[*checks['errors'],reason],passed=False,model_error=isinstance(error,ModelError))
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
    for row in conn.execute('''SELECT id,subject_id,version,sentences_json FROM person_summary_versions WHERE cleared_at IS NULL AND id IN (
        SELECT version_id FROM person_summary_version_memories WHERE memory_id IN (SELECT value FROM json_each(?)))''',
        (dumps(ids),)).fetchall():
        sentences=[]
        for item in json.loads(row['sentences_json']):
            # Whitelist structure: model explanations/time_range/body may all copy prose.
            kept={key:item[key] for key in ('kind','author','valid_until','expires_at') if key in item}
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
    """Synchronous regeneration for data callers; CAS protected and audited."""
    return PersonSummaryEngine(store,gateway,clock=clock).regenerate(subject_id,expected_version=expected_version)


def _report_attempt(conn, attempt, stamp):
    conn.execute("""INSERT OR IGNORE INTO maintenance_items(run_id,phase,item_key,object_id,outcome,reason,details_json,created_at)
        VALUES(?,'consolidation',?,?,'person_summary',?,?,?)""",
        (attempt['run_id'],'person_summary:'+attempt['subject_id'],attempt['id'],attempt['reason'],
         dumps({'subject_id':attempt['subject_id'],'status':attempt['state'],'version':attempt['version'],
                'attempt_id':attempt['id']}),stamp))


def update_summaries_for_run(store, gateway, run, config, *, clock=utc_now, stop=None):
    """Read-snapshot admission; bounded CAS writes and the shared call ledger.

    Replay/report repair is separate from discovery so reaching N stops all due
    calculations without losing previously admitted people after activity changes.
    """
    if not config.get('person_summary_enabled',True) or not config.get('enabled',True):
        return True
    if stop and stop():
        return False
    current=_instant(clock()); stamp=current.isoformat(); run_id=run['id']
    with store.read() as conn:
        runtime=_runtime(conn)
        if not runtime['enabled']:
            return True
        saved=[dict(r) for r in conn.execute("""SELECT id,subject_id,run_id,state,reason,version
            FROM person_summary_attempts WHERE run_id=? ORDER BY id""",(run_id,))]
    for attempt in saved:
        with store.write() as conn:
            if attempt['state']=='running':
                _recover_interrupted(conn,store,attempt['subject_id'],stamp)
            row=conn.execute("""SELECT id,subject_id,run_id,state,reason,version
                FROM person_summary_attempts WHERE id=?""",(attempt['id'],)).fetchone()
            if row['state']=='running':
                return False
            _report_attempt(conn,row,stamp)
    admitted=len(saved)
    if admitted>=runtime['max_subjects_per_run']:
        return True
    with store.read() as conn:
        # Aggregate metadata once rather than scanning all messages per subject.
        subjects=[r[0] for r in conn.execute("""WITH activity AS (
            SELECT sender_subject_id AS subject_id,MAX(julianday(occurred_at)) AS recent FROM messages GROUP BY sender_subject_id
        ), confirmations AS (
            SELECT speaker_subject_id AS subject_id,MAX(julianday(last_confirmed_at)) AS recent
            FROM memories GROUP BY speaker_subject_id
        ) SELECT s.id FROM subjects s LEFT JOIN activity a ON a.subject_id=s.id
        LEFT JOIN confirmations c ON c.subject_id=s.id LEFT JOIN person_summary_settings p ON p.subject_id=s.id
        WHERE s.kind='person' AND s.id NOT IN ('self','scene') AND s.merged_into IS NULL AND COALESCE(p.enabled,1)=1
        ORDER BY COALESCE(a.recent,c.recent,julianday(s.created_at)) DESC,s.id""")]
    done={a['subject_id'] for a in saved}
    for subject_id in subjects:
        if admitted>=runtime['max_subjects_per_run']:
            break
        if subject_id in done:
            continue
        if stop and stop():
            return False
        with store.read() as conn:
            names=_SummaryNames(conn)
            if not _enough_candidates(conn,subject_id,current,runtime['min_memories'],names):
                continue
            due=_due(conn,subject_id,current,names=names)
            if not due['due']:
                continue
            expected=due['current_version']
            count=conn.execute('SELECT COUNT(*) FROM consolidation_calls WHERE run_id=?',(run_id,)).fetchone()[0]
        engine=PersonSummaryEngine(store,gateway,clock=clock)
        try:
            if count+2>config['max_calls']:
                result=engine._skip(subject_id,expected,'periodic',due,'call_budget',run_id,stamp)
            else:
                result=engine.update(subject_id,expected_version=expected,run_id=run_id)
        except PersonSummaryBusy:
            return False
        except PersonSummaryConflict as error:
            result={'status':'conflict','reason':str(error)}
        except Exception:
            # _execute durably records unexpected failures; continue with other
            # people and let consolidation run its remaining independent steps.
            result={'status':'failed','reason':'unexpected_error'}
        with store.write() as conn:
            attempt=conn.execute("""SELECT id,subject_id,run_id,state,reason,version FROM person_summary_attempts
                WHERE subject_id=? AND run_id=?""",(subject_id,run_id)).fetchone()
            if attempt:
                _report_attempt(conn,attempt,stamp)
            admitted=conn.execute('SELECT COUNT(*) FROM person_summary_attempts WHERE run_id=?',(run_id,)).fetchone()[0]
        if result.get('reason')=='interrupted':
            return False
        if result.get('reason') in ('call_budget','usage_limit','paused','temporary_unavailable','unconfigured'):
            break
    return True


PEOPLE_TOKENS = 1000


def people_context(conn, entry_id, *, participants=None, participant_names=None, limit=3, clock=utc_now):
    """Independent, bounded prepare partition; never load evidence for absent versions."""
    from .people import canonical_subject
    if not limit or not _runtime(conn)['enabled']:
        return []
    labels=participant_names or {}; inferred=participants is None
    references=participants if not inferred else [r[0] for r in conn.execute(
        'SELECT sender_subject_id FROM messages WHERE entry_id=? ORDER BY id DESC LIMIT 20',(entry_id,))]
    ordered=[]; display={}
    for ref in references:
        exact=conn.execute('SELECT id FROM subjects WHERE id=?',(ref,)).fetchone()
        if exact:
            sid=canonical_subject(conn,ref)
        else:
            # Existing recall resolves all aliases. This new identity-bearing
            # partition independently forbids links learned only in private entries.
            matches=conn.execute('''SELECT DISTINCT s.id FROM subjects s
                LEFT JOIN subject_aliases a ON a.subject_id=s.id AND a.folded_into IS NULL
                LEFT JOIN messages m ON m.id=a.source_message_id LEFT JOIN entries e ON e.id=m.entry_id
                WHERE s.merged_into IS NULL AND (s.name=? OR (a.alias=? AND
                    (a.source_message_id IS NULL OR e.visibility='shared')))''',(ref,ref)).fetchall()
            if len(matches)!=1:
                continue
            sid=matches[0][0]
        if sid in ('self','scene') or sid in display:
            continue
        ordered.append(sid)
        display[sid]=labels.get(sid,labels.get(ref,ref if not exact else None))
    if not ordered:
        return []
    published={r[0] for r in conn.execute('''SELECT p.subject_id FROM person_summary_settings p
        JOIN subjects s ON s.id=p.subject_id JOIN person_summary_versions v
        ON v.subject_id=p.subject_id AND v.version=p.current_version
        WHERE p.subject_id IN (SELECT value FROM json_each(?)) AND p.enabled=1 AND s.kind='person'
        AND s.merged_into IS NULL AND v.status='published' AND v.cleared_at IS NULL
        AND v.invalidated_reason IS NULL AND v.content!='' ''',(dumps(ordered),))}
    result=[]
    for sid in ordered:
        if sid not in published:
            continue
        item=summary_context(conn,sid,entry_id,clock=clock)
        if item is None:
            continue
        if display[sid] is not None:
            item['name']=display[sid]
        if estimate_tokens(dumps([*result,item]))>PEOPLE_TOKENS:
            continue  # Keep complete items; never cut away a qualification.
        result.append(item)
        if len(result)>=limit:
            break
    return result


_ADMIN_COLUMNS = ('id,subject_id,version,base_version,content,status,source,author,change_degree,'
                  'generated_at,published_at,invalidated_reason,cleared_at')
_ATTEMPT_COLUMNS = '''id,subject_id,base_version,state,reason,version,started_at,finished_at,cleared_at,
    (SELECT status FROM person_summary_versions v WHERE v.subject_id=person_summary_attempts.subject_id
        AND v.version=person_summary_attempts.version) AS version_status,
    CASE WHEN state!='running' THEN 'finished'
         WHEN json_type(outputs_json,'$.consolidation_person_summary_generate') IS NOT NULL THEN 'checking'
         ELSE 'generating' END AS stage'''


def _admin_brief(row, current):
    return {**dict(row),'is_current':row['version']==current} if row else None


def admin_snapshot(store, subject_id, *, clock=utc_now):
    with store.read() as conn:
        subject=_subject(conn,subject_id); settings=_settings(conn,subject_id); config=_runtime(conn)
        current=conn.execute(f'SELECT {_ADMIN_COLUMNS} FROM person_summary_versions WHERE subject_id=? AND version=?',
                             (subject_id,settings['current_version'])).fetchone()
        pending=conn.execute(f"SELECT {_ADMIN_COLUMNS} FROM person_summary_versions WHERE subject_id=? AND status='candidate' ORDER BY version DESC LIMIT 1",(subject_id,)).fetchone()
        count=conn.execute("SELECT COUNT(*) FROM person_summary_versions WHERE subject_id=? AND status='candidate'",(subject_id,)).fetchone()[0]
        latest=conn.execute(f'SELECT {_ATTEMPT_COLUMNS} FROM person_summary_attempts WHERE subject_id=? ORDER BY id DESC LIMIT 1',(subject_id,)).fetchone()
        value=_view(conn,_current(conn,subject_id),current=_instant(clock()))
        return {'subject':subject,'supported':subject['kind']=='person' and subject_id not in ('self','scene') and not subject['merged_into'],
                'global_enabled':config['enabled'],'settings':settings,
                'edit_limits':{k:config[k] for k in ('max_items','max_chars','max_tokens')},
                'current':_admin_brief(current,settings['current_version']),
                'pending':_admin_brief(pending,settings['current_version']),'pending_count':count,
                'possibly_stale':bool(value and value['possibly_stale']),'stale_basis':value['stale_basis'] if value else [],
                'latest_attempt':dict(latest) if latest else None}


def admin_versions(store, subject_id, *, limit=30, offset=0):
    with store.read() as conn:
        current=_settings(conn,subject_id)['current_version']
        count=conn.execute('SELECT COUNT(*) FROM person_summary_versions WHERE subject_id=?',(subject_id,)).fetchone()[0]
        items=[_admin_brief(r,current) for r in conn.execute(f'SELECT {_ADMIN_COLUMNS} FROM person_summary_versions WHERE subject_id=? ORDER BY version DESC LIMIT ? OFFSET ?',
                                                           (subject_id,limit,offset))]
        return {'items':items,'total':count,'limit':limit,'offset':offset}


def admin_version(store, subject_id, version, *, clock=utc_now):
    with store.read() as conn:
        value=_view(conn,_version(conn,subject_id,version),current=_instant(clock()))
        material=value.pop('material'); value.pop('memory_state')
        indexed={m['memory_id']:m for m in material.get('evidence',{}).get('memories',[])}
        for sentence in value['sentences']:
            sentence['admin_written']=sentence['author']=='admin'
            for basis in sentence['basis']:
                cited=indexed.get(basis['memory_id'])
                basis['evidence']=({k:v for k,v in cited.items() if k not in ('sha256','ref')} if cited and not value['cleared_at'] else None)
                row=conn.execute('SELECT id,revision,content,lifecycle,purged_at FROM memories WHERE id=?',(basis['memory_id'],)).fetchone()
                basis['memory']=dict(row) if row and not row['purged_at'] and not value['cleared_at'] else None
        value['checks'].pop('model_input',None)
        return value


def admin_attempt(store, subject_id, attempt_id):
    with store.read() as conn:
        row=conn.execute(f'SELECT {_ATTEMPT_COLUMNS} FROM person_summary_attempts WHERE subject_id=? AND id=?',(subject_id,attempt_id)).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return dict(row)


class PersonSummaryJobs:
    """One admin worker, persistent CAS reservation before 202, drain before close."""
    def __init__(self, store, gateway, *, clock=utc_now):
        self.engine=PersonSummaryEngine(store,gateway,clock=clock)
        self._lock=Lock(); self._closed=False; self._future=None
        self._pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='person-summary')
        with store.write() as conn:
            subjects=[r[0] for r in conn.execute("SELECT subject_id FROM person_summary_attempts WHERE state='running'")]
            for sid in subjects:
                _recover_interrupted(conn,store,sid,_instant(clock()).isoformat())

    def submit(self, subject_id, *, expected_version):
        with self._lock:
            if self._closed or self._future is not None and not self._future.done():
                raise PersonSummaryBusy('generation_in_progress')
            reserved=self.engine._reserve(subject_id,expected_version,'regenerate')
            if 'context' in reserved:
                self._future=self._pool.submit(self._work,reserved['context'])
            return reserved['attempt_id']

    def _work(self, context):
        try:
            self.engine._execute(*context)
        except Exception:
            pass  # Engine persists fixed safe codes; raw exceptions never reach HTTP.

    def close(self):
        with self._lock:
            self._closed=True
        self._pool.shutdown(wait=True)
