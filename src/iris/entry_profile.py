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
        self.incremental_mode = self.memory_mode and getattr(store,'_incremental_profiles',False)
        if self.memory_mode and prompt_version is not None:
            raise EntryProfileRetired('entry_profile_legacy_write_retired')
        self.prompt_version=MEMORY_PROMPT_VERSION if self.memory_mode else DEFAULT_PROMPT_VERSION if prompt_version is None else prompt_version
        self.prompts=load_profile_prompts('entry_profile_memory' if self.memory_mode else 'entry_profile',
            self.prompt_version,('v1','v2') if self.memory_mode else PROMPT_VERSIONS)
        self.prompt_metadata=profile_prompt_metadata(self.prompts)
        if self.incremental_mode:
            loaded=incremental_prompts()
            self.prompt_metadata=dict(prompt_versions=[p['name'] for p in loaded.values()],prompt_sha256={p['name']:p['sha256'] for p in loaded.values()})

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
        if self.incremental_mode:
            return _reserve_incremental_task(self,entry_id,expected,source,run_id=run_id,fail_if_running=fail_if_running)
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
                 gateway=None, evidence=None, material=None, attempt=None, memory_repository=None, incremental_task=None):
        if incremental_task is not None:
            return _execute_incremental_task(self,entry_id,attempt,incremental_task,due)
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
    if getattr(store,'_incremental_profiles',False):
        return _update_incremental_for_run(store,gateway,run,config,clock=clock,stop=stop)
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
MEMORY_PROMPT_VERSION = 'v2'
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
    timeout_seconds: int = 180

    def __post_init__(self):
        if (any(type(v) is not int or v < 1 for v in vars(self).values())
                or self.timeout_seconds > 180 or self.request_tokens > 12000):
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


def validate_memory_profile_shape(generated, limits):
    """Protocol only: malformed fields get one repair; evidence never does.

    Keep source binding, recurrence, privacy and manual protection in the
    deterministic/semantic validators. Do not fill defaults into model output.
    """
    if (not isinstance(generated, dict) or set(generated) != {'operations'}
            or not isinstance(generated['operations'], list)
            or len(generated['operations']) > limits.max_operations):
        raise ValueError('invalid_operations')
    for raw in generated['operations']:
        if not isinstance(raw, dict) or raw.get('op') not in ('new', 'confirm', 'revise', 'retire'):
            raise ValueError('invalid_operation')
        required = {'op', 'sources'}
        if raw['op'] != 'new':
            required |= {'target_id', 'expected_revision'}
        if raw['op'] in ('new', 'revise'):
            required |= {'category', 'body', 'saying', 'meaning', 'importance'}
        if set(raw) != required:
            raise ValueError('invalid_operation_fields')
        if raw['op'] != 'new' and (type(raw['target_id']) not in (str, int)
                or not str(raw['target_id']).strip() or type(raw['expected_revision']) is not int
                or raw['expected_revision'] < 1):
            raise ValueError('invalid_target_fields')
        if (not isinstance(raw['sources'], list) or not 1 <= len(raw['sources']) <= 8):
            raise ValueError('invalid_sources')
        for source in raw['sources']:
            if (not isinstance(source, dict) or set(source) != {'ref', 'field', 'excerpt', 'via_memory'}
                    or not isinstance(source['ref'], str) or not source['ref'].strip()
                    or source['field'] not in ('content', 'quote')
                    or not isinstance(source['excerpt'], str) or not 1 <= len(source['excerpt'].strip()) <= 300
                    or (source['via_memory'] is not None and not isinstance(source['via_memory'], str))):
                raise ValueError('invalid_source_fields')
        if raw['op'] in ('new', 'revise'):
            if (not isinstance(raw['category'], str) or raw['category'] not in ITEM_LABELS
                    or not isinstance(raw['body'], str) or not 1 <= len(raw['body'].strip()) <= 180
                    or not isinstance(raw['saying'], str) or not isinstance(raw['meaning'], str)
                    or type(raw['importance']) is not int or not 0 <= raw['importance'] <= 100):
                raise ValueError('invalid_memory_body')
            body, saying, meaning = (raw[k].strip() for k in ('body', 'saying', 'meaning'))
            if raw['category'] == 'saying':
                if not saying or len(saying) > 40 or body != saying + ('：' + meaning if meaning else ''):
                    raise ValueError('invalid_saying_body')
            elif saying or meaning:
                raise ValueError('unexpected_saying_fields')


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


def _memory_check_repair_guard(previous, corrected):
    """A shape correction cannot turn a parsed negative verdict into approval."""
    if not isinstance(previous, dict) or not isinstance(previous.get('operations'), list):
        return
    prior = {v['index']: v for v in previous['operations']
             if isinstance(v, dict) and type(v.get('index')) is int}
    for verdict in corrected['operations']:
        old = prior.get(verdict['index'], {})
        for key in ('supported', 'stable', 'independent', 'independent_from_previous', 'privacy_safe', 'meaning_supported'):
            if old.get(key) is False and verdict[key] is not False:
                raise ValueError('repair_changed_verdict')
        violations = old.get('violations', [])
        if isinstance(violations, list) and any(v in MEMORY_PROFILE_VIOLATIONS and v not in verdict['violations']
                                              for v in violations):
            raise ValueError('repair_changed_verdict')


class EntryProfileMemoryTask:
    """Two phases with at most one format repair each; detached snapshots."""
    def __init__(self, repository: MemoryProfileRepository | None, gateway, *, limits=None):
        self.repository, self.gateway = repository, gateway
        self.limits = limits or MemoryProfileLimits()
        self.prompts = {stage: dict(name=prompt['prompt_version'], text=prompt['text'],
                                   sha256=prompt['prompt_sha256'])
                        for stage, prompt in load_profile_prompts(
                            'entry_profile_memory', MEMORY_PROMPT_VERSION, ('v1','v2')).items()}

    def _call(self, stage, payload, result):
        prompt = self.prompts[stage]; purpose = 'consolidation_entry_profile_' + stage
        if not callable(getattr(self.gateway, 'chat', None)):
            raise MemoryProfileUnavailable('model_gateway_unavailable')
        messages = [{'role': 'system', 'content': prompt['text']},
                    {'role': 'user', 'content': dumps(payload)}]
        if sum(estimate_tokens(m['content']) for m in messages) > self.limits.request_tokens:
            raise ValueError('request_budget_exceeded')
        monotonic = getattr(self.gateway, 'monotonic', time.monotonic)
        deadline = monotonic() + self.limits.timeout_seconds
        previous = None
        for attempt in range(2):
            # Initial HTTP retries and the single syntax/shape repair all use
            # this phase's original deadline and the gateway's call admission.
            self.repository.assert_model_call_allowed()
            if monotonic() >= deadline:
                raise ModelError('retryable', 'total timeout', reason='timeout')
            health = getattr(self.gateway, 'health', None)
            if health:
                health.check('chat', 'learning')
            suffix = '_repair' if attempt else ''
            call = dict(purpose=purpose + suffix, prompt_version=prompt['name'], prompt_sha256=prompt['sha256'],
                        timeout_seconds=self.limits.timeout_seconds, result='success')
            reply = None; started = monotonic(); invalid = None; parsed = None
            try:
                reply = self.gateway.chat(messages, purpose + suffix, max_tokens=16000, _deadline=deadline)
                if monotonic() >= deadline:
                    raise ModelError('retryable', 'total timeout', reason='timeout')
                try:
                    if reply.finish_reason != 'stop':
                        raise ValueError('incomplete_output')
                    parsed = json_object(reply.content)
                    if stage == 'generate':
                        validate_memory_profile_shape(parsed, self.limits)
                    else:
                        check_memory_profile_operations(parsed, payload['operations'])
                        if attempt:
                            _memory_check_repair_guard(previous, parsed)
                except ValueError as error:
                    invalid = str(error)
                    call['validation_error'] = invalid
                # Invalid parsing/validation can itself exhaust the budget.
                if monotonic() >= deadline:
                    raise ModelError('retryable', 'total timeout', reason='timeout')
                if invalid is None:
                    return parsed
                if attempt:
                    raise ModelError('invalid_output', 'invalid profile output after one repair', reason='invalid_output')
            except ModelError as error:
                if error.category == 'retryable' and not error.reason and monotonic() >= deadline:
                    error.reason = 'timeout'
                call.update(result=error.category, reason=error.reason or error.category)
                raise
            finally:
                # Both originals and corrections persist and share purge guards.
                if reply is not None:
                    result['outputs'][stage + suffix] = reply.content
                    call['finish_reason'] = reply.finish_reason
                result['calls'].append(dict(call, duration_ms=round((monotonic() - started) * 1000),
                                            usage=reply.usage if reply is not None else {}))
                self._record_progress(result)
            previous = parsed
            messages = messages + [
                {'role': 'assistant', 'content': reply.content},
                {'role': 'user', 'content': '上一个回答未通过严格JSON结构校验：' + invalid +
                 '。只输出修正后的完整JSON对象，遵守系统中的全部必填字段、类型和编号。'
                 '这是一次格式修正，不新增事实或来源，不改变证据标准，不把语义不通过改成通过；不要解释。'}]
            if sum(estimate_tokens(m['content']) for m in messages) > self.limits.request_tokens:
                result['repair_blocked'] = dict(stage=stage, reason='request_budget_exceeded')
                raise ModelError('invalid_output', 'repair request exceeds material budget', reason='invalid_output')

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
        admission=conn.execute('SELECT * FROM entry_profile_admissions WHERE memory_id=?',(row['id'],)).fetchone() if incremental_profile_enabled(conn) else None
        if admission:
            _,sources=_inc_candidate_sources(conn,row,admission,as_of)
        body = row['content']; saying,_,meaning = body.partition('：') if row['category']=='saying' else ('','','')
        supported = manual or bool(sources)
        if not manual and row['category'] in ('topic','saying'):
            if admission:
                supported = _inc_enough(sources,row['category'],saying) and _inc_approved_proof_valid(conn,row,admission,sources)
            else:
                originals = [s for s in sources if _memory_original(s,row['category'],saying)]
                supported = any(_memory_independent(a,b,row['category'],[s['message'] for s in sources])
                                for i,a in enumerate(originals) for b in originals[i+1:])
        visible = visibility.memory_visible(row['id'],entry_id)
        usable = row['lifecycle']=='active' and supported and visible and not row['merged_into']
        if admission and not manual:
            usable = usable and admission['state']=='accepted' and admission['body_revision']==row['revision']
        if usable_only and not usable:
            continue
        profiles.append(dict(memory_id=row['id'],revision=row['revision'],kind=row['kind'],category=row['category'],
            body=body,saying=saying,meaning=meaning,author='admin' if manual else 'model',speaker='self',stance=row['stance'],
            about=[owner[0]],owner_entry_id=entry_id,lifecycle=row['lifecycle'],event_time=None,importance=row['importance'],
            manual_protected=manual,manual_created=bool(row['manual_created']),sources=sources,
            pending_sources=[x for x in _inc_all_sources(conn,row,as_of) if x.get('source_id') not in json.loads(admission['checked_sources_json'])] if admission else [],
            confirmed_evidence=[r[0] for r in conn.execute('SELECT identity FROM entry_profile_evidence_receipts WHERE memory_id=? ORDER BY identity',(row['id'],))],
            usable=usable,retention=row['retention'],pinned=bool(row['pinned']),
            last_confirmed_at=row['last_confirmed_at'],
            admission_state='manual' if manual else admission['state'] if admission else 'accepted',
            independent_days=admission['independent_days'] if admission else 0,
            independent_uses=admission['independent_uses'] if admission else 0,
            admission_reason=admission['reason'] if admission else None))
    return profiles


def _projection_items(conn, entry_id):
    if _entry(conn,entry_id)['kind'] not in ('group','live') or not _settings(conn,entry_id)['enabled'] or not _runtime(conn)['enabled']:
        return [], []
    items=[]; omitted=[]; tokens=0
    for row in memory_profile_rows(conn,entry_id,usable_only=True):
        label = ITEM_LABELS.get(row['category'],'管理员说明')
        # Rendering is one line per memory; keep the stored manual body lossless.
        item = dict(memory_id=row['memory_id'],text=label+'：'+whitespace(row['body']))
        cost = estimate_tokens(item['text'])
        # Protected hand-written rules are never removed by the display budget.
        if row['manual_protected'] or len(items)<MAX_ITEMS and tokens+cost<=MAX_PROFILE_TOKENS:
            items.append(item); tokens+=cost
        else:
            omitted.append(row['memory_id'])
    return items, omitted


def flush_memory_profile_projections(conn):
    """Exactly one version increment per entry/transaction when rendered set changes."""
    _refresh_incremental_admissions(conn)
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
        version=state['version'],generated_at=state['generated_at'],possibly_stale=bool(pending or state['rebuild_pending'] or (incremental_profile_enabled(conn) and
            conn.execute("SELECT 1 FROM entry_profile_incremental_work WHERE entry_id=? AND state IN ('queued','running','deferred','failed') LIMIT 1",(entry_id,)).fetchone())))


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


# EF-22: bounded, typed proof contracts. These helpers do not make model calls.
# Admission may carry the already bounded accumulated set; nominations remain six.
_INC_ADMISSION_SOURCE_LIMIT = 32


def _inc_segments(text):
    """Lossless contiguous clauses, indexed by the producer instead of regenerated."""
    return [x for x in re.findall(r'[^，,；;。.!！？?\n]*[，,；;。.!！？?\n]|[^，,；;。.!！？?\n]+$',text) if x]


def _inc_segment_excerpt(text, first, end):
    parts=_inc_segments(text)
    if type(first) is not int or type(end) is not int or not 0<=first<end<=len(parts):
        raise ValueError('invalid_source_interval')
    result=''.join(parts[first:end])
    if not 1<=len(result.strip())<=300: raise ValueError('source_interval_limit')
    return result


def _inc_proof_points(body, category):
    text=body.partition('：')[2] if category=='saying' else body
    points=[x for x in _inc_segments(text) if any(c.isalnum() for c in x)]
    if len(points)>24: raise ValueError('proof_point_limit')
    return [dict(point_id=i,text=x) for i,x in enumerate(points,1)]


def _inc_gendered(text):
    from jieba import posseg
    if re.search(r'\b(?:he|him|his|himself|she|her|hers|herself)\b',text,re.I): return True
    return any(word in {'他','她','妳','祂','他们','她们','妳们','祂们'} for word,flag in posseg.cut(text,HMM=False))


def _inc_whole_sentences(original, excerpt):
    """Conservative sentence edges; commas/colons are not semantic boundaries.

    Never expand a model-approved quote after checking it. Ambiguous mixed
    sentences must be kept whole for review or omitted, not silently restored.
    """
    original=whitespace(original);excerpt=whitespace(excerpt)
    offset=0
    while excerpt and (start:=original.find(excerpt,offset))>=0:
        end=start+len(excerpt)
        before=original[:start].rstrip();through=original[:end].rstrip()
        if (not before or before[-1] in '。！？!?；;') and (end==len(original) or through[-1] in '。！？!?；;'):
            return True
        offset=start+1
    return False


def _inc_resolve_safe(value, memory):
    resolved=[]
    for ref in value:
        if isinstance(ref,dict) and set(ref) in ({'source_id'},{'source_id','start_segment','end_segment'}):
            original=next((s for s in memory['sources'] if s['source_id']==ref['source_id']),None)
            if not original: raise ValueError('checker_unbound_interval')
            ref=dict(message_id=original['message_id'],field=original['field'],
                excerpt=original['excerpt'] if set(ref)=={'source_id'} else
                    _inc_segment_excerpt(original['excerpt'],ref['start_segment'],ref['end_segment']))
        _inc_source_shape(ref)
        if set(ref)!={'message_id','field','excerpt'}: raise ValueError('invalid_safe_source')
        if not any(ref['message_id']==s['message_id'] and ref['field']==s['field']
                and whitespace(ref['excerpt']) in whitespace(s['excerpt']) for s in memory['sources']):
            raise ValueError('checker_unbound_excerpt')
        if memory['category']=='saying' and not any(
                ref['message_id']==s['message_id'] and ref['field']==s['field']
                and _inc_whole_sentences(s['excerpt'],ref['excerpt']) for s in memory['sources']):
            raise ValueError('incomplete_saying_sentence')
        if ref in resolved: raise ValueError('duplicate_safe_source')
        resolved.append(ref)
    return resolved


def _inc_public_canonical(s):
    """Only for recomputing deterministic counts, never a provenance binding."""
    message=dict(position=s['message_id'],ref='msg:'+str(s['message_id']),speaker=s['message_speaker'],
        content=s['context'],date=s['date'],occurred_at=s['occurred_at'],quote=s.get('quote'),kind='message')
    return dict(message=message,speaker=s['speaker'],field=s['field'],excerpt=s['excerpt'],
        burst_id=s.get('burst_id'),evidence_identity='message:'+str(s['message_id']))


def _inc_validate_proof(value, payload, previous=None):
    memory=payload['memory'];final=payload.get('mode','final')=='final';accept=value['decision']=='accept'
    source_by_id={s['source_id']:s for s in memory['sources']}
    safe_by_id={sid:{**s,'excerpt':r['excerpt']} for sid,s in source_by_id.items() for r in value['safe_sources']
        if r['message_id']==s['message_id'] and r['field']==s['field'] and whitespace(r['excerpt']) in whitespace(s['excerpt'])}
    groups={g['group_id']:g for g in memory['discussion_groups']}
    reviews=value['group_reviews'];usages=value['usage_reviews'];points=value['point_support'];rule=value['rule_support']
    if any(not isinstance(x,list) for x in (reviews,usages,points)) or not isinstance(rule,dict):
        raise ValueError('invalid_proof_fields')
    if len(reviews)>_INC_ADMISSION_SOURCE_LIMIT or len(usages)>_INC_ADMISSION_SOURCE_LIMIT or len(points)>24: raise ValueError('proof_array_limit')
    def ids(values, *, empty=True):
        if (not isinstance(values,list) or any(not isinstance(x,str) or x not in source_by_id for x in values)
                or len(values)!=len(set(values)) or not empty and not values): raise ValueError('unbound_proof_sources')
        if accept and any(x not in safe_by_id for x in values): raise ValueError('proof_source_removed')
        return set(values)
    old_groups=(previous or {}).get('group_reviews')
    old_usages=(previous or {}).get('usage_reviews')
    old_groups=old_groups if isinstance(old_groups,list) else []
    old_usages=old_usages if isinstance(old_usages,list) else []
    by_group={}
    for r in reviews:
        if (not isinstance(r,dict) or set(r)!={'group_id','discussion_id','same_topic','independent','focus','reason'}
                or type(r['group_id']) is not int or r['group_id'] not in groups or r['group_id'] in by_group
                or type(r['discussion_id']) is not int or r['discussion_id'] not in groups
                or any(type(r[k]) is not bool for k in ('same_topic','independent'))
                or any(not isinstance(r[k],str) or not r[k].strip() or len(r[k])>240 for k in ('focus','reason'))):
            raise ValueError('invalid_group_review')
        if r['independent'] and r['discussion_id']!=r['group_id']: raise ValueError('split_discussion_count')
        by_group[r['group_id']]=r
        for old in old_groups:
            if isinstance(old,dict) and old.get('group_id')==r['group_id'] and any(old.get(k) is False and r[k] for k in ('same_topic','independent')):
                raise ValueError('repair_changed_group_rejection')
    roles={}
    allowed={'use','definition','use_and_definition','mention','quote','other'}
    for r in usages:
        if (not isinstance(r,dict) or set(r)!={'source_id','role','reason'} or not isinstance(r['source_id'],str)
                or r['source_id'] not in source_by_id or r['source_id'] in roles or not isinstance(r['role'],str) or r['role'] not in allowed
                or not isinstance(r['reason'],str) or not 1<=len(r['reason'])<=240): raise ValueError('invalid_usage_review')
        roles[r['source_id']]=r['role']
        for old in old_usages:
            if (isinstance(old,dict) and old.get('source_id')==r['source_id'] and old.get('role') in ('mention','quote','other')
                    and r['role'] in ('use','use_and_definition')): raise ValueError('repair_changed_usage_rejection')
    support={};expected={p['point_id'] for p in memory['proof_points']}
    for p in points:
        if (not isinstance(p,dict) or set(p)!={'point_id','source_ids'} or type(p['point_id']) is not int
                or p['point_id'] not in expected or p['point_id'] in support): raise ValueError('invalid_point_support')
        support[p['point_id']]=ids(p['source_ids'])
    category=memory['category'];retire=memory.get('action')=='retire'
    if rule:
        if set(rule)!={'scope','persistent','announcement','endorsements'}: raise ValueError('invalid_rule_support')
        for refs in rule.values(): ids(refs)
    if not accept: return value
    if set(support)!=expected: raise ValueError('missing_proof_points')
    if category=='topic' and not retire:
        if set(by_group)!=set(groups): raise ValueError('missing_group_reviews')
        def root(gid):
            review=by_group[gid];target=by_group[review['discussion_id']]
            return target['group_id'] if (review['same_topic'] and target['same_topic'] and target['independent']
                and target['discussion_id']==target['group_id'] and groups[target['group_id']]['eligible']) else None
        # Answers may contribute to their root's content; they never add a root.
        eligible={gid for gid,g in groups.items() if root(gid)==gid
            and any(sid in safe_by_id and safe_by_id[sid]['count_eligible'] for sid in g['source_ids'])}
        if final:
            if len({groups[g]['date'] for g in eligible})<2: raise ValueError('reviewed_groups_insufficient')
            for refs in support.values():
                proved={root(source_by_id[s]['group_id']) for s in refs}
                if len({groups[g]['date'] for g in proved if g in eligible})<2: raise ValueError('point_independent_support_insufficient')
        if final and any(not by_group[s['group_id']]['same_topic'] for s in safe_by_id.values()):
            raise ValueError('unrelated_group_retained')
    elif category=='saying' and not retire:
        if set(roles)!=set(source_by_id): raise ValueError('missing_usage_reviews')
        # A definition can contain the spelling without being an actual use.
        # If the input also has a deterministically recognized bare-use sentence,
        # trimming it away must not keep that source's 'use' classification.
        for sid,retained in safe_by_id.items():
            if roles[sid] not in ('use','use_and_definition'): continue
            bare=[part.strip() for part in re.split(r'(?<=[。！？!?；;])',source_by_id[sid]['excerpt'])
                if _inc_short_saying_use(memory['saying'],part)]
            if bare and not any(whitespace(part) in whitespace(retained['excerpt']) for part in bare):
                raise ValueError('use_sentence_removed')
        if final:
            uses=[_inc_public_canonical(s) for sid,s in safe_by_id.items() if roles[sid] in ('use','use_and_definition')]
            if not _inc_enough(uses,'saying',memory['saying']): raise ValueError('reviewed_uses_insufficient')
        for refs in support.values():
            if final and (not refs or not any(source_by_id[s]['message_id'] in memory['definition_refs']
                    and roles[s] in ('definition','use_and_definition') for s in refs)):
                raise ValueError('meaning_without_bound_definition')
    elif category=='rule' and final:
        if not rule or not rule['scope'] or not rule['persistent'] or not (rule['announcement'] or
                len({source_by_id[s]['speaker'] for s in rule['endorsements']})>=2):
            raise ValueError('rule_positive_basis_required')
    if category!='topic' and reviews or category!='saying' and usages or category!='rule' and rule:
        raise ValueError('unexpected_category_proof')
    if final and any(not refs for refs in support.values()): raise ValueError('point_without_support')
    return value


def _inc_discussion_components(sources):
    """Grouping identity only. Non-counting replies remain available as proof."""
    roots=list(range(len(sources)))
    def root(i):
        while roots[i]!=i: i=roots[i]
        return i
    for i,s in enumerate(sources):
        for j,p in enumerate(sources[:i]):
            if (s['evidence_identity']==p['evidence_identity'] or
                    s.get('burst_id') and s.get('burst_id')==p.get('burst_id') or _inc_same_discussion(s,p)):
                roots[root(i)]=root(j)
    return { _inc_source_key(s):min(sources[j]['message']['position'] for j in range(len(sources)) if root(j)==root(i))
             for i,s in enumerate(sources)}


def _inc_proof_memory(row, sources, category, body, saying, definitions, proposal=None):
    components=_inc_discussion_components(sources)
    public=[]
    for s in sources:
        sid='s'+_hash(_inc_source_key(s))[:12]
        public.append(dict(source_id=sid,message_id=s['message']['position'],field=s['field'],excerpt=s['excerpt'],
            segments=_inc_segments(s['excerpt']),speaker=s['speaker'],date=s['message']['date'],occurred_at=s['message']['occurred_at'],
            context=s['message']['content'],message_speaker=s['message']['speaker'],quote=s['message'].get('quote'),
            burst_id=s.get('burst_id'),group_id=components[_inc_source_key(s)],count_eligible=_inc_original(s,category,saying) and (category!='topic' or s['message']['position']==components[_inc_source_key(s)])))
    memory=dict(memory_id=row['id'],revision=row['revision'],category=category,body=body,saying=saying,
        meaning=body.partition('：')[2] if category=='saying' else '',definition_refs=definitions,
        proof_points=_inc_proof_points(body,category),entity_hints=_inc_entity_hints(body),sources=public,discussion_groups=[])
    if proposal: memory.update(action=proposal['action'],old_body=row['content'])
    return _inc_memory_page(memory,public)


def _inc_memory_page(memory, sources):
    groups=[]
    if memory['category']=='topic' and memory.get('action')!='retire':
        for gid in sorted({s['group_id'] for s in sources}):
            members=[s for s in sources if s['group_id']==gid]
            groups.append(dict(group_id=gid,date=min(s['date'] for s in members),
                source_ids=[s['source_id'] for s in members],eligible=any(s['count_eligible'] for s in members)))
    return {**memory,'sources':sources,'discussion_groups':groups}


def _inc_plan_proof(memory, votes, fits):
    """Select complete conjunctions reported by mapping pages, never bare counts.

    A page's source list for a point is a conjunction. Keep it intact. Topic
    mapping units are local days; final review still resolves real discussions.
    No unit can prove an omitted clause or a definition inferred from mere use.
    """
    from itertools import combinations
    by_id={s['source_id']:s for s in memory['sources']};points={p['point_id'] for p in memory['proof_points']}
    safe={};units=[];roles={};links={};rule={k:set() for k in ('scope','persistent','announcement','endorsements')}
    for vote in votes:
        if vote['decision']!='accept': return None,'mapping_rejected'
        for ref in vote['safe_sources']:
            for sid,s in by_id.items():
                if s['message_id']==ref['message_id'] and s['field']==ref['field'] and whitespace(ref['excerpt']) in whitespace(s['excerpt']):
                    safe[sid]={**s,'excerpt':ref['excerpt'],'segments':_inc_segments(ref['excerpt'])}
        roles.update({r['source_id']:r['role'] for r in vote['usage_reviews']})
        for review in vote['group_reviews']:
            gid,parent=review['group_id'],review['discussion_id']
            if parent!=gid:
                if gid in links and links[gid]!=parent: return None,'proof_gap:conflicting_discussion_links'
                links[gid]=parent
        for key,refs in vote['rule_support'].items(): rule[key].update(refs)
        for point in vote['point_support']:
            refs=set(point['source_ids'])
            if not refs: continue
            if memory['category']=='topic':
                for day in sorted({by_id[s]['date'] for s in refs}):
                    units.append((point['point_id'],day,{s for s in refs if by_id[s]['date']==day}))
            else: units.append((point['point_id'],'',refs))
    # A proof may span mapping pages. Combine all contributors for the same day,
    # so an answer/qualifier on another page is not silently discarded.
    by_day={}
    for point,day,refs in units: by_day.setdefault(day,{}).setdefault(point,set()).update(refs)
    def discussion_root(gid):
        seen=set()
        while gid in links:
            if gid in seen: return None
            seen.add(gid);gid=links[gid]
        return gid
    options=[];category=memory['category']
    if category=='topic':
        days=sorted(by_day)
        choices=list(combinations(days,2))+list(combinations(days,3))
        # A complete proof may need >3 days (different repeated body points).
        # Bounded greedy cover from each seed avoids exponential subset search.
        for seed in [None]+days:
            chosen=[] if seed is None else [seed]
            while len(chosen)<6 and not all(sum(p in by_day[d] for d in chosen)>=2 for p in points):
                missing={p:2-sum(p in by_day[d] for d in chosen) for p in points}
                remaining=[d for d in days if d not in chosen]
                if not remaining: break
                day=min(remaining,key=lambda d:(-sum(missing[p]>0 for p in by_day[d]),
                    len(set().union(*by_day[d].values())),d))
                if not any(missing[p]>0 for p in by_day[day]): break
                chosen.append(day)
            choices.append(tuple(sorted(chosen)))
        for chosen in sorted(set(choices)):
            if not all(sum(p in by_day[d] for d in chosen)>=2 for p in points): continue
            keep=set().union(*(refs for d in chosen for refs in by_day[d].values()))
            # A supporting answer needs its own discussion's initiating
            # witness, not an arbitrary (possibly unrelated) question that day.
            needed={discussion_root(by_id[s]['group_id']) for s in keep}
            if None in needed: continue
            for gid in sorted(needed):
                witnesses=[s for s in safe.values() if s['group_id']==gid and s['count_eligible']]
                if not witnesses: break
                keep.add(min(witnesses,key=lambda s:(len(s['excerpt']),s['message_id'],s['source_id']))['source_id'])
            else: options.append(keep)
    else:
        if any(not any(p==point for p,day,refs in units) for point in points): return None,'proof_gap:missing_point'
        # Retain every contributing definition/qualifier; no keyword-based slicing.
        core=set().union(*(refs for _,_,refs in units)) if units else set()
        if category=='saying':
            uses=[sid for sid in safe if roles.get(sid) in ('use','use_and_definition')]
            for pair in combinations(uses,2):
                if _inc_enough([_inc_public_canonical(safe[s]) for s in pair],'saying',memory['saying']):
                    options.append(core|set(pair))
        else:
            extras=set().union(*rule.values())
            options=[core|extras]
    ranked=[]
    for keep in options:
        if not keep<=set(safe): continue
        selected=[safe[s] for s in sorted(keep,key=lambda sid:(by_id[sid]['message_id'],sid))]
        if fits(selected): ranked.append(selected)
    if not ranked: return None,'proof_gap:complete_evidence_budget' if options else 'proof_gap:independent_point_support'
    # Shortest complete proof first, then latest + earliest; stable tie break.
    result=min(ranked,key=lambda ss:(sum(estimate_tokens(dumps(s)) for s in ss),-max(s['message_id'] for s in ss),min(s['message_id'] for s in ss)))
    return result,None


def _inc_feedback(conn, entry_id, budget=450):
    feedback=[];used=0
    rows=conn.execute("SELECT id,kind,state,reason,result_json,payload_json FROM entry_profile_incremental_work WHERE entry_id=? AND state IN ('applied','failed','rejected') ORDER BY id DESC LIMIT 16",(entry_id,)).fetchall()
    for row in rows:
        result=json.loads(row['result_json'] or '{}');payload=json.loads(row['payload_json']);items=[]
        if row['kind']=='observe':
            for removed in result.get('removed',[]):
                originals=result.get('generated',[]);index=removed.get('index',0)
                raw=originals[index-1] if 0<index<=len(originals) else {}
                items.append(dict(category=raw.get('category'),body=str(raw.get('body',''))[:180],reason=removed['reason']))
        elif row['state'] in ('failed','rejected') or result.get('proof_gap'):
            memory=payload.get('memory',{})
            items.append(dict(memory_id=memory.get('memory_id'),category=memory.get('category'),body=memory.get('body',''),
                reason=result.get('proof_gap') or result.get('reason') or row['reason']))
        for item in items:
            item.update(evidence=False,work_id=row['id']);item['reason']=str(item['reason'])[:240]
            item['_refs']=([dict(message_id=m['message_id']) for m in payload.get('messages',[])]
                + payload.get('_snapshot',{}).get('evidence_refs',[])
                + ([dict(memory_id=item['memory_id'])] if item.get('memory_id') else []))
            cost=estimate_tokens(dumps({k:v for k,v in item.items() if not k.startswith('_')}))
            if len(feedback)>=6 or used+cost>budget: continue
            feedback.append(item);used+=cost
    # A deterministic block before queueing has no work record. Include its
    # current diagnostic without treating it as evidence or losing purge links.
    blocked=conn.execute("""SELECT m.id,m.content,p.category,a.reason FROM entry_profile_admissions a
        JOIN memories m ON m.id=a.memory_id JOIN entry_profile_memories p ON p.memory_id=m.id
        WHERE p.entry_id=? AND m.lifecycle='active' AND m.purged_at IS NULL AND a.reason IS NOT NULL
        ORDER BY m.id DESC LIMIT 12""",(entry_id,)).fetchall()
    for row in blocked:
        if any(item.get('memory_id')==row['id'] and item['reason']==row['reason'] for item in feedback): continue
        item=dict(memory_id=row['id'],category=row['category'],body=row['content'][:180],
            reason=row['reason'][:240],evidence=False,work_id=None)
        cost=estimate_tokens(dumps(item))
        if len(feedback)>=6 or used+cost>budget: continue
        item['_refs']=[dict(memory_id=row['id'])]
        feedback.append(item);used+=cost
    return feedback


# EF-13: bounded observations and independent per-memory admission.
@dataclass(frozen=True)
class IncrementalProfileLimits:
    messages: int = 48
    message_tokens: int = 3000
    profiles: int = 32
    directory_tokens: int = 1500
    observations: int = 8
    evidence: int = 6
    evidence_tokens: int = 2000
    observe_request_tokens: int = 6000
    admit_request_tokens: int = 4000
    repair_request_tokens: int = 12000
    timeout_seconds: int = 180

    def __post_init__(self):
        if any(type(v) is not int or v<1 for v in vars(self).values()) or self.timeout_seconds>180:
            raise ValueError('invalid incremental limits')


def incremental_profile_enabled(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='entry_profile_admissions'").fetchone())


def incremental_prompts():
    from importlib.resources import files
    import hashlib
    result={}
    for stage,name in (('observe','entry_profile_observe_v9'),('admit','entry_profile_admit_v9')):
        value=files('iris').joinpath('prompts',name+'.md').read_text(encoding='utf-8')
        result[stage]=dict(name=name,text=value,sha256=hashlib.sha256(value.encode()).hexdigest())
    return result


def _inc_public_message(message):
    result={k:message[k] for k in ('message_id','speaker','content','occurred_at','date','quote','fragment') if k in message}
    result['segments']={'content':_inc_segments(message['content']),'quote':_inc_segments((message.get('quote') or {}).get('content',''))}
    return result


def _inc_public_payload(payload):
    result={k:v for k,v in payload.items() if not k.startswith('_')}
    if 'messages' in result:
        result['messages']=[_inc_public_message(m) for m in result['messages']]
    if 'feedback' in result:
        result['feedback']=[{k:v for k,v in item.items() if not k.startswith('_')} for item in result['feedback']]
    return result


def _inc_source_shape(value):
    if not isinstance(value,dict): raise ValueError('invalid_source_shape')
    base={'message_id','field'}
    if (type(value.get('message_id')) is not int or value['message_id']<1
            or value.get('field') not in ('content','quote')): raise ValueError('invalid_source_shape')
    if set(value)==base|{'start_segment','end_segment'}:
        if type(value['start_segment']) is not int or type(value['end_segment']) is not int or not 0<=value['start_segment']<value['end_segment']:
            raise ValueError('invalid_source_interval')
    elif (set(value)!=base|{'excerpt'} or not isinstance(value['excerpt'],str)
            or not 1<=len(value['excerpt'].strip())<=300): raise ValueError('invalid_source_shape')


def validate_incremental_observations(value, limits=None):
    limits=limits or IncrementalProfileLimits()
    if not isinstance(value,dict) or set(value)!={'observations'} or not isinstance(value['observations'],list) or len(value['observations'])>limits.observations:
        raise ValueError('invalid_observations')
    for item in value['observations']:
        if not isinstance(item,dict) or item.get('op') not in ('confirm','propose','change'):
            raise ValueError('invalid_observation')
        expected={'op','source_refs'}
        if item['op']!='propose':
            expected.add('memory_id')
            if type(item.get('memory_id')) is not int or item['memory_id']<1:
                raise ValueError('invalid_target_id')
        if item['op']!='confirm':
            expected|={'category','body','saying','meaning','definition_refs'}
            if (not isinstance(item.get('category'),str) or item['category'] not in ITEM_LABELS or any(not isinstance(item.get(k),str) for k in ('body','saying','meaning'))
                    or len(item['body'])>180 or len(item['saying'])>40
                    or item['category']=='saying' and len(item['saying'])+bool(item['meaning'])+len(item['meaning'])>180
                    or not isinstance(item.get('definition_refs'),list)
                    or any(type(x) is not int or x<1 for x in item['definition_refs'])):
                raise ValueError('invalid_candidate_fields')
        if item['op']=='change':
            expected.add('action')
            if item.get('action') not in ('revise','retire'):
                raise ValueError('invalid_change_action')
        if set(item)!=expected or not isinstance(item['source_refs'],list) or not 1<=len(item['source_refs'])<=limits.evidence:
            raise ValueError('invalid_observation_fields')
        for source in item['source_refs']:
            _inc_source_shape(source)
    return value


def validate_incremental_admission(value, payload, previous=None):
    expected={'decision','violations','reason','safe_sources'}
    if payload.get('review_contract')=='semantic_v8': expected|={'point_support','group_reviews','usage_reviews','rule_support'}
    if (not isinstance(value,dict) or set(value)!=expected
            or value['decision'] not in ('accept','reject','insufficient')
            or not isinstance(value['violations'],list) or any(v not in MEMORY_PROFILE_VIOLATIONS for v in value['violations'])
            or value['violations']!=sorted(set(value['violations']),key=MEMORY_PROFILE_VIOLATIONS.index)
            or not isinstance(value['reason'],str) or not value['reason'].strip() or len(value['reason'])>1000
            or not isinstance(value['safe_sources'],list) or len(value['safe_sources'])>
                (_INC_ADMISSION_SOURCE_LIMIT if payload.get('review_contract')=='semantic_v8' else 6)):
        raise ValueError('invalid_admission')
    if value['decision']=='accept' and (value['violations'] or not value['safe_sources'] and payload.get('mode')!='map'):
        raise ValueError('invalid_accepted_admission')
    if value['decision']!='accept' and value['safe_sources']:
        raise ValueError('rejected_sources')
    value=copy.deepcopy(value)
    value['safe_sources']=_inc_resolve_safe(value['safe_sources'],payload['memory']) if payload.get('review_contract')=='semantic_v8' else value['safe_sources']
    for source in value['safe_sources']: _inc_source_shape(source)
    if previous and (previous.get('decision') in ('reject','insufficient') or previous.get('violations')) and value['decision']=='accept':
        raise ValueError('repair_changed_semantic_rejection')
    if payload.get('review_contract')=='semantic_v8': _inc_validate_proof(value,payload,previous)
    return value


def _inc_validate_observation_intervals(value, payload):
    # Bounds are a format failure while the original request is still available,
    # not a reason to silently lose the whole nominated rule during writeback.
    messages={m['message_id']:m for m in payload.get('messages',[])}
    for item in value['observations']:
        for ref in item['source_refs']:
            if 'start_segment' not in ref: continue
            message=messages.get(ref['message_id'])
            if not message: raise ValueError('unbound_source_interval')
            original=message['content'] if ref['field']=='content' else (message.get('quote') or {}).get('content','')
            _inc_segment_excerpt(original,ref['start_segment'],ref['end_segment'])


def _inc_repair_instruction(stage, payload, invalid):
    text='上次输出结构无效：'+invalid+'。仅修正JSON格式、必填字段和类型；不新增事实/来源，不把语义拒绝改成通过。输出完整JSON。'
    if stage=='observe':
        sizes=[dict(message_id=m['message_id'],content=len(_inc_segments(m['content'])),
            quote=len(_inc_segments((m.get('quote') or {}).get('content','')))) for m in payload.get('messages',[])]
        return text+' source_refs的start_segment/end_segment是半开区间，0<=start_segment<end_segment<=该field的段数；不能使用字符位置或越界。各message_id的段数：'+dumps(sizes)
    if payload.get('review_contract')!='semantic_v8': return text
    memory=payload['memory'];category=memory['category']
    text+=' 八个必填字段不得漏掉：decision、violations、reason、safe_sources、point_support、group_reviews、usage_reviews、rule_support。'
    text+=' 当前category='+category+'，mode='+payload.get('mode','final')+'。'
    if category!='saying': text+='usage_reviews=[]（此类别不做说法使用角色判断）。'
    if category!='topic': text+='group_reviews=[]（此类别不做话题讨论组判断）。'
    if category!='rule': text+='rule_support={}（此类别不做群规判断）。'
    text+=' accept时point_support逐一保留point_id '+dumps([p['point_id'] for p in memory['proof_points']])+'；map无本页支持可填source_ids=[]，不能删除point_support。'
    text+=' safe_sources可用{"source_id":"输入ID"}原样保留该来源；说法缩引须保留完整使用句/释义句，不许只保留逗号后半句。所有支持仍以实际保留原话为准。'
    return text


def _inc_bound(conn, entry_id, refs, messages, *, target=None, repairs=None):
    from .memory_ops import entry_profile_source
    pool={m['message_id']:m for m in messages}
    result=[]
    for raw in refs:
        _inc_source_shape(raw)
        message=pool.get(raw['message_id'])
        if not message or message['entry_id']!=entry_id:
            raise ValueError('unbound_source')
        if 'start_segment' in raw:
            text=message['content'] if raw['field']=='content' else (message.get('quote') or {}).get('content','')
            raw=dict(message_id=raw['message_id'],field=raw['field'],excerpt=_inc_segment_excerpt(text,raw['start_segment'],raw['end_segment']))
        field=raw['field'];snippet=whitespace(raw['excerpt'])
        quote=message.get('quote') or {}
        fields={'content':message['content'],'quote':quote.get('content','')}
        valid=[name for name,text in fields.items() if snippet and snippet in whitespace(text)
               and (name=='content' or quote.get('speaker'))]
        if field not in valid and len(valid)==1:
            field=valid[0]
            if repairs is not None: repairs.append(dict(message_id=raw['message_id'],before=raw['field'],after=field,reason='exact_source_field'))
        binding=entry_profile_source(conn,raw['message_id'],field=field,excerpt=raw['excerpt'])
        live=conn.execute('SELECT * FROM messages WHERE id=?',(raw['message_id'],)).fetchone()
        if not live or _message_hash(dict(live))!=message['message_sha256']:
            raise ValueError('source_changed')
        result.append(binding)
    return result


def _inc_all_sources(conn, row, as_of):
    sources=_profile_bound_sources(conn,row,_setting(conn,'timezone','Asia/Shanghai'),as_of)
    for source in sources:
        matches=conn.execute('''SELECT s.id,g.burst_id FROM sources s LEFT JOIN entry_profile_source_groups g ON g.source_id=s.id
            WHERE s.memory_id=? AND s.message_id=? AND s.kind='message' ORDER BY s.id''',(row['id'],source['message']['position'])).fetchall()
        for item in matches:
            note=json.loads(conn.execute('SELECT note FROM sources WHERE id=?',(item['id'],)).fetchone()[0])
            if note.get('field')==source['field'] and note.get('excerpt')==source['excerpt']:
                source['source_id']=item['id'];source['burst_id']=item['burst_id'];break
    return sources


# Conservative grammatical normalization; no semantic synonym or corpus vocabulary table.
_INC_PARTICLES = '呀啊吧呢吗嘛啦哦哟呀呗'
_INC_CLASSIFIERS = '个盏张本道座次遍圈条份件段层块片台只支枚'
_INC_ONCE = re.compile(r'今天|今晚|明天|昨[天晚]|这次|本次|此次|当天|这回|本周|下周|(?<!每)周[一二三四五六日天]|\d{1,2}月\d{1,2}[日号]|\d{1,2}[:：]\d{2}')
_INC_PERSONAL_RULE = re.compile(r'(?:(?:我|本人)(?:的|自己的)|(?:我|本人)个人的?).{0,8}(?:个人|私事|安排|预约|住址|电话)|(?:我(?!们)|本人)(?:刚|已经|还|要|将)?预约|(?:给我|替我|帮我)(?!们)|[\w\u4e00-\u9fff]{1,8}[，,]\s*你(?!们)|@(?!(?:所有人|全体成员|everyone|all)(?:\s|$|[，,：:]))\S+',re.I)
_INC_EXTERNAL_RULE = re.compile(r'(?:公司|学校|协会|机构|单位|组织).{0,4}(?:规定|要求|制度)')
_INC_FOLLOWUP = re.compile(r'还有吗|怎么样了|有结果了吗|有消息了吗|后来呢|找到[了啦]|已经.{0,8}(?:寄|发|给)|之前问|上次问|前几天.{0,6}(?:问|求)|(?:进度|结果).{0,3}(?:怎样|如何)|接着上次|跟进|催办')
_INC_REQUEST = re.compile(r'[?？]|求|谁有|能否|有没有|想找|帮忙|拜托')


# Discourse grammar, not a topic dictionary. Ambiguous semantic continuations
# remain the per-group reviewer's responsibility.
_INC_PRIOR_DISCUSSION = re.compile(r'(?:上[次回场]|前[次回场]|此前|之前|先前|前几天|前些天).{0,12}(?:问|求助|讲|说|讨论|建议)|(?:那次|那场).{0,6}(?:讨论|讲解|求助)')
_INC_RESULT_REPORT = re.compile(r'(?:回来|来|补个|补充).{0,4}(?:报告|汇报|反馈)|(?:按|照着?|采纳|采用).{0,12}(?:建议|办法|方法).{0,16}(?:了|过|已经|解决|成功)|(?:问题|故障).{0,4}(?:解决了|好了)|回去.{0,8}(?:试|做)(?:了|过)|(?:试了|试过).{0,8}(?:有效|有用|好了|确实)')


def _inc_explicit_continuation(text):
    # A reference is not enough by itself (one can introduce a new question).
    # Require an explicit report/adoption as well. Unknown antecedents are
    # excluded as witnesses, rather than linked to an invented earlier ID.
    return bool(_INC_PRIOR_DISCUSSION.search(text) and _INC_RESULT_REPORT.search(text))



def _inc_saying_key(value):
    from jieba import posseg
    value=_memory_observation_text(value)
    pieces=list(posseg.cut(value,HMM=False))
    while pieces and pieces[-1].flag=='y' and all(c in _INC_PARTICLES for c in pieces[-1].word):
        value=value[:-len(pieces.pop().word)]
    # A narrow demonstrative-N + V question, not arbitrary bag-of-words reordering.
    match=re.fullmatch(r'[这那](?:一)?['+_INC_CLASSIFIERS+r']?([\u4e00-\u9fff]{1,4})还([\u4e00-\u9fff])着?',value)
    if match: return match[2]+match[1]
    # A closed grammar around an unchanged lexical core. One full doubled
    # utterance remains one use. Negation and lexical characters are preserved.
    doubled=re.fullmatch(r'([\u4e00-\u9fff]{2,8})\1',value)
    if doubled: value=doubled[1]
    is_verb=lambda text: bool(text) and posseg.dt.word_tag_tab.get(text[0],'').startswith('v')
    if (len(value)>=3 and len(pieces)>1 and pieces[0].word=='先' and pieces[0].flag=='d'
            and pieces[1].flag.startswith('v') and is_verb(value[1:])): value=value[1:]
    if is_verb(value):
        trailing=re.fullmatch(r'([\u4e00-\u9fff]{2,8})(?:一下下?|一会儿?)',value)
        if trailing: value=trailing[1]
        verb=value[0]
        if posseg.dt.word_tag_tab.get(value[1:],'') in ('n','ng','nz','ns','nt'): return value
        prefixes=[verb+'一下下',verb+'一下',verb+'一会儿',verb+'一会',verb+'一'+verb,verb+verb]
        prefixes += [verb+c for c in _INC_CLASSIFIERS]
        for prefix in prefixes:
            rest=value[len(prefix):] if value.startswith(prefix) else ''
            if re.fullmatch(r'[\u4e00-\u9fff]{1,7}',rest):
                value=verb+rest
                break
    return value


def _inc_short_saying_use(saying, text):
    # A copied statement containing additional facts is not a bare saying.
    # Quote/reported-speech exclusions run before this narrow copy exception.
    key=_inc_saying_key(saying)
    return bool(re.fullmatch(r'[\u4e00-\u9fff]{2,8}|[a-z0-9]{2,16}',key)) and _inc_saying_key(text)==key


def _inc_saying_used(saying, excerpt):
    key=_inc_saying_key(saying)
    raw=unicodedata.normalize('NFKC',excerpt).casefold()
    if not key: return False
    if re.search(r'[a-z0-9]',key):
        return bool(re.search(r'(?<![a-z0-9])'+re.escape(key)+r'(?![a-z0-9])',raw))
    text=_memory_observation_text(raw)
    from jieba import posseg
    boundaries={0};offset=0
    for token in posseg.cut(text,HMM=False):
        offset+=len(token.word);boundaries.add(offset)
    def matched(pattern,noun='',inflected=False):
        for m in re.finditer(pattern,text):
            if m.start() not in boundaries or m.end() not in boundaries: continue
            if inflected and posseg.dt.word_tag_tab.get(m.group()[1:],'') in ('n','ng','nz','ns','nt'): continue
            # A second segmentation from the noun prevents overlapping dictionary
            # words from accepting a fragment of a longer, different object.
            tail=text[m.end()-len(noun):] if noun else ''
            if noun and any(posseg.dt.word_tag_tab.get(tail[:size],'').startswith('n')
                    for size in range(len(noun)+1,min(len(tail),len(noun)+4)+1)): continue
            return True
        return False
    if matched(re.escape(key),key[1:]): return True
    # Keep lexical characters and order. Light duration/aspect insertions
    # require a dictionary verb; no unrestricted gaps or semantic synonyms.
    if not re.fullmatch(r'[\u4e00-\u9fff]{2,8}',key) or key[0] in '不没无非别未': return False
    verb,noun=key[0],key[1:]
    light=r'一下下?|一会儿?|' if posseg.dt.word_tag_tab.get(verb,'').startswith('v') else ''
    inflection=r'(?:'+light+r'[的了着过]|(?:[一两三]?['+_INC_CLASSIFIERS+r'])|'+re.escape(verb)+r'|一'+re.escape(verb)+r')?'
    if matched(re.escape(verb)+inflection+re.escape(noun),noun,inflected=True): return True
    return matched(r'[这那](?:一)?['+_INC_CLASSIFIERS+r']?'+re.escape(noun)+r'还'+re.escape(verb)+r'着?[吗呢]?')


def _inc_use_independent(a,b,category,messages,saying):
    if category!='saying': return _memory_independent(a,b,category,messages)
    at,bt=_instant(a['message']['occurred_at']),_instant(b['message']['occurred_at'])
    if abs((at-bt).total_seconds())<60 or at.replace(second=0,microsecond=0)==bt.replace(second=0,microsecond=0): return False
    if a['speaker']==b['speaker'] and abs((at-bt).total_seconds())<1800: return False
    if _inc_short_saying_use(saying,a['excerpt']) and _inc_short_saying_use(saying,b['excerpt']): return True
    return _memory_independent(a,b,category,messages)


def _inc_entity_terms(body):
    """Literal anchors, not an open-ended inventory of nouns in a theme.

    Dictionary person/place/organization names are deterministic hints only.
    Unknown names and ordinary words used as brands remain the semantic check's
    responsibility unless explicitly named or written as an identifier.
    """
    from jieba import posseg
    body=unicodedata.normalize('NFKC',body)
    result=[a or b for a,b in re.findall(r'《([^》]+)》|〈([^〉]+)〉',body)]
    result+=re.findall(r'(?:品牌|型号|系列|产品名|地名|公司名|作品名)(?:为|是|叫|名为|[:：])?\s*[“「『"]([^”」』"]+)[”」』"]',body)
    # Preserve composite dates/times: matching their individual numbers is not enough.
    result+=re.findall(r'\d{4}年\d{1,2}月\d{1,2}[日号]|\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}月\d{1,2}[日号]|\d{1,2}[:：]\d{2}',body)
    result+=re.findall(r'(?<![a-zA-Z0-9_.])\d+(?:\.\d+)?(?:%|％)?',body)
    identifiers=re.findall(r'(?<![a-zA-Z0-9])[a-zA-Z][a-zA-Z0-9]*(?:[-_][a-zA-Z0-9]+)*(?![a-zA-Z0-9])',body)
    result.extend(term for term in identifiers if any(c.isupper() or c.isdigit() for c in term))
    # Explicit measured quantities, not arbitrary numeral prefixes in words
    # such as indefinite classifiers. Units are grammatical, not topic terms.
    result.extend(re.findall(r'[零〇一二两三四五六七八九十百千万]+(?:毫米|厘米|公里|千米|米|千克|公斤|克|毫升|升|摄氏度|度|元|秒|分钟|小时|天|周|个月|年)',body))
    for word,flag in posseg.cut(body,HMM=False):
        if flag=='ns' and len(word)>=2 and re.search(r'[省市县区州国镇乡村]$',word):
            result.append(word)
        elif flag=='m':
            quantity=re.fullmatch(r'[零〇一二两三四五六七八九十百千万]+',word)
            if quantity and word!='一': result.append(quantity[0])
    return list(dict.fromkeys(result))



def _inc_entity_hints(body):
    # Dictionary POS is a hint, not an identity decision (common nouns may be
    # tagged nr/nt). The final point proof must establish any actual referent.
    from jieba import posseg
    hard=set(_inc_entity_terms(body))
    return list(dict.fromkeys(w for w,f in posseg.cut(body,HMM=False)
        if f in ('nr','nt','ns') and len(w)>1 and w not in hard))[:24]

def _inc_missing_entities(body, evidence):
    texts=[unicodedata.normalize('NFKC',x).casefold() for x in evidence]
    missing=[]
    for term in _inc_entity_terms(body):
        term=unicodedata.normalize('NFKC',term).casefold()
        if re.fullmatch(r'\d+(?:\.\d+)?%?',term):
            found=any(re.search(r'(?<![\d.])'+re.escape(term)+r'(?![\d.])',x) for x in texts)
        elif re.match(r'[零〇一二两三四五六七八九十百千万]',term):
            found=any(re.search(r'(?<![零〇一二两三四五六七八九十百千万])'+re.escape(term)+r'(?![零〇一二两三四五六七八九十百千万])',x) for x in texts)
        elif re.search(r'[a-z]',term):
            found=any(re.search(r'(?<![a-z0-9])'+re.escape(term)+r'(?![a-z0-9])',x) for x in texts)
        else: found=any(term in x for x in texts)
        if not found: missing.append(term)
    return missing


def _inc_rule_reason(sources):
    # This is only a negative guard. Announcement/authority/lasting scope must
    # be proved by the per-memory checker, not by a positive keyword allowlist.
    for source in sources:
        text=(source['message']['content'] if source['field']=='content'
              else (source['message'].get('quote') or {}).get('content',''))
        timing=re.sub(r'(?:从|自)(?:今天|明天|\d{1,2}月\d{1,2}[日号])(?:起|开始)','',text)
        if _INC_ONCE.search(timing) or _INC_PERSONAL_RULE.search(text): continue
        if _INC_EXTERNAL_RULE.search(text): continue
        return None
    return 'rule_scope_not_established'


def _inc_original(source, category, saying):
    message=source['message']
    if (source['field']!='content' or _MEMORY_REPORTED.search(message['content'])
            or _MEMORY_REGISTRATION.search(message['content'])): return False
    quote=message.get('quote') or {}
    if quote and _memory_observation_text(source['excerpt']) in _memory_observation_text(quote.get('content','')):
        return False
    if category=='topic' and _inc_explicit_continuation(message['content']): return False
    return category!='saying' or _inc_saying_used(saying,source['excerpt'])


def _inc_request_object(text):
    """Longest explicit noun span in a request; no corpus objects or synonyms."""
    from jieba import posseg
    spans=[];current=''
    for word,flag in posseg.cut(text,HMM=False):
        if flag.startswith('n') or flag=='eng':
            current+=word
        else:
            if len(current)>=2: spans.append(current)
            current=''
    if len(current)>=2: spans.append(current)
    longest=max(map(len,spans),default=0)
    return [s for s in spans if len(s)==longest]


def _inc_same_discussion(left, right):
    a,b=left['message'],right['message']
    if _instant(a['occurred_at'])>_instant(b['occurred_at']): a,b=b,a
    quote=b.get('quote') or {}
    quoted=_memory_observation_text(quote.get('content',''))
    # Without a reply ID, require the actual author's complete quoted text.
    # A short contained phrase is neither a reply link nor an event identity.
    if quote and (quote.get('ref')==a['ref'] or
            quote.get('speaker')==a['speaker'] and quoted and
            quoted==_memory_observation_text(a['content'])):
        return True
    if (a['speaker']!=b['speaker'] or not _INC_FOLLOWUP.search(b['content'])
            or not _INC_REQUEST.search(a['content'])):
        return False
    text=_memory_observation_text(b['content'])
    return any(_memory_observation_text(obj) in text for obj in _inc_request_object(a['content']))


def _inc_body_reason(category,body,saying,sources):
    if category=='rule':
        reason=_inc_rule_reason(sources)
        if reason: return reason
    # Saying spelling itself is covered by variant-use validation, not named-entity inference.
    prose=body.partition('：')[2] if category=='saying' else body
    missing=_inc_missing_entities(prose,[s['excerpt'] for s in sources])
    return 'unbound_entities:'+','.join(missing) if missing else None


def _inc_source_key(source):
    return (source['message']['position'],source['field'],source['excerpt'])


def _inc_groups(sources, category, saying=''):
    ordered=sorted(sources,key=lambda s:(s['message']['position'],s.get('source_id',0)))
    messages=[s['message'] for s in ordered]
    roots=list(range(len(ordered)))
    def root(index):
        while roots[index]!=index:
            roots[index]=roots[roots[index]];index=roots[index]
        return index
    if category=='topic':
        # Only genuine identity/reply/follow-up links may propagate. Calendar
        # proximity and substring similarity cannot join whole discussion days.
        for i,source in enumerate(ordered):
            content=_memory_observation_text(source['message']['content'])
            for j,prior in enumerate(ordered[:i]):
                if (source['evidence_identity']==prior['evidence_identity'] or
                        source.get('burst_id') and source.get('burst_id')==prior.get('burst_id') or
                        content and content==_memory_observation_text(prior['message']['content']) or
                        _inc_same_discussion(source,prior)):
                    roots[root(i)]=root(j)
    selected=[];selected_roots=set()
    for i,source in enumerate(ordered):
        if category in ('topic','saying') and not _inc_original(source,category,saying): continue
        if category=='topic':
            identity=root(i)
            if identity in selected_roots: continue
            # At most one original per local date. Do not turn this necessary
            # threshold into an equivalence edge linking every source that day.
            if any(source['message']['date']==prior['message']['date'] or
                    abs((_instant(source['message']['occurred_at'])-
                         _instant(prior['message']['occurred_at'])).total_seconds())<30*60
                    for prior in selected): continue
            selected_roots.add(identity)
        elif any(source['evidence_identity']==prior['evidence_identity'] or
                 source.get('burst_id') and source.get('burst_id')==prior.get('burst_id') or
                 not _inc_use_independent(source,prior,category,messages,saying) for prior in selected): continue
        selected.append(source)
    return selected


def _inc_enough(sources, category, saying=''):
    chosen=_inc_groups(sources,category,saying)
    return bool(chosen) if category in ('rule',None) else len(chosen)>=2


def _inc_approved_proof_valid(conn, row, admission, sources):
    if not admission or not admission['signature']: return True
    work=conn.execute('SELECT payload_json,parsed_json FROM entry_profile_incremental_work WHERE work_key=?',(admission['signature'],)).fetchone()
    if not work or not work['parsed_json']: return True  # Legacy publication.
    payload=json.loads(work['payload_json'])
    if payload.get('review_contract')!='semantic_v8': return True
    value=json.loads(work['parsed_json'])
    try:
        value['safe_sources']=_inc_resolve_safe(value['safe_sources'],payload['memory'])
        value['safe_sources']=[ref for ref in value['safe_sources'] if any(ref['message_id']==s['message']['position']
            and ref['field']==s['field'] and whitespace(ref['excerpt'])==whitespace(s['excerpt']) for s in sources)]
        validate_incremental_admission(value,_inc_public_payload(payload))
    except (ValueError,KeyError,TypeError): return False
    return True


def _inc_candidate_sources(conn, row, admission, as_of):
    all_sources=_inc_all_sources(conn,row,as_of)
    approved=set(json.loads(admission['checked_sources_json'])) if admission else set()
    return all_sources,[s for s in all_sources if s.get('source_id') in approved]


def _refresh_incremental_admissions(conn):
    """Write-transaction hook: withdrawals never reduce importance or edit prose."""
    if not incremental_profile_enabled(conn):
        return
    from .memory_ops import _profile_manual
    zone=_setting(conn,'timezone','Asia/Shanghai')
    now=_instant(utc_now()).isoformat()
    for row in conn.execute('''SELECT m.*,p.category FROM entry_profile_memories p JOIN memories m ON m.id=p.memory_id
            JOIN entry_profile_projection_dirty d ON d.entry_id=p.entry_id WHERE m.purged_at IS NULL''').fetchall():
        admission=conn.execute('SELECT * FROM entry_profile_admissions WHERE memory_id=?',(row['id'],)).fetchone()
        if not admission:
            continue
        all_sources,approved=_inc_candidate_sources(conn,row,admission,now)
        saying=row['content'].partition('：')[0] if row['category']=='saying' else ''
        groups=_inc_groups(all_sources,row['category'],saying)
        days=len({s['message']['date'] for s in groups})
        valid=admission['body_revision']==row['revision'] and _inc_enough(approved,row['category'],saying) and _inc_approved_proof_valid(conn,row,admission,approved)
        state=admission['state']
        if state=='accepted' and not _profile_manual(conn,row) and not valid:
            state='candidate'
        if (state,days,len(groups))!=(admission['state'],admission['independent_days'],admission['independent_uses']):
            conn.execute('UPDATE entry_profile_admissions SET state=?,independent_days=?,independent_uses=?,reason=? WHERE memory_id=?',
                (state,days,len(groups),'basis_changed' if state!=admission['state'] else admission['reason'],row['id']))


def incremental_progress(conn, entry_id):
    from .memory_ops import _profile_manual
    waiting=conn.execute("""SELECT m.* FROM entry_profile_admissions a JOIN entry_profile_memories p ON p.memory_id=a.memory_id
        JOIN memories m ON m.id=a.memory_id WHERE p.entry_id=? AND a.state!='accepted'
        AND m.lifecycle='active' AND m.purged_at IS NULL""",(entry_id,)).fetchall()
    state=_memory_state(conn,entry_id)
    rows=conn.execute('''SELECT kind,state,COUNT(*) n FROM entry_profile_incremental_work WHERE entry_id=? GROUP BY kind,state''',(entry_id,)).fetchall()
    counts={(r['kind'],r['state']):r['n'] for r in rows}
    pending=sum(counts.get(('admit',s),0) for s in ('queued','running','deferred'))
    return dict(scan_cursor=state['scan_cursor'],applied_cursor=state['cursor'],
        observation_chunks=sum(n for (k,s),n in counts.items() if k=='observe'),
        completed_chunks=counts.get(('observe','applied'),0),pending_checks=pending,
        failed_checks=counts.get(('admit','failed'),0),rejected_checks=counts.get(('admit','rejected'),0),
        observation_gaps=sum(counts.get(('observe',s),0) for s in ('queued','running','deferred','failed')),
        unscanned_messages=conn.execute('SELECT COUNT(*) FROM messages WHERE entry_id=? AND id>?',(entry_id,state['scan_cursor'])).fetchone()[0],
        waiting_candidates=sum(not _profile_manual(conn,memory) for memory in waiting),
        oldest_pending_at=conn.execute("SELECT MIN(created_at) FROM entry_profile_incremental_work WHERE entry_id=? AND state IN ('queued','running','deferred','failed')",(entry_id,)).fetchone()[0])


def _inc_directory(conn, entry_id, messages, limits):
    from .search_text import segmented
    rows=memory_profile_rows(conn,entry_id)
    pool=[{k:r[k] for k in ('memory_id','revision','category','body','manual_protected','independent_days','independent_uses','last_confirmed_at')}
          for r in rows if r['lifecycle']!='deleted']
    terms=set(segmented(' '.join(m['content'] for m in messages)).split())
    cursor=_memory_state(conn,entry_id)['scan_cursor']
    # Stable lexical selection, confirmation recency, and rotation share the directory.
    lexical=sorted(pool,key=lambda r:(-len(terms.intersection(segmented(r['body']).split())),r['memory_id']))
    recent=sorted(pool,key=lambda r:(r['last_confirmed_at'] or '',-r['memory_id']),reverse=True)
    rotated=sorted(pool,key=lambda r:r['memory_id'])
    if rotated:
        offset=cursor%len(rotated);rotated=rotated[offset:]+rotated[:offset]
    result=[];seen=set();used=0
    for triple in zip(lexical,recent,rotated):
        for row in triple:
            mid=row['memory_id'];cost=estimate_tokens(dumps(row))
            if mid in seen or len(result)>=limits.profiles or used+cost>limits.directory_tokens:
                continue
            result.append(row);seen.add(mid);used+=cost
    return result,len(pool)-len(result)


def _inc_work_persist(conn, work_id, **values):
    fields={'payload_json','outputs_json','calls_json','result_json','parsed_json','state','reason','retry_at','finished_at','attempt_id','attempts'}
    if not set(values)<=fields:
        raise ValueError('invalid work update')
    conn.execute('UPDATE entry_profile_incremental_work SET '+','.join(k+'=?' for k in values)+" WHERE id=? AND state!='cancelled'",(*values.values(),work_id))


class IncrementalProfileTask:
    def __init__(self, store, gateway, *, clock=utc_now, limits=None, attempt_id=None, source='regenerate'):
        self.store,self.gateway,self.clock=store,gateway,clock
        self.limits=limits or IncrementalProfileLimits()
        self.prompts=incremental_prompts();self.attempt_id=attempt_id;self.source=source
        self.trace=[]

    def _call(self, work, stage, payload):
        prompt=self.prompts[stage];messages=[{'role':'system','content':prompt['text']},{'role':'user','content':dumps(payload)}]
        budget=self.limits.observe_request_tokens if stage=='observe' else self.limits.admit_request_tokens
        if sum(estimate_tokens(x['content']) for x in messages)>budget:
            raise ModelError('invalid_output','request material limit',reason='request_budget_exceeded')
        monotonic=getattr(self.gateway,'monotonic',time.monotonic);deadline=monotonic()+self.limits.timeout_seconds
        outputs=json.loads(work['outputs_json']);calls=json.loads(work['calls_json']);previous=None
        for repair in range(2):
            if self.store._lock._is_owned() and self.store._writer.in_transaction:
                raise RuntimeError('model call in write transaction')
            if monotonic()>=deadline:
                raise ModelError('retryable','deadline',reason='timeout')
            health=getattr(self.gateway,'health',None)
            if health: health.check('chat','learning')
            purpose='consolidation_entry_profile_'+stage+('_repair' if repair else '')
            call=dict(purpose=purpose,prompt_version=prompt['name'],prompt_sha256=prompt['sha256'],
                      timeout_seconds=self.limits.timeout_seconds,result='success')
            started=monotonic();reply=None;invalid=None;parsed=None
            try:
                reply=self.gateway.chat(messages,purpose,max_tokens=16000,_deadline=deadline)
                if monotonic()>=deadline:
                    raise ModelError('retryable','deadline',reason='timeout')
                try:
                    if reply.finish_reason!='stop': raise ValueError('incomplete_output')
                    parsed=json_object(reply.content)
                    if stage=='observe':
                        validate_incremental_observations(parsed,self.limits)
                        _inc_validate_observation_intervals(parsed,payload)
                    else: parsed=validate_incremental_admission(parsed,payload,previous)
                except ValueError as error:
                    invalid=str(error);call['validation_error']=invalid
                if monotonic()>=deadline: raise ModelError('retryable','deadline',reason='timeout')
                if invalid is None:
                    return parsed
                if repair:
                    raise ModelError('invalid_output','invalid structured output',reason='invalid_output')
            except ModelError as error:
                if error.category=='retryable' and not error.reason and monotonic()>=deadline:
                    error.reason='timeout'
                call.update(result=error.category,reason=error.reason or error.category)
                raise
            finally:
                if reply is not None:
                    outputs[stage+('_repair' if repair else '')+('_attempt_'+str(work['attempts']) if work['attempts']>1 else '')]=reply.content
                    call['finish_reason']=reply.finish_reason
                calls.append({**call,'duration_ms':round((monotonic()-started)*1000),'usage':reply.usage if reply else {}})
                with self.store.write() as conn:
                    _inc_work_persist(conn,work['id'],outputs_json=dumps(outputs),calls_json=dumps(calls))
            previous=parsed
            messages += [{'role':'assistant','content':reply.content},{'role':'user','content':
                _inc_repair_instruction(stage,payload,invalid)}]
            if sum(estimate_tokens(x['content']) for x in messages)>self.limits.repair_request_tokens:
                raise ModelError('invalid_output','repair material limit',reason='repair_budget_exceeded')

    def _fit_observation(self, payload):
        def cost(): return estimate_tokens(self.prompts['observe']['text'])+estimate_tokens(dumps(_inc_public_payload(payload)))
        while cost()>self.limits.observe_request_tokens and payload['profiles']:
            payload['profiles'].pop()
            payload['_snapshot']['omitted_profiles']+=1
        while cost()>self.limits.observe_request_tokens and payload.get('feedback'):
            payload['feedback'].pop()
        if cost()>self.limits.observe_request_tokens: raise MemoryProfileUnavailable('observation_metadata_budget')
        return payload

    def _freeze_chunk(self, entry_id):
        now=_instant(self.clock());stamp=now.isoformat();limit=self.limits
        with self.store.write() as conn:
            _ensure(conn,entry_id,stamp)
            state=_memory_state(conn,entry_id);zone=_setting(conn,'timezone','Asia/Shanghai')
            rows=conn.execute('SELECT * FROM messages WHERE entry_id=? AND id>? ORDER BY id LIMIT ?',
                             (entry_id,state['scan_cursor'],limit.messages+1)).fetchall()
            messages=[];positions=[];skipped=[];used=0;day=None;boundary=json.loads(state['boundary_json'])
            for row in rows[:limit.messages]:
                try:
                    at=_instant(row['occurred_at']);received=_instant(row['received_at'])
                except (ValueError,TypeError):
                    positions.append(row['id']);skipped.append({'message_id':row['id'],'reason':'unparseable_time'});boundary={}
                    continue
                local=at.astimezone(ZoneInfo(zone)).date().isoformat()
                if at>now or received>now or self.source=='periodic' and local>=now.astimezone(ZoneInfo(zone)).date().isoformat():
                    break
                if day is not None and local!=day: break
                snapshot=MemoryProfileSnapshot(entry_id,'','group',zone,stamp,0,0,0,False)
                normalized=_memory_message(_db_observation(row),snapshot)
                # Every scanned turn contributes to the same-author burst boundary.
                burst='message:'+str(row['id'])
                if boundary.get('speaker')==row['sender_subject_id'] and boundary.get('at'):
                    gap=(at-_instant(boundary['at'])).total_seconds()
                    if 0<=gap<1800: burst=boundary['burst_id']
                next_boundary=dict(speaker=row['sender_subject_id'],at=row['occurred_at'],burst_id=burst)
                value={**normalized,'message_id':row['id'],'message_sha256':_message_hash(dict(row)),'burst_id':burst} if normalized else None
                cost=estimate_tokens(dumps(_inc_public_message(value))) if value else 0
                if used+cost>limit.message_tokens:
                    if not positions:
                        return self._freeze_fragments(conn,entry_id,state,value,next_boundary,local)
                    break
                positions.append(row['id']);day=local;boundary=next_boundary
                if value: messages.append(value);used+=cost
            if not positions: return None
            directory,omitted=_inc_directory(conn,entry_id,messages,limit)
            payload=dict(entry_id=entry_id,timezone=zone,as_of=stamp,messages=messages,profiles=directory,feedback=_inc_feedback(conn,entry_id),
                _snapshot=dict(settings_revision=_settings(conn,entry_id)['revision'],cursor_before=state['scan_cursor'],
                    cursor_after=positions[-1],positions=positions,omitted_profiles=omitted,skipped_messages=skipped))
            self._fit_observation(payload)
            key=_hash([entry_id,state['scan_cursor'],positions[-1],messages,zone])
            wid=conn.execute('''INSERT OR IGNORE INTO entry_profile_incremental_work(entry_id,work_key,kind,cursor_before,cursor_after,
                local_day,payload_json,attempt_id,created_at) VALUES(?,?,'observe',?,?,?,?,?,?)''',
                (entry_id,key,state['scan_cursor'],positions[-1],day,dumps(payload),self.attempt_id,stamp)).lastrowid
            conn.execute('UPDATE entry_profile_memory_state SET scan_cursor=?,boundary_json=? WHERE entry_id=?',
                         (positions[-1],dumps(boundary),entry_id))
            return wid

    def _freeze_fragments(self, conn, entry_id, state, original, boundary, day):
        """Lossless contiguous fields; all fragments reserve the same evidence identity."""
        group=_hash([entry_id,original['message_id'],original['message_sha256']])
        fragments=[]
        for field,text in [('content',original['content']),('quote',(original.get('quote') or {}).get('content',''))]:
            offset=0
            while offset<len(text):
                low,high=1,len(text)-offset
                def piece(size):
                    message=copy.deepcopy(original)
                    message['content']=text[offset:offset+size] if field=='content' else ''
                    if message.get('quote'):
                        message['quote']['content']=text[offset:offset+size] if field=='quote' else ''
                    message['fragment']=dict(field=field,start=offset,end=offset+size,total=len(text))
                    return message
                while low<high:
                    middle=(low+high+1)//2
                    if estimate_tokens(dumps(_inc_public_message(piece(middle))))<=self.limits.message_tokens: low=middle
                    else: high=middle-1
                part=piece(low)
                if estimate_tokens(dumps(_inc_public_message(part)))>self.limits.message_tokens:
                    raise MemoryProfileUnavailable('message_metadata_budget')
                fragments.append(part);offset+=low
        directory,omitted=_inc_directory(conn,entry_id,[original],self.limits)
        first=None
        for index,fragment in enumerate(fragments):
            end=original['message_id'] if index==len(fragments)-1 else state['scan_cursor']
            payload=dict(entry_id=entry_id,timezone=_setting(conn,'timezone','Asia/Shanghai'),as_of=_instant(self.clock()).isoformat(),
                messages=[fragment],profiles=directory,feedback=_inc_feedback(conn,entry_id),_snapshot=dict(settings_revision=_settings(conn,entry_id)['revision'],
                cursor_before=state['scan_cursor'],cursor_after=end,positions=[original['message_id']],
                omitted_profiles=omitted,fragment_group=group,fragment_index=index,fragment_count=len(fragments)))
            self._fit_observation(payload)
            wid=conn.execute("""INSERT INTO entry_profile_incremental_work(entry_id,work_key,kind,cursor_before,cursor_after,
                local_day,fragment_group,payload_json,attempt_id,created_at) VALUES(?,?,'observe',?,?,?,?,?,?,?)""",
                (entry_id,_hash([group,index]),state['scan_cursor'],end,day,group,dumps(payload),self.attempt_id,_instant(self.clock()).isoformat())).lastrowid
            first=first or wid
        conn.execute('UPDATE entry_profile_memory_state SET scan_cursor=?,boundary_json=? WHERE entry_id=?',
                     (original['message_id'],dumps(boundary),entry_id))
        return first

    def _resnapshot(self, entry_id):
        with self.store.write() as conn:
            zone=_setting(conn,'timezone','Asia/Shanghai');stamp=_instant(self.clock()).isoformat()
            settings=_settings(conn,entry_id)
            if not settings['enabled'] or not _runtime(conn)['enabled']: return
            for work in conn.execute("""SELECT * FROM entry_profile_incremental_work WHERE entry_id=? AND kind='observe'
                    AND state='failed' AND reason IN ('source_changed','settings_changed') AND attempts<3 ORDER BY id""",(entry_id,)).fetchall():
                payload=json.loads(work['payload_json']);snap=payload['_snapshot']
                if snap.get('fragment_group'): continue  # A changed long message requires full refragmentation; remains an explicit gap.
                snapshot=MemoryProfileSnapshot(entry_id,'','group',zone,stamp,0,0,0,False)
                messages=[]
                for mid in snap['positions']:
                    row=conn.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
                    if not row: continue
                    normalized=_memory_message(_db_observation(row),snapshot)
                    if normalized:
                        old=next((m for m in payload['messages'] if m['message_id']==mid),{})
                        messages.append({**normalized,'message_id':mid,'message_sha256':_message_hash(dict(row)),
                                         'burst_id':old.get('burst_id','message:'+str(mid))})
                if sum(estimate_tokens(dumps(_inc_public_message(m))) for m in messages)>self.limits.message_tokens: continue
                directory,omitted=_inc_directory(conn,entry_id,messages,self.limits)
                payload.update(messages=messages,profiles=directory,timezone=zone,as_of=stamp,feedback=_inc_feedback(conn,entry_id))
                snap.update(settings_revision=settings['revision'],omitted_profiles=omitted)
                self._fit_observation(payload)
                _inc_work_persist(conn,work['id'],payload_json=dumps(payload),parsed_json=None,state='queued',reason='resnapshotted')

    def _assert_snapshot(self, conn, payload):
        entry_id=payload['entry_id'];snap=payload['_snapshot']
        settings=_settings(conn,entry_id)
        if (not settings['enabled'] or not _runtime(conn)['enabled'] or settings['revision']!=snap['settings_revision']
                or _setting(conn,'timezone','Asia/Shanghai')!=payload['timezone'] or _entry(conn,entry_id)['kind'] not in ('group','live')):
            raise EntryProfileConflict('settings_changed')
        for message in payload.get('messages',[]):
            live=conn.execute('SELECT * FROM messages WHERE id=?',(message['message_id'],)).fetchone()
            if not live or _message_hash(dict(live))!=message['message_sha256']:
                raise EntryProfileConflict('source_changed')

    def _apply_observations(self, work, parsed):
        from .memory_ops import entry_profile_source, write_entry_profile_memory, _profile_manual
        payload=json.loads(work['payload_json']);entry_id=work['entry_id'];now=_instant(self.clock())
        result=dict(generated=parsed['observations'],deterministic=[],removed=[],applied=[],normalizations=[])
        with self.store.write() as conn:
            self._assert_snapshot(conn,payload)
            if conn.execute('SELECT state FROM entry_profile_incremental_work WHERE id=?',(work['id'],)).fetchone()[0]=='cancelled':
                raise EntryProfileConflict('purged')
            directory={m['memory_id']:m for m in payload['profiles']}
            for index,raw in enumerate(parsed['observations'],1):
                conn.execute('SAVEPOINT profile_observation')
                try:
                    op=raw['op'];target=None
                    if op!='propose':
                        declared=directory.get(raw['memory_id'])
                        target=conn.execute('''SELECT m.*,p.category FROM memories m JOIN entry_profile_memories p ON p.memory_id=m.id
                            WHERE m.id=? AND p.entry_id=? AND m.lifecycle!='deleted' AND m.purged_at IS NULL''',
                            (raw['memory_id'],entry_id)).fetchone()
                        if not declared or not target or target['revision']!=declared['revision']:
                            raise ValueError('target_revision_changed')
                    category=raw['category'] if op!='confirm' else target['category']
                    body=whitespace(raw['body']) if op!='confirm' else target['content']
                    saying=raw.get('saying','') if op!='confirm' else body.partition('：')[0] if category=='saying' else ''
                    meaning=raw.get('meaning','') if op!='confirm' else body.partition('：')[2] if category=='saying' else ''
                    if op!='confirm':
                        if raw.get('action')=='retire':
                            if body or saying or meaning or raw['definition_refs'] or category!=target['category']:
                                raise ValueError('invalid_retirement')
                        elif (not (saying if category=='saying' else body) or _inc_gendered(saying+meaning if category=='saying' else body) or _ACCOUNT.search(saying+meaning if category=='saying' else body)):
                            raise ValueError('invalid_or_gendered_body')
                        elif category=='saying':
                            expected=saying+('：'+meaning if meaning else '')
                            if not saying or bool(meaning)!=bool(raw['definition_refs']):
                                raise ValueError('unbound_meaning')
                            if body!=expected:
                                result['normalizations'].append(dict(index=index,reason='saying_from_fields'))
                            body=expected
                        elif saying or meaning or raw['definition_refs']:
                            raise ValueError('unexpected_meaning')
                    repairs=[]
                    bindings=_inc_bound(conn,entry_id,raw['source_refs'],payload['messages'],repairs=repairs)
                    result['normalizations'].extend(dict(index=index,**r) for r in repairs)
                    if any(mid not in [b['message_id'] for b in bindings] for mid in raw.get('definition_refs',[])):
                        raise ValueError('unbound_definition')
                    canonical=[]
                    for binding in bindings:
                        msg=next(m for m in payload['messages'] if m['message_id']==binding['message_id'])
                        bound=_memory_bind_source(dict(ref=msg['ref'],field=binding['field'],excerpt=binding['excerpt'],via_memory=None),
                                                  dict(messages=payload['messages'],memories=[]))
                        bound['burst_id']=msg.get('burst_id');canonical.append(bound)
                    if category=='rule' and op=='propose' and _inc_rule_reason(canonical):
                        raise ValueError('rule_scope_not_established')
                    if op=='change':
                        if _profile_manual(conn,target): raise ValueError('protected_manual_conflict')
                        proposal={**raw,'body':body,'source_refs':[{k:b[k] for k in ('message_id','field','excerpt')} for b in bindings]}
                        self._queue_admission(conn,dict(target),_inc_all_sources(conn,target,now.isoformat())+canonical,proposal=proposal)
                        result['deterministic'].append({'index':index,**raw,'sources':canonical})
                        continue
                    if target is None:
                        target=conn.execute('''SELECT m.*,p.category FROM memories m JOIN entry_profile_memories p ON p.memory_id=m.id
                            WHERE p.entry_id=? AND p.category IS ? AND m.content=? AND m.purged_at IS NULL ORDER BY m.id LIMIT 1''',
                            (entry_id,category,body)).fetchone()
                    if target and target['lifecycle']=='deleted': raise ValueError('deleted_target')
                    old_sources=_inc_all_sources(conn,target,now.isoformat()) if target else []
                    chosen=_inc_groups(old_sources+canonical,category,saying)
                    current_ids={b['message_id'] for b in bindings}
                    group_keys=['message:'+str(s['message']['position']) for s in chosen if s['message']['position'] in current_ids]
                    receipt=write_entry_profile_memory(self.store,entry_id,op='confirm' if target else 'new',content=body,
                        category=category,importance=30,sources=bindings,memory_id=target['id'] if target else None,
                        expected_revision=target['revision'] if target else None,confirmation_groups=group_keys,
                        accumulate_only=bool(target and any(x['memory_id']==target['id'] and (x['confirmed'] or x['op']=='new') for x in result['applied'])),
                        current=now,_conn=conn)
                    mid=receipt['memory_id']
                    for sid in receipt['source_ids']:
                        source_row=conn.execute('SELECT message_id FROM sources WHERE id=?',(sid,)).fetchone()
                        message=next(m for m in payload['messages'] if m['message_id']==source_row[0])
                        conn.execute('INSERT OR IGNORE INTO entry_profile_source_groups VALUES(?,?,?)',(sid,mid,message['burst_id']))
                    if receipt['source_ids']:
                        conn.execute('UPDATE entry_profile_admissions SET evidence_revision=evidence_revision+1 WHERE memory_id=?',(mid,))
                    if not target:
                        conn.execute('UPDATE entry_profile_admissions SET definition_refs_json=? WHERE memory_id=?',
                                     (dumps(raw.get('definition_refs',[])),mid))
                    result['applied'].append({'index':index,**receipt})
                    result['deterministic'].append({'index':index,'memory_id':mid,**raw,'effective_body':body,'sources':canonical})
                except (ValueError,EntryProfileConflict) as error:
                    conn.execute('ROLLBACK TO profile_observation')
                    result['removed'].append(dict(index=index,reason=str(error)))
                finally:
                    conn.execute('RELEASE profile_observation')
            _inc_work_persist(conn,work['id'],state='applied',result_json=dumps(result),finished_at=now.isoformat())
            # Make the enclosing attempt depend on every created/confirmed target before a check can run.
            if self.attempt_id is not None:
                previous=json.loads(conn.execute('SELECT material_json FROM entry_profile_attempts WHERE id=?',(self.attempt_id,)).fetchone()[0])
                previous.setdefault('evidence_refs',[]).extend({'memory_id':r['memory_id'],'revision':r['revision']} for r in result['applied'])
                conn.execute('UPDATE entry_profile_attempts SET material_json=? WHERE id=?',(dumps(previous),self.attempt_id))
            self._advance_cursor(conn,entry_id)
        return result

    def _advance_cursor(self, conn, entry_id):
        cursor=_memory_state(conn,entry_id)['cursor']
        for row in conn.execute("SELECT cursor_before,cursor_after,state,fragment_group FROM entry_profile_incremental_work WHERE entry_id=? AND kind='observe' AND cursor_after>? ORDER BY cursor_before,id",(entry_id,cursor)):
            if row['cursor_before']!=cursor or row['state'] not in ('applied','cancelled'):
                break
            if row['fragment_group'] and conn.execute("SELECT 1 FROM entry_profile_incremental_work WHERE fragment_group=? AND state NOT IN ('applied','cancelled') LIMIT 1",(row['fragment_group'],)).fetchone():
                break
            cursor=row['cursor_after']
        conn.execute('UPDATE entry_profile_memory_state SET cursor=?,rebuild_pending=? WHERE entry_id=?',
            (cursor,int(bool(conn.execute('SELECT 1 FROM messages WHERE entry_id=? AND id>? LIMIT 1',(entry_id,cursor)).fetchone())),entry_id))

    def _proof_payload(self, memory, snapshot, mode, stamp, zone):
        return dict(entry_id=snapshot['entry_id'],as_of=stamp,timezone=zone,memory=memory,
            review_contract='semantic_v8',mode=mode,_snapshot=snapshot)

    def _proof_fits(self, memory, snapshot, mode, stamp, zone):
        payload=self._proof_payload(memory,snapshot,mode,stamp,zone)
        return (len(memory['sources'])<=_INC_ADMISSION_SOURCE_LIMIT
            and estimate_tokens(dumps(memory))<=self.limits.evidence_tokens
            and estimate_tokens(self.prompts['admit']['text'])+estimate_tokens(dumps(_inc_public_payload(payload)))<=self.limits.admit_request_tokens)

    def _persist_proof_work(self, conn, memory, snapshot, mode, stamp, zone, *, group, index=0, count=1):
        snap={**snapshot,'admission_prompt_sha256':self.prompts['admit']['sha256']}
        if mode=='map': snap['bundle']=dict(key=group,index=index,count=count)
        payload=self._proof_payload(memory,snap,mode,stamp,zone)
        key=_hash([group,mode,index,memory,self.prompts['admit']['sha256']])
        if conn.execute('SELECT 1 FROM entry_profile_incremental_work WHERE work_key=?',(key,)).fetchone(): return None
        return conn.execute('''INSERT INTO entry_profile_incremental_work(entry_id,work_key,kind,memory_id,memory_revision,
            payload_json,attempt_id,created_at) VALUES(?,?,'admit',?,?,?,?,?)''',
            (snap['entry_id'],key,memory['memory_id'],memory['revision'],dumps(payload),self.attempt_id,stamp)).lastrowid

    def _queue_admission(self, conn, row, sources, *, proposal=None):
        from .memory_ops import _profile_manual
        if _profile_manual(conn,row) or row['lifecycle']!='active': return None
        meta=conn.execute('SELECT * FROM entry_profile_admissions WHERE memory_id=?',(row['id'],)).fetchone()
        if not meta: return None
        if meta['state']=='accepted' and not proposal and meta['signature'] and meta['body_revision']==row['revision']:
            previous=conn.execute('SELECT payload_json FROM entry_profile_incremental_work WHERE work_key=?',(meta['signature'],)).fetchone()
            snap=json.loads(previous[0]).get('_snapshot',{}) if previous else {}
            if (snap.get('evidence_revision')==meta['evidence_revision'] and
                    snap.get('admission_prompt_sha256')==self.prompts['admit']['sha256']): return None
        for source in sources:
            if conn.execute('''SELECT 1 FROM entry_profile_incremental_work w JOIN purge_record_refs r
                ON r.table_name='entry_profile_incremental_work' AND r.row_id=CAST(w.id AS TEXT)
                WHERE r.ref_kind='message' AND r.ref_id=? AND w.fragment_group IS NOT NULL
                AND w.state NOT IN ('applied','cancelled') LIMIT 1''',(str(source['message']['position']),)).fetchone(): return None
        category=proposal['category'] if proposal else row['category'];body=proposal['body'] if proposal else row['content']
        saying=proposal['saying'] if proposal else body.partition('：')[0] if category=='saying' else ''
        retire=proposal and proposal['action']=='retire'
        if not _inc_enough(sources,category,saying) and not (retire and sources): return None
        reason=None if retire else _inc_body_reason(category,body,saying,sources)
        definitions=proposal['definition_refs'] if proposal else json.loads(meta['definition_refs_json'])
        witnesses=list({_inc_source_key(s):s for s in sources}.values())
        witnesses.sort(key=lambda s:(_instant(s['message']['occurred_at']),s['message']['position'],s['field'],s['excerpt']))
        if len(witnesses)>_INC_ADMISSION_SOURCE_LIMIT: reason='proof_gap:source_limit'
        if reason:
            conn.execute('UPDATE entry_profile_admissions SET reason=? WHERE memory_id=?',(reason,row['id']))
            return None
        try:
            memory=_inc_proof_memory(row,witnesses,category,body,saying,definitions,proposal)
        except ValueError as error:
            conn.execute('UPDATE entry_profile_admissions SET reason=? WHERE memory_id=?',('proof_gap:'+str(error),row['id']))
            return None
        zone=_setting(conn,'timezone','Asia/Shanghai');stamp=_instant(self.clock()).isoformat()
        snapshot=dict(entry_id=row['owner_entry_id'],settings_revision=_settings(conn,row['owner_entry_id'])['revision'],
            evidence_revision=meta['evidence_revision'],proposal=proposal,
            source_fingerprints={str(s['message']['position']):s['message']['fingerprint'] for s in witnesses},
            evidence_sources=memory['sources'],evidence_refs=[dict(message_id=s['message']['position']) for s in witnesses])
        group=_hash([memory,meta['evidence_revision'],zone,snapshot['settings_revision'],self.prompts['admit']['sha256']])
        first=None
        if self._proof_fits(memory,snapshot,'final',stamp,zone):
            first=self._persist_proof_work(conn,memory,snapshot,'final',stamp,zone,group=group)
        else:
            # Pages only map local contribution. They are not acceptance votes on
            # a whole body whose proof is split across other pages.
            pages=[];page=[]
            for s in memory['sources']:
                if page and not self._proof_fits(_inc_memory_page(memory,page+[s]),snapshot,'map',stamp,zone):
                    pages.append(page);page=[]
                page.append(s)
                if not self._proof_fits(_inc_memory_page(memory,page),snapshot,'map',stamp,zone):
                    reason='proof_gap:single_source_budget';break
            if page: pages.append(page)
            if len(pages)>8: reason='proof_gap:page_limit'
            if reason:
                conn.execute('UPDATE entry_profile_admissions SET reason=? WHERE memory_id=?',(reason,row['id']))
                return None
            for index,page in enumerate(pages):
                wid=self._persist_proof_work(conn,_inc_memory_page(memory,page),snapshot,'map',stamp,zone,
                    group=group,index=index,count=len(pages))
                first=first or wid
        if first and meta['state']!='accepted':
            conn.execute("UPDATE entry_profile_admissions SET state='pending',reason=NULL WHERE memory_id=?",(row['id'],))
        return first

    def _apply_mapping(self, work, parsed):
        payload=json.loads(work['payload_json']);memory=payload['memory'];snap=payload['_snapshot'];bundle=snap['bundle']
        stamp=_instant(self.clock()).isoformat()
        parsed=validate_incremental_admission(parsed,_inc_public_payload(payload))
        with self.store.write() as conn:
            if not self._check_current(conn,work,payload): raise EntryProfileConflict('check_snapshot_changed')
            _inc_work_persist(conn,work['id'],state='applied',finished_at=stamp,
                result_json=dumps(dict(semantic_decision=parsed['decision'],admission_complete=False,writeback='not_attempted',mode='map')))
            siblings=conn.execute("SELECT * FROM entry_profile_incremental_work WHERE memory_id=? AND json_extract(payload_json,'$._snapshot.bundle.key')=? ORDER BY id",
                (memory['memory_id'],bundle['key'])).fetchall()
            if len(siblings)!=bundle['count'] or any(s['state']!='applied' for s in siblings): return
            all_memory=_inc_memory_page(memory,snap['evidence_sources'])
            votes=[validate_incremental_admission(json.loads(s['parsed_json']),_inc_public_payload(json.loads(s['payload_json']))) for s in siblings]
            base={k:v for k,v in snap.items() if k!='bundle'}
            selected,reason=_inc_plan_proof(all_memory,votes,
                lambda ss:self._proof_fits(_inc_memory_page(all_memory,ss),base,'final',stamp,payload['timezone']))
            if reason:
                conn.execute("UPDATE entry_profile_admissions SET state=CASE WHEN state='accepted' THEN state ELSE 'candidate' END,reason=? WHERE memory_id=?",
                    (reason,memory['memory_id']))
                _inc_work_persist(conn,work['id'],result_json=dumps(dict(semantic_decision=parsed['decision'],admission_complete=False,
                    writeback='blocked',proof_gap=reason,required_points=memory['proof_points'])))
                return
            self._persist_proof_work(conn,_inc_memory_page(all_memory,selected),base,'final',stamp,payload['timezone'],group=bundle['key'])

    def _queue_candidates(self, entry_id):
        with self.store.write() as conn:
            rows=conn.execute('''SELECT m.*,p.category FROM memories m JOIN entry_profile_memories p ON p.memory_id=m.id
                JOIN entry_profile_admissions a ON a.memory_id=m.id WHERE p.entry_id=?
                AND m.lifecycle='active' AND m.purged_at IS NULL ORDER BY m.id''',(entry_id,)).fetchall()
            for row in rows:
                self._queue_admission(conn,row,_inc_all_sources(conn,row,_instant(self.clock()).isoformat()))

    def _apply_admission(self, work, parsed):
        from .memory_ops import write_entry_profile_memory, entry_profile_source, _profile_manual
        payload=json.loads(work['payload_json']);memory=payload['memory'];snap=payload['_snapshot'];stamp=_instant(self.clock()).isoformat()
        if payload.get('mode')!='final': raise ValueError('mapping_cannot_admit')
        parsed=validate_incremental_admission(parsed,_inc_public_payload(payload))
        with self.store.write() as conn:
            current=conn.execute('''SELECT m.*,p.category FROM memories m JOIN entry_profile_memories p ON p.memory_id=m.id
                WHERE m.id=? AND m.purged_at IS NULL AND m.lifecycle='active' ''',(memory['memory_id'],)).fetchone()
            if (not current or current['revision']!=memory['revision'] or _profile_manual(conn,current)
                    or conn.execute('SELECT state FROM entry_profile_incremental_work WHERE id=?',(work['id'],)).fetchone()[0]=='cancelled'):
                raise EntryProfileConflict('target_changed')
            settings=_settings(conn,work['entry_id']);meta=conn.execute('SELECT * FROM entry_profile_admissions WHERE memory_id=?',(current['id'],)).fetchone()
            if (not settings['enabled'] or not _runtime(conn)['enabled'] or settings['revision']!=snap['settings_revision']
                    or _setting(conn,'timezone','Asia/Shanghai')!=payload['timezone'] or meta['evidence_revision']!=snap['evidence_revision']):
                raise EntryProfileConflict('check_snapshot_changed')
            all_sources=_inc_all_sources(conn,current,stamp)
            if snap.get('proposal'):
                snapshot=MemoryProfileSnapshot(work['entry_id'],'','group',payload['timezone'],stamp,0,0,0,False)
                all_sources=[]
                values=list(snap.get('evidence_sources',memory['sources']))
                for value in values:
                    row=conn.execute('SELECT * FROM messages WHERE id=?',(value['message_id'],)).fetchone()
                    if row:
                        normalized=_memory_message(_db_observation(row),snapshot)
                        if normalized:
                            s=_memory_bind_source(dict(ref=normalized['ref'],field=value['field'],excerpt=value['excerpt'],via_memory=None),dict(messages=[normalized],memories=[]))
                            s['burst_id']=value.get('burst_id');all_sources.append(s)
            for mid,fingerprint in snap['source_fingerprints'].items():
                if not any(s['message']['position']==int(mid) and s['message']['fingerprint']==fingerprint for s in all_sources):
                    raise EntryProfileConflict('check_source_changed')
            if parsed['decision']!='accept':
                # A pending revision does not erase an independently valid old body.
                if not snap.get('proposal') or meta['state']!='accepted':
                    conn.execute("UPDATE entry_profile_admissions SET state=?,reason=? WHERE memory_id=?",('candidate' if meta['signature'] else 'rejected',parsed['decision'],current['id']))
                _inc_work_persist(conn,work['id'],state='rejected',reason=parsed['decision'],result_json=dumps(parsed),finished_at=stamp)
                return dict(memory_id=current['id'],decision=parsed['decision'])
            safe=[];canonical=[]
            for source in parsed['safe_sources']:
                bound=next((s for s in all_sources if s['message']['position']==source['message_id'] and s['field']==source['field']
                    and whitespace(source['excerpt']) in s['excerpt']),None)
                expected=next((s for s in memory['sources'] if s['message_id']==source['message_id'] and s['field']==source['field']
                    and whitespace(source['excerpt']) in s['excerpt']),None)
                if not bound or not expected: raise ValueError('checker_unbound_excerpt')
                canonical.append({**bound,'excerpt':whitespace(source['excerpt'])})
                safe.append(entry_profile_source(conn,source['message_id'],excerpt=source['excerpt'],field=source['field'],
                    source_memory_id=int(bound['via_memory']) if bound['via_memory'] else None,
                    source_revision=bound['memory']['revision'] if bound['memory'] else None))
            proposal=snap.get('proposal');retire=proposal and proposal['action']=='retire'
            if not retire and (reason:=_inc_body_reason(memory['category'],memory['body'],memory['saying'],canonical)):
                raise ValueError(reason)
            if not retire and not _inc_enough(canonical,memory['category'],memory['saying']):
                raise ValueError('safe_sources_insufficient')
            if memory['meaning'] and not set(memory['definition_refs'])&{s['message_id'] for s in safe}:
                raise ValueError('definition_removed')
            if proposal and not any(s['message_id']==ref['message_id'] and s['field']==ref['field'] for s in safe for ref in proposal['source_refs']):
                raise ValueError('change_basis_removed')
            if proposal:
                receipt=write_entry_profile_memory(self.store,work['entry_id'],op='retire' if retire else 'revise',content=memory['body'],
                    category=memory['category'],importance=current['importance'],sources=safe,memory_id=current['id'],
                    expected_revision=current['revision'],_conn=conn,current=_instant(stamp))
                revision=receipt['revision']
                if retire:
                    conn.execute("UPDATE entry_profile_admissions SET state='candidate',reason='retired' WHERE memory_id=?",(current['id'],))
                    _inc_work_persist(conn,work['id'],state='applied',result_json=dumps(receipt),finished_at=stamp)
                    return receipt
            else: revision=current['revision']
            receipt=write_entry_profile_memory(self.store,work['entry_id'],op='admit',sources=safe,memory_id=current['id'],
                expected_revision=revision,_conn=conn,current=_instant(stamp))
            conn.execute("""UPDATE entry_profile_admissions SET state='accepted',body_revision=?,checked_sources_json=?,
                signature=?,reason=NULL,definition_refs_json=?,checked_at=? WHERE memory_id=?""",
                (receipt['revision'],dumps(receipt['source_ids']),work['work_key'],dumps(memory['definition_refs']),stamp,current['id']))
            _inc_work_persist(conn,work['id'],state='applied',result_json=dumps({'decision':'accept','semantic_decision':'accept','writeback':'applied','admission_complete':True,**receipt}),finished_at=stamp)
            conn.execute("UPDATE entry_profile_incremental_work SET state='cancelled',reason='superseded_check',finished_at=? WHERE memory_id=? AND kind='admit' AND id!=? AND state IN ('queued','deferred','failed')",
                         (stamp,current['id'],work['id']))
            return receipt

    def _check_current(self, conn, work, payload):
        """Cheap preflight in addition to post-call CAS; never spend on an already stale target."""
        from .memory_ops import _profile_manual
        memory=payload['memory'];snap=payload['_snapshot']
        if payload.get('review_contract')!='semantic_v8' or snap.get('admission_prompt_sha256')!=self.prompts['admit']['sha256']: return False
        row=conn.execute('SELECT * FROM memories WHERE id=?',(memory['memory_id'],)).fetchone()
        meta=conn.execute('SELECT * FROM entry_profile_admissions WHERE memory_id=?',(memory['memory_id'],)).fetchone()
        if (not row or not meta or row['purged_at'] or row['lifecycle']!='active' or _profile_manual(conn,row)
                or row['revision']!=memory['revision'] or meta['evidence_revision']!=snap['evidence_revision']
                or row['owner_entry_id']!=work['entry_id'] or not Visibility(conn).memory_visible(row['id'],work['entry_id'])):
            return False
        settings=_settings(conn,work['entry_id'])
        if (not settings['enabled'] or not _runtime(conn)['enabled'] or settings['revision']!=snap['settings_revision']
                or _setting(conn,'timezone','Asia/Shanghai')!=payload['timezone']): return False
        snapshot=MemoryProfileSnapshot(work['entry_id'],'','group',payload['timezone'],_instant(self.clock()).isoformat(),0,0,0,False)
        for mid,fingerprint in snap['source_fingerprints'].items():
            source=conn.execute('SELECT * FROM messages WHERE id=?',(int(mid),)).fetchone()
            normalized=_memory_message(_db_observation(source),snapshot) if source else None
            if not normalized or normalized['fingerprint']!=fingerprint: return False
        return True

    def _run_one(self, work):
        stamp=_instant(self.clock()).isoformat()
        with self.store.write() as conn:
            live=conn.execute('SELECT * FROM entry_profile_incremental_work WHERE id=?',(work['id'],)).fetchone()
            if not live or live['state'] in ('applied','rejected','cancelled'): return
            _inc_work_persist(conn,work['id'],state='running',attempts=live['attempts']+1,attempt_id=self.attempt_id)
            work=dict(conn.execute('SELECT * FROM entry_profile_incremental_work WHERE id=?',(work['id'],)).fetchone())
            if work['state']=='cancelled': return
            if self.attempt_id is not None:
                material=json.loads(conn.execute('SELECT material_json FROM entry_profile_attempts WHERE id=?',(self.attempt_id,)).fetchone()[0])
                payload=json.loads(work['payload_json']);refs=material.setdefault('evidence_refs',[])
                refs.extend(dict(message_id=m['message_id']) for m in payload.get('messages',[]))
                refs.extend(ref for item in payload.get('feedback',[]) for ref in item.get('_refs',[]))
                refs.extend(dict(memory_id=p['memory_id'],revision=p['revision']) for p in payload.get('profiles',[]))
                if payload.get('memory'):
                    refs.append(dict(memory_id=payload['memory']['memory_id'],revision=payload['memory']['revision']))
                    refs.extend(dict(message_id=s['message_id']) for s in payload['memory']['sources'])
                material['incremental']=True
                conn.execute('UPDATE entry_profile_attempts SET material_json=? WHERE id=?',(dumps(material),self.attempt_id))
        try:
            payload=json.loads(work['payload_json'])
            if any(m.get('oversized') for m in payload.get('messages',[])):
                raise ModelError('invalid_output','single message exceeds budget',reason='oversized_message_pending')
            with self.store.read() as conn:
                if work['kind']=='observe': self._assert_snapshot(conn,payload)
                stale=work['kind']=='admit' and not self._check_current(conn,work,payload)
            if stale:
                with self.store.write() as conn:
                    _inc_work_persist(conn,work['id'],state='cancelled',reason='stale_check',finished_at=stamp)
                    conn.execute("UPDATE entry_profile_admissions SET state=CASE WHEN state='accepted' THEN state ELSE 'candidate' END,reason='stale_check' WHERE memory_id=?",(work['memory_id'],))
                return
            public=_inc_public_payload(payload)
            if work['parsed_json'] is None:
                parsed={'observations':[]} if work['kind']=='observe' and not payload.get('messages') else self._call(work,work['kind'],public)
                with self.store.write() as conn:
                    _inc_work_persist(conn,work['id'],parsed_json=dumps(parsed))
            else:
                parsed=json.loads(work['parsed_json'])
            if work['kind']=='observe': self._apply_observations(work,parsed)
            elif payload.get('mode')=='map': self._apply_mapping(work,parsed)
            else: self._apply_admission(work,parsed)
        except (ModelError,EntryProfileConflict,ValueError) as error:
            reason=(error.reason or error.category) if isinstance(error,ModelError) else str(error)
            retry=isinstance(error,ModelError) and (error.category in ('retryable','paused','budget') or reason=='interrupted')
            with self.store.write() as conn:
                live=conn.execute('SELECT * FROM entry_profile_incremental_work WHERE id=?',(work['id'],)).fetchone()
                deferred=retry and (live['attempts']<=2 or reason=='call_budget' or isinstance(error,ModelError) and error.category in ('paused','budget'))
                _inc_work_persist(conn,work['id'],state='deferred' if deferred else 'failed',reason=reason,
                    retry_at=(_instant(self.clock())+timedelta(minutes=5*(2**min(live['attempts'],4)))).isoformat() if deferred else None,
                    finished_at=stamp)
                if work['kind']=='admit' and live['parsed_json']:
                    saved=json.loads(live['parsed_json'])
                    _inc_work_persist(conn,work['id'],result_json=dumps(dict(semantic_decision=saved.get('decision'),
                        writeback='failed',admission_complete=False,reason=reason)))
                if work['kind']=='admit' and live['state']!='cancelled':
                    conn.execute("UPDATE entry_profile_admissions SET state=CASE WHEN state='accepted' THEN state ELSE 'candidate' END,reason=? WHERE memory_id=?",
                                 (reason,work['memory_id']))
            if reason in ('timeout','rate_limited'):
                self.transport_error=reason
            if reason in ('call_budget','interrupted') or isinstance(error,ModelError) and error.paused:
                self.stop_reason=reason
        finally:
            with self.store.read() as conn:
                self.trace.append(dict(conn.execute('SELECT * FROM entry_profile_incremental_work WHERE id=?',(work['id'],)).fetchone()))

    def run(self, entry_id):
        self.transport_error=None;self.stop_reason=None;seen=set()
        with self.store.write() as conn:
            _ensure(conn,entry_id,_instant(self.clock()).isoformat())
        self._queue_candidates(entry_id)
        while True:
            progress=False
            for kind in ('observe','admit'):
                with self.store.read() as conn:
                    work=conn.execute("""SELECT * FROM entry_profile_incremental_work WHERE entry_id=? AND kind=?
                        AND (state='queued' OR state='deferred' AND retry_at<=?) ORDER BY id LIMIT 1""",
                        (entry_id,kind,_instant(self.clock()).isoformat())).fetchone()
                if kind=='observe' and not work:
                    wid=self._freeze_chunk(entry_id)
                    if wid:
                        with self.store.read() as conn:
                            work=conn.execute('SELECT * FROM entry_profile_incremental_work WHERE id=?',(wid,)).fetchone()
                if work and work['id'] not in seen:
                    seen.add(work['id']);self._run_one(dict(work));progress=True
                    if kind=='observe': self._queue_candidates(entry_id)
                if self.stop_reason or self.transport_error and getattr(self.gateway,'abort_on_model_transport_error',False):
                    break
            if getattr(self,'one_round',False) or not progress or self.stop_reason or self.transport_error and getattr(self.gateway,'abort_on_model_transport_error',False):
                break
        with self.store.read() as conn:
            stats=incremental_progress(conn,entry_id)
        calls=[c for w in self.trace for c in json.loads(w['calls_json'])]
        return dict(status='committed' if any(w['state'] in ('applied','rejected') for w in self.trace) else 'failed' if self.trace else 'no_new_evidence',
            reason=self.transport_error or self.stop_reason or next((w['reason'] for w in self.trace if w['state'] in ('failed','deferred')),None),
            work=self.trace,progress=stats,calls=calls,prompt_versions=[p['name'] for p in self.prompts.values()],
            prompt_sha256={p['name']:p['sha256'] for p in self.prompts.values()})


def _reserve_incremental_task(engine, entry_id, expected, source, *, run_id=None, fail_if_running=False):
    stamp=_instant(engine.clock()).isoformat();store=engine.store
    with store.write() as conn:
        _memory_expect(conn,entry_id,expected)
        _recover_interrupted(conn,store,entry_id,stamp)
        if conn.execute("SELECT 1 FROM entry_profile_attempts WHERE entry_id=? AND state='running'",(entry_id,)).fetchone():
            raise EntryProfileBusy('generation_in_progress')
        # Service/offline leases exclude a second owner; completed responses can resume without a call.
        conn.execute("UPDATE entry_profile_incremental_work SET state='queued' WHERE entry_id=? AND state='running'",(entry_id,))
        if run_id is not None:
            previous=conn.execute('SELECT * FROM entry_profile_attempts WHERE entry_id=? AND run_id=?',(entry_id,run_id)).fetchone()
        _ensure(conn,entry_id,stamp)
        settings=_settings(conn,entry_id);entry=_entry(conn,entry_id)
        reason='entry_kind' if entry['kind'] not in ('group','live') else 'disabled' if not settings['enabled'] or not _runtime(conn)['enabled'] else None
        zone=_setting(conn,'timezone','Asia/Shanghai');today=_instant(engine.clock()).astimezone(ZoneInfo(zone)).date().isoformat()
    due=dict(local_day=today,due=not reason,reason=reason,current_version=expected)
    if reason: return engine._skip(entry_id,expected,source,due,reason,run_id,stamp)
    try: gateway=engine._gateway()
    except ModelError as error:
        return engine._skip(entry_id,expected,source,due,error.reason or error.category,run_id,stamp)
    task=IncrementalProfileTask(store,gateway,clock=engine.clock,source=source)
    task.one_round=run_id is not None
    task._resnapshot(entry_id)
    task._queue_candidates(entry_id)
    with store.read() as conn:
        queued=conn.execute("SELECT 1 FROM entry_profile_incremental_work WHERE entry_id=? AND (state='queued' OR state='deferred' AND retry_at<=?) LIMIT 1",(entry_id,stamp)).fetchone()
    if not queued and not task._freeze_chunk(entry_id):
        return engine._skip(entry_id,expected,source,due,'no_new_evidence',run_id,stamp)
    with store.write() as conn:
        _memory_expect(conn,entry_id,expected)
        if conn.execute("SELECT 1 FROM entry_profile_attempts WHERE entry_id=? AND state='running'",(entry_id,)).fetchone():
            raise EntryProfileBusy('generation_in_progress')
        if run_id is not None:
            old=conn.execute('SELECT id FROM entry_profile_attempts WHERE entry_id=? AND run_id=?',(entry_id,run_id)).fetchone()
        else: old=None
        if old:
            attempt=old[0]
            conn.execute("UPDATE entry_profile_attempts SET state='running',reason=NULL,finished_at=NULL WHERE id=?",(attempt,))
        else:
            attempt=conn.execute('''INSERT INTO entry_profile_attempts(entry_id,run_id,base_version,state,local_day,started_at,material_json)
                VALUES(?,?,?,'running',?,?,?)''',(entry_id,run_id,expected,today,stamp,dumps({'incremental':True,'evidence_refs':[]}))).lastrowid
        task.attempt_id=attempt
        _audit(conn,'generation_requested',entry_id,stamp,attempt_id=attempt,base_version=expected,source=source)
        with _ACTIVE_LOCK: _ACTIVE_ATTEMPTS.add((store.path,attempt))
    return dict(status='running',attempt_id=attempt,context=dict(entry_id=entry_id,attempt=attempt,incremental_task=task,due=due))


def _execute_incremental_task(engine, entry_id, attempt, task, due):
    try:
        result=task.run(entry_id)
        status={'committed':'published','no_new_evidence':'skipped'}.get(result['status'],result['status'])
        with engine.store.write() as conn:
            version=_memory_state(conn,entry_id)['version']
            previous=json.loads(conn.execute('SELECT outputs_json FROM entry_profile_attempts WHERE id=?',(attempt,)).fetchone()[0])
            persisted={**result,'work':previous.get('work',[])+result['work'],'calls':previous.get('calls',[])+result['calls']}
            # Existing attempt redaction guards also cover linked incremental work.
            conn.execute('''UPDATE entry_profile_attempts SET state=?,reason=?,version=?,outputs_json=?,calls_json=?,finished_at=?
                WHERE id=? AND state='running' ''',(status,result.get('reason'),version,dumps(persisted),dumps(persisted['calls']),
                _instant(engine.clock()).isoformat(),attempt))
        return dict(status=status,reason=result.get('reason'),version={'version':version},attempt_id=attempt,due=due,result=result)
    except Exception:
        engine._finish(attempt,'failed','unexpected_error',{},[])
        raise
    finally:
        with _ACTIVE_LOCK: _ACTIVE_ATTEMPTS.discard((engine.store.path,attempt))


def _update_incremental_for_run(store, gateway, run, config, *, clock=utc_now, stop=None):
    if not config.get('entry_profile_enabled',True): return True
    with store.read() as conn:
        if not _runtime(conn)['enabled']: return True
        entries=[r[0] for r in conn.execute("SELECT id FROM entries WHERE kind IN ('group','live') ORDER BY id")]
    remaining=entries
    while remaining:
        next_round=[]
        for entry_id in remaining:
            if stop and stop(): return False
            with store.read() as conn: expected=_settings(conn,entry_id)['current_version']
            try:
                outcome=EntryProfileEngine(store,gateway,clock=clock).update(entry_id,expected_version=expected,run_id=run['id'])
            except (EntryProfileConflict,EntryProfileBusy):
                continue
            result=outcome.get('result',{})
            with store.write() as conn:
                conn.execute("""INSERT INTO maintenance_items(run_id,phase,item_key,object_id,outcome,reason,details_json,created_at)
                    VALUES(?,'consolidation',?,?,'entry_profile',?,?,?)
                    ON CONFLICT(run_id,phase,item_key) DO UPDATE SET reason=excluded.reason,details_json=excluded.details_json""",
                    (run['id'],'entry_profile:'+entry_id,outcome['attempt_id'],outcome.get('reason'),
                     dumps({'entry_id':entry_id,'status':outcome['status'],'progress':incremental_progress(conn,entry_id)}),_instant(clock()).isoformat()))
            if result.get('reason') in ('call_budget','interrupted'):
                return result['reason']!='interrupted'
            if result.get('work'): next_round.append(entry_id)
        remaining=next_round
    return True
