"""Entry-local profiles derived from original evidence; bounded IO and version CAS.

No reply/learning integration lives here. Reading a profile never regenerates it.
Evidence IDs are snapshots, not foreign keys that pin cleaned source messages.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from importlib.resources import files
from threading import Lock
from zoneinfo import ZoneInfo

from .db import dumps
from .memory_ops import Visibility, operation
from .models import Gateway, ModelError
from .persona import split_sentences
from .queue import estimate_tokens, truncate_material

DEFAULTS = dict(enabled=True, publish_mode='check_auto', window_days=14,
                message_tokens=6000, memory_tokens=2000, min_messages=50,
                update_days=7, min_chars=150, max_chars=500,
                generate_timeout_seconds=120, check_timeout_seconds=120)
PUBLISH_MODES = ('check_auto', 'all_manual')
DEGREES = ('small', 'medium', 'large')
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
    return hashlib.sha256(dumps(value).encode('utf-8')).hexdigest()


def _setting(conn, key, default):
    row = conn.execute('SELECT value_json FROM runtime_settings WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


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
    return (dict(row) | {'enabled':bool(row['enabled'])}) if row else dict(
        entry_id=entry_id, enabled=True, publish_mode=defaults['publish_mode'], revision=0,
        current_version=0, updated_at=None)


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
    version = _settings(conn, entry_id)['current_version']
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


def _message(row, zone, *, limit=300):
    value = {'message_id':row['id'], 'source_id':row['dedupe_key'], 'entry_id':row['entry_id'], 'kind':row['kind'],
            'date':_instant(row['occurred_at']).astimezone(zone).date().isoformat(),
            'occurred_at':row['occurred_at'], 'speaker':row['sender_subject_id'],
            'content':truncate_material(row['content'], limit), 'sha256':_message_hash(row)}
    if row['quote_content'] is not None:
        value['quote']={'speaker':row['quote_author_subject_id'],
                        'content':truncate_material(row['quote_content'], min(limit,180))}
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
    for row in conn.execute('''SELECT m.* FROM memories m WHERE m.lifecycle='active' AND m.purged_at IS NULL
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
    value['is_current']=_settings(conn,value['entry_id'])['current_version']==value['version']
    return value


def current_profile(store, entry_id):
    with store.read() as conn:
        return _view(conn,_current(conn,entry_id))


def profile_context(conn, entry_id):
    """For phase two's optional prepare partition; no writes, calls or cross-entry reads."""
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
        _expect(conn,entry_id,expected_version)
        settings=_ensure(conn,entry_id,stamp)
        values={'enabled':settings['enabled'] if enabled is None else enabled,
                'publish_mode':settings['publish_mode'] if publish_mode is None else publish_mode}
        conn.execute('UPDATE entry_profile_settings SET enabled=?,publish_mode=?,revision=revision+1,updated_at=? WHERE entry_id=?',
                     (values['enabled'],values['publish_mode'],stamp,entry_id))
        _audit(conn,'settings',entry_id,stamp,expected_version=expected_version,**values)
        return _settings(conn,entry_id)


def _length_errors(content, config):
    size=len(content.strip())
    return (['empty'] if not size else ['too_short'] if size<config['min_chars'] else
            ['too_long'] if size>config['max_chars'] else [])


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
    stamp=_instant(clock()).isoformat()
    with store.write() as conn:
        previous=_expect(conn,entry_id,expected_version); settings=_ensure(conn,entry_id,stamp)
        if _entry(conn,entry_id)['kind'] not in ('group','live'):
            raise ValueError('entry kind does not support profiles')
        if not isinstance(content,str) or _length_errors(content,_runtime(conn)):
            raise ValueError('profile must contain 150–500 characters')
        old=defaultdict(list)
        for sentence in previous['sentences'] if previous else []:
            old[sentence['text']].append(sentence)
        sentences=[]
        for text in split_sentences(content):
            sentences.append(old[text].pop(0) if old[text] else {'text':text,'basis':[],'author':'admin','message_count':0})
        through=conn.execute('SELECT COALESCE(MAX(id),0) FROM messages WHERE entry_id=?',(entry_id,)).fetchone()[0]
        return _insert(conn,entry_id,previous=expected_version,content=content,sentences=sentences,
            checks={'passed':True,'deterministic':{'passed':True},'model':None,'admin_edit':True},degree='large',status='published',
            stamp=stamp,author='admin',source='edit',material={},message_through=through,settings_revision=settings['revision'])


def version_diff(store, entry_id, before, after):
    with store.read() as conn:
        left=_version(conn,entry_id,before)['sentences']; right=_version(conn,entry_id,after)['sentences']
        changes=[{'op':op,'before':left[a:b],'after':right[c:d]} for op,a,b,c,d in
                 SequenceMatcher(a=[s['text'] for s in left],b=[s['text'] for s in right],autojunk=False).get_opcodes()]
        return {'entry_id':entry_id,'before':before,'after':after,'changed':left!=right,'sentences':changes}


def rollback(store, entry_id, version, *, expected_version, clock=utc_now):
    stamp=_instant(clock()).isoformat()
    with store.write() as conn:
        _expect(conn,entry_id,expected_version); settings=_ensure(conn,entry_id,stamp)
        target=_version(conn,entry_id,version)
        if target['status']!='published' or _length_errors(target['content'],_runtime(conn)):
            raise ValueError('rollback requires a previously published valid profile')
        through=conn.execute('SELECT COALESCE(MAX(id),0) FROM messages WHERE entry_id=?',(entry_id,)).fetchone()[0]
        return _insert(conn,entry_id,previous=expected_version,content=target['content'],sentences=target['sentences'],
            checks={**target['checks'],'rollback_from':version},degree='large',status='published',stamp=stamp,author='admin',
            source='rollback',material=target['material'],message_through=through,settings_revision=settings['revision'])


def confirm_candidate(store, entry_id, version, *, expected_version, clock=utc_now):
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
    stamp=_instant(clock()).isoformat()
    with store.write() as conn:
        _expect(conn,entry_id,expected_version); value=_version(conn,entry_id,version)
        if value['status']!='candidate':
            raise EntryProfileConflict('candidate_changed')
        checks={**value['checks'],'reason':reason}
        conn.execute("UPDATE entry_profile_versions SET status='rejected',checks_json=? WHERE entry_id=? AND version=?",(dumps(checks),entry_id,version))
        _audit(conn,'reject',entry_id,stamp,version=version)
        return _view(conn,_version(conn,entry_id,version))


def _resolve(generated, evidence, previous):
    references={item['ref']:item for key in ('messages','memories') for item in evidence[key]}
    manual=[dict(s) for s in previous['sentences'] if s['author']=='admin'] if previous else []
    protected={s['text'] for s in manual}
    sentences=[]; deleted=[]; errors=[]; seen=set()
    rows=generated.get('sentences') if isinstance(generated,dict) else None
    if not isinstance(rows,list) or len(rows)>50:
        return manual,[],['invalid_sentences'],len(manual)
    original_count=len(rows)+len(manual)
    for index,row in enumerate(rows):
        reason=None
        if not isinstance(row,dict) or not isinstance(row.get('text'),str) or not row['text'].strip():
            reason='invalid_sentence'
        elif row['text'] in protected:
            # It will be appended verbatim by code. Never turn it into a model fact.
            original_count-=1
            continue
        elif row['text'] in seen:
            reason='duplicate_sentence'
        elif (not isinstance(row.get('basis'),list) or not row['basis'] or
              any(not isinstance(ref,str) or ref not in references for ref in row['basis'])):
            reason='invalid_evidence_reference'
        if reason:
            deleted.append({'index':index+1,'sentence':row,'reason':reason,'stage':'deterministic'})
            continue
        seen.add(row['text'])
        refs=list(dict.fromkeys(row['basis'])); linked=[references[ref] for ref in refs]
        message_ids={s['message_id'] for item in linked for s in item.get('source_messages',[item])}
        sentences.append({'text':row['text'],'basis':[_basis(item) for item in linked],
                          'author':'model','message_count':len(message_ids),'refs':refs})
    return [*sentences,*manual],deleted,errors,original_count


def _check(output, sentences):
    if not isinstance(output,dict) or output.get('change_degree') not in DEGREES or not isinstance(output.get('reason'),str):
        raise ValueError('invalid_check')
    rows=output.get('sentences')
    if not isinstance(rows,list) or len(rows)!=len(sentences):
        raise ValueError('invalid_check_sentence_count')
    removed=[]; retained=[]; admin_warnings=[]
    for index,(row,sentence) in enumerate(zip(rows,sentences,strict=True)):
        if (not isinstance(row,dict) or type(row.get('index')) is not int or row['index']!=index+1
                or type(row.get('supported')) is not bool or type(row.get('scene_qualified')) is not bool
                or not isinstance(row.get('violations'),list) or not isinstance(row.get('reason'),str)
                or any(v not in VIOLATIONS for v in row['violations'])):
            raise ValueError('invalid_check_sentence')
        bad=(not row['supported'] or bool(row['violations']) or
             (sentence['message_count']<=1 and not row['scene_qualified']))
        if sentence['author']=='admin':
            retained.append(sentence)
            if row['violations']:
                admin_warnings.append({'index':index+1,'result':row})
        elif bad:
            removed.append({'index':index+1,'sentence':sentence,'reason':row['reason'],'result':row,'stage':'model'})
        else:
            retained.append(sentence)
    return retained,removed,admin_warnings,output['change_degree']


def _admin_removed(previous, sentences):
    from collections import Counter
    before=Counter(s['text'] for s in previous['sentences'] if s['author']=='admin') if previous else Counter()
    after=Counter(s['text'] for s in sentences if s['author']=='admin')
    return bool(before-after)


class _HealthGate:
    """Apply the shared chat daily limit on every HTTP attempt, including retries."""
    def __init__(self, health):
        self.real=health
    def check(self, kind, purpose, *, probe=False):
        return self.real.check(kind,'learning' if kind=='chat' else purpose,probe=probe)
    def __getattr__(self, key):
        return getattr(self.real,key)


class EntryProfileEngine:
    def __init__(self, store, gateway, *, clock=utc_now):
        self.store,self.gateway,self.clock=store,gateway,clock

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
        prompt=files('iris').joinpath(f'prompts/entry_profile_{kind}_v1.md').read_text(encoding='utf-8')
        messages=[{'role':'system','content':prompt},{'role':'user','content':dumps(payload)}]
        monotonic=getattr(gateway,'monotonic',time.monotonic)
        started=monotonic(); deadline=started+config[kind+'_timeout_seconds']
        try:
            reply=gateway.chat(messages,purpose,max_tokens=16000,_deadline=deadline)
        except ModelError as error:
            calls.append({'purpose':purpose,'result':error.category,'reason':error.reason or error.category,
                          'duration_ms':round((monotonic()-started)*1000),'usage':{}})
            raise
        outputs[purpose]=reply.content
        calls.append({'purpose':purpose,'result':'success','duration_ms':round((monotonic()-started)*1000),
                      'usage':reply.usage,'finish_reason':reply.finish_reason})
        if reply.finish_reason!='stop':
            raise ValueError('incomplete_output')
        def unique(pairs):
            value={}
            for key,item in pairs:
                if key in value:
                    raise ValueError('duplicate_key')
                value[key]=item
            return value
        value=json.loads(reply.content,object_pairs_hook=unique)
        if not isinstance(value,dict):
            raise ValueError('invalid_output')
        return value

    def _finish(self, attempt, state, reason, outputs, calls):
        with self.store.write() as conn:
            conn.execute('''UPDATE entry_profile_attempts SET state=?,reason=?,outputs_json=?,calls_json=?,finished_at=?
                WHERE id=? AND state='running' ''',(state,reason,dumps(outputs),dumps(calls),_instant(self.clock()).isoformat(),attempt))

    def _skip(self, entry_id, expected, source, due, reason, run_id, stamp):
        with self.store.write() as conn:
            _expect(conn,entry_id,expected)
            _audit(conn,'generation_skipped',entry_id,stamp,base_version=expected,reason=reason,source=source)
            attempt=None
            if run_id is not None:
                attempt=conn.execute('''INSERT OR IGNORE INTO entry_profile_attempts(entry_id,run_id,base_version,state,
                    reason,local_day,started_at,finished_at) VALUES(?,?,?,'skipped',?,?,?,?)''',
                    (entry_id,run_id,expected,reason,due['local_day'],stamp,stamp))
                attempt=conn.execute('SELECT id FROM entry_profile_attempts WHERE entry_id=? AND run_id=?',(entry_id,run_id)).fetchone()[0]
        return {'status':'skipped','reason':reason,'version':None,'attempt_id':attempt,'due':due}

    def _run(self, entry_id, expected, source, *, run_id=None):
        current=_instant(self.clock()); stamp=current.isoformat()
        with self.store.write() as conn:
            _expect(conn,entry_id,expected)
            _recover_interrupted(conn,self.store,entry_id,stamp)
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
            material={'evidence':evidence,'settings':settings,'runtime':config,
                      'previous':previous['content'] if previous else '',
                      'admin_sentences':[s for s in previous['sentences'] if s['author']=='admin'] if previous else []}
            attempt=conn.execute('''INSERT INTO entry_profile_attempts(entry_id,run_id,base_version,state,local_day,
                started_at,material_json) VALUES(?,?,?,'running',?,?,?)''',
                (entry_id,run_id,expected,due['local_day'],stamp,dumps(material))).lastrowid
            _audit(conn,'generation_requested',entry_id,stamp,attempt_id=attempt,base_version=expected,source=source)
            with _ACTIVE_LOCK:
                _ACTIVE_ATTEMPTS.add((self.store.path,attempt))
        outputs={}; calls=[]; sentences=None
        try:
            checks={'passed':False,'errors':[],'deleted_sentences':[],'model':None}
            degree='large'
            try:
                generated=self._call(gateway,'generate',material,outputs,calls,config)
                sentences,deleted,errors,count=_resolve(generated,evidence,previous)
                checks.update(errors=errors,deleted_sentences=deleted,original_sentence_count=count,
                              original_sentences=generated.get('sentences'),deterministic={'passed':not errors and not deleted})
                content=''.join(s['text'] for s in sentences)
                if len(deleted)*10>count*3:
                    checks['errors'].append('too_many_deleted')
                checks['errors']+=_length_errors(content,config)
                if not checks['errors']:
                    checks['checked_sentences']=sentences
                    # Persist progress without holding a transaction over network IO.
                    with self.store.write() as conn:
                        conn.execute('UPDATE entry_profile_attempts SET outputs_json=?,calls_json=? WHERE id=?',
                                     (dumps(outputs),dumps(calls),attempt))
                    output=self._call(gateway,'check',{**material,'candidate':sentences},outputs,calls,config)
                    checks['model']=output
                    sentences,removed,warnings,degree=_check(output,sentences)
                    checks['deleted_sentences']+=removed
                    checks['admin_warnings']=warnings
                    content=''.join(s['text'] for s in sentences)
                    if len(checks['deleted_sentences'])*10>count*3:
                        checks['errors'].append('too_many_deleted')
                    checks['errors']+=_length_errors(content,config)
                    # A checker saw the larger draft; do not pretend it judged the
                    # final deletion's degree. Auto mode permits large changes too.
                    if removed:
                        degree='large'
                checks['passed']=not checks['errors']
                checks['deterministic']={'passed':not checks['errors'],'errors':list(checks['errors']),
                    'characters':len(content.strip()),'deleted_count':len(checks['deleted_sentences'])}
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
