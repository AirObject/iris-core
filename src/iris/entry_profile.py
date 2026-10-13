"""Entry-local profiles derived from original evidence; bounded IO and version CAS.

Reading a profile never regenerates it. The service owns background admin jobs.
Evidence IDs are snapshots, not foreign keys that pin cleaned source messages.
"""
from __future__ import annotations

import copy
import json
import re
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Protocol
from difflib import SequenceMatcher
from threading import Lock
from zoneinfo import ZoneInfo

from .db import dumps
from .memory_ops import Visibility, operation
from .models import Gateway, ModelError
from .persona import split_sentences
from .queue import estimate_tokens, truncate_material
from .profile_common import evidence_hash, setting, whitespace, excerpt, json_object, ChatHealthGate, load_profile_prompts, profile_prompt_metadata

DEFAULTS = dict(enabled=True, publish_mode='check_auto', window_days=14,
                message_tokens=6000, memory_tokens=2000, min_messages=50,
                update_days=7, min_chars=150, max_chars=500,
                generate_timeout_seconds=120, check_timeout_seconds=120)
DEFAULT_PROMPT_VERSION = 'v2'
PROMPT_VERSIONS = ('v2',)
PUBLISH_MODES = ('check_auto', 'all_manual')
DEGREES = ('small', 'medium', 'large')
# The optional prepare partition has its own cap; the 1500-token memory cap is unchanged.
PROFILE_FORMAT = 2
MAX_ITEMS = 6
MAX_PROFILE_TOKENS = 500
ITEM_LABELS = {'topic':'常聊话题', 'saying':'群内说法', 'rule':'明确群规／禁忌'}
_ACCOUNT = re.compile(
    r'(?i)(?<!\d)\d{5,}(?!\d)|<@!?\d+>|\[CQ:at[,\]]|'
    r'(?:qq|uid|user[_ -]?id|微信号?|账号|帐号|群号)\s*[:：=]\s*[a-z0-9_.@+-]+|'
    r'[a-z0-9_.+-]+@[a-z0-9.-]+\.[a-z]{2,}')
VIOLATIONS = ('无依据', '单条消息泛化', '个人信息或隐私', '个人评价', '性别推断', '指令', '其他入口', '虚构')
# Service/offline locks prevent multiple processes from owning this database.
# A durable running receipt without a live owner is an interrupted call.
_ACTIVE_LOCK = Lock()
_ACTIVE_ATTEMPTS = set()

MESSAGE_FIELDS = ('id', 'entry_id', 'kind', 'sender_subject_id', 'content', 'occurred_at',
                  'received_at', 'dedupe_key', 'quote_author_subject_id', 'quote_content')


def utc_now():
    return datetime.now(timezone.utc)


class EntryProfileConflict(ValueError):
    """The published version, settings or selected evidence changed."""


class EntryProfileBusy(ValueError):
    """A bounded generation is already in progress for this entry."""


def _instant(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None:
        raise ValueError('entry profile time requires a timezone')
    return result


def _hash(value):
    return evidence_hash(value)


def _setting(conn, key, default):
    return setting(conn, key, default)


def _runtime(conn):
    config = {**DEFAULTS, **_setting(conn, 'entry_profile', {})}
    if type(config['enabled']) is not bool or config['publish_mode'] not in PUBLISH_MODES:
        raise ValueError('invalid entry profile settings')
    for key in ('window_days','message_tokens','memory_tokens','min_messages','update_days',
                'min_chars','max_chars','generate_timeout_seconds','check_timeout_seconds'):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError('invalid entry profile settings')
    if (config['window_days'] > 14 or config['message_tokens'] > 6000 or config['memory_tokens'] > 2000
            or config['min_messages'] < 50 or config['min_chars'] < 150 or config['max_chars'] > 500
            or config['min_chars'] > config['max_chars']
            or max(config['generate_timeout_seconds'],config['check_timeout_seconds']) > 120):
        raise ValueError('entry profile settings exceed specification')
    return config


def _entry(conn, entry_id):
    row = conn.execute('SELECT id,kind,name FROM entries WHERE id=?', (entry_id,)).fetchone()
    if row is None:
        raise KeyError(entry_id)
    return dict(row)


def _settings(conn, entry_id):
    _entry(conn, entry_id)
    defaults = _runtime(conn)
    row = conn.execute('SELECT * FROM entry_profile_settings WHERE entry_id=?', (entry_id,)).fetchone()
    result = (dict(row) | {'enabled':bool(row['enabled'])}) if row else dict(
        entry_id=entry_id, enabled=True, publish_mode=defaults['publish_mode'], revision=0,
        current_version=0, updated_at=None)
    if memory_profile_enabled(conn):
        result.update(current_version=_memory_state(conn,entry_id)['version'],publish_mode='check_auto',mode='memory')
    return result


def profile_settings(store, entry_id):
    with store.read() as conn:
        return _settings(conn, entry_id)


def _ensure(conn, entry_id, stamp):
    config = _settings(conn, entry_id)
    if not config['revision']:
        conn.execute('INSERT INTO entry_profile_settings(entry_id,publish_mode,updated_at) VALUES(?,?,?)',
                     (entry_id,config['publish_mode'],stamp))
    return _settings(conn, entry_id)


def _decode(row):
    if row is None:
        return None
    value = dict(row)
    for key in ('sentences','checks','material'):
        value[key] = json.loads(value.pop(key+'_json'))
    return value


def _version(conn, entry_id, version):
    row = conn.execute('SELECT * FROM entry_profile_versions WHERE entry_id=? AND version=?', (entry_id,version)).fetchone()
    if row is None:
        raise KeyError((entry_id,version))
    return _decode(row)


def _current(conn, entry_id):
    row = conn.execute('SELECT current_version FROM entry_profile_settings WHERE entry_id=?',(entry_id,)).fetchone()
    version = row[0] if row else 0
    return _version(conn, entry_id, version) if version else None


def _expect(conn, entry_id, expected_version):
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError('expected_version must be a nonnegative integer')
    current = _current(conn, entry_id)
    if (current['version'] if current else 0) != expected_version:
        raise EntryProfileConflict('current_version_changed')
    return current


def _message_hash(row):
    return _hash({key:row[key] for key in MESSAGE_FIELDS})


def _excerpt(text, limit):
    return excerpt(text, limit)


def _message(row, zone, *, limit=300):
    value = {'message_id':row['id'], 'source_id':row['dedupe_key'], 'entry_id':row['entry_id'], 'kind':row['kind'],
            'date':_instant(row['occurred_at']).astimezone(zone).date().isoformat(),
            'occurred_at':row['occurred_at'], 'speaker':row['sender_subject_id'],
            'content':_excerpt(row['content'], limit), 'sha256':_message_hash(row)}
    if row['quote_content'] is not None:
        value['quote']={'speaker':row['quote_author_subject_id'],
                        'content':_excerpt(row['quote_content'], min(limit,180))}
    return value


def _direct_sources(conn, mid, entry_id):
    return [dict(r) for r in conn.execute('''SELECT x.* FROM sources s JOIN messages x ON x.id=s.message_id
        WHERE s.memory_id=? AND s.kind='message' AND x.entry_id=? ORDER BY julianday(x.occurred_at),x.id''', (mid,entry_id))]


def _memory_hash(row, sources):
    return _hash({'id':row['id'],'revision':row['revision'],'content':row['content'],
                  'lifecycle':row['lifecycle'],'sources':[(x['id'],_message_hash(x)) for x in sources]})


def _message_rows(conn, entry_id, current, config):
    return [dict(r) for r in conn.execute('''SELECT * FROM messages WHERE entry_id=?
        AND kind IN ('message','self_output') AND sender_subject_id!='scene'
        AND julianday(occurred_at)>=julianday(?) AND julianday(occurred_at)<=julianday(?)
        AND julianday(received_at)<=julianday(?) ORDER BY julianday(occurred_at),id''',
        (entry_id,(current-timedelta(days=config['window_days'])).isoformat(),current.isoformat(),current.isoformat()))]


def _even_order(rows):
    """Deterministic farthest-interval traversal: first, last, then interval midpoints."""
    if not rows:
        return []
    indices=[0]
    if len(rows)>1:
        indices.append(len(rows)-1)
    intervals=[(0,len(rows)-1)]
    while intervals:
        new=[]
        for left,right in intervals:
            if right-left>1:
                middle=(left+right)//2
                indices.append(middle)
                new.extend(((left,middle),(middle,right)))
        intervals=new
    return [rows[i] for i in indices]


def _select(conn, entry_id, current):
    entry=_entry(conn,entry_id); config=_runtime(conn)
    zone=ZoneInfo(_setting(conn,'timezone','Asia/Shanghai'))
    rows=_message_rows(conn,entry_id,current,config)
    days=defaultdict(list)
    for row in rows:
        days[_instant(row['occurred_at']).astimezone(zone).date().isoformat()].append(row)
    selected=[]
    # Equal per-day token quotas prevent a busy day from displacing a quiet day.
    per_day=config['message_tokens']//max(1,len(days))
    for day in sorted(days):
        used=0
        for row in _even_order(days[day]):
            item=_message(row,zone,limit=min(300,per_day))
            # Account for JSON metadata and the eventual reference key as well.
            item['ref']='S999999'
            while estimate_tokens(dumps(item))+used+2>per_day and item['content']:
                item['content']=item['content'][:max(0,len(item['content'])-16)]
            if not item['content']:
                continue
            used+=estimate_tokens(dumps(item))+1
            selected.append(item)
    selected.sort(key=lambda x:(_instant(x['occurred_at']),x['message_id']))
    for i,item in enumerate(selected):
        item['ref']=f'S{i+1}'
    while selected and estimate_tokens(dumps(selected))>config['message_tokens']:
        selected.pop()
    visibility=Visibility(conn)
    memories=[]
    for row in conn.execute('''SELECT m.* FROM memories m WHERE m.lifecycle='active' AND m.kind!='入口画像' AND m.purged_at IS NULL
        AND EXISTS(SELECT 1 FROM sources s JOIN messages x ON x.id=s.message_id
            WHERE s.memory_id=m.id AND s.kind='message' AND x.entry_id=?)
        ORDER BY m.importance DESC,m.id''',(entry_id,)):
        if not visibility.memory_visible(row['id'],entry_id):
            continue
        sources=_direct_sources(conn,row['id'],entry_id)
        display=sources if len(sources)<=4 else [sources[0],*sources[-3:]]
        item={'ref':f'M{len(memories)+1}','memory_id':row['id'],'revision':row['revision'],
              'content':truncate_material(row['content'],300),'stance':row['stance'],
              'source_messages':[_message(s,zone,limit=180) for s in display],
              'source_message_count':len(sources),'sha256':_memory_hash(row,sources)}
        if estimate_tokens(dumps([*memories,item]))<=config['memory_tokens']:
            memories.append(item)
    return {'entry':entry,'as_of':current.isoformat(),'messages':selected,'memories':memories,
            'window_message_count':len(rows),'message_through':max((r['id'] for r in rows),default=0),
            'message_tokens':estimate_tokens(dumps(selected)), 'memory_tokens':estimate_tokens(dumps(memories))}


def select_material(store, entry_id, *, clock=utc_now):
    with store.read() as conn:
        return _select(conn,entry_id,_instant(clock()))


def _basis(item):
    if 'memory_id' in item:
        return {'memory_id':item['memory_id'],'revision':item['revision'],'sha256':item['sha256'],
                'source_messages':[{'message_id':s['message_id'],'sha256':s['sha256']} for s in item['source_messages']]}
    return {'message_id':item['message_id'],'sha256':item['sha256']}


def _stale(conn, entry_id, sentences):
    result=[]; seen=set(); visibility=Visibility(conn)
    for sentence in sentences:
        for basis in sentence['basis']:
            key=dumps(basis)
            if key in seen:
                continue
            seen.add(key)
            reason=None
            if 'memory_id' in basis:
                row=conn.execute('SELECT * FROM memories WHERE id=?',(basis['memory_id'],)).fetchone()
                if row is None or row['lifecycle']!='active' or row['purged_at']:
                    reason='memory_unavailable'
                elif row['revision']!=basis['revision']:
                    reason='memory_changed'
                elif not visibility.memory_visible(row['id'],entry_id):
                    reason='visibility_changed'
                elif _memory_hash(row,_direct_sources(conn,row['id'],entry_id))!=basis['sha256']:
                    reason='memory_sources_changed'
            else:
                row=conn.execute('SELECT * FROM messages WHERE id=?',(basis['message_id'],)).fetchone()
                if row is None:
                    reason='message_cleaned'
                elif row['entry_id']!=entry_id or _message_hash(row)!=basis['sha256']:
                    reason='message_changed'
            if reason:
                result.append({**basis,'reason':reason})
    return result


def _view(conn, value):
    if value is None:
        return None
    value={**value,'stale_basis':_stale(conn,value['entry_id'],value['sentences'])}
    value['possibly_stale']=bool(value['stale_basis'])
    value['legacy']=memory_profile_enabled(conn)
    value['is_current']=not value['legacy'] and _settings(conn,value['entry_id'])['current_version']==value['version']
    return value


def current_profile(store, entry_id):
    with store.read() as conn:
        return _view(conn,_current(conn,entry_id))


def profile_context(conn, entry_id, *, known_memory_ids=()):
    """Optional prepare partition; no writes, calls or cross-entry reads."""
    if memory_profile_enabled(conn):
        return memory_profile_context(conn,entry_id,known_memory_ids=known_memory_ids)
    if (_entry(conn,entry_id)['kind'] not in ('group','live')
            or not _settings(conn,entry_id)['enabled'] or not _runtime(conn)['enabled']):
        return None
    value=_view(conn,_current(conn,entry_id))
    return ({'text':value['content'],'version':value['version'],'generated_at':value['generated_at'],
             'possibly_stale':value['possibly_stale']} if value else None)


def get_version(store, entry_id, version):
    with store.read() as conn:
        return _view(conn,_version(conn,entry_id,version))


def list_versions(store, entry_id, *, limit=50, offset=0):
    if type(limit) is not int or not 1<=limit<=200 or type(offset) is not int or offset<0:
        raise ValueError('invalid pagination')
    with store.read() as conn:
        settings=_settings(conn,entry_id)
        return [dict(r)|{'is_current':r['version']==settings['current_version']} for r in conn.execute(
            '''SELECT id,entry_id,version,base_version,content,change_degree,status,generated_at,published_at,author,source
               FROM entry_profile_versions WHERE entry_id=? ORDER BY version DESC LIMIT ? OFFSET ?''',(entry_id,limit,offset))]


def _due(conn, entry_id, current):
    if memory_profile_enabled(conn):
        return _memory_due(conn,entry_id,current)
    entry=_entry(conn,entry_id); config=_runtime(conn); settings=_settings(conn,entry_id)
    previous=_current(conn,entry_id)
    window_count=conn.execute('''SELECT COUNT(*) FROM messages WHERE entry_id=?
        AND kind IN ('message','self_output') AND sender_subject_id!='scene'
        AND julianday(occurred_at)>=julianday(?) AND julianday(occurred_at)<=julianday(?)
        AND julianday(received_at)<=julianday(?)''',
        (entry_id,(current-timedelta(days=config['window_days'])).isoformat(),current.isoformat(),current.isoformat())).fetchone()[0]
    new=conn.execute('''SELECT COUNT(*) FROM messages WHERE entry_id=? AND id>?
        AND kind IN ('message','self_output') AND sender_subject_id!='scene'
        AND julianday(occurred_at)<=julianday(?) AND julianday(received_at)<=julianday(?)''',
        (entry_id,previous['message_through'] if previous else 0,current.isoformat(),current.isoformat())).fetchone()[0]
    stale=bool(previous and _stale(conn,entry_id,previous['sentences']))
    day=current.astimezone(ZoneInfo(_setting(conn,'timezone','Asia/Shanghai'))).date().isoformat()
    attempted=conn.execute("SELECT 1 FROM entry_profile_attempts WHERE entry_id=? AND local_day=? AND state!='skipped' LIMIT 1",(entry_id,day)).fetchone()
    reason=('entry_kind' if entry['kind'] not in ('group','live') else
            'disabled' if not config['enabled'] or not settings['enabled'] else
            'daily_limit' if attempted else
            'insufficient_messages' if previous is None and window_count<config['min_messages'] else None)
    if reason is None and previous and not (stale or new>=config['min_messages'] or (
            new and current-_instant(previous['generated_at'])>=timedelta(days=config['update_days']))):
        reason='not_due'
    return {'due':reason is None,'reason':reason,'window_message_count':window_count,'new_messages':new,
            'current_version':previous['version'] if previous else 0,'local_day':day,'possibly_stale':stale}


def profile_due(store, entry_id, *, clock=utc_now):
    with store.read() as conn:
        return _due(conn,entry_id,_instant(clock()))


def _audit(conn, action, entry_id, stamp, **details):
    operation(conn,'entry_profile_'+action,'entry_profile',entry_id,details,actor='entry_profile' if action.startswith('generation') else 'admin',stamp=stamp)


def set_settings(store, entry_id, *, expected_version, enabled=None, publish_mode=None, clock=utc_now):
    if enabled is not None and type(enabled) is not bool:
        raise ValueError('enabled must be boolean')
    if publish_mode is not None and publish_mode not in PUBLISH_MODES:
        raise ValueError('invalid publish mode')
    stamp=_instant(clock()).isoformat()
    with store.write() as conn:
        if memory_profile_enabled(conn):
            _memory_expect(conn,entry_id,expected_version)
            if publish_mode not in (None,'check_auto'):
                raise EntryProfileRetired('entry_profile_publish_mode_retired')
        else:
            _expect(conn,entry_id,expected_version)
        settings=_ensure(conn,entry_id,stamp)
        values={'enabled':settings['enabled'] if enabled is None else enabled,
                'publish_mode':settings['publish_mode'] if publish_mode is None else publish_mode}
        conn.execute('UPDATE entry_profile_settings SET enabled=?,publish_mode=?,revision=revision+1,updated_at=? WHERE entry_id=?',
                     (values['enabled'],values['publish_mode'],stamp,entry_id))
        _audit(conn,'settings',entry_id,stamp,expected_version=expected_version,**values)
    return profile_settings(store,entry_id)


def _length_errors(content, config, *, structured=False):
    size=len(content) if structured else len(content.strip())
    return (['empty'] if not content.strip() else ['too_short'] if not structured and size<config['min_chars'] else
            ['too_long'] if size>config['max_chars'] else
            ['token_budget'] if structured and estimate_tokens(content)>MAX_PROFILE_TOKENS else [])


def _edit_segments(content, previous):
    # Quotes can contain punctuation. Preserve unchanged rendered items atomically,
    # and use the legacy sentence splitter only for genuinely new/edited text.
    # Cleared items retain audit metadata, not text that can match an edit.
    known=sorted({s['text'] for s in previous['sentences'] if s['text'] and s.get('format_version')==PROFILE_FORMAT}
                 if previous else [],key=len,reverse=True)
    if not known:
        return split_sentences(content)
    parts=[]; start=0
    for match in re.finditer('^(?:'+'|'.join(re.escape(text) for text in known)+')',content,re.MULTILINE):
        parts.extend(split_sentences(content[start:match.start()]))
        parts.append(match.group())
        start=match.end()
    parts.extend(split_sentences(content[start:]))
    return parts


def _insert(conn, entry_id, *, previous, content, sentences, checks, degree, status, stamp,
            author, source, material, message_through, settings_revision):
    version=conn.execute('SELECT COALESCE(MAX(version),0)+1 FROM entry_profile_versions WHERE entry_id=?',(entry_id,)).fetchone()[0]
    # Only three public states: supersession is a rejected candidate with a reason.
    for row in conn.execute("SELECT version,checks_json FROM entry_profile_versions WHERE entry_id=? AND status='candidate'",(entry_id,)).fetchall():
        old=json.loads(row['checks_json']); old.update(reason='superseded',superseded_by=version)
        conn.execute("UPDATE entry_profile_versions SET status='rejected',checks_json=? WHERE entry_id=? AND version=?",(dumps(old),entry_id,row['version']))
    conn.execute('''INSERT INTO entry_profile_versions(entry_id,version,base_version,content,sentences_json,checks_json,
        change_degree,status,generated_at,published_at,author,source,message_through,material_json,settings_revision)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(entry_id,version,previous,content,dumps(sentences),dumps(checks),degree,status,
        stamp,stamp if status=='published' else None,author,source,message_through,dumps(material),settings_revision))
    if status=='published':
        conn.execute('UPDATE entry_profile_settings SET current_version=? WHERE entry_id=?',(version,entry_id))
    _audit(conn,'generation_'+status if author=='model' else source,entry_id,stamp,version=version,base_version=previous)
    return _view(conn,_version(conn,entry_id,version))


def admin_edit(store, entry_id, content, *, expected_version, clock=utc_now):
    require_legacy_write(store,entry_id)
    stamp=_instant(clock()).isoformat()
    with store.write() as conn:
        previous=_expect(conn,entry_id,expected_version); settings=_ensure(conn,entry_id,stamp)
        if _entry(conn,entry_id)['kind'] not in ('group','live'):
            raise ValueError('entry kind does not support profiles')
        if not isinstance(content,str) or _length_errors(content,_runtime(conn)):
            raise ValueError('profile must contain 150–500 characters')
        old=defaultdict(list)
        for sentence in previous['sentences'] if previous else []:
            if sentence['text']:
                old[sentence['text']].append(sentence)
        sentences=[]
        for text in _edit_segments(content,previous):
            sentences.append(old[text].pop(0) if old[text] else {'text':text,'basis':[],'author':'admin','message_count':0})
        through=conn.execute('SELECT COALESCE(MAX(id),0) FROM messages WHERE entry_id=?',(entry_id,)).fetchone()[0]
        return _insert(conn,entry_id,previous=expected_version,content=content,sentences=sentences,
            checks={'passed':True,'deterministic':{'passed':True},'model':None,'admin_edit':True},degree='large',status='published',
            stamp=stamp,author='admin',source='edit',material=previous['material'] if previous else {},message_through=through,settings_revision=settings['revision'])


def version_diff(store, entry_id, before, after):
    with store.read() as conn:
        left=_version(conn,entry_id,before)['sentences']; right=_version(conn,entry_id,after)['sentences']
        changes=[{'op':op,'before':left[a:b],'after':right[c:d]} for op,a,b,c,d in
                 SequenceMatcher(a=[s['text'] for s in left],b=[s['text'] for s in right],autojunk=False).get_opcodes()]
        return {'entry_id':entry_id,'before':before,'after':after,'changed':left!=right,'sentences':changes}


def rollback(store, entry_id, version, *, expected_version, clock=utc_now):
    require_legacy_write(store,entry_id)
    stamp=_instant(clock()).isoformat()
    with store.write() as conn:
        _expect(conn,entry_id,expected_version); settings=_ensure(conn,entry_id,stamp)
        target=_version(conn,entry_id,version)
        if target['status']!='published' or _length_errors(target['content'],_runtime(conn),structured=target['checks'].get('format_version')==PROFILE_FORMAT):
            raise ValueError('rollback requires a previously published valid profile')
        through=conn.execute('SELECT COALESCE(MAX(id),0) FROM messages WHERE entry_id=?',(entry_id,)).fetchone()[0]
        return _insert(conn,entry_id,previous=expected_version,content=target['content'],sentences=target['sentences'],
            checks={**target['checks'],'rollback_from':version},degree='large',status='published',stamp=stamp,author='admin',
            source='rollback',material=target['material'],message_through=through,settings_revision=settings['revision'])


def confirm_candidate(store, entry_id, version, *, expected_version, clock=utc_now):
    require_legacy_write(store,entry_id)
    stamp=_instant(clock()).isoformat()
    with store.write() as conn:
        _expect(conn,entry_id,expected_version); value=_version(conn,entry_id,version)
        if value['status']!='candidate' or value['base_version']!=expected_version:
            raise EntryProfileConflict('candidate_changed')
        if not value['checks']['passed'] or _stale(conn,entry_id,value['sentences']):
            raise EntryProfileConflict('evidence_changed')
        conn.execute("UPDATE entry_profile_versions SET status='published',published_at=? WHERE entry_id=? AND version=?",(stamp,entry_id,version))
        conn.execute('UPDATE entry_profile_settings SET current_version=? WHERE entry_id=?',(version,entry_id))
        _audit(conn,'confirm',entry_id,stamp,version=version,base_version=expected_version)
        return _view(conn,_version(conn,entry_id,version))


def reject_candidate(store, entry_id, version, *, expected_version, reason='administrator_rejected', clock=utc_now):
    require_legacy_write(store,entry_id)
    stamp=_instant(clock()).isoformat()
    with store.write() as conn:
        _expect(conn,entry_id,expected_version); value=_version(conn,entry_id,version)
        if value['status']!='candidate':
            raise EntryProfileConflict('candidate_changed')
        checks={**value['checks'],'reason':reason}
        conn.execute("UPDATE entry_profile_versions SET status='rejected',checks_json=? WHERE entry_id=? AND version=?",(dumps(checks),entry_id,version))
        _audit(conn,'reject',entry_id,stamp,version=version)
        return _view(conn,_version(conn,entry_id,version))


def _whitespace(text):
    """Collapse whitespace only; do not fold punctuation, case, or missing words."""
    return whitespace(text)


def _evidence_index(evidence):
    messages={}; owners={}
    for item in evidence['messages']:
        if item['entry_id']==evidence['entry']['id']:
            messages[item['message_id']]=item
    for memory in evidence['memories']:
        for item in memory['source_messages']:
            if item['entry_id']!=evidence['entry']['id']:
                continue
            # A quiet-day quota can shorten the direct excerpt below the memory
            # source excerpt. Accept the longest material view and bind its owner.
            old=messages.get(item['message_id'])
            if old is None or len(item['content'])>len(old['content']):
                messages[item['message_id']]=item
                owners[item['message_id']]=memory['ref']
    return messages,owners


def _contains_account(text, evidence):
    if _ACCOUNT.search(text):
        return True
    messages,_=_evidence_index(evidence)
    # Opaque account IDs are metadata, never valid background prose. Do not treat
    # ordinary display names or self/scene as account numbers.
    identifiers={str(speaker) for message in messages.values()
                 for speaker in (message.get('speaker'),message.get('quote',{}).get('speaker'))
                 if speaker and re.search(r'[0-9:_@-]',str(speaker)) and len(str(speaker))>=5}
    return any(re.search(r'(?<![a-zA-Z0-9_])'+re.escape(identifier)+r'(?![a-zA-Z0-9_])',text) for identifier in identifiers)


def _quote(quote, messages):
    if not isinstance(quote,dict) or set(quote)!={'message_id','text'}:
        return None,'invalid_quote'
    if type(quote['message_id']) is not int or quote['message_id'] not in messages:
        return None,'invalid_quote_reference'
    if not isinstance(quote['text'],str) or not 1<=len(_whitespace(quote['text']))<=100:
        return None,'invalid_quote'
    message=messages[quote['message_id']]; text=_whitespace(quote['text'])
    for field,source in (('content',message),('quote',message.get('quote',{}))):
        if text in _whitespace(source.get('content','')):
            return {**quote,'text':text,'field':field,'speaker':source.get('speaker')},None
    return None,'quote_not_found'


def _independent_topic(quotes, messages):
    # The count alone does not prove a recurring topic; the checker still checks
    # independent discussions and scope. Reject same-day exchanges and quote copies.
    originals=[]; dates=set(); ids=set()
    for quote in quotes:
        # A forwarded original has no trustworthy original event date. It may
        # explain an item, but never supplies a second observation of recurrence.
        if quote['field']!='content':
            continue
        message=messages[quote['message_id']]
        text=_whitespace(message['content'])
        if any(text in old or old in text for old in originals):
            continue
        originals.append(text)
        dates.add(message['date']); ids.add(message['message_id'])
    return len(ids)>=2 and len(dates)>=2



def _render_item(item):
    text=ITEM_LABELS[item['kind']]+'：'+item['label']
    if item.get('meaning'):
        text+='——'+item['meaning']
    return text+'（'+'；'.join('“'+q['text']+'”' for q in item['quotes'])+'）。\n'


def _resolve(generated, evidence, previous):
    messages,owners=_evidence_index(evidence)
    memories={item['ref']:item for item in evidence['memories']}
    manual=[dict(s) for s in previous['sentences'] if s['author']=='admin'] if previous else []
    sentences=[]; deleted=[]; errors=[]; seen=set()
    protected={s['text'] for s in manual}
    quote_stats={'total':0,'passed':0}
    rows=generated.get('items') if isinstance(generated,dict) else None
    if not isinstance(rows,list) or len(rows)>50 or set(generated)!={'items'}:
        return manual,[],['invalid_items'],0,quote_stats
    original_count=len(rows)
    for index,row in enumerate(rows):
        reason=None; quotes=[]
        raw_quotes=row.get('quotes') if isinstance(row,dict) else None
        # Audit every supplied quote, even in an item with another schema error.
        if isinstance(raw_quotes,list):
            for raw in raw_quotes:
                quote,issue=_quote(raw,messages)
                quote_stats['total']+=1
                quote_stats['passed']+=issue is None
                if issue:
                    reason=reason or issue
                else:
                    quotes.append(quote)
        if (not isinstance(row,dict) or set(row)-{'kind','label','meaning','quotes','memory_refs'}
                or not isinstance(row.get('kind'),str) or row['kind'] not in ITEM_LABELS
                or not isinstance(row.get('label'),str) or not 1<=len(_whitespace(row['label']))<=40
                or not isinstance(row.get('meaning',''),str) or len(_whitespace(row.get('meaning','')))>80
                or (row.get('meaning') and row.get('kind')!='saying')):
            reason=reason or 'invalid_item'
        elif not isinstance(raw_quotes,list) or not 1<=len(raw_quotes)<=3:
            reason=reason or 'invalid_quotes'
        refs=row.get('memory_refs',[]) if isinstance(row,dict) else []
        if (not isinstance(refs,list) or len(refs)>3
                or any(not isinstance(ref,str) or ref not in memories for ref in refs)):
            reason=reason or 'invalid_evidence_reference'
        if not reason:
            item={'kind':row['kind'],'label':_whitespace(row['label']),
                  'meaning':_whitespace(row.get('meaning','')),'quotes':quotes}
            key=(item['kind'],item['label'])
            if _render_item(item) in protected:
                # As in v1, model echoes never replace or duplicate handwritten content.
                original_count-=1
                continue
            if key in seen:
                reason='duplicate_item'
            elif _contains_account(' '.join([item['label'],item['meaning'],*[q['text'] for q in quotes]]),evidence):
                reason='account_identifier'
            elif item['kind']=='topic' and not _independent_topic(quotes,messages):
                reason='insufficient_topic_recurrence'
            if not reason:
                seen.add(key)
                refs=list(dict.fromkeys([*refs,*(owners[q['message_id']] for q in quotes if q['message_id'] in owners)]))
                basis=[_basis(messages[mid]) for mid in dict.fromkeys(q['message_id'] for q in quotes) if mid not in owners]
                basis.extend(_basis(memories[ref]) for ref in refs)
                sentences.append({**item,'text':_render_item(item),'basis':basis,'author':'model',
                                  'message_count':len({q['message_id'] for q in quotes}), 'refs':refs,'format_version':PROFILE_FORMAT})
        if reason:
            deleted.append({'index':index+1,'sentence':row,'reason':reason,'stage':'deterministic'})
    if original_count+len(manual)>MAX_ITEMS:
        errors.append('too_many_items')
    return [*sentences,*manual],deleted,errors,original_count,quote_stats


def _check_input(material, sentences):
    """Bind each structured item to its quotes and explicitly linked memories."""
    evidence=material['evidence']; messages,_=_evidence_index(evidence)
    memories={item['ref']:item for item in evidence['memories']}
    def message(item):
        return {key:item[key] for key in ('message_id','entry_id','kind','date','occurred_at',
                                          'speaker','content','quote') if key in item}
    def memory(item):
        return {**{key:item[key] for key in ('ref','memory_id','revision','content','stance')},
                'source_messages':[message(source) for source in item['source_messages']]}
    candidate=[]
    for index,sentence in enumerate(sentences,1):
        model=sentence['author']=='model'
        quotes=sentence.get('quotes',[]) if model else []
        refs=sentence.get('refs',[]) if model else []
        candidate.append({'index':index,'text':sentence['text'],'author':sentence['author'],
            **({key:sentence[key] for key in ('kind','label','meaning','quotes')} if model else {}),
            'evidence':{'messages':[message(messages[mid]) for mid in dict.fromkeys(q['message_id'] for q in quotes)],
                        'memories':[memory(memories[ref]) for ref in refs]}})
    return {'entry':evidence['entry'],'as_of':evidence['as_of'],'previous':material['previous'],
            'candidate':candidate}


def _check(output, sentences):
    if not isinstance(output,dict) or output.get('change_degree') not in DEGREES or not isinstance(output.get('reason'),str):
        raise ValueError('invalid_check')
    rows=output.get('sentences')
    if not isinstance(rows,list) or len(rows)!=len(sentences):
        raise ValueError('invalid_check_sentence_count')
    removed=[]; retained=[]; admin_warnings=[]; removed_meanings=[]
    for index,(row,sentence) in enumerate(zip(rows,sentences,strict=True)):
        if (not isinstance(row,dict) or type(row.get('index')) is not int or row['index']!=index+1
                or type(row.get('supported')) is not bool or type(row.get('scene_qualified')) is not bool
                or type(row.get('meaning_supported')) is not bool
                or not isinstance(row.get('violations'),list) or not isinstance(row.get('reason'),str) or not row['reason'].strip()
                or any(v not in VIOLATIONS for v in row['violations'])):
            raise ValueError('invalid_check_sentence')
        bad=not row['supported'] or bool(row['violations']) or not row['scene_qualified']
        if sentence['author']=='admin':
            retained.append(sentence)
            if row['violations']:
                admin_warnings.append({'index':index+1,'result':row})
        elif bad:
            removed.append({'index':index+1,'sentence':sentence,'reason':row['reason'],'result':row,'stage':'model'})
        else:
            if sentence.get('meaning') and not row['meaning_supported']:
                removed_meanings.append({'index':index+1,'meaning':sentence['meaning'],'reason':row['reason']})
                sentence={**sentence,'meaning':''}
                sentence['text']=_render_item(sentence)
            retained.append(sentence)
    return retained,removed,admin_warnings,output['change_degree'],removed_meanings


def _admin_removed(previous, sentences):
    from collections import Counter
    before=Counter(s['text'] for s in previous['sentences'] if s['author']=='admin') if previous else Counter()
    after=Counter(s['text'] for s in sentences if s['author']=='admin')
    return bool(before-after)


class _HealthGate(ChatHealthGate):
    pass


class EntryProfileEngine:
    def __init__(self, store, gateway, *, clock=utc_now, prompt_version=None):
        self.store,self.gateway,self.clock=store,gateway,clock
        self.memory_mode = getattr(store,'_memory_profiles',False)
        if self.memory_mode and prompt_version is not None:
            raise EntryProfileRetired('entry_profile_legacy_write_retired')
        self.prompt_version='v1' if self.memory_mode else DEFAULT_PROMPT_VERSION if prompt_version is None else prompt_version
        self.prompts=load_profile_prompts('entry_profile_memory' if self.memory_mode else 'entry_profile',
            self.prompt_version,('v1',) if self.memory_mode else PROMPT_VERSIONS)
        self.prompt_metadata=profile_prompt_metadata(self.prompts)

    def update(self, entry_id, *, expected_version, run_id=None):
        return self._run(entry_id,expected_version,'periodic',run_id=run_id)

    def regenerate(self, entry_id, *, expected_version):
        return self._run(entry_id,expected_version,'regenerate')

    def _gateway(self):
        if self.gateway is None or not callable(getattr(self.gateway,'chat',None)):
            raise ModelError('paused','unconfigured',paused=True,reason='unconfigured')
        gateway=self.gateway
        if isinstance(gateway,Gateway):
            health=gateway.health
            if health is None:
                from .model_health import ModelHealth
                health=ModelHealth(self.store,gateway._raw_configs,clock=self.clock)
            # Do not create/close pools or replace the caller's live health gate.
            gateway=copy.copy(gateway)
            gateway.health=_HealthGate(health)
        return gateway

    def _call(self, gateway, kind, payload, outputs, calls, config):
        health=getattr(gateway,'health',None)
        if health:
            health.check('chat','learning')
        purpose='consolidation_entry_profile_'+kind
        loaded=self.prompts[kind]
        provenance={key:loaded[key] for key in ('prompt_version','prompt_sha256')}
        prompt=loaded['text']
        messages=[{'role':'system','content':prompt},{'role':'user','content':dumps(payload)}]
        monotonic=getattr(gateway,'monotonic',time.monotonic)
        started=monotonic(); deadline=started+config[kind+'_timeout_seconds']
        try:
            reply=gateway.chat(messages,purpose,max_tokens=16000,_deadline=deadline)
        except ModelError as error:
            calls.append({**provenance,'purpose':purpose,'result':error.category,'reason':error.reason or error.category,
                          'duration_ms':round((monotonic()-started)*1000),'usage':{}})
            raise
        outputs[purpose]=reply.content
        calls.append({**provenance,'purpose':purpose,'result':'success','duration_ms':round((monotonic()-started)*1000),
                      'usage':reply.usage,'finish_reason':reply.finish_reason})
        if reply.finish_reason!='stop':
            raise ValueError('incomplete_output')
        return json_object(reply.content)

    def _finish(self, attempt, state, reason, outputs, calls):
        with self.store.write() as conn:
            conn.execute('''UPDATE entry_profile_attempts SET state=?,reason=?,outputs_json=?,calls_json=?,finished_at=?
                WHERE id=? AND state='running' ''',(state,reason,dumps(outputs),dumps(calls),_instant(self.clock()).isoformat(),attempt))

    def _skip(self, entry_id, expected, source, due, reason, run_id, stamp):
        with self.store.write() as conn:
            (_memory_expect if self.memory_mode else _expect)(conn,entry_id,expected)
            _audit(conn,'generation_skipped',entry_id,stamp,base_version=expected,reason=reason,source=source)
            attempt=conn.execute('''INSERT OR IGNORE INTO entry_profile_attempts(entry_id,run_id,base_version,state,
                    reason,local_day,started_at,finished_at) VALUES(?,?,?,'skipped',?,?,?,?)''',
                    (entry_id,run_id,expected,reason,due['local_day'],stamp,stamp))
            attempt=(conn.execute('SELECT id FROM entry_profile_attempts WHERE entry_id=? AND run_id=?',
                                  (entry_id,run_id)).fetchone()[0] if run_id is not None else attempt.lastrowid)
        return {'status':'skipped','reason':reason,'version':None,'attempt_id':attempt,'due':due}

    def _run(self, entry_id, expected, source, *, run_id=None):
        reservation=self._reserve(entry_id,expected,source,run_id=run_id)
        return self._execute(**reservation['context']) if 'context' in reservation else reservation

    def _reserve(self, entry_id, expected, source, *, run_id=None, fail_if_running=False):
        if self.memory_mode:
            return _reserve_memory_task(self,entry_id,expected,source,run_id=run_id,fail_if_running=fail_if_running)
        """Persist admission and take an evidence snapshot; never call a model here."""
        current=_instant(self.clock()); stamp=current.isoformat()
        with self.store.write() as conn:
            _expect(conn,entry_id,expected)
            _recover_interrupted(conn,self.store,entry_id,stamp)
            if fail_if_running and conn.execute("SELECT 1 FROM entry_profile_attempts WHERE entry_id=? AND state='running'",(entry_id,)).fetchone():
                raise EntryProfileBusy('generation_in_progress')
        with self.store.read() as conn:
            previous=_expect(conn,entry_id,expected); due=_due(conn,entry_id,current)
            config=_runtime(conn); settings=_settings(conn,entry_id)
            reason=due['reason']
            if source=='regenerate' and reason=='not_due':
                reason=None
        if reason:
            return self._skip(entry_id,expected,source,due,reason,run_id,stamp)
        try:
            gateway=self._gateway()
            if getattr(gateway,'health',None):
                gateway.health.check('chat','learning')
        except ModelError as error:
            return self._skip(entry_id,expected,source,due,error.reason or error.category,run_id,stamp)
        with self.store.read() as conn:
            evidence=_select(conn,entry_id,current)
        if not evidence['messages'] and not evidence['memories']:
            return self._skip(entry_id,expected,source,due,'no_evidence',run_id,stamp)
        with self.store.write() as conn:
            _expect(conn,entry_id,expected)
            live=_settings(conn,entry_id)
            if live['revision']!=settings['revision'] or _runtime(conn)!=config:
                raise EntryProfileConflict('settings_changed')
            if conn.execute("SELECT 1 FROM entry_profile_attempts WHERE entry_id=? AND state='running'",(entry_id,)).fetchone():
                raise EntryProfileBusy('generation_in_progress')
            if conn.execute("SELECT 1 FROM entry_profile_attempts WHERE entry_id=? AND local_day=? AND state!='skipped' LIMIT 1",
                            (entry_id,due['local_day'])).fetchone():
                raise EntryProfileBusy('daily_limit')
            settings=_ensure(conn,entry_id,stamp)
            material={'format_version':PROFILE_FORMAT,'evidence':evidence,'settings':settings,'runtime':config,
                      'output_limits':{'max_items':MAX_ITEMS,'max_chars':config['max_chars'],'max_tokens':MAX_PROFILE_TOKENS},
                      'previous':previous['content'] if previous else '',
                      'admin_sentences':[s for s in previous['sentences'] if s['author']=='admin'] if previous else []}
            attempt=conn.execute('''INSERT INTO entry_profile_attempts(entry_id,run_id,base_version,state,local_day,
                started_at,material_json) VALUES(?,?,?,'running',?,?,?)''',
                (entry_id,run_id,expected,due['local_day'],stamp,dumps(material))).lastrowid
            _audit(conn,'regenerate_requested' if source=='regenerate' else 'generation_requested',
                   entry_id,stamp,attempt_id=attempt,base_version=expected,source=source)
            with _ACTIVE_LOCK:
                _ACTIVE_ATTEMPTS.add((self.store.path,attempt))
        return {'status':'running','attempt_id':attempt,'context':dict(entry_id=entry_id,expected=expected,
            source=source,stamp=stamp,previous=previous,due=due,config=config,settings=settings,
            gateway=gateway,evidence=evidence,material=material,attempt=attempt)}

    def _execute(self, *, entry_id, expected=None, source=None, stamp=None, previous=None, due=None, config=None, settings=None,
                 gateway=None, evidence=None, material=None, attempt=None, memory_repository=None):
        if memory_repository is not None:
            return _execute_memory_task(self,entry_id,attempt,gateway,memory_repository,due)
        outputs={}; calls=[]; sentences=None
        try:
            checks={'format_version':PROFILE_FORMAT,'passed':False,'errors':[],'deleted_sentences':[],'model':None}
            degree='large'
            try:
                generated=self._call(gateway,'generate',material,outputs,calls,config)
                sentences,deleted,errors,count,quote_stats=_resolve(generated,evidence,previous)
                checks.update(errors=errors,deleted_sentences=deleted,original_sentence_count=count,original_item_count=count,quote_validation=quote_stats,
                              original_sentences=generated.get('items'),deterministic={'passed':not errors and not deleted})
                content=''.join(s['text'] for s in sentences)
                if len(deleted)*2>count:
                    checks['errors'].append('too_many_deleted')
                checks['errors']+=_length_errors(content,config,structured=True)
                if not checks['errors']:
                    checks['checked_sentences']=sentences
                    # Persist progress without holding a transaction over network IO.
                    with self.store.write() as conn:
                        conn.execute('UPDATE entry_profile_attempts SET outputs_json=?,calls_json=? WHERE id=?',
                                     (dumps(outputs),dumps(calls),attempt))
                    checks['model_input']=_check_input(material,sentences)
                    output=self._call(gateway,'check',checks['model_input'],outputs,calls,config)
                    checks['model']=output
                    sentences,removed,warnings,degree,removed_meanings=_check(output,sentences)
                    checks['removed_meanings']=removed_meanings
                    checks['deleted_sentences']+=removed
                    checks['admin_warnings']=warnings
                    content=''.join(s['text'] for s in sentences)
                    if len(checks['deleted_sentences'])*2>count:
                        checks['errors'].append('too_many_deleted')
                    checks['errors']+=_length_errors(content,config,structured=True)
                    # A checker saw the larger draft; do not pretend it judged the
                    # final deletion's degree. Auto mode permits large changes too.
                    if removed or removed_meanings:
                        degree='large'
                checks['passed']=not checks['errors']
                checks['deterministic']={'passed':not checks['errors'],'errors':list(checks['errors']),
                    'characters':len(content),'tokens':estimate_tokens(content),
                    'deleted_count':len(checks['deleted_sentences'])}
            except (ModelError,ValueError) as error:
                reason=(error.reason or error.category) if isinstance(error,ModelError) else 'invalid_output'
                checks.update(reason=reason,errors=[*checks['errors'],reason],passed=False)
                if sentences is None:
                    self._finish(attempt,'failed',reason,outputs,calls)
                    return {'status':'failed','reason':reason,'version':None,'attempt_id':attempt,'due':due}
            content=''.join(s['text'] for s in sentences)
            manual_removed=_admin_removed(previous,sentences)
            checks['admin_content_removed_or_changed']=manual_removed
            if manual_removed:
                degree='large'
            if not checks['passed'] and not checks.get('reason'):
                checks['reason']=checks['errors'][0]
            status=('rejected' if not checks['passed'] else 'candidate' if
                    settings['publish_mode']=='all_manual' or manual_removed or checks.get('admin_warnings') else 'published')
            with self.store.write() as conn:
                _expect(conn,entry_id,expected)
                if _settings(conn,entry_id)['revision']!=settings['revision'] or _runtime(conn)!=config:
                    raise EntryProfileConflict('settings_changed')
                if _stale(conn,entry_id,sentences):
                    raise EntryProfileConflict('evidence_changed')
                if conn.execute('SELECT state FROM entry_profile_attempts WHERE id=?',(attempt,)).fetchone()[0]!='running':
                    raise EntryProfileConflict('attempt_changed')
                version=_insert(conn,entry_id,previous=expected,content=content,sentences=sentences,checks=checks,
                    degree=degree,status=status,stamp=stamp,author='model',source=source,material=material,
                    message_through=evidence['message_through'],settings_revision=settings['revision'])
                conn.execute('''UPDATE entry_profile_attempts SET state=?,reason=?,version=?,outputs_json=?,calls_json=?,finished_at=?
                    WHERE id=?''',(status,checks.get('reason'),version['version'],dumps(outputs),dumps(calls),
                    _instant(self.clock()).isoformat(),attempt))
            return {'status':status,'reason':checks.get('reason'),'version':version,'attempt_id':attempt,'due':due}
        except EntryProfileConflict as error:
            self._finish(attempt,'conflict',str(error),outputs,calls)
            raise
        except Exception:
            self._finish(attempt,'failed','unexpected_error',outputs,calls)
            raise
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE_ATTEMPTS.discard((self.store.path,attempt))


def _recover_interrupted(conn, store, entry_id, stamp):
    rows=conn.execute("SELECT id FROM entry_profile_attempts WHERE entry_id=? AND state='running'",(entry_id,)).fetchall()
    for row in rows:
        with _ACTIVE_LOCK:
            active=(store.path,row['id']) in _ACTIVE_ATTEMPTS
        if not active:
            conn.execute("UPDATE entry_profile_attempts SET state='failed',reason='interrupted',finished_at=? WHERE id=?",(stamp,row['id']))
            _audit(conn,'generation_interrupted',entry_id,stamp,attempt_id=row['id'])


class EntryProfileJobs:
    """One admitted admin job per service; persistent receipts survive page/restart.

    Reservation and CAS happen before HTTP 202, all network IO in the worker.
    The pool drains before the app closes its gateway and database.
    """
    def __init__(self, store, gateway, *, clock=utc_now):
        self.engine=EntryProfileEngine(store,gateway,clock=clock)
        self._lock=Lock()
        self._closed=False
        self._future=None
        self._pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='entry-profile')
        with store.write() as conn:
            entries=[r[0] for r in conn.execute("SELECT DISTINCT entry_id FROM entry_profile_attempts WHERE state='running'")]
            for entry_id in entries:
                _recover_interrupted(conn,store,entry_id,_instant(clock()).isoformat())

    def submit(self, entry_id, *, expected_version):
        with self._lock:
            if self._closed or self._future is not None and not self._future.done():
                raise EntryProfileBusy('generation_in_progress')
            reservation=self.engine._reserve(entry_id,expected_version,'regenerate',fail_if_running=True)
            if 'context' in reservation:
                self._future=self._pool.submit(self._work,reservation['context'])
            return reservation['attempt_id']

    def _work(self, context):
        try:
            self.engine._execute(**context)
        except Exception:
            # _execute persists only safe reason codes. Never expose exception text.
            pass

    def close(self):
        with self._lock:
            self._closed=True
        self._pool.shutdown(wait=True)


def update_profiles_for_run(store, gateway, run, config, *, clock=utc_now, stop=None):
    """One durable consolidation step. The supplied gateway owns max_calls admission."""
    if not config.get('entry_profile_enabled',True):
        return True
    with store.read() as conn:
        if not _runtime(conn)['enabled']:
            return True
        entries=[r[0] for r in conn.execute("SELECT id FROM entries WHERE kind IN ('group','live') ORDER BY id")]
    for entry_id in entries:
        if stop and stop():
            return False
        with store.write() as conn:
            _recover_interrupted(conn,store,entry_id,_instant(clock()).isoformat())
            saved=conn.execute('SELECT * FROM entry_profile_attempts WHERE entry_id=? AND run_id=?',(entry_id,run['id'])).fetchone()
            if saved and saved['state']=='running':
                return False  # Another live worker still owns this step.
            expected=_settings(conn,entry_id)['current_version']
            count=conn.execute('SELECT COUNT(*) FROM consolidation_calls WHERE run_id=?',(run['id'],)).fetchone()[0]
        if saved:
            result={'status':saved['state'],'reason':saved['reason']}
        else:
            with store.read() as conn:
                due=_due(conn,entry_id,_instant(clock()))
            engine=EntryProfileEngine(store,gateway,clock=clock)
            if due['due'] and count+2>config['max_calls']:
                result=engine._skip(entry_id,expected,'periodic',due,'call_budget',run['id'],_instant(clock()).isoformat())
            else:
                try:
                    result=engine.update(entry_id,expected_version=expected,run_id=run['id'])
                except (EntryProfileConflict,EntryProfileBusy) as error:
                    result={'status':'conflict','reason':str(error),'version':None}
        # The attempt is authoritative across restarts; this is a compact report projection.
        with store.write() as conn:
            attempt=conn.execute('SELECT * FROM entry_profile_attempts WHERE entry_id=? AND run_id=?',(entry_id,run['id'])).fetchone()
            if attempt:
                conn.execute('''INSERT OR IGNORE INTO maintenance_items(run_id,phase,item_key,object_id,outcome,reason,details_json,created_at)
                    VALUES(?,'consolidation',?,?, 'entry_profile',?,?,?)''',
                    (run['id'],'entry_profile:'+entry_id,attempt['id'],result.get('reason'),
                     dumps({'entry_id':entry_id,'status':result['status'],'version':attempt['version'],'attempt_id':attempt['id']}),_instant(clock()).isoformat()))
    return True


# Memory producer shared by the production SQLite adapter and offline tests.
MEMORY_PROFILE_IMPORTANCE_CAP = 69
MEMORY_PROFILE_VIOLATIONS = (*VIOLATIONS, '容易过时的描述')
_MEMORY_REPORTED = re.compile(r'转(?:发|引|述)|引用|复述|原话|上次.{0,8}(?:说|提)|之前.{0,8}(?:说|提)')
_MEMORY_REGISTRATION = re.compile(r'接龙|报名|算我|加我|(?<!\w)\+\s*1(?!\d)')
_MEMORY_GENDERED = re.compile(r'她|妳|祂|(?<!其)(?<!吉)他|\b(?:he|him|his|himself|she|her|hers|herself)\b', re.I)


class MemoryProfileUnavailable(ValueError):
    """Missing lower-layer adapter, unsafe snapshot or insufficient IO budget."""


@dataclass(frozen=True)
class MemoryProfileLimits:
    new_messages: int = 128
    anchors: int = 64
    memories: int = 32
    message_tokens: int = 6000
    memory_tokens: int = 2500
    max_operations: int = 12
    request_tokens: int = 12000
    timeout_seconds: int = 120

    def __post_init__(self):
        if (any(type(v) is not int or v < 1 for v in vars(self).values())
                or self.timeout_seconds > 120 or self.request_tokens > 12000):
            raise ValueError('invalid memory profile limits')


@dataclass(frozen=True)
class MemoryProfileSnapshot:
    """Detached, entry-local snapshot returned AFTER the read transaction ends.

    messages: ordered prefix after cursor, up to scan_through (not max DB ID).
    anchors: bounded older ORIGINAL evidence, including pending topic anchors.
    memories: visible ordinary facts with complete, current source_messages.
    profiles: target memories including forgotten and protected manual bodies.
    revision/settings, source validity, visibility and receipts MUST be checked
    again by commit. New messages appended during a call need not invalidate it.
    """
    entry_id: str
    entry_subject_id: str
    entry_kind: str
    timezone: str
    as_of: str
    settings_revision: int
    cursor: int
    scan_through: int
    has_more: bool
    messages: tuple[dict, ...] = ()
    anchors: tuple[dict, ...] = ()
    memories: tuple[dict, ...] = ()
    profiles: tuple[dict, ...] = ()
    enabled: bool = True
    history_complete: bool = True


@dataclass(frozen=True)
class MemoryProfilePlan:
    snapshot: MemoryProfileSnapshot
    material: dict
    operations: tuple[dict, ...]
    removed: tuple[dict, ...]
    # Only examined evidence may become anchors; never model prose.
    cursor_after: int
    plan_id: str


class MemoryProfileRepository(Protocol):
    """Contract for the future real adapter, not an alternative memory store.

    commit is atomic: revalidate settings/cursor, every read target including
    manual protection, all selected sources/revisions/scope and new-evidence
    receipts; then call ordinary memory writers and advance cursor together.
    On ANY conflict roll back operations AND cursor, raising EntryProfileConflict.
    Deduplicate creates, atomically confirm retention at most once per target
    per plan, receipt every new original evidence identity. A replay must not
    add retention. Remember bounded original anchors even for rejected items.
    Retire means ordinary forgetting, never deletion or manual suspension.
    This contract does not implement hard scope, purge or generic lifecycle.
    """
    def snapshot(self, entry_id: str, limits: MemoryProfileLimits) -> MemoryProfileSnapshot: ...
    def assert_model_call_allowed(self) -> None: ...
    def commit(self, plan: MemoryProfilePlan) -> dict: ...


def _memory_observation_text(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())


def _memory_copies(left, right):
    a, b = _memory_observation_text(left), _memory_observation_text(right)
    if not a or not b or a in b or b in a:
        return True
    return min(len(a), len(b)) >= 12 and SequenceMatcher(None, a, b, autojunk=False).ratio() >= .85


def _memory_message(message, snapshot):
    """Only entry-origin observations. No self output as new group evidence."""
    if (message.get('entry_id') != snapshot.entry_id or message.get('kind') != 'message'
            or message.get('speaker') in (None, '', 'self', 'scene')
            or not isinstance(message.get('ref'), str) or not message['ref']
            or not isinstance(message.get('content'), str)):
        return None
    try:
        instant = _instant(message['occurred_at'])
        if instant > _instant(snapshot.as_of):
            return None
    except (KeyError, ValueError, TypeError):
        return None
    value = {k: copy.deepcopy(message[k]) for k in (
        'ref', 'position', 'entry_id', 'kind', 'speaker', 'content', 'occurred_at', 'quote') if k in message}
    value['date'] = instant.astimezone(ZoneInfo(snapshot.timezone)).date().isoformat()
    value['fingerprint'] = evidence_hash(value)
    return value


def select_memory_profile_material(snapshot, limits=None):
    """Bounded prefix, then original anchors and fact roots; no age cutoff.

    Budgets apply to full JSON, not just bodies. A message is never silently
    shortened. The cursor stops BEFORE an unexamined over-budget row. Ineligible
    kinds are explicitly scanned and may advance the cursor without a model.
    """
    limits = limits or MemoryProfileLimits()
    if (not snapshot.entry_subject_id or snapshot.entry_kind not in ('group', 'live')
            or not snapshot.enabled):
        raise MemoryProfileUnavailable('entry_memory_scope_unavailable')
    ZoneInfo(snapshot.timezone); _instant(snapshot.as_of)
    if (type(snapshot.cursor) is not int or type(snapshot.scan_through) is not int
            or snapshot.cursor < 0 or snapshot.scan_through < snapshot.cursor):
        raise MemoryProfileUnavailable('invalid_cursor')
    material = dict(entry_id=snapshot.entry_id, entry_subject_id=snapshot.entry_subject_id,
        as_of=snapshot.as_of, timezone=snapshot.timezone, messages=[], memories=[],
        profiles=copy.deepcopy(list(snapshot.profiles)), cursor_before=snapshot.cursor,
        cursor_after=snapshot.cursor, has_more=snapshot.has_more, skipped=[],
        diagnostics={'history_complete': snapshot.history_complete, 'omitted_anchors': 0,
                     'omitted_memories': 0, 'truncated_messages': 0})
    target_ids = [p['memory_id'] for p in snapshot.profiles]
    if (len(set(target_ids)) != len(target_ids) or any(
            p.get('owner_entry_id') != snapshot.entry_id or p.get('kind') != '入口画像'
            or p.get('about') != [snapshot.entry_subject_id] for p in snapshot.profiles)):
        raise MemoryProfileUnavailable('invalid_profile_scope')
    used_mem = estimate_tokens(dumps(material['profiles']))
    if used_mem > limits.memory_tokens:
        raise MemoryProfileUnavailable('profile_budget_exceeded')
    used = 0; previous = snapshot.cursor; seen = set()
    for position, message in enumerate(snapshot.messages):
        sequence = message.get('position')
        if (type(sequence) is not int or not previous < sequence <= snapshot.scan_through
                or message.get('ref') in seen):
            raise MemoryProfileUnavailable('non_contiguous_snapshot')
        previous = sequence; seen.add(message.get('ref'))
        value = _memory_message(message, snapshot)
        cost = estimate_tokens(dumps(value)) if value else 0
        if position >= limits.new_messages or used + cost > limits.message_tokens:
            material['has_more'] = True
            if material['cursor_after'] == snapshot.cursor:
                raise MemoryProfileUnavailable('message_budget_exceeded')
            break
        material['cursor_after'] = sequence
        if value:
            material['messages'].append(value); used += cost
        else:
            material['skipped'].append({'position': sequence, 'reason': 'ineligible_source'})
    else:
        # The adapter must account for every scanned row; a high-water mark
        # without its prefix would hide evidence. Gaps in global IDs are fine.
        if previous != snapshot.scan_through:
            raise MemoryProfileUnavailable('unexamined_cursor_tail')
    material['new_refs'] = [m['ref'] for m in material['messages']]
    for index, message in enumerate(snapshot.anchors):
        value = _memory_message(message, snapshot)
        if not value or value['ref'] in seen:
            continue
        cost = estimate_tokens(dumps(value))
        if index >= limits.anchors or used + cost > limits.message_tokens:
            material['diagnostics']['omitted_anchors'] += 1
            continue
        material['messages'].append(value); seen.add(value['ref']); used += cost
    for index, memory in enumerate(snapshot.memories):
        sources = [_memory_message(m, snapshot) for m in memory.get('source_messages', [])]
        if (memory.get('kind') == '入口画像' or memory.get('lifecycle') != 'active'
                or memory.get('visible') is not True or not sources or any(s is None for s in sources)
                or type(memory.get('revision')) is not int or memory['revision'] < 1):
            material['diagnostics']['omitted_memories'] += 1
            continue
        value = {k: copy.deepcopy(memory[k]) for k in ('memory_id', 'revision', 'body', 'kind', 'lifecycle')}
        value['source_messages'] = sources
        value['fingerprint'] = evidence_hash(value)
        cost = estimate_tokens(dumps(value))
        if index >= limits.memories or used_mem + cost > limits.memory_tokens:
            material['diagnostics']['omitted_memories'] += 1
            continue
        material['memories'].append(value); used_mem += cost
    material['diagnostics']['message_tokens'] = used
    material['diagnostics']['memory_tokens'] = used_mem
    return material


def _memory_bind_source(raw, material):
    if (not isinstance(raw, dict) or set(raw) != {'ref', 'field', 'excerpt', 'via_memory'}
            or not isinstance(raw['ref'], str) or raw['field'] not in ('content', 'quote')
            or not isinstance(raw['excerpt'], str) or not 1 <= len(raw['excerpt'].strip()) <= 300
            or (raw['via_memory'] is not None and not isinstance(raw['via_memory'], str))):
        raise ValueError('invalid_source')
    memory = None
    if raw['via_memory'] is not None:
        memory = next((m for m in material['memories'] if m['memory_id'] == raw['via_memory']), None)
        pool = memory['source_messages'] if memory else []
    else:
        pool = material['messages']
    message = next((m for m in pool if m['ref'] == raw['ref']), None)
    if message is None:
        raise ValueError('invalid_source_reference')
    quote = message.get('quote') or {}
    content = message['content'] if raw['field'] == 'content' else quote.get('content', '')
    speaker = message['speaker'] if raw['field'] == 'content' else quote.get('speaker')
    if not speaker or not whitespace(raw['excerpt']) or whitespace(raw['excerpt']) not in whitespace(content):
        raise ValueError('invalid_excerpt')
    identity = (quote.get('ref') if raw['field'] == 'quote' else message['ref'])
    if not identity:
        identity = evidence_hash([speaker, _memory_observation_text(content)])
    return {**copy.deepcopy(raw), 'excerpt': whitespace(raw['excerpt']),
            'evidence_identity': identity, 'speaker': speaker,
            'message': copy.deepcopy(message), 'memory': copy.deepcopy(memory)}


def _memory_original(source, category, saying):
    message = source['message']
    if (source['field'] != 'content' or message.get('quote')
            or _MEMORY_REPORTED.search(message['content'])
            or _MEMORY_REGISTRATION.search(message['content'])):
        return False
    return category != 'saying' or saying in source['excerpt']


def _memory_independent(left, right, category, messages):
    a, b = left['message'], right['message']
    if (left['evidence_identity'] == right['evidence_identity']
            or _memory_copies(a['content'], b['content'])):
        return False
    ta, tb = _instant(a['occurred_at']), _instant(b['occurred_at'])
    gap = abs((ta - tb).total_seconds())
    if category == 'topic':
        return a['date'] != b['date'] and gap >= 30 * 60
    if category == 'saying':
        if gap < 60 or ta.replace(second=0, microsecond=0) == tb.replace(second=0, microsecond=0):
            return False
        if a['speaker'] == b['speaker']:
            # Same-author adjacent turns within 30 minutes are a burst even if
            # a sparse excerpt omitted intervening text. Far-apart uses survive.
            if gap < 30 * 60:
                return False
            ordered = sorted(messages, key=lambda m: (_instant(m['occurred_at']), m['ref']))
            between = [m for m in ordered if min(ta, tb) <= _instant(m['occurred_at']) <= max(ta, tb)]
            if len(between) > 1 and all(m['speaker'] == a['speaker'] for m in between):
                if all((_instant(y['occurred_at']) - _instant(x['occurred_at'])).total_seconds() < 30 * 60
                       for x, y in zip(between, between[1:])):
                    return False
    return True


def _memory_validate_operation(raw, index, material):
    if not isinstance(raw, dict) or raw.get('op') not in ('new', 'confirm', 'revise', 'retire'):
        raise ValueError('invalid_operation')
    op = raw['op']; target = None
    required = {'op', 'sources'}
    if op != 'new':
        required |= {'target_id', 'expected_revision'}
        target = next((p for p in material['profiles'] if p['memory_id'] == raw.get('target_id')), None)
        if (target is None or type(raw.get('expected_revision')) is not int
                or target['revision'] != raw['expected_revision'] or target['lifecycle'] == 'deleted'):
            raise ValueError('invalid_target_revision')
        if target.get('manual_protected') and op in ('revise', 'retire'):
            raise ValueError('protected_manual_conflict')
    if op in ('new', 'revise'):
        required |= {'category', 'body', 'saying', 'meaning', 'importance'}
    if set(raw) != required:
        raise ValueError('invalid_operation_fields')
    if (not isinstance(raw['sources'], list) or not 1 <= len(raw['sources']) <= 8):
        raise ValueError('invalid_sources')
    if op in ('new', 'revise'):
        if (not isinstance(raw['category'], str) or raw['category'] not in ITEM_LABELS or not isinstance(raw['body'], str)
                or not 1 <= len(raw['body'].strip()) <= 180
                or not isinstance(raw['saying'], str) or not isinstance(raw['meaning'], str)
                or type(raw['importance']) is not int or not 0 <= raw['importance'] <= 100):
            raise ValueError('invalid_memory_body')
        category, body, saying, meaning = (raw[k].strip() for k in ('category', 'body', 'saying', 'meaning'))
        if category == 'saying':
            if not saying or len(saying) > 40 or body != saying + ('：' + meaning if meaning else ''):
                raise ValueError('invalid_saying_body')
        elif saying or meaning:
            raise ValueError('unexpected_saying_fields')
        if _MEMORY_GENDERED.search(body):
            raise ValueError('gendered_pronoun')
        if _ACCOUNT.search(body):
            raise ValueError('account_identifier')
    else:
        category, body = target['category'], target['body']
        saying, meaning = target.get('saying', ''), target.get('meaning', '')
    sources = [_memory_bind_source(source, material) for source in raw['sources']]
    originals = [s for s in sources if _memory_original(s, category, saying)]
    contexts = material['messages'] + [m for fact in material['memories'] for m in fact['source_messages']]
    if category in ('topic', 'saying') and op in ('new', 'revise'):
        if not any(_memory_independent(a, b, category, contexts)
                   for i, a in enumerate(originals) for b in originals[i + 1:]):
            raise ValueError('insufficient_independent_evidence')
    if op == 'new':
        # Exact same claim goes through confirm, never duplicate creation.
        target = next((p for p in material['profiles'] if p['category'] == category
                       and whitespace(p['body']) == body), None)
        if target:
            if target['lifecycle'] == 'deleted':
                raise ValueError('deleted_target')
            op = 'confirm'
    new_ids = list(dict.fromkeys(s['evidence_identity'] for s in sources))
    if target:
        old = target.get('sources', [])
        prior_ids = set(target.get('confirmed_evidence', [])) | {s['evidence_identity'] for s in old}
        fresh = [s for s in originals if s['evidence_identity'] not in prior_ids
                 and all(_memory_independent(s, prior, category, contexts) for prior in old)]
        # Explicit rule quotes may support a change but not repeated quoted use.
        if category in ('rule', None):
            fresh = [s for s in sources if s['evidence_identity'] not in prior_ids
                     and all(not _memory_copies(s['excerpt'], prior['excerpt']) for prior in old)]
        new_ids = list(dict.fromkeys(s['evidence_identity'] for s in fresh))
        if not new_ids:
            raise ValueError('no_new_independent_evidence')
    return dict(index=index, op=op, category=category, body=body, saying=saying, meaning=meaning,
                importance=min(raw.get('importance', target.get('importance', 50) if target else 50),
                               MEMORY_PROFILE_IMPORTANCE_CAP),
                sources=sources, new_evidence=new_ids,
                target_id=target['memory_id'] if target else None,
                expected_revision=target['revision'] if target else None,
                target=copy.deepcopy(target))


def validate_memory_profile_operations(generated, material, limits=None):
    limits = limits or MemoryProfileLimits()
    if (not isinstance(generated, dict) or set(generated) != {'operations'}
            or not isinstance(generated['operations'], list)
            or len(generated['operations']) > limits.max_operations):
        raise ValueError('invalid_operations')
    accepted, removed, seen = [], [], set()
    for index, raw in enumerate(generated['operations'], 1):
        try:
            item = _memory_validate_operation(raw, index, material)
            key = ('target', item['target_id']) if item['target_id'] else (item['category'], item['body'])
            if key in seen:
                raise ValueError('duplicate_operation')
            seen.add(key); accepted.append(item)
        except ValueError as error:
            removed.append(dict(index=index, stage='deterministic', reason=str(error)))
    return accepted, removed


def check_memory_profile_operations(output, operations):
    if (not isinstance(output, dict) or set(output) != {'operations'}
            or not isinstance(output['operations'], list) or len(output['operations']) != len(operations)):
        raise ValueError('invalid_check_count')
    kept, removed = [], []
    flags = ('supported', 'stable', 'independent', 'independent_from_previous', 'privacy_safe', 'meaning_supported')
    for item, verdict in zip(operations, output['operations'], strict=True):
        if (not isinstance(verdict, dict) or set(verdict) != {
                'index', *flags, 'meaning_checks', 'evidence_groups', 'violations', 'reason'}
                or type(verdict['index']) is not int or verdict['index'] != item['index']
                or any(type(verdict[k]) is not bool for k in flags)
                or not isinstance(verdict['reason'], str) or not verdict['reason'].strip()
                or not isinstance(verdict['violations'], list)
                or any(v not in MEMORY_PROFILE_VIOLATIONS for v in verdict['violations'])
                or len(set(verdict['violations'])) != len(verdict['violations'])):
            raise ValueError('invalid_check_verdict')
        groups = verdict['evidence_groups']
        if not isinstance(groups, list) or any(not isinstance(g, list) or not g for g in groups):
            raise ValueError('invalid_evidence_groups')
        members = [i for group in groups for i in group]
        if (any(type(i) is not int for i in members) or sorted(members) != list(range(len(item['sources'])))):
            raise ValueError('invalid_evidence_groups')
        terms = verdict['meaning_checks']
        if not isinstance(terms, list) or (item['meaning'] and not terms):
            raise ValueError('missing_meaning_checks')
        for term in terms:
            if (not isinstance(term, dict) or set(term) != {'term', 'source_indices', 'supported'}
                    or not isinstance(term['term'], str) or not term['term'].strip()
                    or type(term['supported']) is not bool
                    or not isinstance(term['source_indices'], list) or not term['source_indices']
                    or any(type(i) is not int or not 0 <= i < len(item['sources']) for i in term['source_indices'])):
                raise ValueError('invalid_meaning_check')
        good = all(verdict[k] for k in flags) and not verdict['violations'] and all(t['supported'] for t in terms)
        if item['category'] in ('topic', 'saying') and item['op'] in ('new', 'revise'):
            good = good and len(groups) >= 2
        if good:
            kept.append(item)
        else:
            removed.append(dict(index=item['index'], stage='model', reason=verdict['reason'], verdict=verdict))
    return kept, removed


class EntryProfileMemoryTask:
    """Two-call, detached-snapshot producer. Must be explicitly given an adapter."""
    def __init__(self, repository: MemoryProfileRepository | None, gateway, *, limits=None):
        self.repository, self.gateway = repository, gateway
        self.limits = limits or MemoryProfileLimits()
        self.prompts = {stage: dict(name=prompt['prompt_version'], text=prompt['text'],
                                   sha256=prompt['prompt_sha256'])
                        for stage, prompt in load_profile_prompts(
                            'entry_profile_memory', 'v1', ('v1',)).items()}

    def _call(self, stage, payload, result):
        self.repository.assert_model_call_allowed()
        prompt = self.prompts[stage]; purpose = 'consolidation_entry_profile_' + stage
        if not callable(getattr(self.gateway, 'chat', None)):
            raise MemoryProfileUnavailable('model_gateway_unavailable')
        encoded = dumps(payload)
        if estimate_tokens(prompt['text']) + estimate_tokens(encoded) > self.limits.request_tokens:
            raise ValueError('request_budget_exceeded')
        health = getattr(self.gateway, 'health', None)
        if health:
            health.check('chat', 'learning')
        monotonic = getattr(self.gateway, 'monotonic', time.monotonic)
        started = monotonic()
        call = dict(purpose=purpose, prompt_version=prompt['name'], prompt_sha256=prompt['sha256'])
        try:
            reply = self.gateway.chat([{'role': 'system', 'content': prompt['text']},
                                       {'role': 'user', 'content': encoded}], purpose,
                                      max_tokens=16000, _deadline=started + self.limits.timeout_seconds)
        except ModelError as error:
            result['calls'].append({**call, 'result': error.category, 'reason': error.reason or error.category,
                'duration_ms': round((monotonic() - started) * 1000), 'usage': {}})
            self._record_progress(result)
            raise
        result['outputs'][stage] = reply.content
        result['calls'].append(dict(**call, result='success', finish_reason=reply.finish_reason,
            duration_ms=round((monotonic() - started) * 1000), usage=reply.usage))
        self._record_progress(result)
        if reply.finish_reason != 'stop':
            raise ValueError('incomplete_output')
        return json_object(reply.content)

    def _record_progress(self, result):
        record = getattr(self.repository, 'record_model_progress', None)
        if record is not None:
            record(result)

    def run(self, entry_id):
        if self.repository is None:
            raise MemoryProfileUnavailable('memory_repository_unavailable')
        snapshot = copy.deepcopy(self.repository.snapshot(entry_id, self.limits))
        if snapshot.entry_id != entry_id:
            raise MemoryProfileUnavailable('snapshot_entry_mismatch')
        material = select_memory_profile_material(snapshot, self.limits)
        result = dict(status='no_new_evidence', material=material, outputs={}, calls=[], removed=[],
            stages={'generated': [], 'deterministic': [], 'checked': []}, receipt=None,
            quote_validation={'total': 0, 'matched': 0},
            prompt_versions=[p['name'] for p in self.prompts.values()],
            prompt_sha256={p['name']: p['sha256'] for p in self.prompts.values()})
        if material['cursor_after'] == snapshot.cursor:
            return result
        try:
            operations = []
            if material['new_refs']:
                generated = self._call('generate', material, result)
                rows = generated.get('operations', [])
                result['stages']['generated'] = copy.deepcopy(rows) if isinstance(rows, list) else []
                for raw in result['stages']['generated']:
                    if not isinstance(raw, dict) or not isinstance(raw.get('sources'), list):
                        continue
                    for source in raw['sources']:
                        result['quote_validation']['total'] += 1
                        try:
                            _memory_bind_source(source, material)
                        except ValueError:
                            continue
                        result['quote_validation']['matched'] += 1
                operations, result['removed'] = validate_memory_profile_operations(generated, material, self.limits)
                result['stages']['deterministic'] = copy.deepcopy(operations)
                if operations:
                    # Only item-bound sources, target and original evidence history.
                    # Unbound neighboring facts cannot supply a meaning or assertion.
                    checked = self._call('check', dict(entry_id=entry_id, as_of=snapshot.as_of,
                        operations=operations, protected_rules=[
                            {k: p[k] for k in ('memory_id', 'revision', 'body')}
                            for p in material['profiles'] if p.get('manual_protected')]), result)
                    operations, removed = check_memory_profile_operations(checked, operations)
                    result['removed'].extend(removed)
                result['stages']['checked'] = copy.deepcopy(operations)
            plan = MemoryProfilePlan(snapshot, material, tuple(operations), tuple(result['removed']),
                                     material['cursor_after'], evidence_hash([material, operations]))
            result['receipt'] = self.repository.commit(plan)
            result['status'] = 'committed'
        except EntryProfileConflict:
            result.update(status='conflict', reason='snapshot_changed')
        except ModelError as error:
            # Do not log provider text: it can echo configuration or source data.
            result.update(status='failed', reason=error.reason or error.category)
        except ValueError as error:
            result.update(status='failed', reason=str(error))
        return result


class EntryProfileRetired(ValueError):
    """A legacy whole-document write is retired on the memory path."""


def memory_profile_enabled(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='entry_profile_memory_state'").fetchone())


def _memory_state(conn, entry_id):
    _entry(conn, entry_id)
    row = conn.execute('SELECT * FROM entry_profile_memory_state WHERE entry_id=?', (entry_id,)).fetchone()
    if row is None:
        raise EntryProfileConflict('memory_profile_state_missing')
    return dict(row)


def initialize_memory_profiles(store, conn):
    """Lossless deterministic hand-written conversion, idempotent at startup.

    Only the last published profile contributes manual items; previous versions
    remain read-only history. Never revive removed historical rules. Model items
    are diagnosed and rebuilt asynchronously from original evidence.
    """
    from .memory_ops import write_entry_profile_memory
    stamp = utc_now().isoformat()
    runtime = _setting(conn, 'entry_profile', {})
    if runtime.get('publish_mode') == 'all_manual':
        conn.execute("UPDATE runtime_settings SET value_json=? WHERE key='entry_profile'",
                     (dumps({**runtime, 'publish_mode':'check_auto'}),))
        operation(conn, 'entry_profile_publish_mode_migrated', 'settings', 'entry_profile',
                  {'before':'all_manual','after':'check_auto'}, actor='migration', stamp=stamp)
    for row in conn.execute('SELECT * FROM entry_profile_memory_state WHERE legacy_pending=1').fetchall():
        entry_id = row['entry_id']
        setting_row = conn.execute('SELECT * FROM entry_profile_settings WHERE entry_id=?', (entry_id,)).fetchone()
        if setting_row and setting_row['publish_mode'] == 'all_manual':
            conn.execute("UPDATE entry_profile_settings SET publish_mode='check_auto',revision=revision+1,updated_at=? WHERE entry_id=?", (stamp,entry_id))
            operation(conn, 'entry_profile_publish_mode_migrated', 'entry', entry_id,
                      {'before':'all_manual','after':'check_auto'}, actor='migration', stamp=stamp)
        legacy = conn.execute('SELECT * FROM entry_profile_versions WHERE entry_id=? AND version=?',
                             (entry_id, setting_row['current_version'] if setting_row else 0)).fetchone()
        if legacy:
            sentences = json.loads(legacy['sentences_json'])
            if not sentences and legacy['author']=='admin' and legacy['content']:
                sentences = [{'text':legacy['content'],'author':'admin'}]
            # Preserve a baseline signature so removing an old model-only
            # projection also advances the entry's existing version sequence.
            conn.execute('UPDATE entry_profile_memory_state SET projection_json=? WHERE entry_id=?',
                         (dumps([{'legacy':legacy['version']}]) if legacy['content'] else '[]',entry_id))
            for index, sentence in enumerate(sentences, 1):
                body = sentence.get('text','')
                kind = sentence.get('category',sentence.get('kind'))
                category = kind if kind in ITEM_LABELS else None
                mid = None
                if sentence.get('author') == 'admin' and body:
                    result = write_entry_profile_memory(store,entry_id,op='new',content=body,
                        category=category,importance=80,manual=True,_conn=conn)
                    mid = result['memory_id']
                    conn.execute('UPDATE memories SET pinned=1 WHERE id=?',(mid,))
                    disposition = 'manual_imported' if category else 'manual_unclassified'
                else:
                    disposition = 'model_rebuild_pending' if body else 'cleared_history'
                conn.execute('''INSERT OR IGNORE INTO entry_profile_legacy_migration
                    VALUES(?,?,?,?,?,?,?)''',(entry_id,legacy['version'],index,disposition,mid,category,stamp))
            material=json.loads(legacy['material_json'])
            material['migration_memory_refs']=[dict(memory_id=r['memory_id'],revision=1) for r in conn.execute(
                'SELECT memory_id FROM entry_profile_legacy_migration WHERE entry_id=? AND legacy_version=? AND memory_id IS NOT NULL',
                (entry_id,legacy['version']))]
            conn.execute('UPDATE entry_profile_versions SET material_json=? WHERE id=?', (dumps(material),legacy['id']))
        conn.execute('UPDATE entry_profile_memory_state SET legacy_pending=0 WHERE entry_id=?',(entry_id,))
        conn.execute('INSERT OR IGNORE INTO entry_profile_projection_dirty VALUES(?)',(entry_id,))


def _db_observation(row):
    value = dict(ref='msg:'+str(row['id']), position=row['id'], entry_id=row['entry_id'],
                 kind=row['kind'], speaker=row['sender_subject_id'], content=row['content'], occurred_at=row['occurred_at'])
    if row['quote_content']:
        value['quote'] = dict(speaker=row['quote_author_subject_id'],content=row['quote_content'])
    return value


def _source_fact(conn, mid, entry_id, zone, as_of):
    row = conn.execute("SELECT * FROM memories WHERE id=? AND kind!='入口画像' AND lifecycle='active' AND purged_at IS NULL",(mid,)).fetchone()
    if not row or not Visibility(conn).memory_visible(mid,entry_id):
        return None
    roots = conn.execute('''WITH RECURSIVE parents(id) AS (
        SELECT ? UNION SELECT s.source_memory_id FROM sources s JOIN parents p ON p.id=s.memory_id WHERE s.kind='memory')
        SELECT DISTINCT x.* FROM parents p JOIN sources s ON s.memory_id=p.id JOIN messages x ON x.id=s.message_id
        WHERE s.kind='message' ORDER BY x.id LIMIT 33''',(mid,)).fetchall()
    if not roots or len(roots)>32 or any(r['entry_id']!=entry_id for r in roots):
        return None
    return dict(memory_id=str(mid),revision=row['revision'],body=row['content'],kind=row['kind'],
        lifecycle=row['lifecycle'],visible=True,source_messages=[_db_observation(r) for r in roots])


def _profile_bound_sources(conn, row, zone, as_of):
    """Current revision's excerpts only; old audit evidence cannot prove a revision."""
    from .memory_ops import _profile_sources
    snapshot = MemoryProfileSnapshot(row['owner_entry_id'], '', 'group', zone, as_of, 0, 0, 0, False)
    result = []
    for source in conn.execute('''SELECT s.* FROM entry_profile_revision_sources r JOIN sources s ON s.id=r.source_id
            WHERE r.memory_id=? AND r.revision=? AND s.kind='message' ORDER BY s.id''',(row['id'],row['revision'])):
        try:
            note = json.loads(source['note'])
            binding = {key:note.get(key) for key in ('field','excerpt','message_sha256','source_memory_id','source_revision')}
            binding['message_id'] = source['message_id']
            _profile_sources(conn,row['owner_entry_id'],[binding],row['id'])
            message = conn.execute('SELECT * FROM messages WHERE id=?',(source['message_id'],)).fetchone()
            fact = _source_fact(conn,binding['source_memory_id'],row['owner_entry_id'],zone,as_of) if binding['source_memory_id'] else None
            normalized = _memory_message(_db_observation(message),snapshot)
            if normalized is None:
                raise ValueError('ineligible_source')
            if fact:
                fact['source_messages'] = [_memory_message(m,snapshot) for m in fact['source_messages']]
                if any(m is None for m in fact['source_messages']):
                    raise ValueError('ineligible_fact_source')
            material = dict(messages=[normalized],memories=[fact] if fact else [])
            result.append(_memory_bind_source(dict(ref='msg:'+str(message['id']),field=binding['field'],excerpt=binding['excerpt'],
                via_memory=str(binding['source_memory_id']) if binding['source_memory_id'] else None),material))
        except (ValueError,TypeError,KeyError):
            continue
    return result


def memory_profile_rows(conn, entry_id, *, as_of=None, usable_only=False):
    from .memory_ops import _profile_manual
    zone = _setting(conn,'timezone','Asia/Shanghai'); as_of = as_of or utc_now().isoformat()
    owner = conn.execute('SELECT subject_id FROM entry_subjects WHERE entry_id=?',(entry_id,)).fetchone()
    visibility = Visibility(conn); profiles=[]
    rows = conn.execute('''SELECT m.*,p.category,p.manual_created FROM entry_profile_memories p
        JOIN memories m ON m.id=p.memory_id WHERE p.entry_id=? AND m.purged_at IS NULL
        ORDER BY p.manual_created DESC,CASE p.category WHEN 'rule' THEN 0 WHEN 'topic' THEN 1 WHEN 'saying' THEN 2 ELSE 3 END,m.id''',(entry_id,)).fetchall()
    for row in rows:
        manual = _profile_manual(conn,row)
        sources = _profile_bound_sources(conn,row,zone,as_of)
        body = row['content']; saying,_,meaning = body.partition('：') if row['category']=='saying' else ('','','')
        supported = manual or bool(sources)
        if not manual and row['category'] in ('topic','saying'):
            originals = [s for s in sources if _memory_original(s,row['category'],saying)]
            supported = any(_memory_independent(a,b,row['category'],[s['message'] for s in sources])
                            for i,a in enumerate(originals) for b in originals[i+1:])
        visible = visibility.memory_visible(row['id'],entry_id)
        usable = row['lifecycle']=='active' and supported and visible and not row['merged_into']
        if usable_only and not usable:
            continue
        profiles.append(dict(memory_id=row['id'],revision=row['revision'],kind=row['kind'],category=row['category'],
            body=body,saying=saying,meaning=meaning,author='admin' if manual else 'model',speaker='self',stance=row['stance'],
            about=[owner[0]],owner_entry_id=entry_id,lifecycle=row['lifecycle'],event_time=None,importance=row['importance'],
            manual_protected=manual,manual_created=bool(row['manual_created']),sources=sources,
            confirmed_evidence=[r[0] for r in conn.execute('SELECT identity FROM entry_profile_evidence_receipts WHERE memory_id=? ORDER BY identity',(row['id'],))],
            usable=usable,retention=row['retention'],pinned=bool(row['pinned'])))
    return profiles


def _projection_items(conn, entry_id):
    if _entry(conn,entry_id)['kind'] not in ('group','live') or not _settings(conn,entry_id)['enabled'] or not _runtime(conn)['enabled']:
        return [], []
    items=[]; omitted=[]; tokens=0
    for row in memory_profile_rows(conn,entry_id,usable_only=True):
        label = ITEM_LABELS.get(row['category'],'管理员说明')
        item = dict(memory_id=row['memory_id'],text=label+'：'+row['body'])
        cost = estimate_tokens(item['text'])
        # Protected hand-written rules are never removed by the display budget.
        if row['manual_protected'] or len(items)<MAX_ITEMS and tokens+cost<=MAX_PROFILE_TOKENS:
            items.append(item); tokens+=cost
        else:
            omitted.append(row['memory_id'])
    return items, omitted


def flush_memory_profile_projections(conn):
    """Exactly one version increment per entry/transaction when rendered set changes."""
    for dirty in conn.execute('SELECT entry_id FROM entry_profile_projection_dirty ORDER BY entry_id').fetchall():
        entry_id=dirty['entry_id']; state=_memory_state(conn,entry_id)
        items,omitted=_projection_items(conn,entry_id)
        encoded=dumps(items)
        if encoded != state['projection_json']:
            conn.execute('''UPDATE entry_profile_memory_state SET projection_json=?,version=version+1,generated_at=?
                WHERE entry_id=?''',(encoded,utc_now().isoformat(),entry_id))
        conn.execute('UPDATE entry_profile_memory_state SET diagnostics_json=? WHERE entry_id=?',
                     (dumps({'omitted_memory_ids':omitted}),entry_id))
        conn.execute('DELETE FROM entry_profile_projection_dirty WHERE entry_id=?',(entry_id,))


def memory_profile_context(conn, entry_id, *, known_memory_ids=()):
    state=_memory_state(conn,entry_id)
    if conn.execute('SELECT 1 FROM entry_profile_projection_dirty WHERE entry_id=?',(entry_id,)).fetchone():
        return None  # No stale cache if a non-Store writer left an unflushed dirty receipt.
    items=[x for x in json.loads(state['projection_json']) if x['memory_id'] not in known_memory_ids]
    if not items:
        return None
    pending=conn.execute('SELECT 1 FROM messages WHERE entry_id=? AND id>? LIMIT 1',(entry_id,state['cursor'])).fetchone()
    return dict(text='\n'.join(x['text'] for x in items),memory_ids=[x['memory_id'] for x in items],
        version=state['version'],generated_at=state['generated_at'],possibly_stale=bool(pending or state['rebuild_pending']))


class SQLiteMemoryProfileRepository:
    """Detached SQLite snapshots and ordinary memory writes with atomic cursor CAS."""
    mode='sqlite'

    def __init__(self, store, *, clock=utc_now, attempt_id=None):
        self.store,self.clock,self.attempt_id=store,clock,attempt_id
        self.prepared_snapshot = None

    def assert_model_call_allowed(self):
        if self.store._writer.in_transaction:
            # Only the calling thread's transaction would be unsafe; unrelated
            # concurrent writers are allowed. RLock ownership distinguishes it.
            if self.store._lock._is_owned():
                raise RuntimeError('model call in write transaction')

    def record_model_progress(self, result):
        if self.attempt_id is None:
            return
        # The admitted material already carries purge roots. Existing redaction
        # triggers suppress late output after a concurrent clear.
        with self.store.write() as conn:
            conn.execute("""UPDATE entry_profile_attempts SET outputs_json=?,calls_json=?
                WHERE id=? AND state='running'""",(dumps({'outputs':result['outputs']}),dumps(result['calls']),self.attempt_id))

    def snapshot(self, entry_id, limits):
        if self.prepared_snapshot is not None:
            return copy.deepcopy(self.prepared_snapshot)
        as_of=_instant(self.clock()).isoformat()
        with self.store.read() as conn:
            state=_memory_state(conn,entry_id); settings=_settings(conn,entry_id); entry=_entry(conn,entry_id)
            rows=conn.execute('SELECT * FROM messages WHERE entry_id=? AND id>? ORDER BY id LIMIT ?',
                              (entry_id,state['cursor'],limits.new_messages+1)).fetchall()
            page=[]
            for row in rows[:limits.new_messages]:
                if _instant(row['occurred_at'])>_instant(as_of) or _instant(row['received_at'])>_instant(as_of):
                    break
                page.append(row)
            through=page[-1]['id'] if page else state['cursor']
            # Recent and retained original anchors are bounded; no evidence age cutoff.
            prior=conn.execute('SELECT * FROM messages WHERE entry_id=? AND id<=? ORDER BY id DESC LIMIT ?',
                               (entry_id,state['cursor'],limits.anchors+1)).fetchall()
            anchors=list(reversed(prior[:limits.anchors]))
            retained=json.loads(state['anchors_json'])
            if retained:
                anchor_map={r['id']:r for r in anchors}
                for r in conn.execute('SELECT * FROM messages WHERE entry_id=? AND id IN (SELECT value FROM json_each(?)) ORDER BY id', (entry_id,dumps(retained))):
                    anchor_map[r['id']]=r
                anchors=sorted(anchor_map.values(),key=lambda r:r['id'])[:limits.anchors]
            facts=[]
            for row in conn.execute("""SELECT DISTINCT m.id FROM memories m JOIN sources s ON s.memory_id=m.id
                JOIN messages x ON x.id=s.message_id WHERE m.kind!='入口画像' AND m.lifecycle='active'
                AND x.entry_id=? ORDER BY m.updated_at DESC,m.id DESC LIMIT ?""",(entry_id,limits.memories)):
                fact=_source_fact(conn,row['id'],entry_id,_setting(conn,'timezone','Asia/Shanghai'),as_of)
                if fact:
                    facts.append(fact)
            return MemoryProfileSnapshot(entry_id,conn.execute('SELECT subject_id FROM entry_subjects WHERE entry_id=?',(entry_id,)).fetchone()[0],
                entry['kind'],_setting(conn,'timezone','Asia/Shanghai'),as_of,settings['revision'],state['cursor'],through,len(rows)>len(page),
                tuple(_db_observation(r) for r in page),tuple(_db_observation(r) for r in anchors),tuple(facts),
                tuple(memory_profile_rows(conn,entry_id,as_of=as_of)),settings['enabled'] and _runtime(conn)['enabled'],len(prior)<=limits.anchors)

    def commit(self, plan):
        from .memory_ops import entry_profile_source, write_entry_profile_memory
        with self.store.write() as conn:
            replay=conn.execute('SELECT receipt_json FROM entry_profile_memory_plans WHERE entry_id=? AND plan_id=?',
                                (plan.snapshot.entry_id,plan.plan_id)).fetchone()
            if replay:
                return {**json.loads(replay[0]),'replayed':True}
            snapshot=plan.snapshot; entry_id=snapshot.entry_id
            state=_memory_state(conn,entry_id); settings=_settings(conn,entry_id)
            if state['cursor']!=snapshot.cursor or settings['revision']!=snapshot.settings_revision or not settings['enabled'] or not _runtime(conn)['enabled']:
                raise EntryProfileConflict('cursor_or_settings_changed')
            if _setting(conn,'timezone','Asia/Shanghai')!=snapshot.timezone:
                raise EntryProfileConflict('timezone_changed')
            if _entry(conn,entry_id)['kind']!=snapshot.entry_kind:
                raise EntryProfileConflict('entry_kind_changed')
            if memory_profile_rows(conn,entry_id,as_of=snapshot.as_of)!=list(snapshot.profiles):
                raise EntryProfileConflict('targets_changed')
            for message in plan.material['messages']:
                row=conn.execute('SELECT * FROM messages WHERE id=?',(message['position'],)).fetchone()
                if not row or _memory_message(_db_observation(row),snapshot)!=message:
                    raise EntryProfileConflict('message_changed')
            for fact in plan.material['memories']:
                live=_source_fact(conn,int(fact['memory_id']),entry_id,snapshot.timezone,snapshot.as_of)
                previous=next(m for m in snapshot.memories if m['memory_id']==fact['memory_id'])
                if live!=previous:
                    raise EntryProfileConflict('fact_changed')
            if self.attempt_id is not None:
                running=conn.execute('SELECT state FROM entry_profile_attempts WHERE id=?',(self.attempt_id,)).fetchone()
                if not running or running['state']!='running':
                    raise EntryProfileConflict('attempt_changed')
            applied=[]
            for op in plan.operations:
                bindings=[]
                for source in op['sources']:
                    bindings.append(entry_profile_source(conn,source['message']['position'],excerpt=source['excerpt'],field=source['field'],
                        source_memory_id=int(source['via_memory']) if source['via_memory'] else None,
                        source_revision=source['memory']['revision'] if source['memory'] else None))
                try:
                    result=write_entry_profile_memory(self.store,entry_id,op=op['op'],content=op['body'],category=op['category'],
                        importance=op['importance'],sources=bindings,memory_id=op['target_id'],expected_revision=op['expected_revision'],
                        current=_instant(snapshot.as_of),_conn=conn)
                except ValueError as error:
                    raise EntryProfileConflict('memory_write_guard') from error
                applied.append({'index':op['index'],**result})
            # Keep only original message IDs, never model prose as future evidence.
            anchors=[m['position'] for m in plan.material['messages'] if m['position']<=plan.cursor_after]
            anchors=list(dict.fromkeys([*json.loads(state['anchors_json']),*anchors]))[-MemoryProfileLimits().anchors:]
            conn.execute('UPDATE entry_profile_memory_state SET cursor=?,anchors_json=?,rebuild_pending=? WHERE entry_id=?',
                         (plan.cursor_after,dumps(anchors),int(plan.material['has_more']),entry_id))
            receipt=dict(applied=applied,cursor_before=snapshot.cursor,cursor_after=plan.cursor_after,replayed=False)
            conn.execute('INSERT INTO entry_profile_memory_plans VALUES(?,?,?,?)',(entry_id,plan.plan_id,dumps(receipt),snapshot.as_of))
            if self.attempt_id is not None:
                flush_memory_profile_projections(conn)
                conn.execute("UPDATE entry_profile_attempts SET state='published',version=?,finished_at=? WHERE id=?",
                    (_memory_state(conn,entry_id)['version'],snapshot.as_of,self.attempt_id))
            return receipt


def require_legacy_write(store, entry_id):
    with store.read() as conn:
        _entry(conn,entry_id)
        if memory_profile_enabled(conn):
            raise EntryProfileRetired('entry_profile_legacy_write_retired')


def _memory_expect(conn, entry_id, expected):
    if type(expected) is not int or expected < 0:
        raise ValueError('expected_version must be a nonnegative integer')
    if _memory_state(conn,entry_id)['version'] != expected:
        raise EntryProfileConflict('projection_version_changed')


def _memory_due(conn, entry_id, current):
    state=_memory_state(conn,entry_id); settings=_settings(conn,entry_id)
    new=conn.execute('SELECT COUNT(*) FROM messages WHERE entry_id=? AND id>?',(entry_id,state['cursor'])).fetchone()[0]
    reason=('entry_kind' if _entry(conn,entry_id)['kind'] not in ('group','live') else
            'disabled' if not settings['enabled'] or not _runtime(conn)['enabled'] else 'no_new_evidence' if not new else None)
    return dict(due=reason is None,reason=reason,new_messages=new,window_message_count=None,current_version=state['version'],
                local_day=current.astimezone(ZoneInfo(_setting(conn,'timezone','Asia/Shanghai'))).date().isoformat(),
                possibly_stale=bool(new or state['rebuild_pending']))


def _attempt_material(material):
    # The existing purge index requires numeric message_id / memory_id roots.
    refs=[dict(message_id=m['position']) for m in material['messages']]
    for fact in material['memories']:
        refs.append(dict(memory_id=int(fact['memory_id']),revision=fact['revision']))
        refs.extend(dict(message_id=m['position']) for m in fact['source_messages'])
    refs.extend(dict(memory_id=p['memory_id'],revision=p['revision']) for p in material['profiles'])
    return dict(format_version=2,memory_material=material,evidence_refs=refs)


def _reserve_memory_task(engine, entry_id, expected, source, *, run_id, fail_if_running):
    store=engine.store; current=_instant(engine.clock()); stamp=current.isoformat()
    with store.write() as conn:
        _memory_expect(conn,entry_id,expected)
        _recover_interrupted(conn,store,entry_id,stamp)
        if conn.execute("SELECT 1 FROM entry_profile_attempts WHERE entry_id=? AND state='running'",(entry_id,)).fetchone():
            raise EntryProfileBusy('generation_in_progress')
        if run_id is not None:
            saved=conn.execute('SELECT * FROM entry_profile_attempts WHERE entry_id=? AND run_id=?',(entry_id,run_id)).fetchone()
            if saved:
                return dict(status=saved['state'],reason=saved['reason'],attempt_id=saved['id'],version=None)
        _ensure(conn,entry_id,stamp)
        due=_memory_due(conn,entry_id,current)
    if not due['due']:
        return engine._skip(entry_id,expected,source,due,due['reason'],run_id,stamp)
    try:
        gateway=engine._gateway()
    except ModelError as error:
        return engine._skip(entry_id,expected,source,due,error.reason or error.category,run_id,stamp)
    repository=SQLiteMemoryProfileRepository(store,clock=engine.clock)
    snapshot=repository.snapshot(entry_id,MemoryProfileLimits())
    try:
        material=select_memory_profile_material(snapshot)
    except MemoryProfileUnavailable as error:
        return engine._skip(entry_id,expected,source,due,str(error),run_id,stamp)
    repository.prepared_snapshot=snapshot
    with store.write() as conn:
        _memory_expect(conn,entry_id,expected)
        if _settings(conn,entry_id)['revision'] != snapshot.settings_revision or _memory_state(conn,entry_id)['cursor'] != snapshot.cursor:
            raise EntryProfileConflict('snapshot_changed_before_admission')
        if conn.execute("SELECT 1 FROM entry_profile_attempts WHERE entry_id=? AND state='running'",(entry_id,)).fetchone():
            raise EntryProfileBusy('generation_in_progress')
        attempt=conn.execute('''INSERT INTO entry_profile_attempts(entry_id,run_id,base_version,state,local_day,started_at,material_json)
            VALUES(?,?,?,'running',?,?,?)''',(entry_id,run_id,expected,due['local_day'],stamp,dumps(_attempt_material(material)))).lastrowid
        repository.attempt_id=attempt
        _audit(conn,'generation_requested',entry_id,stamp,attempt_id=attempt,base_version=expected,source=source)
        with _ACTIVE_LOCK:
            _ACTIVE_ATTEMPTS.add((store.path,attempt))
    return dict(status='running',attempt_id=attempt,context=dict(entry_id=entry_id,attempt=attempt,
                gateway=gateway,memory_repository=repository,due=due))


def _execute_memory_task(engine, entry_id, attempt, gateway, repository, due):
    try:
        result=EntryProfileMemoryTask(repository,gateway).run(entry_id)
        status={'committed':'published','no_new_evidence':'skipped'}.get(result['status'],result['status'])
        with engine.store.write() as conn:
            # Add result memory roots before storing prose; purge guards also
            # suppress any late output whose input was cleared in flight.
            material=_attempt_material(result['material'])
            material['evidence_refs'].extend(dict(memory_id=x['memory_id'],revision=x['revision'])
                for x in (result.get('receipt') or {}).get('applied',[]))
            conn.execute('UPDATE entry_profile_attempts SET material_json=? WHERE id=?',(dumps(material),attempt))
            version=_memory_state(conn,entry_id)['version']
            conn.execute('''UPDATE entry_profile_attempts SET state=?,reason=?,version=?,outputs_json=?,calls_json=?,finished_at=?
                WHERE id=? AND state IN ('running','published') ''',(status,result.get('reason'),version,dumps(result),dumps(result['calls']),_instant(engine.clock()).isoformat(),attempt))
        return dict(status=status,reason=result.get('reason'),version={'version':version},attempt_id=attempt,due=due,result=result)
    except Exception:
        engine._finish(attempt,'failed','unexpected_error',{},[])
        raise
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_ATTEMPTS.discard((engine.store.path,attempt))
