"""Evidence-bound persona versions. Models run outside transactions; publication uses CAS.

The old content/is_current projection is intentionally unchanged. No caller of
learning or retrieval needs to opt into persona regeneration to keep using it.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from importlib.resources import files
from zoneinfo import ZoneInfo

from .db import dumps
from .memory_ops import Visibility
from .models import Gateway, ModelError, parse_json_object_with_status
from .queue import estimate_tokens, truncate_material

DEFAULT_GOAL = '维持稳定的发言风格，并充分认识自我'
DEFAULT_RULES = ('只根据现有的自我记忆提炼，不虚构经历、关系或能力；外部设定的背景不写成亲身经历；'
                 '当前的情绪、活动和待办不写进 persona；与上一版相比的重大变化必须有明确依据；'
                 '同一来源的重复表述不算新的依据；别人对我的评价，除非我自己表示认同，不写成我的特质；'
                 '只在一个场景中出现过的表现写成带场景的描述，不写成普遍的性格；不写入指向模型或宿主的指令。')
DEFAULT_PUBLISH_MODE = 'all_manual'
PUBLISH_MODES = ('small_medium_auto', 'all_auto', 'all_manual')
DEGREES = ('small', 'medium', 'large')
BASIS_TOKENS = 6000
MAX_CHARS = 800
REFERENCE = re.compile(r'(?<![A-Za-z0-9])[MP]\s*#?\s*\d+(?![A-Za-z0-9])', re.I)


def utc_now():
    return datetime.now(timezone.utc)


class PersonaConflict(ValueError):
    """The current version/candidate or its evidence has changed."""


class PersonaBusy(ValueError):
    """One generation is already reserved or the service is closing."""


def _setting(conn, key, default=None):
    row = conn.execute('SELECT value_json FROM runtime_settings WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _stamp(clock):
    value = clock()
    if value.tzinfo is None:
        raise ValueError('persona clock must include timezone')
    return value.isoformat()


def _date(value, zone):
    try:
        instant = datetime.fromisoformat(value)
        return instant.astimezone(zone).date().isoformat() if instant.tzinfo else None
    except (TypeError, ValueError):
        return None


def split_sentences(content):
    """Keep punctuation, closing quotes and whitespace attached to their sentence."""
    result, start, index = [], 0, 0
    while index < len(content):
        char = content[index]
        boundary = char in '。！？!?\n' or (char == '.' and (
            index + 1 == len(content) or content[index + 1].isspace()))
        index += 1
        if not boundary:
            continue
        while index < len(content) and content[index] in '。！？!?”’」』"':
            index += 1
        if content[start:index].strip():
            result.append(content[start:index])
            start = index
    if content[start:].strip():
        result.append(content[start:])
    elif result:
        result[-1] += content[start:]
    return result


def _eligible(row):
    return row is not None and row['lifecycle'] == 'active' and (row['speaker_subject_id'] == 'self' or row['stance'] == '设定')


def _trace(conn, memory_id, zone):
    """Traverse real source edges once; cycles and repeated paths add no dates."""
    todo, visited, messages, initial, refs, source_rows = [memory_id], set(), {}, [], [], []
    while todo:
        mid = todo.pop()
        if mid in visited:
            continue
        visited.add(mid)
        row = conn.execute('SELECT id,revision,lifecycle FROM memories WHERE id=?', (mid,)).fetchone()
        if row is None:
            continue
        refs.append(dict(memory_id=mid, revision=row['revision'], lifecycle=row['lifecycle']))
        for source in conn.execute('SELECT * FROM sources WHERE memory_id=? ORDER BY id', (mid,)):
            source_rows.append(dict(source))
            if source['kind'] == 'memory' and source['source_memory_id'] is not None:
                todo.append(source['source_memory_id'])
            elif source['kind'] == 'initial_setting':
                initial.append({'memory_id': mid, 'date': _date(source['created_at'], zone)})
            elif source['message_id'] is not None:
                message = conn.execute('''SELECT m.*,e.kind AS entry_kind,s.name AS speaker
                    FROM messages m JOIN entries e ON e.id=m.entry_id
                    JOIN subjects s ON s.id=m.sender_subject_id WHERE m.id=?''', (source['message_id'],)).fetchone()
                if message:
                    messages[message['id']] = dict(message)
    def instant(m):
        try:
            value = datetime.fromisoformat(m['occurred_at'])
            return value.timestamp() if value.tzinfo else float('-inf')
        except (TypeError, ValueError):
            return float('-inf')
    ordered = sorted(messages.values(), key=lambda m: (instant(m), m['id']))
    selected = ordered if len(ordered) <= 4 else [ordered[0], *ordered[-3:]]
    excerpts = []
    for m in selected:
        prose = m['content']
        if m['quote_content']:
            prose += '\n引用原话：' + m['quote_content']
        excerpts.append({'message_id': m['id'], 'date': _date(m['occurred_at'], zone),
            'occurred_at': m['occurred_at'], 'entry_id': m['entry_id'], 'entry_kind': m['entry_kind'],
            'speaker': m['speaker'], 'speaker_subject_id': m['sender_subject_id'],
            'kind': m['kind'], 'scene_identity': m['scene_identity'],
            'quote_author_subject_id': m['quote_author_subject_id'], 'excerpt': (prose if estimate_tokens(prose) <= 300 else
                truncate_material(prose, 300-estimate_tokens(' [已截断；原文仍完整保存]')))})
    dates = sorted({d for d in [_date(m['occurred_at'], zone) for m in ordered] + [x['date'] for x in initial] if d})
    signature = hashlib.sha256(dumps({'refs': sorted(refs, key=lambda x: x['memory_id']),
        'sources': sorted(source_rows, key=lambda s: s['id']), 'messages': ordered}).encode('utf-8')).hexdigest()
    return {'dates': dates, 'date_count': len(dates), 'excerpts': excerpts,
            'trace_refs': sorted(refs, key=lambda x: x['memory_id']), 'trace_sha256': signature,
            'initial_sources': initial}


def _self_snapshot(conn):
    visibility = Visibility(conn)
    # Strength/last-confirmation alone are not new knowledge. New source edges are.
    return {str(r['id']): {'revision': r['revision'], 'lifecycle': r['lifecycle'],
            'sources': [s[0] for s in conn.execute('SELECT id FROM sources WHERE memory_id=? ORDER BY id', (r['id'],))]}
        for r in conn.execute('''SELECT m.* FROM memories m JOIN memory_subjects a ON a.memory_id=m.id
            WHERE a.subject_id='self' AND (m.speaker_subject_id='self' OR m.stance='设定') ORDER BY m.id''') if visibility.memory_visible(r['id'])}


def _select(conn, token_limit=BASIS_TOKENS, *, timezone_name=None):
    zone = ZoneInfo(timezone_name or _setting(conn, 'timezone', 'Asia/Shanghai'))
    selected = []
    rows = conn.execute('''SELECT m.*,e.kind AS entry_kind FROM memories m
        JOIN memory_subjects a ON a.memory_id=m.id LEFT JOIN entries e ON e.id=m.entry_id
        WHERE a.subject_id='self' AND m.lifecycle='active'
        AND (m.speaker_subject_id='self' OR m.stance='设定')
        ORDER BY m.pinned DESC,m.importance DESC,m.retention DESC,m.id''')
    visibility = Visibility(conn)
    for row in rows:
        if not visibility.memory_visible(row['id']):
            continue
        record = {key: row[key] for key in ('content', 'stance', 'belief', 'importance', 'retention', 'pinned', 'entry_kind', 'event_time', 'speaker_subject_id', 'lifecycle')}
        record['about'] = [r[0] for r in conn.execute('SELECT subject_id FROM memory_subjects WHERE memory_id=? ORDER BY subject_id', (row['id'],))]
        record.update(ref=f'M{len(selected)+1}', memory_id=row['id'], revision=row['revision'],
                      date=_date(row['created_at'], zone), **_trace(conn, row['id'], zone))
        # Account for metadata and excerpts too, not just memory prose. Never ask
        # a model to compress a large item to make it fit.
        if estimate_tokens(dumps([*selected, record])) <= token_limit:
            selected.append(record)
    return {'memories': selected, 'estimated_tokens': estimate_tokens(dumps(selected)), 'token_limit': token_limit}


def select_evidence(store, *, token_limit=BASIS_TOKENS):
    if type(token_limit) is not int or not 1 <= token_limit <= BASIS_TOKENS:
        raise ValueError('invalid evidence token limit')
    with store.read() as conn:
        return _select(conn, token_limit)


def _initial_sentences(conn, content, memory_ids, name, background):
    zone = ZoneInfo(_setting(conn, 'timezone', 'Asia/Shanghai'))
    spans = []
    search_start = content.find('初始设定：')
    for mid in memory_ids:
        row = conn.execute('SELECT id,revision,content FROM memories WHERE id=?', (mid,)).fetchone()
        if row is None or search_start < 0:
            continue
        pos = content.find(row['content'], search_start + len('初始设定：'))
        if pos >= 0 and row['content']:
            spans.append((pos, pos+len(row['content']),
                          dict(memory_id=mid, revision=row['revision']), _trace(conn, mid, zone)['dates']))
    result, offset = [], 0
    for text in split_sentences(content):
        matched = [span for span in spans if span[0] < offset+len(text) and span[1] > offset]
        dates = sorted({date for span in matched for date in span[3]})
        result.append({'text': text, 'origin': 'initial_template', 'basis': [span[2] for span in matched],
                       'dates': dates, 'date_count': len(dates), 'initial_setting': True,
                       'initial_settings': {'role_name': name, 'background': background}})
        offset += len(text)
    return result


def _decode(conn, row):
    if row is None:
        return None
    version = dict(row)
    for column, field in (('sentences_json','sentences'), ('checks_json','checks'),
                          ('material_json','material'), ('self_snapshot_json','self_snapshot')):
        value = version.pop(column)
        version[field] = json.loads(value) if value is not None else None
    return version


def current_persona(store):
    with store.read() as conn:
        return _decode(conn, conn.execute('SELECT * FROM persona_versions WHERE is_current=1').fetchone())


def get_version(store, version_id):
    with store.read() as conn:
        return _get(conn, version_id)


def _get(conn, version_id):
    row = conn.execute('SELECT * FROM persona_versions WHERE id=?', (version_id,)).fetchone()
    if row is None:
        raise ValueError('persona version does not exist')
    return _decode(conn, row)


def list_versions(store):
    with store.read() as conn:
        return [_decode(conn, row) for row in conn.execute('SELECT * FROM persona_versions ORDER BY id DESC')]


def _expect(conn, expected_version):
    row = conn.execute('SELECT * FROM persona_versions WHERE is_current=1').fetchone()
    if type(expected_version) is not int or row is None or row['id'] != expected_version:
        raise PersonaConflict('current persona version changed')
    return _decode(conn, row)


def _operation(conn, action, vid, stamp, *, actor='persona', object_type='persona'):
    conn.execute('''INSERT INTO admin_operations(actor,action,object_type,object_id,details_json,created_at)
        VALUES(?,?,?,?,'{}',?)''', (actor, action, object_type, str(vid), stamp))


def _insert(conn, *, content, sentences, checks, degree, status, source, stamp,
            base=None, rollback_of=None, material=None, snapshot=None):
    if status in ('current', 'pending') or source in ('periodic', 'regenerate'):
        conn.execute("UPDATE persona_versions SET status='superseded' WHERE status='pending'")
    if status == 'current':
        conn.execute("UPDATE persona_versions SET is_current=0,status='history' WHERE is_current=1")
    vid = conn.execute('''INSERT INTO persona_versions(content,created_at,is_current,status,source,sentences_json,
        checks_json,change_degree,base_version_id,rollback_of,published_at,material_json,self_snapshot_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''', (content,stamp,int(status=='current'),status,source,dumps(sentences),
        dumps(checks),degree,base,rollback_of,stamp if status=='current' else None,dumps(material or {}),
        dumps(snapshot if snapshot is not None else _self_snapshot(conn)))).lastrowid
    action = {'admin_edit': 'persona_edit', 'rollback': 'persona_rollback'}.get(source, 'persona_' + status)
    _operation(conn, action, vid, stamp, actor='admin' if source in ('admin_edit', 'rollback') else 'persona')
    return _get(conn, vid)


def record_initial(conn, content, memory_ids, name, background, stamp, *, version_id=None):
    """Used only by the existing deterministic setup path, in its transaction."""
    if version_id is not None:
        _expect(conn, version_id)
    elif conn.execute('SELECT 1 FROM persona_versions WHERE is_current=1').fetchone():
        raise PersonaConflict('initial persona already exists')
    sentences = _initial_sentences(conn, content, memory_ids, name, background)
    checks = {'passed': True, 'deterministic': {'passed': True, 'errors': [],
              'warnings': ['initial template retains the pre-M3 length behavior']}, 'model': None}
    if version_id is not None:
        conn.execute('''UPDATE persona_versions SET content=?,sentences_json=?,checks_json=?,self_snapshot_json=?
            WHERE id=? AND is_current=1''', (content,dumps(sentences),dumps(checks),dumps(_self_snapshot(conn)),version_id))
        return _get(conn, version_id)
    base = conn.execute('SELECT MAX(id) FROM persona_versions').fetchone()[0]
    return _insert(conn, content=content, sentences=sentences, checks=checks, degree='small',
                   status='current', source='initial_setting', stamp=stamp, base=base)


def _text_errors(content):
    errors = []
    if not content.strip():
        errors.append('empty persona')
    if len(content) > MAX_CHARS:
        errors.append('persona exceeds 800 characters')
    if REFERENCE.search(unicodedata.normalize('NFKC', content)):
        errors.append('residual memory or participant reference')
    return errors


def _admin_sentences(previous):
    return {f'A{i+1}': sentence for i, sentence in enumerate(previous['sentences']) if sentence['origin'] == 'admin'}


def _deterministic(output, material, previous):
    errors, sentences = [], []
    available = {m['ref']: m for m in material['evidence']['memories']}
    manual = _admin_sentences(previous)
    rows = output.get('sentences') if isinstance(output, dict) else None
    if not isinstance(rows, list):
        rows = []
        errors.append('sentences must be an array')
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or not isinstance(row.get('text'), str) or not row['text'].strip():
            errors.append(f'sentence {index}: nonempty text required')
            continue
        text = row['text']
        if len(split_sentences(text)) != 1:
            errors.append(f'sentence {index}: one sentence per item required')
        basis, admin = row.get('basis'), row.get('admin_sentence')
        if admin is not None:
            valid_admin = (isinstance(admin, str) and admin in manual and basis == []
                           and text == manual[admin]['text'])
            if not valid_admin:
                errors.append(f'sentence {index}: invalid administrator attribution')
            sentences.append({'text': text, 'origin': 'admin' if valid_admin else 'memory', 'basis': [], 'dates': [],
                              'date_count': 0, 'initial_setting': False})
            continue
        if (not isinstance(basis, list) or not basis or any(type(ref) is not str or ref not in available for ref in basis)):
            errors.append(f'sentence {index}: missing or unknown evidence')
            basis = []
        records = [available[ref] for ref in dict.fromkeys(basis)]
        dates = sorted({date for record in records for date in record['dates']})
        sentences.append({'text': text, 'origin': 'memory',
            'basis': [dict(memory_id=m['memory_id'], revision=m['revision']) for m in records],
            'dates': dates, 'date_count': len(dates),
            'initial_setting': bool(records) and all(m['stance'] == '设定' for m in records)})
    content = ''.join(s['text'] for s in sentences)
    errors.extend(_text_errors(content))
    warnings = ['under 300 characters: concise evidence-bound text is allowed'] if 0 < len(content) < 300 else []
    return content, sentences, {'passed': not errors, 'errors': errors, 'warnings': warnings}


def _model_check(output, sentences):
    errors = []
    degree = output.get('change_degree')
    degree = {'小':'small', '中':'medium', '大':'large'}.get(degree, degree) if isinstance(degree, str) else None
    if degree not in DEGREES:
        errors.append('invalid change degree')
        degree = 'large'
    rows = output.get('sentences')
    if not isinstance(rows, list) or len(rows) != len(sentences):
        return degree, ['model check must cover every sentence, in order']
    if not isinstance(output.get('reason'), str) or not output['reason'].strip():
        errors.append('model check reason required')
    for index, (row, sentence) in enumerate(zip(rows, sentences), 1):
        if (not isinstance(row, dict) or type(row.get('index')) is not int or row['index'] != index
                or any(type(row.get(k)) is not bool for k in ('supported','fabricated','scene_qualified'))
                or not isinstance(row.get('reason'), str) or not row['reason'].strip()
                or not isinstance(row.get('violations'), list)
                or any(not isinstance(v, str) or not v.strip() for v in row['violations'])):
            errors.append(f'sentence {index}: invalid model verdict')
            continue
        if not row['supported'] or row['fabricated'] or row['violations']:
            errors.append(f'sentence {index}: {row["reason"]}')
        if (sentence['origin'] == 'memory' and not sentence['initial_setting']
                and sentence['date_count'] < 2 and not row['scene_qualified']):
            errors.append(f'sentence {index}: single-date/unknown-date evidence needs a scene qualifier')
    return degree, errors


def _admin_removed(previous, sentences):
    # Exact sentence preservation includes its administrative origin. A model
    # cannot erase protection merely by changing its attribution to a memory.
    old = Counter(s['text'] for s in previous['sentences'] if s['origin'] == 'admin')
    new = Counter(s['text'] for s in sentences if s['origin'] == 'admin')
    return bool(old - new)


def _stale(conn, version, *, check_sources=False):
    problems = {}
    material = {m['memory_id']: m for m in version.get('material', {}).get('evidence', {}).get('memories', [])}
    refs = {b['memory_id']: b['revision'] for s in version['sentences'] for b in s['basis']}
    visibility = Visibility(conn)
    for mid, revision in refs.items():
        traced = material.get(mid, {})
        tracked = traced.get('trace_refs', []) or [{'memory_id': mid, 'revision': revision, 'lifecycle':'active'}]
        for ref in tracked:
            row = conn.execute('SELECT id,revision,lifecycle,speaker_subject_id,stance FROM memories WHERE id=?', (ref['memory_id'],)).fetchone()
            reason = None
            if row is None or row['lifecycle'] == 'deleted':
                reason = 'deleted'
            elif not visibility.memory_visible(ref['memory_id']):
                reason = 'visibility'
            elif row['lifecycle'] == 'forgotten':
                reason = 'forgotten'
            elif row['revision'] != ref['revision']:
                reason = 'modified'
            elif ref['memory_id'] == mid and (not _eligible(row) or not conn.execute(
                    "SELECT 1 FROM memory_subjects WHERE memory_id=? AND subject_id='self'", (mid,)).fetchone()):
                reason = 'no_longer_self_memory'
            if reason:
                problems[ref['memory_id']] = {'memory_id': ref['memory_id'], 'expected_revision': ref['revision'],
                    'current_revision': row['revision'] if row else None, 'reason': reason}
        if check_sources and traced and mid not in problems:
            zone = ZoneInfo(_setting(conn, 'timezone', 'Asia/Shanghai'))
            if _trace(conn, mid, zone)['trace_sha256'] != traced['trace_sha256']:
                problems[mid] = {'memory_id': mid, 'expected_revision': revision, 'current_revision': revision,
                                 'reason': 'sources_changed'}
    return list(problems.values())


def pending_update(store):
    with store.read() as conn:
        version = _decode(conn, conn.execute('SELECT * FROM persona_versions WHERE is_current=1').fetchone())
        basis = _stale(conn, version) if version else []
        return {'version_id': version['id'] if version else None, 'pending': bool(basis), 'basis': basis}



def persona_context(conn, *, include_basis=False):
    """One read snapshot; do not load the potentially large self-memory snapshot."""
    row = conn.execute('''SELECT id,content,created_at,sentences_json,material_json FROM persona_versions
        WHERE is_current=1''').fetchone()
    basis = _stale(conn, {'sentences': json.loads(row['sentences_json']),
                         'material': json.loads(row['material_json'])}) if row else []
    content = row['content'] if row else ''
    if row:
        visibility = Visibility(conn)
        sentences = json.loads(row['sentences_json'])
        if any(not visibility.memory_visible(b['memory_id']) for sentence in sentences for b in sentence['basis']):
            content = ''.join(s['text'] for s in sentences if all(visibility.memory_visible(b['memory_id']) for b in s['basis']))
    result = {'version': row['id'] if row else None, 'content': content,
              'generated_at': row['created_at'] if row else None,
              'needs_update': bool(basis), 'stale_basis_count': len(basis)}
    if include_basis:
        result['stale_basis'] = basis
    return result


def _due(conn, version, current):
    if version is None:
        return {'due': False, 'reason': 'not_initialized', 'changed_memory_ids': []}
    previous, latest = version['self_snapshot'], _self_snapshot(conn)
    if previous is None:  # Upgrade of a pre-M3 initial template; no invented snapshot.
        since = version['created_at']
        changed = [r[0] for r in conn.execute('''SELECT m.id FROM memories m JOIN memory_subjects a ON a.memory_id=m.id
            WHERE a.subject_id='self' AND (m.speaker_subject_id='self' OR m.stance='设定')
            AND (julianday(m.updated_at)>julianday(?) OR m.lifecycle!='active'
                 OR EXISTS(SELECT 1 FROM sources s WHERE s.memory_id=m.id AND julianday(s.created_at)>julianday(?)))
            ORDER BY m.id''', (since, since))]
    else:
        changed = sorted(int(mid) for mid in previous.keys() | latest.keys() if previous.get(mid) != latest.get(mid)
                         and (mid in previous or latest[mid]['lifecycle'] == 'active'))
    # A deliberate rollback can restore old revisions. Do not let the fresh
    # publication timestamp erase those still-pending self-memory changes.
    stale_ids = {item['memory_id'] for item in _stale(conn, version)}
    traced = {item['memory_id']: {r['memory_id'] for r in item['trace_refs']}
              for item in version['material'].get('evidence', {}).get('memories', [])}
    stale_self = {ref['memory_id'] for sentence in version['sentences'] for ref in sentence['basis']
                  if stale_ids & (traced.get(ref['memory_id'], set()) | {ref['memory_id']})}
    changed = sorted(set(changed) | stale_self)
    elapsed = current.astimezone(timezone.utc) - datetime.fromisoformat(version['created_at']).astimezone(timezone.utc)
    due = len(changed) >= 5 or (bool(changed) and elapsed >= timedelta(days=7))
    return {'due': due, 'reason': 'five_changes' if len(changed) >= 5 else 'seven_days' if due else 'not_due',
            'changed_memory_ids': changed, 'changed_count': len(changed), 'elapsed_days': elapsed.total_seconds()/86400}


def persona_due(store, *, clock=utc_now):
    with store.read() as conn:
        version = _decode(conn, conn.execute('SELECT * FROM persona_versions WHERE is_current=1').fetchone())
        return _due(conn, version, clock())


def admin_edit(store, content, *, expected_version, clock=utc_now):
    if not isinstance(content, str):
        raise ValueError('persona content must be text')
    errors = _text_errors(content)
    if errors:
        raise ValueError('; '.join(errors))
    with store.write() as conn:
        previous = _expect(conn, expected_version)
        unchanged = {}
        for sentence in previous['sentences']:
            unchanged.setdefault(sentence['text'], []).append(sentence)
        sentences = []
        for text in split_sentences(content):
            matches = unchanged.get(text, [])
            sentences.append(matches.pop(0) if matches else {'text': text, 'origin': 'admin',
                'basis': [], 'dates': [], 'date_count': 0, 'initial_setting': False})
        return _insert(conn, content=content, sentences=sentences,
            checks={'passed':True, 'deterministic':{'passed':True,'errors':[], 'warnings':[]},
                    'model':None, 'administrator_published':True},
            degree='small' if content == previous['content'] else 'large', status='current',
            source='admin_edit', stamp=_stamp(clock), base=expected_version)


def version_diff(store, before_id, after_id):
    with store.read() as conn:
        before, after = _get(conn, before_id), _get(conn, after_id)
    a, b = before['sentences'], after['sentences']
    matcher = SequenceMatcher(a=[s['text'] for s in a], b=[s['text'] for s in b], autojunk=False)
    changes = []
    for tag, i, j, k, l in matcher.get_opcodes():
        if tag != 'equal' or a[i:j] != b[k:l]:
            changes.append({'kind': 'basis_changed' if tag == 'equal' else tag,
                            'before_start':i+1, 'after_start':k+1, 'before':a[i:j], 'after':b[k:l]})
    return {'before_version':before_id, 'after_version':after_id, 'changes':changes}


def rollback(store, version_id, *, expected_version, clock=utc_now):
    with store.write() as conn:
        _expect(conn, expected_version)
        old = _get(conn, version_id)
        if old['status'] not in ('current','history'):
            raise ValueError('rollback requires a previously published version')
        # Explicit administrator rollback may restore stale support; pending_update
        # exposes it. Preserve the old sentence provenance, never invent new proof.
        return _insert(conn, content=old['content'], sentences=old['sentences'], checks=old['checks'],
            degree='large', status='current', source='rollback', stamp=_stamp(clock), base=expected_version,
            rollback_of=version_id, material=old['material'])


def confirm_candidate(store, version_id, *, expected_version, clock=utc_now):
    with store.write() as conn:
        _expect(conn, expected_version)
        candidate = _get(conn, version_id)
        if candidate['status'] != 'pending' or candidate['base_version_id'] != expected_version:
            raise PersonaConflict('pending candidate changed')
        if not candidate['checks'].get('passed') or _stale(conn, candidate, check_sources=True):
            raise PersonaConflict('candidate evidence changed; regenerate it')
        # The candidate was checked against its frozen settings. Later settings
        # edits apply to future candidates, including when confirmation is delayed.
        stamp = _stamp(clock)
        conn.execute("UPDATE persona_versions SET is_current=0,status='history' WHERE is_current=1")
        conn.execute("UPDATE persona_versions SET is_current=1,status='current',published_at=? WHERE id=?", (stamp,version_id))
        _operation(conn, 'persona_confirm', version_id, stamp, actor='admin')
        return _get(conn, version_id)


def reject_candidate(store, version_id, *, expected_version, reason='administrator rejected', clock=utc_now):
    with store.write() as conn:
        _expect(conn, expected_version)
        candidate = _get(conn, version_id)
        if candidate['status'] != 'pending' or candidate['base_version_id'] != expected_version:
            raise PersonaConflict('pending candidate changed')
        checks = {**candidate['checks'], 'administrator_rejection': reason}
        conn.execute("UPDATE persona_versions SET status='rejected',checks_json=? WHERE id=?", (dumps(checks),version_id))
        _operation(conn, 'persona_reject', version_id, _stamp(clock), actor='admin')
        return _get(conn, version_id)


def persona_settings(conn):
    """The editable persona settings, read from a single snapshot."""
    mode = _setting(conn, 'persona_publish_mode', DEFAULT_PUBLISH_MODE)
    if mode not in PUBLISH_MODES:
        raise ValueError('invalid persona publication mode')
    return {'goal':_setting(conn,'persona_goal',DEFAULT_GOAL), 'rules':_setting(conn,'persona_rules',DEFAULT_RULES),
            'publish_mode':mode}


def _settings(conn):
    return {**persona_settings(conn), 'timezone':_setting(conn,'timezone','Asia/Shanghai')}


def _reserve_attempt(conn, expected_version, source, stamp, *, actor='persona'):
    _expect(conn, expected_version)
    if conn.execute("SELECT 1 FROM persona_attempts WHERE state IN ('queued','running') LIMIT 1").fetchone():
        raise PersonaBusy('persona generation already in progress')
    attempt = conn.execute('''INSERT INTO persona_attempts(source,base_version_id,state,material_json,created_at)
        VALUES(?,?,'queued',?,?)''', (source, expected_version, dumps({'settings': _settings(conn)}), stamp)).lastrowid
    _operation(conn, 'persona_generation_requested', attempt, stamp, actor=actor, object_type='persona_attempt')
    return attempt


class PersonaJobs:
    """One service-owned worker; accepted attempts and terminal outcomes persist."""
    def __init__(self, store, gateway, *, clock=utc_now):
        self.store, self.engine = store, PersonaEngine(store, gateway, clock=clock)
        self._lock, self._closing = Lock(), False
        # Service startup holds the existing exclusive StoreLease. A crashed
        # attempt is reported as interrupted, never silently regenerated.
        with store.write() as conn:
            rows = conn.execute("SELECT id FROM persona_attempts WHERE state IN ('queued','running')").fetchall()
            for row in rows:
                conn.execute("UPDATE persona_attempts SET state='failed',reason='interrupted',finished_at=? WHERE id=?",
                             (_stamp(clock), row['id']))
                _operation(conn, 'persona_generation_interrupted', row['id'], _stamp(clock), object_type='persona_attempt')
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='iris-persona')

    def submit(self, *, expected_version):
        with self._lock:
            if self._closing:
                raise PersonaBusy('persona generation is stopping')
            with self.store.write() as conn:
                attempt = _reserve_attempt(conn, expected_version, 'regenerate', _stamp(self.engine.clock), actor='admin')
            try:
                self._executor.submit(self._work, expected_version, attempt)
            except Exception:
                self.engine._finish_attempt(attempt, 'failed', 'worker_unavailable', {})
                raise PersonaBusy('persona worker unavailable') from None
        return attempt

    def _work(self, expected_version, attempt):
        try:
            self.engine._run(expected_version, 'regenerate', only_if_due=False, attempt_id=attempt)
        except Exception:
            # Engine persists a safe terminal reason; never expose/log arbitrary
            # provider exceptions or silently retry a possibly completed request.
            pass

    def close(self):
        with self._lock:
            self._closing = True
        self._executor.shutdown(wait=True)


class PersonaEngine:
    def __init__(self, store, gateway, *, clock=utc_now):
        self.store, self.gateway, self.clock = store, gateway, clock

    def update(self, *, expected_version):
        return self._run(expected_version, 'periodic', only_if_due=True)

    def regenerate(self, *, expected_version):
        return self._run(expected_version, 'regenerate', only_if_due=False)

    def _call(self, purpose, payload, outputs):
        """One 120s request budget includes HTTP retries and at most one JSON repair."""
        gateway, owned = self.gateway, False
        if gateway is None:
            raise ModelError('paused', 'model unconfigured', paused=True, reason='unconfigured')
        if isinstance(gateway, Gateway) and gateway.health is None:
            from .model_health import ModelHealth
            health = ModelHealth(self.store, gateway._raw_configs, clock=self.clock)
            gateway = Gateway(gateway._raw_configs, self.store, client=gateway.client, health=health,
                              clock=self.clock, sleeper=gateway.sleeper, monotonic=gateway.monotonic, jitter=gateway.jitter)
            owned = True
        import time
        monotonic = getattr(gateway, 'monotonic', time.monotonic)
        deadline = monotonic() + 120
        prompt = files('iris').joinpath(f'prompts/{purpose}_v1.md').read_text(encoding='utf-8')
        messages = [{'role':'system','content':prompt}, {'role':'user','content':dumps(payload)}]
        try:
            for repair in (False, True):
                health = getattr(gateway, 'health', None)
                if health is not None:
                    # ModelHealth's existing learning gate includes the shared daily
                    # budget. Gateway still records the actual persona purpose.
                    health.check('chat', 'learning')
                label = purpose + ('_repair' if repair else '')
                reply = gateway.chat(messages, label, max_tokens=16000, _deadline=deadline)
                outputs[label] = reply.content
                try:
                    if reply.finish_reason == 'length':
                        raise ValueError('finish_reason=length')
                    result, _ = parse_json_object_with_status(reply.content)
                    return result
                except ValueError:
                    if repair:
                        raise ModelError('invalid_output', 'JSON parse failed after repair')
                    messages += [{'role':'assistant','content':reply.content},
                        {'role':'user','content':'上次不是完整的 JSON 对象。只输出修正后的完整 JSON，不要解释。'}]
        finally:
            if owned:
                gateway.close()

    def _finish_attempt(self, attempt, state, reason, outputs, version_id=None):
        with self.store.write() as conn:
            conn.execute('''UPDATE persona_attempts SET state=?,reason=?,outputs_json=?,version_id=?,finished_at=?
                WHERE id=?''', (state,reason,dumps(outputs),version_id,_stamp(self.clock),attempt))
            _operation(conn, 'persona_generation_' + state, attempt, _stamp(self.clock), object_type='persona_attempt')

    def _run(self, expected_version, source, *, only_if_due, attempt_id=None):
        if attempt_id is None:
            with self.store.write() as conn:
                attempt_id = _reserve_attempt(conn, expected_version, source, _stamp(self.clock))
        attempt, outputs = attempt_id, {}
        try:
            with self.store.read() as conn:
                previous = _expect(conn, expected_version)
                reserved = conn.execute('SELECT * FROM persona_attempts WHERE id=?', (attempt,)).fetchone()
                if (reserved is None or reserved['state'] != 'queued' or reserved['source'] != source
                        or reserved['base_version_id'] != expected_version):
                    raise PersonaConflict('generation reservation changed')
                due = _due(conn, previous, self.clock())
                settings = json.loads(reserved['material_json'])['settings']
                material = {'evidence':_select(conn, timezone_name=settings['timezone']), 'settings':settings, 'as_of':_stamp(self.clock),
                            'role_name':_setting(conn, 'role_name', 'Iris'),
                            'previous':previous['content'], 'previous_sentences':previous['sentences'],
                            'admin_sentences':{k:v['text'] for k,v in _admin_sentences(previous).items()}}
                snapshot = _self_snapshot(conn)
            with self.store.write() as conn:
                _expect(conn, expected_version)
                conn.execute("UPDATE persona_attempts SET state='running',material_json=? WHERE id=?", (dumps(material),attempt))
            reason = 'not_due' if only_if_due and not due['due'] else None
            if not material['evidence']['memories'] and not material['admin_sentences']:
                reason = reason or 'no_self_evidence'
            if reason:
                self._finish_attempt(attempt, 'skipped', reason, outputs)
                return {'status':'skipped','reason':reason,'attempt_id':attempt,'version':None,'due':due}
            payload = {**settings, 'evidence':material['evidence'], 'role_name':material['role_name'],
                       'as_of':material['as_of'],
                       'previous':material['previous'], 'admin_sentences':material['admin_sentences']}
            generated = self._call('persona_generate', payload, outputs)
            content, sentences, deterministic = _deterministic(generated, material, previous)
            model, degree, model_errors = None, 'large', []
            if deterministic['passed']:
                # Also supplies the persisted "checking" progress projection.
                with self.store.write() as conn:
                    conn.execute('UPDATE persona_attempts SET outputs_json=? WHERE id=?', (dumps(outputs),attempt))
                model = self._call('persona_check', {**payload,'candidate':sentences}, outputs)
                degree, model_errors = _model_check(model, sentences)
            manual_removed = _admin_removed(previous, sentences)
            if manual_removed:
                degree = 'large'
            passed = deterministic['passed'] and not model_errors
            checks = {'passed':passed,'deterministic':deterministic,'model':model,
                      'model_errors':model_errors,'admin_content_removed_or_changed':manual_removed}
            mode = settings['publish_mode']
            status = 'rejected' if not passed else ('current' if mode == 'all_auto' or (
                mode == 'small_medium_auto' and degree in ('small','medium')) else 'pending')
            with self.store.write() as conn:
                _expect(conn, expected_version)
                candidate = {'sentences':sentences, 'material':material}
                stale = _stale(conn, candidate, check_sources=True)
                if stale:
                    state, version, reason = 'conflict', None, 'evidence_changed'
                else:
                    state, reason = status, None
                    version = _insert(conn, content=content, sentences=sentences, checks=checks, degree=degree,
                        status=status, source=source, stamp=_stamp(self.clock), base=expected_version,
                        material=material, snapshot=snapshot)
                conn.execute('''UPDATE persona_attempts SET state=?,reason=?,outputs_json=?,version_id=?,finished_at=? WHERE id=?''',
                    (state,reason,dumps(outputs),version['id'] if version else None,_stamp(self.clock),attempt))
                _operation(conn, 'persona_generation_' + state, attempt, _stamp(self.clock), object_type='persona_attempt')
            return {'status':state,'reason':reason,'attempt_id':attempt,'version':version,'due':due}
        except PersonaConflict:
            self._finish_attempt(attempt, 'conflict', 'current_version_changed', outputs)
            raise
        except ModelError as error:
            state = 'skipped' if error.paused else 'failed'
            reason = error.reason or error.category
            self._finish_attempt(attempt, state, reason, outputs)
            return {'status':state,'reason':reason,'attempt_id':attempt,'version':None,'due':due}
        except Exception:
            self._finish_attempt(attempt, 'failed', 'unexpected_error', outputs)
            raise
