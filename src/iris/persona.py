"""Evidence-bound persona versions. Models run outside transactions; publication uses CAS.

The old content/is_current projection is intentionally unchanged. No caller of
learning or retrieval needs to opt into persona regeneration to keep using it.
"""
from __future__ import annotations

import copy
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
from .search_text import words

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
RELATIVE_TIMES = frozenset(('今天 今日 昨天 昨日 明天 明日 前天 后天 昨晚 今晚 明晚 昨夜 今夜 今早 今晨 '
                           '今年 去年 明年 上周 本周 这周 下周 上星期 本星期 这星期 下星期 '
                           '上个月 本月 这个月 下个月 最近 近日 近来 刚才 刚刚 现在 目前').split())
PROMPT_VERSIONS = {'persona_generate': 'persona_generate_v11', 'persona_check': 'persona_check_v15',
                   'persona_sentence_repair': 'persona_repair_v10', 'persona_recheck': 'persona_check_v15'}
CALL_TIMEOUTS = {'persona_generate': 180, 'persona_check': 180,
                 'persona_sentence_repair': 120, 'persona_recheck': 180}
CALL_MAX_TOKENS = {'persona_generate':6144, 'persona_check':6144,
                   'persona_sentence_repair':2048, 'persona_recheck':6144}
PERSONA_KINDS = ('knowledge', 'episode', 'setting', 'admin', 'state', 'task', 'instruction')
EXCLUDED_KINDS = {'state': 'current state is not persona', 'task': 'pending action is not persona',
                  'instruction': 'host/model instruction is not persona'}
CHECK_BATCH_SIZE = 4
CHECK_BATCH_CLAUSES = 2
MAX_CHECK_STAGE_SECONDS = 900
REPAIR_BATCH_SIZE = 2
MAX_REPAIR_STAGE_SECONDS = 180
BOUNDED_SCENE = re.compile(r'(?:这|那|当|某|本|同)(?:一)?(?:次|场|回|天)|\d{4}-\d{2}-\d{2}|[0-9零〇一二三四五六七八九十廿三]+(?:年|月|日|号)')
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


def _select(conn, token_limit=BASIS_TOKENS, *, timezone_name=None, required_ids=()):
    zone = ZoneInfo(timezone_name or _setting(conn, 'timezone', 'Asia/Shanghai'))
    selected = []
    reserved = ",".join("?" for _ in required_ids) or "NULL"
    rows = conn.execute(f'''SELECT m.*,e.kind AS entry_kind FROM memories m
        JOIN memory_subjects a ON a.memory_id=m.id LEFT JOIN entries e ON e.id=m.entry_id
        WHERE a.subject_id='self' AND m.lifecycle='active'
        AND (m.speaker_subject_id='self' OR m.stance='设定')
        ORDER BY CASE WHEN m.id IN ({reserved}) THEN 0 ELSE 1 END,
        m.pinned DESC,m.importance DESC,m.retention DESC,m.id''', tuple(required_ids))
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


def _template_snapshot(conn):
    """Only the latest actual setup is authoritative, never a generated predecessor.

    Keep each complete template sentence verbatim. If any basis has changed,
    omit that sentence rather than reconstructing obsolete facts from settings.
    """
    row = conn.execute("SELECT * FROM persona_versions WHERE source='initial_setting' ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return [], []
    initial = _decode(conn, row)
    locked, omitted = [], []
    for sentence in initial['sentences']:
        if sentence['origin'] != 'initial_template':
            continue
        if not sentence['basis']:
            # Setup's name/header and empty-background placeholder have no memory
            # binding. Keep them in the initial version and role settings only.
            continue
        stale = _stale(conn, {'sentences':[sentence], 'material':{}})
        if stale:
            omitted.append({'sentence':sentence, 'reason':'template_evidence_changed', 'basis':stale})
        else:
            locked.append(sentence)
    return locked, omitted


def _generation_material(conn, settings, previous, stamp):
    locked, omitted = _template_snapshot(conn)
    required = sorted({b['memory_id'] for s in locked for b in s['basis']})
    evidence = _select(conn, timezone_name=settings['timezone'], required_ids=required)
    available = {(m['memory_id'],m['revision']):m for m in evidence['memories']}
    retained = []
    for sentence in locked:
        if all((b['memory_id'],b['revision']) in available for b in sentence['basis']):
            dates = sorted({date for b in sentence['basis']
                            for date in available[(b['memory_id'],b['revision'])]['dates']})
            retained.append({**sentence, 'dates':dates, 'date_count':len(dates)})
        else:
            omitted.append({'sentence':sentence, 'reason':'template_evidence_over_budget'})
    return {'evidence':evidence, 'settings':settings, 'as_of':stamp,
        'prompt_versions':PROMPT_VERSIONS, 'timeouts_seconds':CALL_TIMEOUTS, 'output_token_limits':CALL_MAX_TOKENS, 'check_batch_size':CHECK_BATCH_SIZE,
        'max_check_stage_seconds':MAX_CHECK_STAGE_SECONDS, 'check_batch_clauses':CHECK_BATCH_CLAUSES,
        'repair_batch_size':REPAIR_BATCH_SIZE, 'max_repair_stage_seconds':MAX_REPAIR_STAGE_SECONDS,
        'role_name':_setting(conn, 'role_name', 'Iris'), 'locked_sentences':retained,
        'omitted_template_sentences':omitted, 'previous':previous['content'],
        'previous_sentences':previous['sentences'],
        'admin_sentences':{k:v['text'] for k,v in _admin_sentences(previous).items()}}


def _editable_material(material):
    locked_ids = {b['memory_id'] for s in material.get('locked_sentences',[]) for b in s['basis']}
    evidence = {**material['evidence'], 'memories':[m for m in material['evidence']['memories']
                                                 if m['memory_id'] not in locked_ids]}
    evidence['estimated_tokens'] = estimate_tokens(dumps(evidence['memories']))
    return {**material, 'evidence':evidence}


def _final_deterministic(sentences, material, previous):
    """Revalidate the assembled text; template attribution cannot be model-forged."""
    locked = material.get('locked_sentences', [])
    refs = {(m['memory_id'],m['revision']):m['ref'] for m in material['evidence']['memories']}
    manual = _admin_sentences(previous)
    errors = []
    for index, sentence in enumerate(sentences,1):
        if sentence['origin'] == 'initial_template':
            if sentence not in locked:
                errors.append(f'sentence {index}: invalid locked template')
            continue
        basis = [refs.get((b['memory_id'],b['revision'])) for b in sentence['basis']]
        row = {'text':sentence['text'], 'basis':basis}
        if sentence['origin'] == 'admin':
            row['admin_sentence'] = next((k for k,v in manual.items() if v['text']==sentence['text']), 'unknown')
        _, normalized, checked = _deterministic({'sentences':[row]}, _editable_material(material), previous)
        errors.extend(f'sentence {index}: {e}' for e in checked['errors'])
        if normalized != [sentence]:
            errors.append(f'sentence {index}: evidence metadata changed')
    content = ''.join(s['text'] for s in sentences)
    errors.extend(_text_errors(content))
    return content, {'passed':not errors, 'errors':errors,
        'warnings':['under 300 characters: concise evidence-bound text is allowed'] if 0<len(content)<300 else []}


def _clause_parts(text):
    # Focus spans, not a claim to parse Chinese syntax. The complete sentence is
    # always supplied so scope/negation/subjects still need semantic checking.
    return [{'index': i, 'text': part.strip()} for i, part in enumerate(
        (part for part in re.split(r'[，,；;]', text) if part.strip()), 1)]



def _check_batches(sentences):
    """Bound proof work while each window retains its entire sentence context."""
    views = []
    for index,sentence in enumerate(sentences,1):
        base = {**sentence, '_check_sentence':index}
        if sentence['origin'] == 'memory' and not sentence.get('initial_setting'):
            parts = _clause_parts(sentence['text'])
            windows = [parts[i:i+CHECK_BATCH_CLAUSES] for i in range(0,len(parts),CHECK_BATCH_CLAUSES)] or [[]]
            views.extend({**base, '_check_focus':window} for window in windows)
        else:
            views.append(base)  # Protected/setting rows have no automatic proofs.
    batches, batch, work = [], [], 0
    for view in views:
        cost = len(view.get('_check_focus',[]))
        if batch and (len(batch) >= CHECK_BATCH_SIZE or work + cost > CHECK_BATCH_CLAUSES):
            batches.append(batch)
            batch, work = [], 0
        batch.append(view)
        work += cost
    if batch or not batches:
        batches.append(batch)
    return batches


def _model_memory(memory, *, proof_choices=False):
    """Project a single authoritative prose source; keep the full audit elsewhere.

    A summary and its source are not independent witnesses. Showing both invites
    a model to rationalize the stronger summary (e.g. a request made into an
    accomplished act). Without excerpts, existing memory text remains available.
    """
    fields = ('ref', 'memory_id', 'revision', 'initial_sources', 'stance',
              'speaker_subject_id', 'about', 'date', 'event_time', 'entry_kind',
              'dates', 'date_count', 'belief')
    result = {key: memory[key] for key in fields}
    result['excerpts'] = [dict(source, source_index=index,
        **({'quote_spans': _clause_parts(source['excerpt'])} if proof_choices else {}))
        for index, source in enumerate(memory['excerpts'], 1)]
    if not memory['excerpts']:
        result['content'] = memory['content']
        if proof_choices:
            result['content_spans'] = _clause_parts(memory['content'])
    return result


def _check_payload(material, sentences, *, full_content, proof_choices=True):
    # No unbound memories, former proofs, trace graph or self snapshot in the
    # checker. Every sentence carries only its own currently frozen evidence.
    memories = {(m['memory_id'],m['revision']):m for m in material['evidence']['memories']}
    rows, evidence = [], {}
    for index, sentence in enumerate(sentences, 1):
        refs = []
        for basis in sentence['basis']:
            memory = memories[(basis['memory_id'],basis['revision'])]
            refs.append(memory['ref'])
            evidence[memory['ref']] = _model_memory(memory, proof_choices=proof_choices)
        row = {k:v for k,v in sentence.items() if k not in ('_check_sentence','_check_focus')}
        row.update(index=index, evidence_refs=refs,
                   clauses=sentence.get('_check_focus',_clause_parts(sentence['text'])),
                   full_clause_count=len(_clause_parts(sentence['text'])))
        if sentence['origin'] == 'initial_template':
            # The whole original background must not become an extra fact source
            # attached to a name sentence or to a partially omitted template.
            row['initial_settings'] = {'template_text':sentence['text']}
            if not sentence['basis']:
                row['initial_settings']['role_name'] = material['role_name']
        rows.append(row)
    return {**material['settings'], 'task':'check' if sentences else 'degree_only',
            'role_name':material['role_name'], 'as_of':material['as_of'], 'evidence_by_ref':evidence,
            'candidate':rows, 'previous':material['previous'], 'candidate_text':full_content}



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


def _completion_witness(predicate, source):
    # An explicit aspect cue for the same predicate is necessary here, not proof
    # of attribution, negation, event date or entailment. Those remain model checks.
    stem = re.escape(predicate)
    for clause in re.split(r'[。！？；，.!?;,\n]', source):
        if re.search(stem + r'.*?(?:了|过)', clause):
            return True
        if re.search(r'(?:已经|已然|早已|已|刚才|刚刚|刚)(?:(?:把|将).*?)?' + stem, clause):
            return True
    return False


def _bound_source_issues(sentence, tokens, memories):
    if memories is None or sentence['initial_setting']:
        return []
    refs = {(b['memory_id'], b['revision']) for b in sentence.get('basis', [])}
    bound = [m for m in memories if (m['memory_id'], m['revision']) in refs]
    issues = []
    sources = []
    for memory in bound:
        originals = [e['excerpt'] for e in memory.get('excerpts', [])
                     if isinstance(e, dict) and isinstance(e.get('excerpt'), str) and e['excerpt'].strip()]
        # Existing self memories without message sources can use their own text;
        # when originals exist, a completed summary cannot override an intention.
        sources.extend(originals or [memory['content']])
    # An entry is a context, not proof that all its events are fictional. Preserve
    # an explicitly sourced real-world qualification instead of inventing a game.
    real_scope = {'现实', '线下'} & set(tokens)
    source_real_scope = {word for source in sources for word in words(source)} & real_scope
    if (bound and all(m.get('entry_kind') == 'game' for m in bound)
            and not {'游戏', '联机'} & set(tokens) and not source_real_scope):
        issues.append('game-only evidence needs explicit scope: 自动句须明确游戏或联机场景；'
                      '现实／线下限定须来自本句绑定原话，不能仅按动作名称推定现实经历')
    predicates = set()
    for clause in re.split(r'[。！？；，.!?;,\n]', sentence['text']):
        offsets, position = {0}, 0
        for token in words(clause):
            position += len(token)
            offsets.add(position)
        for match in re.finditer(r'(?=((?:完成|[\u3400-\u9fff]完)(?:[了过])?))', clause):
            # 完整/完全/完善 are lexical units, not resultative predicates.
            # The result suffix must end at a segmentation boundary.
            resultative = match.group(1)
            if match.start() + len(resultative) not in offsets:
                continue
            prefix = clause[:match.start()]
            explicit_aspect = resultative.endswith(('了', '过'))
            scope_tokens = words(prefix)
            scope = set(scope_tokens) | {''.join(scope_tokens[i:i+2]) for i in range(len(scope_tokens)-1)}
            if scope & {'能', '能够', '可以', '假装', '装作', '而非'}:
                continue
            # A bare result in an affirmative assertion also needs a result
            # witness. Negation/intention/preference in a different clause cannot
            # license it. This gate is deliberately only a necessary boundary.
            non_result = re.search(r'没有|没能|未能|尚未|未曾|不曾|未|不|打算|计划|想要|希望|准备|喜欢|偏好|倾向|愿意', prefix)
            if explicit_aspect or not non_result:
                predicates.add(resultative.rstrip('了过'))
    for predicate in sorted(predicates):
        if not any(_completion_witness(predicate, source) for source in sources):
            issues.append('completion aspect needs original source support: '
                          f'“{predicate}”的结果断言须在本句绑定原话中有同一动作的明确完成表述，动作目标不能补成结果')
    return issues


def _surface_issues(sentence, *, memories=None):
    """Conservative wording gates; these do not establish semantic support."""
    if sentence['origin'] != 'memory':
        return []
    text, issues = sentence['text'], []
    tokens = list(words(text))
    # Whole tokens avoid matching 其他/他人/吉他. Do not guess a pronoun's
    # referent or rewrite it into an invented name, including the reader's 你.
    if {'他','她','他们','她们','他俩','她俩','你','您','你们','您们','妳','妳们'} & set(tokens):
        issues.append('neutral person reference required: 自动句使用本句依据中的姓名或“对方”，不要用他／她／你及其复数指称')
    # Match whole tokens, including split phrases such as 今/早 and 这个/月.
    # Substrings like 体现在 or 接近日落 do not contain a relative-time phrase.
    if not sentence['initial_setting'] and any(
            ''.join(tokens[i:i+size]) in RELATIVE_TIMES
            for i in range(len(tokens)) for size in (1, 2, 3)):
        # Never resolve an event date from the request date: a source may describe
        # a past event. Reject this wording for repair, preserving raw evidence.
        issues.append('relative time is not durable: 自动句须使用有据的事件日期或具体活动，不保留相对日历时间')
    issues.extend(_bound_source_issues(sentence, tokens, memories))
    if not sentence['initial_setting'] and sentence['date_count'] < 2:
        # This is the same necessary boundary used by the proof validator. Apply
        # it before selecting a repair that could never pass the later check.
        if not BOUNDED_SCENE.search(text):
            issues.append('single-date/unknown-date evidence needs an explicit bounded scene in this sentence')
        # Narrow style restrictions, not a general Chinese entailment parser.
        # In particular, a union of multiple dates is not proof for every clause;
        # the semantic checker must still verify each claim's scope independently.
        habitual = re.search(r'一向|向来|通常|经常|惯常|总是|每次|每逢|每当|往往|习惯于', text)
        opening = text.strip().split('，', 1)[0].split(',', 1)[0]
        conditional = re.match(r'^(?:在)?(?:遇到|碰到)|^(?:如果|只要|一旦)', opening)
        bounded = re.search(r'(?:这|那|本|当)(?:一)?(?:次|天|场|回)', opening)
        if habitual or (conditional and not bounded):
            issues.append('single-date/unknown-date evidence cannot use habitual or unbounded conditional wording')
    return issues


def _repair_options(sentence, material):
    """One complete bound memory, never an arbitrary span or token deletion.

    Keep context and negation intact, and bind only the selected record. Message
    excerpts can be truncated or contain another speaker's quote, so are not
    extraction options. The subsequent semantic check still compares sources.
    """
    if sentence['origin'] != 'memory':
        return []
    bound = {(b['memory_id'], b['revision']) for b in sentence['basis']}
    options, seen = [], set()
    for memory in material['evidence']['memories']:
        if (memory['memory_id'], memory['revision']) not in bound or memory['stance'] == '设定':
            continue
        text = memory['content'].strip()
        if not text or len(split_sentences(text)) != 1:
            continue
        if text[-1] not in '。！？!?.”’」』"':
            text += '。'
        if text not in seen:
            options.append({'text': text, 'basis': [memory['ref']]})
            seen.add(text)
    return options


def _validate_clause_checks(output, payload):
    """Check quote identity and per-focus dates; never assert semantic entailment."""
    errors, problems = [], {}
    rows = output.get('sentences')
    candidates = payload['candidate']
    if not isinstance(rows, list) or len(rows) != len(candidates):
        return ['clause checks must cover every sentence'], {}
    for sentence, row in zip(candidates, rows):
        index = sentence['index']
        if isinstance(row, dict):
            row.pop('unchecked_clause_indices', None)  # Computed here, never trusted from the model.
        if sentence['origin'] != 'memory' or sentence.get('initial_setting'):
            continue
        parts = row.get('clauses') if isinstance(row, dict) else None
        focus = sentence['clauses']
        # One explicit negative proves the whole conjunction false. This is a
        # rejection-only protocol: a retained sentence still needs every focus.
        # Preserve what was not checked rather than inventing verdicts for it.
        stopped = (isinstance(row, dict) and row.get('stop_after_failure') is True
            and row.get('supported') is False and isinstance(parts, list)
            and 0 < len(parts) <= len(focus) and isinstance(parts[-1], dict)
            and parts[-1].get('supported') is False)
        if (isinstance(row, dict) and 'stop_after_failure' in row
                and (type(row['stop_after_failure']) is not bool
                     or (row['stop_after_failure'] and not stopped))):
            errors.append(f'sentence {index}: invalid rejection short circuit')
            continue
        if not isinstance(parts, list) or (len(parts) != len(focus) and not stopped):
            errors.append(f'sentence {index}: clause checks must cover every focus span, in order')
            continue
        issues, errors_before = [], len(errors)
        for expected, part in zip(focus, parts):
            label = f"clause {expected['index']}"
            if (not isinstance(part, dict) or type(part.get('index')) is not int
                    or part['index'] != expected['index']
                    or any(type(part.get(k)) is not bool for k in ('supported', 'scene_qualified'))
                    or not isinstance(part.get('reason'), str) or not part['reason'].strip()
                    or not isinstance(part.get('evidence'), list)):
                errors.append(f'sentence {index}: {label}: invalid clause verdict')
                continue
            dates, valid = set(), 0
            for proof in part['evidence']:
                ref = proof.get('ref') if isinstance(proof, dict) else None
                n = proof.get('source_index') if isinstance(proof, dict) else None
                quote = proof.get('quote') if isinstance(proof, dict) else None
                memory = payload['evidence_by_ref'].get(ref) if isinstance(ref, str) else None
                source = None
                if memory is not None and ref in sentence['evidence_refs'] and type(n) is int:
                    excerpts = memory.get('excerpts', [])
                    if 1 <= n <= len(excerpts):
                        source = excerpts[n-1]
                    elif n == 0 and not excerpts:
                        source = {'excerpt': memory['content'], 'date': memory.get('date')}
                if source is not None and 'quote_index' in proof:
                    choice = proof['quote_index']
                    spans = _clause_parts(source['excerpt'])
                    selected = (spans[choice-1]['text']
                                if type(choice) is int and 1 <= choice <= len(spans) else None)
                    # Never choose between conflicting alternatives. Old exact-quote
                    # outputs remain readable; an indexed output must be unambiguous.
                    if selected is None or ('quote' in proof and quote != selected):
                        source = None
                    else:
                        quote = selected
                        proof['quote'] = selected
                if (source is None or not isinstance(quote, str) or not quote.strip()
                        or quote not in source['excerpt']):
                    issues.append(f'{label}: proof must quote an exact own bound original source')
                    continue
                valid += 1
                if source.get('date'):
                    dates.add(source['date'])
            if not part['supported']:
                issues.append(f"{label}: {part['reason']}")
            elif not valid:
                issues.append(f'{label}: no valid original proof')
            if part['supported'] and len(dates) < 2:
                if not part['scene_qualified']:
                    issues.append(f'{label}: general claim needs at least two distinct dates of its own proof')
                elif not BOUNDED_SCENE.search(sentence['text']):
                    issues.append(f'{label}: single-date claim needs an explicit bounded scene in this sentence')
        if stopped and len(parts) < len(focus) and len(errors) == errors_before:
            row['unchecked_clause_indices'] = [part['index'] for part in focus[len(parts):]]
        if issues:
            problems[index] = issues
    return errors, problems


def _valid_sentence_verdict(row, index, *, require_kind=False):
    return (isinstance(row, dict) and type(row.get('index')) is int and row['index'] == index
            and all(type(row.get(k)) is bool for k in ('supported','fabricated','scene_qualified'))
            and isinstance(row.get('reason'), str) and bool(row['reason'].strip())
            and isinstance(row.get('violations'), list)
            and all(isinstance(v, str) and bool(v.strip()) for v in row['violations'])
            and (not require_kind or row.get('persona_kind') in PERSONA_KINDS))


def _check_verdicts(output, sentences, *, material=None):
    errors, problems = list(output.get('validation_errors', [])), {}
    memories = material['evidence']['memories'] if material is not None else None
    degree = output.get('change_degree')
    degree = {'小':'small', '中':'medium', '大':'large'}.get(degree, degree) if isinstance(degree, str) else None
    if degree not in DEGREES:
        errors.append('invalid change degree')
        degree = 'large'
    rows = output.get('sentences')
    if not isinstance(rows, list) or len(rows) != len(sentences):
        return degree, errors + ['model check must cover every sentence, in order'], {}
    if not isinstance(output.get('reason'), str) or not output['reason'].strip():
        errors.append('model check reason required')
    for index, (row, sentence) in enumerate(zip(rows, sentences), 1):
        if not _valid_sentence_verdict(row, index):
            errors.append(f'sentence {index}: invalid model verdict')
            continue
        issues = _surface_issues(sentence, memories=memories) + row.get('clause_errors', [])
        if isinstance(row.get('persona_kind'), str) and row['persona_kind'] in EXCLUDED_KINDS:
            issues.append(EXCLUDED_KINDS[row['persona_kind']])
        if not row['supported'] or row['fabricated'] or row['violations']:
            issues.append(row['reason'])
        if (sentence['origin'] == 'memory' and not sentence['initial_setting']
                and sentence['date_count'] < 2 and not row['scene_qualified']):
            issues.append('single-date/unknown-date evidence needs a scene qualifier')
        if issues:
            problems[index] = issues
    return degree, errors, problems


def _model_check(output, sentences, *, material=None):
    degree, errors, problems = _check_verdicts(output, sentences, material=material)
    return degree, errors + [f'sentence {i}: {reason}' for i, reasons in problems.items() for reason in reasons]


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
    regenerate = bool(version['checks'].get('needs_regeneration'))
    due = regenerate or len(changed) >= 5 or (bool(changed) and elapsed >= timedelta(days=7))
    return {'due': due, 'reason': 'needs_regeneration' if regenerate else 'five_changes' if len(changed) >= 5 else 'seven_days' if due else 'not_due',
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
        if old['status'] not in ('current','history') or old['checks'].get('redacted') or not old['content'].strip():
            raise ValueError('rollback requires a previously published non-redacted, non-empty version')
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

    def _call(self, purpose, payload, outputs, *, deadline=None):
        """Per-purpose total budget includes HTTP retries and at most one JSON repair."""
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
        deadline = min(deadline, monotonic() + CALL_TIMEOUTS[purpose]) if deadline is not None else monotonic() + CALL_TIMEOUTS[purpose]
        prompt = files('iris').joinpath(f'prompts/{PROMPT_VERSIONS[purpose]}.md').read_text(encoding='utf-8')
        messages = [{'role':'system','content':prompt}, {'role':'user','content':dumps(payload)}]
        try:
            for repair in (False, True):
                if monotonic() >= deadline:
                    raise ModelError('timeout', 'total timeout', reason='timeout')
                health = getattr(gateway, 'health', None)
                if health is not None:
                    # ModelHealth's existing learning gate includes the shared daily
                    # budget. Gateway still records the actual persona purpose.
                    health.check('chat', 'learning')
                label = purpose + ('_repair' if repair else '')
                reply = gateway.chat(messages, label, max_tokens=CALL_MAX_TOKENS[purpose], _deadline=deadline)
                outputs[label] = reply.content
                outputs[label + '_finish_reason'] = reply.finish_reason
                if monotonic() >= deadline:
                    raise ModelError('timeout', 'total timeout', reason='timeout')
                if reply.finish_reason == 'length':
                    # Exhausted reasoning/output is not malformed JSON to retry.
                    # Keep the raw result; no partial or parseable fragment passes.
                    raise ModelError('invalid_output', 'output budget exhausted', reason='output_limit')
                try:
                    result, _ = parse_json_object_with_status(reply.content)
                    if set(result) == {'answer'} and isinstance(result['answer'], dict):
                        outputs[label + '_normalizations'] = ['unwrap_single_answer']
                        result = result['answer']
                    return result
                except ValueError:
                    if repair:
                        raise ModelError('invalid_output', 'JSON parse failed after repair')
                    messages += [{'role':'assistant','content':reply.content},
                        {'role':'user','content':'上次不是完整的 JSON 对象。只输出修正后的完整 JSON，不要解释。'}]
        except ModelError as error:
            # The gateway's late-response path records timed_out but may omit the
            # reason. Keep the attempt diagnostic consistent with that call record.
            if error.reason is None and error.summary in {
                    'total timeout', 'ReadTimeout', 'WriteTimeout', 'ConnectTimeout', 'PoolTimeout'}:
                error.reason = 'timeout'
            raise
        finally:
            if owned:
                gateway.close()

    def _check_all(self, purpose, material, sentences, *, full_content, outputs):
        """Check small focus windows, then AND facts and union failures per sentence."""
        import time
        subsets = _check_batches(sentences)
        stage_seconds = min(MAX_CHECK_STAGE_SECONDS, CALL_TIMEOUTS[purpose] * len(subsets))
        deadline = getattr(self.gateway, 'monotonic', time.monotonic)() + stage_seconds
        raw_batches = outputs.setdefault(purpose + '_batches', [])
        rows, batches, degrees, reasons, incomplete, short_circuits = {}, [], [], [], [], []
        window_map = outputs.setdefault(purpose + '_windows', [])
        for batch_index,subset in enumerate(subsets):
            window_map.append([{'sentence_index':s['_check_sentence'],
                                'clause_indices':[p['index'] for p in s.get('_check_focus',[])]}
                               for s in subset])
            payload = _check_payload(material, subset, full_content=full_content)
            if batch_index:
                payload['task'] = 'check_facts_and_rules'
                payload.pop('previous')
                payload.pop('candidate_text')
            raw = {}
            raw_batches.append(raw)
            try:
                batch = self._call(purpose, payload, raw, deadline=deadline)
            except ModelError as error:
                if error.reason != 'output_limit':
                    raise
                if batch_index == 0:
                    outputs.update(raw)
                return {'sentences':[], 'change_degree':'large',
                        'reason':'incomplete check: output_limit',
                        'validation_errors':[f'batch {batch_index+1}: output_limit; no complete judgment'],
                        'batches':batches, 'window_map':window_map}
            if batch_index == 0:
                outputs.update(raw)
            checked_batch = {**batch, 'validation_errors': []}
            returned = batch.get('sentences')
            if isinstance(returned,list):
                checked_batch['sentences'] = [{**row,'clause_errors':[]} if isinstance(row,dict) else row
                                              for row in returned]
            mapped = (isinstance(returned, list) and len(returned) == len(subset)
                and all(isinstance(row, dict) and type(row.get('index')) is int and row['index'] == i
                        for i,row in enumerate(returned,1)))
            proof_errors, problems = [], {}
            if mapped:
                normalized = []
                for local,(sentence,row) in enumerate(zip(subset,returned),1):
                    row = {**copy.deepcopy(row), 'clause_errors': []}  # Preserve the raw model output.
                    errors, issues = _validate_clause_checks({'sentences':[row]},
                        {**payload, 'candidate':[payload['candidate'][local-1]]})
                    malformed = ([] if _valid_sentence_verdict(row,local,require_kind=True)
                                 else [f'sentence {local}: invalid model verdict']) + errors
                    if malformed:
                        incomplete.append(f'batch {batch_index+1}, sentence {local}: ' + '; '.join(malformed))
                    elif row.get('unchecked_clause_indices'):
                        short_circuits.append({'batch':batch_index+1,
                            'sentence_index':sentence['_check_sentence'],
                            'unchecked_clause_indices':row['unchecked_clause_indices']})
                    if isinstance(row.get('persona_kind'), str) and row['persona_kind'] in EXCLUDED_KINDS:
                        issues.setdefault(local, []).append(EXCLUDED_KINDS[row['persona_kind']])
                    if malformed and sentence['origin'] == 'memory' and not sentence.get('initial_setting'):
                        # Exact sentence mapping is known, but this fact has no valid
                        # verdict. Never accept it: send only this sentence to recovery.
                        row = {**row, 'supported':False, 'fabricated':False,
                               'scene_qualified':False, 'violations':[],
                               'reason':'invalid sentence check: ' + '; '.join(malformed)}
                        problems[local] = malformed + issues.get(local,[])
                    else:
                        proof_errors.extend(malformed)
                        problems.update(issues)
                    # A false/invalid proof cannot be overridden by a true row flag.
                    if _valid_sentence_verdict(row,local) and problems.get(local):
                        row['supported'] = False
                    normalized.append(row)
                checked_batch['sentences'] = normalized
            degree, structural, _ = _check_verdicts(checked_batch, subset, material=material)
            batches.append(batch)  # Keep raw malformed verdicts, never silently correct them.
            if structural or proof_errors:
                return {'sentences': [], 'change_degree': 'large', 'reason': 'invalid check batch',
                        'validation_errors': [f'batch {len(batches)}: {e}' for e in structural + proof_errors],
                        'batches': batches, 'window_map':window_map}
            for local,(sentence,row) in enumerate(zip(subset,checked_batch['sentences']),1):
                original = sentence['_check_sentence']
                part = {**row, 'index':original, 'clause_errors':problems.get(local,[]),
                        'clauses':row.get('clauses') if isinstance(row.get('clauses'),list) else []}
                if original not in rows:
                    rows[original] = part
                else:
                    previous = rows[original]
                    rows[original] = {**previous,
                        'supported':previous['supported'] and part['supported'],
                        'fabricated':previous['fabricated'] or part['fabricated'],
                        'scene_qualified':previous['scene_qualified'] and part['scene_qualified'],
                        'violations':list(dict.fromkeys(previous['violations']+part['violations'])),
                        'reason':previous['reason']+'；'+part['reason'],
                        'clauses':previous['clauses']+part['clauses'],
                        'unchecked_clause_indices':previous.get('unchecked_clause_indices',[])
                            + part.get('unchecked_clause_indices',[]),
                        'clause_errors':previous['clause_errors']+part['clause_errors']}
            degrees.append(degree)
            reasons.append(batch['reason'])
        return {'sentences':[rows[i] for i in sorted(rows)], 'change_degree':degrees[0],
                'reason':reasons[0], 'batches':batches, 'window_map':window_map,
                'incomplete_checks':incomplete, 'short_circuits':short_circuits}

    def _recover_sentences(self, original, initial_model, problems, material, previous, checks, outputs):
        payload = _check_payload(material, original, full_content=''.join(s['text'] for s in original), proof_choices=False)
        repairable = {i:original[i-1] for i in problems if original[i-1]['origin'] != 'initial_template'}
        proposed, replacements = {}, {}
        checks['repair_policy'] = 'bound_memory_extract_v1'
        checks['repair_options_rejected'] = []
        for i,sentence in repairable.items():
            replacements[i] = []
            for option in _repair_options(sentence,material):
                _,normalized,deterministic = _deterministic({'sentences':[option]},_editable_material(material),previous)
                issues = (_surface_issues(normalized[0],memories=material['evidence']['memories'])
                          if normalized else [])
                if not deterministic['passed'] or issues:
                    checks['repair_options_rejected'].append({'index':i,'option':option,
                        'reasons':deterministic['errors']+issues})
                else:
                    replacements[i].append(option)
        eligible = [i for i in repairable if replacements[i]]
        checks['repair_outputs'] = []
        if eligible:
            import time
            monotonic = getattr(self.gateway,'monotonic',time.monotonic)
            deadline = monotonic() + MAX_REPAIR_STAGE_SECONDS
            raw_batches = outputs.setdefault('persona_sentence_repair_batches',[])
            for start in range(0,len(eligible),REPAIR_BATCH_SIZE):
                indices = eligible[start:start+REPAIR_BATCH_SIZE]
                request = {**material['settings'], 'task':'repair', 'as_of':material['as_of'],
                    'repair_policy':checks['repair_policy'],
                    'replacement_text_by_ref':{option['basis'][0]:option['text']
                        for i in indices for option in replacements[i]},
                    'problems':[{'index':i,'text':original[i-1]['text'],'problems':problems[i],
                        'evidence_refs':[option['basis'][0] for option in replacements[i]],
                        'allowed_replacement_refs':[option['basis'][0] for option in replacements[i]]}
                        for i in indices]}
                refs = set(request['replacement_text_by_ref'])
                request['evidence_by_ref'] = {ref:record for ref,record in payload['evidence_by_ref'].items() if ref in refs}
                raw = {}
                raw_batches.append(raw)
                try:
                    repaired = self._call('persona_sentence_repair',request,raw,deadline=deadline)
                except ModelError as error:
                    if error.reason != 'output_limit':
                        raise
                    # This was the sentence's one permitted repair. No valid repair
                    # means deletion below, never use even parseable partial output.
                    repaired = {'repairs':[], 'error':'output_limit'}
                if start == 0:
                    outputs.update(raw)
                    checks['repair_output'] = repaired
                checks['repair_outputs'].append(repaired)
                rows = repaired.get('repairs')
                if (isinstance(rows,list) and len(rows)==len(indices)
                        and all(isinstance(r,dict) and type(r.get('index')) is int for r in rows)
                        and {r['index'] for r in rows}==set(indices)):
                    proposed.update({r['index']:r for r in rows})
        retained, verdicts, modifications, positions = [], [], [], []
        def deleted(index, stage, reasons, replacement=None):
            item = {'index':index, 'sentence':original[index-1], 'stage':stage, 'reasons':reasons}
            if replacement is not None:
                item['replacement'] = replacement
            checks['deleted_sentences'].append(item)
        for i, sentence in enumerate(original,1):
            if i not in problems:
                retained.append(sentence)
                verdicts.append(initial_model['sentences'][i-1])
                continue
            if i in repairable and not replacements[i]:
                rejected = [reason for item in checks['repair_options_rejected'] if item['index']==i
                            for reason in item['reasons']]
                deleted(i,'repair_options',problems[i]+['no deterministically eligible bound replacement']+rejected)
                continue
            row = proposed.get(i,{})
            text = row.get('text')
            if (not isinstance(text,str) or not text.strip() or text==sentence['text']
                    or set(row) != {'index','text'} or sentence['origin']=='initial_template'):
                deleted(i,'repair',problems[i]+['no valid changed sentence; immutable templates are never rewritten'])
                continue
            option = next((option for option in replacements[i] if option['text'] == text), None)
            if option is None:
                deleted(i,'repair_extract',problems[i]+['repair must match one complete bound memory text'],text)
                continue
            # The other original references cannot inflate this extraction's date
            # count. No new memory or revision can enter through model output.
            raw = {'text':text, 'basis':option['basis']}
            _, normalized, deterministic = _deterministic({'sentences':[raw]},_editable_material(material),previous)
            surface = _surface_issues(normalized[0], memories=material['evidence']['memories']) if normalized else []
            if not deterministic['passed'] or surface:
                # Known failures cannot become valid through another model pass.
                # Keep them out of semantic recheck; its degree sees the final text.
                deleted(i,'repair_deterministic',problems[i]+deterministic['errors']+surface,text)
                continue
            positions.append((len(retained),i))
            modifications.append(normalized[0])
            retained.append(normalized[0])
            verdicts.append(None)
        content = ''.join(s['text'] for s in retained)
        if not content.strip():
            return retained, None, 'large', ['all sentences removed']
        # Recheck only modified facts. Also reassess change degree for the assembled
        # final draft (an empty candidate array means degree-only after deletion).
        recheck = self._check_all('persona_recheck', material, modifications, full_content=content, outputs=outputs)
        checks['recheck'] = recheck
        degree, structural, bad = _check_verdicts(recheck,modifications,material=material)
        if structural:
            return retained, recheck, 'large', structural
        discarded = set()
        for j,(position,original_index) in enumerate(positions,1):
            if j in bad:
                discarded.add(position)
                deleted(original_index,'recheck',bad[j],retained[position]['text'])
            else:
                verdicts[position] = recheck['sentences'][j-1]
                checks['repaired_sentences'].append({'index':original_index,
                    'before':original[original_index-1], 'after':retained[position]})
        retained = [s for i,s in enumerate(retained) if i not in discarded]
        verdicts = [{**v,'index':i+1} for i,v in enumerate(v for j,v in enumerate(verdicts) if j not in discarded)]
        if discarded:
            # No third model pass: its degree described a draft that still contained
            # rejected repairs. Conservatively require confirmation of this final diff.
            degree = 'large'
            checks['degree_override'] = 'sentences_removed_after_recheck'
        if ''.join(s['text'] for s in retained) == previous['content']:
            degree = 'small'
        final = {'sentences':verdicts, 'change_degree':degree,
                 'reason':recheck['reason'], 'assembled_from':'initial_check_and_recheck'}
        return retained, final, degree, []

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
                material = _generation_material(conn, settings, previous, _stamp(self.clock))
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
            editable = _editable_material(material)
            visible_memories = [_model_memory(m) for m in editable['evidence']['memories']]
            visible_evidence = {**editable['evidence'], 'memories':visible_memories,
                                'estimated_tokens':estimate_tokens(dumps(visible_memories))}
            payload = {**settings, 'task':'generate', 'evidence':visible_evidence,
                       'locked_sentences':[{'text':s['text'],'origin':s['origin']} for s in material['locked_sentences']],
                       'role_name':material['role_name'],
                       'as_of':material['as_of'], 'previous':material['previous'],
                       'admin_sentences':material['admin_sentences'],
                       'remaining_characters':max(0,MAX_CHARS-sum(len(s['text']) for s in material['locked_sentences']))}
            generated = self._call('persona_generate', payload, outputs)
            _, generated_sentences, checked = _deterministic(generated, editable, previous)
            # An empty extension is valid: the intact template may be the entire candidate.
            if generated.get('sentences') == []:
                checked = {'passed':True, 'errors':[], 'warnings':[]}
            sentences = material['locked_sentences'] + generated_sentences
            content, deterministic = _final_deterministic(sentences, material, previous)
            deterministic['errors'] = checked['errors'] + deterministic['errors']
            deterministic['passed'] = not deterministic['errors']
            checks = {'original_sentence_count':len(sentences), 'deleted_sentences':[], 'repaired_sentences':[]}
            model, degree, model_errors = None, 'large', []
            if deterministic['passed']:
                with self.store.write() as conn:
                    conn.execute('UPDATE persona_attempts SET outputs_json=? WHERE id=?', (dumps(outputs),attempt))
                model = self._check_all('persona_check', material, sentences, full_content=content, outputs=outputs)
                degree, structural, problems = _check_verdicts(model, sentences, material=material)
                _, initial_errors = _model_check(model,sentences,material=material)
                checks.update(initial_model=model, initial_model_errors=initial_errors)
                model_errors = initial_errors
                if not structural and problems:
                    sentences, model, degree, model_errors = self._recover_sentences(
                        sentences, model, problems, material, previous, checks, outputs)
                    content, deterministic = _final_deterministic(sentences, material, previous)
            manual_removed = _admin_removed(previous, sentences)
            if manual_removed:
                degree = 'large'
            passed = deterministic['passed'] and not model_errors
            checks.update(passed=passed, deterministic=deterministic, model=model, model_errors=model_errors,
                          admin_content_removed_or_changed=manual_removed)
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
