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
from threading import Event, Lock
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from importlib.resources import files
from zoneinfo import ZoneInfo

import httpx

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
PROMPT_VERSIONS = {'persona_generate': 'persona_generate_v20', 'persona_check': 'persona_check_v33',
                   'persona_recheck': 'persona_check_v33'}
DEGREE_PROMPT_VERSION = 'persona_degree_v1'
CALL_TIMEOUTS = {'persona_generate': 360, 'persona_check': 360,
                 'persona_recheck': 360}
CALL_MAX_TOKENS = {'persona_generate':16000, 'persona_check':16000,
                   'persona_recheck':16000}
PERSONA_KINDS = ('knowledge', 'episode', 'setting', 'admin', 'state', 'task', 'instruction')
EXCLUDED_KINDS = {'state': 'current state is not persona', 'task': 'pending action is not persona',
                  'instruction': 'host/model instruction is not persona'}
CHECK_BATCH_SIZE = 4
CHECK_BATCH_CLAUSES = 2
MAX_CHECK_STAGE_SECONDS = 900
MAX_CHECK_REQUEST_BYTES = 32768
CHECK_TIMEOUT_RETRIES = 1
# PE-11b: 80 successful streams had <=2.442 s between data events, including
# one 99.581 s call. Allow 90 s without bytes for heavier reasoning too.
# The 360 s total call budget and 900 s stage budget remain unchanged.
CHECK_STREAM_IDLE_SECONDS = 90
REPAIR_POLICY = 'first_eligible_bound_memory_v2'
BOUNDED_SCENE = re.compile(r'(?:这|那|当|某|本|同)(?:一)?(?:次|场|回|天)|\d{4}-\d{2}-\d{2}|[0-9零〇一二三四五六七八九十廿三]+(?:年|月|日|号)')
CROSS_SCENE_LINK = re.compile(r'^(?:(?:也|又|还|同样)(?:也)?(?:是)?在(?:这|那)(?:一)?(?:次|场|天|回)|'
                              r'(?:也(?:是)?)?在同一(?:次|场|天|回))')
PENDING_INTENTION_FRAGMENT = re.compile(
    r'(?:这|那|它|这些|那些)?(?:还|仍|尚|也)?(?:只是|仅是|仅仅是|只算)'
    r'(?:一[个项]|[个项])?(?:新(?:的)?)?(?:打算|计划|承诺|意向|设想)(?:而已|罢了)?')
# A necessary lexical boundary for explicit opening contexts, not an entailment
# parser. Dates/entry metadata do not license a concrete topic from another row.
BOUNDED_CONTEXT = re.compile(
    r'^(?:在)?(?:(?:这|那|当|某|本)(?:一)?(?:次|场|回|天)|\d{4}-\d{2}-\d{2}|'
    r'(?:[0-9零〇一二三四五六七八九十廿]+(?:年|月|日|号))+)'
    r'(?:的)?(?P<context>[^，,。！？!?；;]{1,60}?)(?:中|里|时)(?=[，,])')
ENTRY_CONTEXT_TERMS = {'live': ('直播',), 'group': ('群聊',),
                       'private': ('私聊',), 'game': ('游戏', '联机')}

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
        'prompt_versions':PROMPT_VERSIONS, 'degree_prompt_version':DEGREE_PROMPT_VERSION, 'timeouts_seconds':CALL_TIMEOUTS, 'output_token_limits':CALL_MAX_TOKENS, 'check_batch_size':CHECK_BATCH_SIZE,
        'max_check_stage_seconds':MAX_CHECK_STAGE_SECONDS, 'check_batch_clauses':CHECK_BATCH_CLAUSES,
        'max_check_request_bytes':MAX_CHECK_REQUEST_BYTES, 'check_timeout_retries':CHECK_TIMEOUT_RETRIES,
        'repair_policy':REPAIR_POLICY,
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



def _check_batches(sentences, *, material=None, purpose='persona_check'):
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
    if material is None:
        return batches
    bounded = []
    def split(batch):
        size = _check_request_bytes(purpose, _fact_payload(material, batch))
        if size <= MAX_CHECK_REQUEST_BYTES:
            bounded.append(batch)
        elif len(batch) > 1:
            middle = len(batch) // 2
            split(batch[:middle])
            split(batch[middle:])
        elif len(batch[0].get('_check_focus', [])) > 1:
            # Keep the full sentence and all its bound sources in every window.
            for focus in batch[0]['_check_focus']:
                split([{**batch[0], '_check_focus': [focus]}])
        else:
            bounded.append(batch)  # Explicit abstention in _check_all; never truncate evidence.
    for batch in batches:
        split(batch)
    return bounded


def _model_memory(memory, *, proof_choices=False):
    """Separate factual utterances from restrictive memory context.

    A summary and its source are not independent witnesses. Current text can
    narrow the scope or correct a claim, but not prove an act absent from the
    original utterance. Without excerpts, memory text is the factual fallback.
    """
    fields = ('ref', 'memory_id', 'revision', 'initial_sources', 'stance',
              'speaker_subject_id', 'about', 'date', 'event_time', 'entry_kind',
              'dates', 'date_count', 'belief')
    result = {key: memory[key] for key in fields}
    result['excerpts'] = [dict(source, source_index=index,
        **({'quote_spans': _clause_parts(source['excerpt'])} if proof_choices else {}))
        for index, source in enumerate(memory['excerpts'], 1)]
    if memory['excerpts']:
        # Even revision 1 can retain a domain omitted by elliptical utterances.
        # Current text only narrows scope/corrections; it never proves extra acts
        # or becomes another factual quote choice or independent event date.
        result['current_revision_text'] = memory['content']
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
        row['scope_check_required'] = (sentence['origin'] == 'memory'
            and not sentence.get('initial_setting')
            and (not BOUNDED_SCENE.search(sentence['text'])
                 or bool({'认同', '认可'} & set(words(sentence['text'])))))
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



def _fact_payload(material, sentences):
    payload = _check_payload(material, sentences, full_content='')
    payload['task'] = 'check_facts_and_rules'
    payload.pop('previous')
    payload.pop('candidate_text')
    return payload


def _request_messages(purpose, payload):
    version = (DEGREE_PROMPT_VERSION if purpose in ('persona_check', 'persona_recheck')
               and payload.get('task') == 'degree_only' else PROMPT_VERSIONS[purpose])
    prompt = files('iris').joinpath(f'prompts/{version}.md').read_text(encoding='utf-8')
    return [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': dumps(payload)}]


def _check_request_bytes(purpose, payload):
    return len(dumps(_request_messages(purpose, payload)).encode('utf-8'))


def _conservative_check_row(index, reason, original=None):
    """A controller rejection, explicitly distinguished from a model opinion."""
    original = original if isinstance(original, dict) else {}
    violations = original.get('violations')
    return {'index': index, 'supported': False, 'fabricated': original.get('fabricated') is True,
            'scene_qualified': False, 'violations': [v for v in violations if isinstance(v, str) and v.strip()]
                if isinstance(violations, list) else [],
            'reason': reason, 'clauses': [], 'verdict_origin': 'conservative_rejection'}


def _adverse_field_verdicts(row):
    """Keep already supplied negative flags even if another field is malformed."""
    reasons = []
    if row.get('supported') is False: reasons.append('original sentence verdict rejected')
    if row.get('fabricated') is True: reasons.append('original sentence marked fabricated')
    if isinstance(row.get('violations'), list) and row['violations']:
        reasons.append('original sentence has violations')
    if isinstance(row.get('persona_kind'), str) and row['persona_kind'] in EXCLUDED_KINDS:
        reasons.append(EXCLUDED_KINDS[row['persona_kind']])
    parts = row.get('clauses') if isinstance(row.get('clauses'), list) else []
    for part in [row] + [p for p in parts if isinstance(p, dict)]:
        if part is not row and part.get('supported') is False:
            reasons.append('original clause verdict rejected')
        scope = part.get('scope_check')
        if isinstance(scope, dict):
            if scope.get('preserved') is False: reasons.append('original activity scope rejected')
            proofs = scope.get('source_scopes')
            if isinstance(proofs, list) and any(isinstance(p, dict) and p.get('preserved') is False for p in proofs):
                reasons.append('original bound activity scope rejected')
    return reasons


def _structure_failure(error):
    """Provider/output shape failures are recoverable; infrastructure is not."""
    return (error.category == 'invalid_output' and not error.paused
            and error.reason not in ('output_limit', 'check_request_limit'))


def _parse_structure_error(error):
    code = getattr(error, 'structure_code', 'gateway_output')
    return code + ': invalid model output'


def _fact_envelope_errors(answer):
    errors = []
    value = answer.get('change_degree')
    value = {'小':'small', '中':'medium', '大':'large'}.get(value, value) if isinstance(value, str) else None
    if value not in DEGREES:
        errors.append('invalid change degree')
    if not isinstance(answer.get('reason'), str) or not answer['reason'].strip():
        errors.append('model check reason required')
    return errors


def _sentence_format_errors(row, index):
    if not isinstance(row, dict):
        return [f'sentence {index}: invalid model verdict index']
    errors = []
    if type(row.get('index')) is not int or row['index'] != index:
        errors.append(f'sentence {index}: invalid model verdict index')
    for field in ('supported', 'fabricated', 'scene_qualified'):
        if type(row.get(field)) is not bool:
            errors.append(f'sentence {index}: invalid {field} boolean')
    if not isinstance(row.get('reason'), str) or not row['reason'].strip():
        errors.append(f'sentence {index}: sentence reason required')
    if (not isinstance(row.get('violations'), list)
            or any(not isinstance(v, str) or not v.strip() for v in row['violations'])):
        errors.append(f'sentence {index}: invalid sentence violations')
    if not isinstance(row.get('persona_kind'), str) or row['persona_kind'] not in PERSONA_KINDS:
        errors.append(f'sentence {index}: invalid persona kind')
    return errors


def _proof_format_errors(row, index):
    errors = []
    parts = row.get('clauses') if isinstance(row.get('clauses'), list) else []
    for position, part in enumerate(parts, 1):
        if not isinstance(part, dict) or not isinstance(part.get('evidence'), list):
            continue  # The existing clause contract checks this container.
        for number, proof in enumerate(part['evidence'], 1):
            if (not isinstance(proof, dict) or not isinstance(proof.get('ref'), str)
                    or type(proof.get('source_index')) is not int
                    or ('quote_index' in proof and type(proof['quote_index']) is not int)
                    or ('quote' in proof and not isinstance(proof['quote'], str))
                    or not {'quote', 'quote_index'}.intersection(proof)):
                errors.append(f'sentence {index}: clause {position}: proof {number}: invalid proof shape')
    return errors


def _structure_types(errors, unit):
    types = []
    for error in errors:
        if error.startswith(('json_syntax:', 'json_root:', 'gateway_output:')):
            code = error.split(':', 1)[0]
        elif 'invalid change degree' in error:
            code = 'degree_value' if unit == 'degree' else 'batch_degree'
        elif 'model check reason required' in error:
            code = 'degree_reason' if unit == 'degree' else 'batch_reason'
        elif 'degree: invalid fields' in error:
            code = 'degree_fields'
        elif 'degree: sentences must be empty' in error:
            code = 'degree_sentences'
        elif 'invalid proof shape' in error:
            code = 'proof_shape'
        elif 'activity scope verdict' in error:
            code = 'scope_fields'
        elif 'rejection short circuit' in error:
            code = 'short_circuit'
        elif 'invalid clause verdict' in error:
            code = 'clause_fields'
        elif 'every focus span' in error:
            code = 'clause_coverage'
        elif 'boolean' in error:
            code = 'sentence_bool'
        elif 'sentence reason required' in error:
            code = 'sentence_reason'
        elif 'sentence violations' in error:
            code = 'sentence_violations'
        elif 'persona kind' in error:
            code = 'sentence_kind'
        elif ('sentence results' in error or 'every sentence' in error
                or 'invalid model verdict index' in error):
            code = 'degree_sentences' if unit == 'degree' else 'batch_alignment'
        else:
            code = 'sentence_fields' if unit == 'sentence' else 'batch_fields'
        if code not in types:
            types.append(code)
    return types


def _structure_event(container, purpose, unit, errors, **fields):
    event = {'unit':unit, 'errors':list(errors), 'retry_requested':False,
             'status':'conservative', **fields}
    container.setdefault(purpose + '_structure_events', []).append(event)
    return event


def _check_records(batches, purpose):
    for raw in batches:
        yield raw
        for key in ('_timeout_retries', '_structure_events', '_focus_completions'):
            for event in raw.get(purpose + key, []):
                if isinstance(event.get('outputs'), dict):
                    yield from _check_records([event['outputs']], purpose)
        for split in raw.get(purpose + '_output_splits', []):
            yield from _check_records([child['outputs'] for child in split['children']], purpose)


def _output_limit_diagnostics(batches, purpose):
    records = list(_check_records(batches, purpose))
    truncations = [event for raw in records for event in raw.get(purpose + '_output_limits', [])]
    splits = [event for raw in records for event in raw.get(purpose + '_output_splits', [])]
    return {'truncations':len(truncations), 'splits':len(splits),
            'split_results':dict(Counter(event['status'] for event in splits)),
            'terminal_truncations':sum(event['candidate_count'] <= 1 for event in truncations)}


def _structure_diagnostics(batches, purpose):
    initial, repeated, outcomes = Counter(), Counter(), Counter()
    sentences, events, degree_count, batch_count = set(), [], 0, 0
    for raw in _check_records(batches, purpose):
        for event in raw.get(purpose + '_structure_events', []):
            unit = event['unit']
            (repeated if event.get('covered_by_batch_retry') else initial).update(
                _structure_types(event['errors'], unit))
            repeated.update(_structure_types(event.get('retry_errors', []), unit))
            status = event['status']
            result = ('corrected' if status == 'corrected' else
                      'infrastructure_failure' if status == 'infrastructure_failure' else
                      'salvaged' if status == 'partial' and not event.get('conservative_sentence_indices')
                      else 'conservative')
            if event.get('retry_requested'):
                outcomes[result] += 1
            sentences.update(event.get('conservative_sentence_indices', []))
            if result == 'conservative' and unit == 'sentence' and 'sentence_index' in event:
                sentences.add(event['sentence_index'])
            degree_count += unit == 'degree' and result == 'conservative'
            batch_count += unit == 'batch' and result == 'conservative'
            events.append({k:v for k,v in event.items() if k != 'outputs'})
    return {'initial_failures':dict(initial), 'retry_failures':dict(repeated),
            'retry_results':dict(outcomes), 'conservative_sentence_count':len(sentences),
            'conservative_sentence_indices':sorted(sentences),
            'conservative_batch_count':batch_count, 'conservative_degree_count':degree_count,
            'events':events}


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


# Necessary aspect boundaries for finite surface forms, not an entailment parser.
# The independent checker still owns actor, object, amount, date and semantics.
_ACTION_COUNT = r'[零〇一二两三四五六七八九十百千万0-9]+(?:次|遍|回|趟|轮|段|张|页|件|份|个|封|首|章|组|条|幅|本|圈|步)'
_NON_EVENT_PREFIX = re.compile(r'如果|假如|倘若|要是|希望|打算|计划|准备|想要|愿意|能够|可以|假装|装作')
_NEGATIVE_RESULT = re.compile(r'没有|未曾|尚未|不曾|没能|未能|没|未')
_NEGATIVE_INTENTION = re.compile(r'暂时不|暂且不|先不|暂不|打算不|计划不|准备不|不打算|不准备|不计划')


def _aspect_clauses(text):
    # A title is not an assertion about its words. Keep speech quotations for the
    # independent attribution check; do not manufacture an action from a title.
    text = re.sub(r'《[^》]*》', '', text)
    return re.split(r'[。！？；，.!?;,\n]', text)


def _counted_completion_witness(predicate, sources):
    for source in sources:
        for clause in _aspect_clauses(source):
            for match in re.finditer(re.escape(predicate), clause):
                prefix, suffix = clause[:match.start()], clause[match.end():]
                if (_NON_EVENT_PREFIX.search(prefix) or _NEGATIVE_RESULT.search(prefix)
                        or '不' in prefix):
                    continue
                # Keep the aspect attached to this predicate. A neighbouring
                # action's 了 cannot supply a witness.
                if re.match(r'(?:了|过)|' + _ACTION_COUNT + r'了', suffix):
                    return True
                if re.search(r'(?:已经|已然|早已|已|刚才|刚刚|刚)(?:(?:把|将).*?)?(?:重新|再次|再|又)?$', prefix):
                    return True
    return False


def _negative_action_key(text):
    # Only grammatical pronouns/coverbs and aspect particles are removed. No
    # activity, corpus subject, date or answer vocabulary is encoded here.
    grammar = {'我', '自己', '你', '您', '对方', '替', '为', '给', '帮',
               '把', '将', '再', '又', '了', '过', '也', '先', '暂时'}
    return ''.join(token for token in words(text)
                   if token not in grammar and re.search(r'[\u3400-\u9fffA-Za-z0-9]', token))


def _same_negative_action(left, right):
    # This is only a necessary lexical link; suffix overlap does not establish
    # who did it, whether it was entailed, or the event's time.
    return len(left) >= 2 and len(right) >= 2 and (left.endswith(right) or right.endswith(left))


def _action_aspect_issues(text, sources):
    issues, counted = [], set()
    source_clauses = [clause for source in sources for clause in _aspect_clauses(source)]
    for clause in _aspect_clauses(text):
        for match in re.finditer(r'[了过](?=' + _ACTION_COUNT + r')', clause):
            prefix = clause[:match.start()]
            if _NON_EVENT_PREFIX.search(prefix) or _NEGATIVE_RESULT.search(prefix):
                continue
            tokens = list(words(prefix))
            if tokens:
                counted.add(tokens[-1])
        negative = _NEGATIVE_RESULT.search(clause)
        if negative is None or _NON_EVENT_PREFIX.search(clause[:negative.start()]):
            continue
        action = _negative_action_key(clause[negative.end():])
        intentions, outcomes = [], []
        for source_clause in source_clauses:
            intention = _NEGATIVE_INTENTION.search(source_clause)
            if intention is not None:
                intentions.append(_negative_action_key(source_clause[intention.end():]))
            outcome = _NEGATIVE_RESULT.search(source_clause)
            if (outcome is not None
                    and not _NON_EVENT_PREFIX.search(source_clause[:outcome.start()])
                    and not re.search(r'不|没有|没|未', source_clause[:outcome.start()])):
                outcomes.append(_negative_action_key(source_clause[outcome.end():]))
        if (any(_same_negative_action(action, key) for key in intentions)
                and not any(_same_negative_action(action, key) for key in outcomes)):
            issues.append('negative intention needs an actual result witness: '
                          '先不／暂不／不打算只证明意图，不能由摘要补成实际没有做；须有同一动作的否定结果原话')
    for predicate in sorted(counted):
        if not _counted_completion_witness(predicate, sources):
            issues.append('counted completion needs original source support: '
                          f'“{predicate}了／过若干次”的结果断言须有同一动作的完成原话，不能借相邻动作或摘要的完成态')
    return issues


def _bounded_context_issues(text, sources):
    """Opening context words need anchors in the actual bound proof sources.

    Current text may retain a domain omitted by an utterance. It still cannot
    prove the action. Literal overlap never establishes attribution or entailment;
    the existing semantic and per-focus date checks remain independent.
    """
    match = BOUNDED_CONTEXT.match(text.strip())
    if match is None:
        return []
    context = match['context']
    # Relation words are grammatical glue; quoted titles remain source terms.
    quoted_terms = {word.casefold() for title in re.findall(r'[《“「『"]([^》”」』"]+)[》”」』"]', context)
                    for word in words(title)}
    grammar = {'的', '在', '中', '里', '时', '与', '和', '跟', '同'}
    terms = [word.casefold() for word in words(context)
             if re.search(r'\w', word) and (word not in grammar or word.casefold() in quoted_terms)]
    known = set()
    for memory, source in sources:
        for value in (memory.get('current_revision_text') or memory.get('content', ''),
                      source.get('excerpt', '')):
            known.update(word.casefold() for word in words(value))
        # Use the quoted source's entry, never a different source's metadata.
        kind = source.get('entry_kind') or memory.get('entry_kind')
        known.update(ENTRY_CONTEXT_TERMS.get(kind, ()))
    missing = list(dict.fromkeys(term for term in terms if term not in known))
    return ([f'bounded context needs its own source terms: {", ".join(missing)}']
            if missing else [])


def _reported_event_context_issues(text, focus, sources):
    """A bounded report about self needs its own speech-event anchor.

    This narrow grammar gate does not infer who an unnamed speaker is or prove
    the reported content. Direct named-speaker metadata or explicit local time
    wording is only a necessary witness; semantic checks still own entailment.
    """
    opening = _clause_parts(text)[0]['text'] if text.strip() else ''
    if not BOUNDED_SCENE.search(opening):
        return []
    report = re.match(r'^(?P<actor>[^，,。！？!?；;：:]{1,24}?)(?P<verb>'
                      r'夸奖|称赞|赞扬|夸|评价|告诉|表示|提到|认为|觉得|说)(?:了|过)?我', focus.strip())
    if report is None or '我' in report['actor']:
        return []
    actor, verb = report['actor'].strip(), report['verb']
    for _, source in sources:
        # The actual author's contemporaneous words can witness that author
        # speaking. A self-authored retrospective cannot borrow its timestamp.
        if source.get('speaker') == actor and source.get('speaker_subject_id') not in (None, 'self', 'scene'):
            return []
        for utterance in re.split(r'[。！？!?；;\n]', source.get('excerpt', '')):
            for occurrence in re.finditer(re.escape(actor) + r'\s*' + re.escape(verb), utterance):
                prefix = utterance[:occurrence.start()].strip()
                # Do not borrow a date from an earlier action or a later
                # recognition. Only the report's immediate framing can qualify.
                if prefix.endswith(('，', ',')):
                    prefix = prefix[:-1].strip()
                if re.search(r'[，,]', prefix):
                    continue
                timed = BOUNDED_SCENE.search(prefix) or any(
                    word in RELATIVE_TIMES for word in words(prefix))
                date_only = bool(re.fullmatch(r'(?:在)?(?:\d{4}-\d{2}-\d{2}|'
                    r'[0-9零〇一二三四五六七八九十廿]+(?:年|月|日|号))+', prefix))
                deictic_only = bool(re.fullmatch(r'(?:在)?(?:这|那|当|某|本)(?:一)?(?:次|天|场|回)', prefix))
                if timed and (date_only or deictic_only or prefix in RELATIVE_TIMES
                              or prefix.endswith(('中', '里', '时', '后', '前', '期间'))):
                    return []
    return ['reported event needs its own time/scene anchor: 被转述言语行为不能借用转述或认同的日期／场景']


def _resultative_non_result_scope(prefix):
    # Only an immediately governing grammatical frame exempts this predicate.
    # An ability, wish or negation inside an earlier modifier is not its scope.
    tokens = list(words(prefix))
    scopes = {'能', '能够', '可以', '假装', '装作', '而非', '没有', '没能', '未能',
              '尚未', '未曾', '不曾', '没', '未', '不', '打算', '计划', '想要', '希望',
              '准备', '喜欢', '偏好', '倾向', '愿意', '不愿意'}
    adverbs = {'先', '再', '也', '都', '一口气', '直接', '继续', '全部', '彻底'}
    boundaries = {'的', '地', '得', '又', '再', '并', '并且', '然后', '随后', '接着',
                  '就', '才', '后', '以后', '之后'}
    for index in range(len(tokens)):
        for width in (1, 2):
            if ''.join(tokens[index:index+width]) not in scopes:
                continue
            tail = tokens[index+width:]
            while tail and tail[0] in adverbs:
                tail = tail[1:]
            if not tail:
                return True
            if tail[0] in {'把', '将'} and len(tail) > 1 and not boundaries.intersection(tail[1:]):
                return True
    return False


def _resultative_aspect_issues(text, sources):
    predicates, issues = set(), []
    for clause in _aspect_clauses(text):
        offsets, position = {0}, 0
        for token in words(clause):
            position += len(token)
            offsets.add(position)
        for match in re.finditer(r'(?=((?:完成|[\u3400-\u9fff]完)(?:[了过])?))', clause):
            # 完整/完全/完善 are lexical units, not resultative predicates.
            resultative = match.group(1)
            if match.start() + len(resultative) not in offsets:
                continue
            if not _resultative_non_result_scope(clause[:match.start()]):
                predicates.add(resultative.rstrip('了过'))
    for predicate in sorted(predicates):
        if not any(_completion_witness(predicate, source) for source in sources):
            issues.append('completion aspect needs original source support: '
                          f'“{predicate}”的结果断言须在本焦点绑定原话中有同一动作的明确完成表述，动作目标不能补成结果')
    return issues


def _bound_source_issues(sentence, tokens, memories):
    if memories is None or sentence['initial_setting']:
        return []
    refs = {(b['memory_id'], b['revision']) for b in sentence.get('basis', [])}
    bound = [m for m in memories if (m['memory_id'], m['revision']) in refs]
    context_sources = [(memory, source) for memory in bound for source in
                       (memory.get('excerpts') or [{'excerpt': memory['content']}])]
    issues = _bounded_context_issues(sentence['text'], context_sources)
    for focus in _clause_parts(sentence['text']):
        issues.extend(_reported_event_context_issues(sentence['text'], focus['text'], context_sources))
    sources = []
    for memory in bound:
        originals = [e['excerpt'] for e in memory.get('excerpts', [])
                     if isinstance(e, dict) and isinstance(e.get('excerpt'), str) and e['excerpt'].strip()]
        # Existing self memories without message sources can use their own text;
        # when originals exist, a completed summary cannot override an intention.
        sources.extend(originals or [memory['content']])
    issues.extend(_action_aspect_issues(sentence['text'], sources))
    # An entry is a context, not proof that all its events are fictional. Preserve
    # an explicitly sourced real-world qualification instead of inventing a game.
    real_scope = {'现实', '线下'} & set(tokens)
    source_real_scope = {word for source in sources for word in words(source)} & real_scope
    if (bound and all(m.get('entry_kind') == 'game' for m in bound)
            and not {'游戏', '联机'} & set(tokens) and not source_real_scope):
        issues.append('game-only evidence needs explicit scope: 自动句须明确游戏或联机场景；'
                      '现实／线下限定须来自本句绑定原话，不能仅按动作名称推定现实经历')
    issues.extend(_resultative_aspect_issues(sentence['text'], sources))
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
    if not sentence['initial_setting'] and CROSS_SCENE_LINK.search(text.strip()):
        # A local evidence window has no verified neighbouring-scene antecedent.
        # Require independent wording rather than guessing that events coincide.
        issues.append('cross-sentence scene reference needs independent wording: '
                      '自动句须独立限定本句事件，不用也是在那次／同一场等借邻句关联场景')
    if not sentence['initial_setting'] and any(
            PENDING_INTENTION_FRAGMENT.fullmatch(part['text'].rstrip('。.!！?？'))
            for part in _clause_parts(text)):
        # A narrow, affirmative whole-fragment wording gate, not an intent parser.
        # Truthfully announcing a still-only plan does not make it an episode.
        # Negated/conditional phrases and mere occurrences of plan words do not
        # match; the model must still check all other pending-action semantics.
        issues.append('explicit pending intention is not persona: '
                      '明确仅为打算、计划或承诺的片段须修复或删除；日期与宣布行为不豁免待办')
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


def _repair_claim_key(text):
    """Only trivial rewordings of an already failed complete claim are equal.

    Keep interior punctuation, names, dates, conditions and negation. Adding a
    previously missing bounded scope is a real change, as is changing its topic.
    This comparison never changes the option text or supplies missing evidence.
    """
    text = text.strip().rstrip('。.')
    return re.sub(r'^(?:在)?[这那](?:一)?(?=[次场回])', '当', text)


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


def _validate_activity_scope(row, sentence, evidence):
    """Verify a retained scope anchor, leaving its semantic choice to the model.

    A scope quote can constrain a claim but never supplies an act or a date.
    Bounded episodes keep event/clause checks; recognizing a personal quality
    still requires its activity domain even when the recognition has a date.
    """
    if not sentence.get('scope_check_required'):
        return [], []
    if not isinstance(row, dict):
        return ['invalid activity scope verdict'], []
    parts = row.get('clauses')
    explicit = 'scope_check' in row
    # Models may attach the same complete contract to focus objects. Without
    # a sentence-level verdict every returned focus must carry one; a partial
    # set cannot stand in for the missing overall judgment. When an overall
    # verdict exists, every supplied extra still counts, especially negatives.
    if not explicit and (not isinstance(parts, list) or not parts
            or any(not isinstance(part, dict) or 'scope_check' not in part for part in parts)):
        return ['invalid activity scope verdict'], []
    scopes = [('', row['scope_check'])] if explicit else []
    if isinstance(parts, list):
        scopes.extend((f"clause {part.get('index', '?')}: ", part['scope_check'])
                      for part in parts if isinstance(part, dict) and 'scope_check' in part)
    errors, issues = [], []
    for label, scope in scopes:
        invalid, unsupported = _validate_activity_scope_value(scope, sentence, evidence)
        errors.extend(label + error for error in invalid)
        issues.extend(label + issue for issue in unsupported)
    return errors, issues


def _validate_activity_scope_value(scope, sentence, evidence):
    """Validate one full scope contract, wherever supplied; never infer support."""
    if (not isinstance(scope, dict)
            or set(scope) != {'candidate_scope', 'source_scopes', 'preserved'}
            or type(scope['preserved']) is not bool
            or not isinstance(scope['candidate_scope'], str)
            or not isinstance(scope['source_scopes'], list)
            or any(not isinstance(proof, dict)
                   or set(proof) not in ({'ref', 'quote'}, {'ref', 'quote', 'preserved'})
                   or not isinstance(proof['ref'], str) or not isinstance(proof['quote'], str)
                   or ('preserved' in proof and type(proof['preserved']) is not bool)
                   for proof in scope['source_scopes'])):
        return ['invalid activity scope verdict'], []
    issues = []
    if not scope['preserved']:
        issues.append('activity scope not preserved by candidate')
    candidate_scope = scope['candidate_scope']
    if scope['preserved'] and (not candidate_scope.strip() or candidate_scope not in sentence['text']):
        issues.append('activity scope needs an exact candidate qualifier')
    if scope['preserved'] and not scope['source_scopes']:
        issues.append('activity scope needs its own source qualifier')
    for proof in scope['source_scopes']:
        # A redundant per-source verdict is meaningful and remains in the audit.
        # It can add a rejection, never override the overall or literal checks.
        if proof.get('preserved') is False:
            issues.append('activity scope not preserved by bound source ' + proof['ref'])
        memory = evidence.get(proof['ref'])
        # Elliptical utterances cannot replace the explicit scope in current text.
        constraint = None if memory is None else (memory.get('current_revision_text') or memory.get('content'))
        source_texts = ([constraint] if constraint else [] if memory is None else
                        [source['excerpt'] for source in memory.get('excerpts', [])])
        quote = proof['quote']
        if (proof['ref'] not in sentence['evidence_refs'] or not re.search(r'\w', quote)
                or not any(quote in text for text in source_texts)):
            issues.append('activity scope must quote an exact own bound source qualifier')
        elif scope['preserved'] and quote not in candidate_scope:
            issues.append('activity scope qualifier was omitted or broadened in candidate')
    return [], issues


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
            row.pop('additional_clause_checks', None)
            row.pop('repeated_clause_checks', None)
            row.pop('guard_rejected_prefix', None)
            row.pop('unchecked_scope_check', None)
        if sentence['origin'] != 'memory' or sentence.get('initial_setting'):
            continue
        parts = row.get('clauses') if isinstance(row, dict) else None
        requested = sentence['clauses']
        full = _clause_parts(sentence['text'])
        # A model may also check other spans from the full sentence context.
        # Explicit IDs identify them without guessing. Every requested span
        # must appear, with first appearances in request order. Repeated verdicts
        # are separate proof obligations: validate each, never pool evidence or
        # discard an adverse verdict when projecting to the requested spans.
        full_by_index = {part['index']: part for part in full}
        requested_indices = [part['index'] for part in requested]
        part_indices = ([part['index'] for part in parts]
            if isinstance(parts, list) and all(isinstance(part, dict)
                and type(part.get('index')) is int for part in parts) else [])
        expanded_response = (len(part_indices) > len(requested_indices)
            and all(index in full_by_index for index in part_indices)
            and list(dict.fromkeys(index for index in part_indices
                                   if index in requested_indices)) == requested_indices)
        focus = [full_by_index[index] for index in part_indices] if expanded_response else requested
        row_errors_before = len(errors)
        # One explicit negative proves the whole conjunction false. This is a
        # rejection-only protocol: a retained sentence still needs every focus.
        # Preserve what was not checked rather than inventing verdicts for it.
        complete = isinstance(parts, list) and len(parts) == len(focus)
        # A structurally valid positive prefix can already fail an independent
        # proof/date/scope guard. Validate it before classifying the omitted tail:
        # a known adverse conjunct rejects the sentence without inventing a
        # model verdict for the missing spans. This never permits retention.
        guardable_prefix = (not complete and _valid_sentence_verdict(row, index, require_kind=True)
            and row['supported'] and not row['fabricated'] and not row['violations']
            and row['persona_kind'] not in EXCLUDED_KINDS and 'stop_after_failure' not in row
            and isinstance(parts, list) and 0 < len(parts) < len(focus)
            and part_indices == [part['index'] for part in focus[:len(parts)]]
            and all(part.get('supported') is True for part in parts))
        # The explicit negative is sufficient for rejection. A missing redundant
        # stop marker is auditable; an explicit false/non-boolean marker is not.
        stopped = (not complete and isinstance(row, dict) and row.get('stop_after_failure', True) is True
            and row.get('supported') is False and isinstance(parts, list)
            and 0 < len(parts) < len(focus) and isinstance(parts[-1], dict)
            and parts[-1].get('supported') is False)
        if (isinstance(row, dict) and 'stop_after_failure' in row
                and (type(row['stop_after_failure']) is not bool
                     or (row['stop_after_failure'] and not complete and not stopped))):
            errors.append(f'sentence {index}: invalid rejection short circuit')
            continue
        if not isinstance(parts, list) or (not complete and not stopped and not guardable_prefix):
            errors.append(f'sentence {index}: clause checks must cover every focus span, in order')
            continue
        scope_absent = (sentence.get('scope_check_required') and 'scope_check' not in row
            and not any(isinstance(part, dict) and 'scope_check' in part for part in parts))
        # An omitted support obligation may stay unchecked only when another
        # fully validated obligation already rejects this conjunction. Supplied
        # scope objects, even malformed/partial ones, are never ignored.
        scope_errors, issues = (([], []) if scope_absent else
            _validate_activity_scope(row, sentence, payload['evidence_by_ref']))
        errors.extend(f'sentence {index}: {error}' for error in scope_errors)
        errors_before = len(errors)
        for expected, part in zip(focus, parts):
            label = f"clause {expected['index']}"
            if (not isinstance(part, dict) or type(part.get('index')) is not int
                    or part['index'] != expected['index']
                    or any(type(part.get(k)) is not bool for k in ('supported', 'scene_qualified'))
                    or not isinstance(part.get('reason'), str) or not part['reason'].strip()
                    or not isinstance(part.get('evidence'), list)):
                errors.append(f'sentence {index}: {label}: invalid clause verdict')
                continue
            dates, valid, context_sources = set(), 0, []
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
                context_sources.append((memory, source))
                if source.get('date'):
                    dates.add(source['date'])
            if not part['supported']:
                issues.append(f"{label}: {part['reason']}")
            elif not valid:
                issues.append(f'{label}: no valid original proof')
            if part['supported'] and valid:
                issues.extend(f'{label}: {issue}' for issue in
                              _bounded_context_issues(sentence['text'], context_sources))
                issues.extend(f'{label}: {issue}' for issue in
                              _reported_event_context_issues(sentence['text'], expected['text'], context_sources))
                issues.extend(f'{label}: {issue}' for issue in
                              _action_aspect_issues(expected['text'],
                                  [source['excerpt'] for _, source in context_sources]))
                issues.extend(f'{label}: {issue}' for issue in
                              _resultative_aspect_issues(expected['text'],
                                  [source['excerpt'] for _, source in context_sources]))
            if part['supported'] and len(dates) < 2:
                if not part['scene_qualified']:
                    issues.append(f'{label}: general claim needs at least two distinct dates of its own proof')
                elif not BOUNDED_SCENE.search(sentence['text']):
                    issues.append(f'{label}: single-date claim needs an explicit bounded scene in this sentence')
        if scope_absent:
            if (issues and len(errors) == row_errors_before
                    and _valid_sentence_verdict(row, index, require_kind=True)):
                row['supported'] = False
                row['unchecked_scope_check'] = True
            else:
                errors.append(f'sentence {index}: invalid activity scope verdict')
        if guardable_prefix:
            if issues and len(errors) == row_errors_before:
                row['supported'] = False
                row['guard_rejected_prefix'] = True
                row['unchecked_clause_indices'] = [part['index'] for part in focus[len(parts):]]
            else:
                errors.append(f'sentence {index}: clause checks must cover every focus span, in order')
        if stopped and len(parts) < len(focus) and len(errors) == errors_before:
            row['unchecked_clause_indices'] = [part['index'] for part in focus[len(parts):]]
        if expanded_response and len(errors) == row_errors_before:
            primary, repeated, additional = {}, [], []
            for part in parts:
                if part['index'] not in requested_indices:
                    additional.append(part)
                elif part['index'] in primary:
                    repeated.append(part)
                else:
                    primary[part['index']] = part
            row['clauses'] = [primary[index] for index in requested_indices]
            row['additional_clause_checks'] = additional
            if repeated:
                row['repeated_clause_checks'] = repeated
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


# The tested GLM deployment supports SSE with a final usage packet. Other chat
# models retain their existing transport; no shared Gateway/client is mutated.
CHECK_STREAM_MODELS = frozenset({'glm-5.3-flash'})


def check_transport(config):
    return 'sse' if config and config.model.casefold() in CHECK_STREAM_MODELS else 'json'


class _PersonaCheckStreamClient:
    """Adapt one persona call's SSE to the Gateway's complete-response boundary.

    The Gateway continues to own retries, health, usage and total deadlines.
    Only timings/counts leave this adapter; reasoning is transient response data.
    A partial stream never becomes a successful chat response.
    """
    def __init__(self, client, monotonic, deadline):
        self.client, self.monotonic, self.deadline = client, monotonic, deadline
        self.purpose = None
        self._lock, self._cancelled, self._finished = Lock(), Event(), Event()
        self._finished.set()
        self._response, self._attempts = None, []

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._attempts)

    def cancel(self):
        self._cancelled.set()
        with self._lock:
            response = self._response
        if response is not None:
            try:
                response.close()
            except httpx.HTTPError:
                pass
        # Usually already finished. If Gateway's future deadline won the race,
        # close the in-flight body and give its worker time to leave the stream.
        self._finished.wait(timeout=1)

    def post(self, url, *, headers, json, timeout):
        import json as json_module
        started = self.monotonic()
        audit = {'purpose': self.purpose, 'mode': 'sse', 'status_code': None,
            'complete': False, 'received_done': False, 'chunks': 0,
            'first_headers_seconds': None, 'first_data_seconds': None,
            'first_reasoning_seconds': None, 'first_content_seconds': None,
            'last_data_seconds': None, 'max_data_gap_seconds': 0,
            'reasoning_chars': 0, 'content_chars': 0, 'events': [],
            'idle_timeout_seconds': CHECK_STREAM_IDLE_SECONDS, 'stall_aborted': False,
            'receive_chunks': 0, 'received_bytes': 0, 'last_receive_seconds': None,
            'max_receive_gap_seconds': 0}
        with self._lock:
            self._attempts.append(audit)
            self._finished.clear()

        def elapsed():
            return round(self.monotonic() - started, 3)

        def mark(**values):
            with self._lock:
                audit.update(values)

        def guard():
            if self._cancelled.is_set() or self.monotonic() >= self.deadline:
                mark(abort_reason='total_deadline')
                raise httpx.ReadTimeout('persona check total deadline')

        def trace(name, info):
            # Never inspect info: it includes URLs, headers and request objects.
            if (name.startswith(('connection.', 'http11.', 'http2.'))
                    and name.endswith(('.started', '.complete', '.failed'))):
                with self._lock:
                    if len(audit['events']) < 64:
                        audit['events'].append({'event': name, 'seconds': elapsed()})

        content, reasoning, refusal = [], {}, []
        finish, usage, flags = None, None, {}
        last_data = last_receive = 0
        # Socket reads enforce the bound even when an iterator never yields.
        # Preserve connect/write/pool timeouts and the absolute total deadline.
        timeout = httpx.Timeout(timeout)
        timeout.read = min(CHECK_STREAM_IDLE_SECONDS, timeout.read) if timeout.read is not None else CHECK_STREAM_IDLE_SECONDS

        def received(chunk):
            nonlocal last_receive
            guard()
            if not chunk:
                return
            instant = elapsed()
            gap = round(instant - last_receive, 3)
            mark(max_receive_gap_seconds=max(audit['max_receive_gap_seconds'], gap))
            if gap >= CHECK_STREAM_IDLE_SECONDS:
                mark(stall_aborted=True, abort_reason='stream_stalled')
                raise httpx.ReadTimeout('persona check stream stalled')
            last_receive = instant
            mark(last_receive_seconds=instant, receive_chunks=audit['receive_chunks'] + 1,
                 received_bytes=audit['received_bytes'] + len(chunk))

        class ProgressStream(httpx.SyncByteStream):
            # Count raw bytes before UTF-8/SSE buffering: heartbeats, partial
            # lines and reasoning all count, regardless of generated content.
            def __init__(self, source):
                self.source = source

            def __iter__(self):
                for chunk in self.source:
                    received(chunk)
                    yield chunk

            def close(self):
                self.source.close()

        try:
            guard()
            body = {**json, 'stream': True, 'stream_options': {'include_usage': True}}
            with self.client.stream('POST', url, headers=headers, json=body,
                                    timeout=timeout, extensions={'trace': trace}) as response:
                with self._lock:
                    self._response = response
                mark(status_code=response.status_code, first_headers_seconds=elapsed())
                guard()
                last_receive = elapsed()
                mark(max_receive_gap_seconds=last_receive)
                if response.status_code >= 400:
                    response.read()
                    guard()
                    # Keep the real status and Retry-After for Gateway; provider
                    # prose remains transient and is scrubbed by its recorder.
                    return response
                response.stream = ProgressStream(response.stream)
                data_lines = []
                for line in response.iter_lines():
                    guard()
                    if line:
                        if line.startswith('data:'):
                            data_lines.append(line[5:].lstrip(' '))
                        continue
                    if not data_lines:
                        continue
                    raw, data_lines = '\n'.join(data_lines), []
                    instant = elapsed()
                    mark(first_data_seconds=audit['first_data_seconds'] if audit['first_data_seconds'] is not None else instant,
                         last_data_seconds=instant,
                         max_data_gap_seconds=max(audit['max_data_gap_seconds'], round(instant - last_data, 3)))
                    last_data = instant
                    if raw == '[DONE]':
                        mark(received_done=True)
                        break
                    packet = json_module.loads(raw)
                    if not isinstance(packet, dict) or packet.get('error'):
                        raise ValueError('invalid persona stream packet')
                    mark(chunks=audit['chunks'] + 1)
                    if audit['chunks'] > 100000:
                        raise ValueError('persona stream exceeds packet limit')
                    if packet.get('usage') is not None:
                        if not isinstance(packet['usage'], dict):
                            raise ValueError('invalid persona stream usage')
                        usage = packet['usage']
                    for key in ('input_sensitive', 'output_sensitive', 'base_resp'):
                        if key in packet:
                            flags[key] = packet[key]
                    choices = packet.get('choices')
                    if not isinstance(choices, list) or len(choices) > 1:
                        raise ValueError('invalid persona stream choices')
                    for choice in choices:
                        if (not isinstance(choice, dict) or type(choice.get('index')) is not int
                                or choice['index'] != 0 or not isinstance(choice.get('delta'), dict)):
                            raise ValueError('invalid persona stream choice')
                        delta = choice['delta']
                        for key in ('content', 'reasoning_content', 'reasoning', 'refusal'):
                            value = delta.get(key)
                            if value is None:
                                continue
                            if not isinstance(value, str) or (finish is not None and value):
                                raise ValueError('invalid persona stream delta')
                            if key == 'content':
                                content.append(value)
                                mark(content_chars=audit['content_chars'] + len(value))
                                if value and audit['first_content_seconds'] is None:
                                    mark(first_content_seconds=instant)
                            elif key == 'refusal':
                                refusal.append(value)
                            else:
                                reasoning.setdefault(key, []).append(value)
                                mark(reasoning_chars=audit['reasoning_chars'] + len(value))
                                if value and audit['first_reasoning_seconds'] is None:
                                    mark(first_reasoning_seconds=instant)
                        ended = choice.get('finish_reason')
                        if ended is not None:
                            if ended not in ('stop', 'length', 'content_filter') or finish not in (None, ended):
                                raise ValueError('invalid persona stream finish')
                            finish = ended
                    if audit['content_chars'] + audit['reasoning_chars'] > 1000000:
                        raise ValueError('persona stream exceeds content limit')
                guard()
                if not audit['received_done'] or finish is None:
                    raise ValueError('incomplete persona stream')
                message = {'role': 'assistant', 'content': ''.join(content),
                           **{key: ''.join(parts) for key, parts in reasoning.items()}}
                if refusal:
                    message['refusal'] = ''.join(refusal)
                data = {**flags, 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}]}
                if usage is not None:
                    data['usage'] = usage
                mark(complete=True, finish_reason=finish)
                return httpx.Response(response.status_code, json=data)
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
            if (isinstance(error, httpx.ReadTimeout) and not self._cancelled.is_set()
                    and self.monotonic() < self.deadline
                    and timeout.read == CHECK_STREAM_IDLE_SECONDS
                    and elapsed() - last_receive >= CHECK_STREAM_IDLE_SECONDS - .01):
                mark(stall_aborted=True, abort_reason='stream_stalled')
            mark(error_type=type(error).__name__)
            raise
        finally:
            with self._lock:
                audit['elapsed_seconds'] = elapsed()
                audit['max_data_gap_seconds'] = max(
                    audit['max_data_gap_seconds'], round(elapsed() - last_data, 3))
                audit['max_receive_gap_seconds'] = max(
                    audit['max_receive_gap_seconds'], round(elapsed() - last_receive, 3))
                audit['cancelled'] = self._cancelled.is_set()
                self._response = None
                self._finished.set()


class PersonaEngine:
    def __init__(self, store, gateway, *, clock=utc_now):
        self.store, self.gateway, self.clock = store, gateway, clock

    def update(self, *, expected_version):
        return self._run(expected_version, 'periodic', only_if_due=True)

    def regenerate(self, *, expected_version):
        return self._run(expected_version, 'regenerate', only_if_due=False)

    def _call(self, purpose, payload, outputs, *, deadline=None, json_repair=True):
        """Per-purpose total budget includes HTTP retries and at most one JSON repair."""
        gateway, owned, stream = self.gateway, False, None
        if gateway is None:
            raise ModelError('paused', 'model unconfigured', paused=True, reason='unconfigured')
        import time
        monotonic = getattr(gateway, 'monotonic', time.monotonic)
        deadline = min(deadline, monotonic() + CALL_TIMEOUTS[purpose]) if deadline is not None else monotonic() + CALL_TIMEOUTS[purpose]
        if isinstance(gateway, Gateway) and gateway.health is None:
            from .model_health import ModelHealth
            health = ModelHealth(self.store, gateway._raw_configs, clock=self.clock)
            gateway = Gateway(gateway._raw_configs, self.store, client=gateway.client, health=health,
                              clock=self.clock, sleeper=gateway.sleeper, monotonic=monotonic, jitter=gateway.jitter)
            owned = True
        inner = gateway if isinstance(gateway, Gateway) else getattr(gateway, 'gateway', None)
        if (isinstance(inner, Gateway) and purpose in ('persona_check', 'persona_recheck')
                and check_transport(inner.configs.get('chat')) == 'sse'):
            stream = _PersonaCheckStreamClient(inner.client, monotonic, deadline)
            streamed = copy.copy(inner)
            streamed.client, streamed._own_client = stream, False
            if inner is gateway:
                gateway = streamed
            else:
                # The maintenance facade retains its stop hook and per-HTTP
                # budget/recording subclass. Never close its shared pools.
                gateway = copy.copy(gateway)
                gateway.gateway = streamed
        messages = _request_messages(purpose, payload)
        try:
            for repair in ((False, True) if json_repair else (False,)):
                if monotonic() >= deadline:
                    raise ModelError('timeout', 'total timeout', reason='timeout')
                health = getattr(gateway, 'health', None)
                if health is not None:
                    # ModelHealth's existing learning gate includes the shared daily
                    # budget. Gateway still records the actual persona purpose.
                    health.check('chat', 'learning')
                label = purpose + ('_repair' if repair else '')
                if stream is not None:
                    stream.purpose = label
                request_bytes = len(dumps(messages).encode('utf-8'))
                if purpose in ('persona_check', 'persona_recheck'):
                    outputs.setdefault(purpose + '_request_bytes', []).append(request_bytes)
                    if request_bytes > MAX_CHECK_REQUEST_BYTES:
                        raise ModelError('invalid_output', 'check request material exceeds byte limit',
                                         reason='check_request_limit')
                reply = gateway.chat(messages, label, max_tokens=CALL_MAX_TOKENS[purpose], _deadline=deadline)
                outputs[label] = reply.content
                outputs[label + '_finish_reason'] = reply.finish_reason
                if monotonic() >= deadline:
                    raise ModelError('timeout', 'total timeout', reason='timeout')
                if reply.finish_reason == 'length':
                    # Exhausted reasoning/output is not malformed JSON to retry.
                    # Keep the raw result; no partial or parseable fragment passes.
                    outputs.setdefault(purpose + '_output_limits', []).append({
                        'candidate_count':len(payload.get('candidate', [])),
                        'request_bytes':request_bytes, 'finish_reason':reply.finish_reason})
                    failure = ModelError('invalid_output', 'output budget exhausted', reason='output_limit')
                    failure.check_sentence_count = len(payload.get('candidate', []))
                    raise failure
                try:
                    result, _ = parse_json_object_with_status(reply.content)
                    if set(result) == {'answer'} and isinstance(result['answer'], dict):
                        outputs[label + '_normalizations'] = ['unwrap_single_answer']
                        result = result['answer']
                    return result
                except (ValueError, AttributeError, TypeError, RecursionError) as error:
                    if json_repair and not isinstance(error, ValueError):
                        raise  # Keep the generation/legacy parser contract unchanged.
                    diagnostic = {'kind':type(error).__name__}
                    if isinstance(error, json.JSONDecodeError):
                        diagnostic.update(message=error.msg, line=error.lineno,
                                          column=error.colno, offset=error.pos)
                        hint = f'解析候选 JSON 第 {error.lineno} 行、第 {error.colno} 列：{error.msg}。'
                    else:
                        hint = '没有可解析的根 JSON 对象。'
                    if not json_repair:
                        try:
                            decoded = json.loads(reply.content)
                        except (ValueError, TypeError, RecursionError):
                            code = ('json_syntax' if isinstance(reply.content, str)
                                    and isinstance(error, json.JSONDecodeError) else 'json_root')
                        else:
                            code = 'json_root' if not isinstance(decoded, dict) else 'json_syntax'
                        diagnostic['code'] = code
                    outputs[label + '_json_error'] = diagnostic
                    if not json_repair:
                        failure = ModelError('invalid_output', 'invalid check JSON object', reason='json_parse')
                        failure.structure_code = code
                        raise failure
                    if repair:
                        raise ModelError('invalid_output', 'JSON parse failed after repair')
                    messages += [{'role':'assistant','content':reply.content},
                        {'role':'user','content':hint + '只修正 JSON 语法，保留所有字段、值和逐句判定；'
                         '检查对象／数组闭合、逗号和字符串转义，不重新判断事实或变化程度。'
                         '不要原样重复错误文本，只输出修正后的完整根 JSON 对象，不要解释。'}]
        except ModelError as error:
            if error.reason == 'output_limit' and not hasattr(error, 'check_sentence_count'):
                error.check_sentence_count = len(payload.get('candidate', []))
                outputs.setdefault(purpose + '_output_limits', []).append({
                    'candidate_count':error.check_sentence_count, 'signal':'output_limit'})
            # The gateway's late-response path records timed_out but may omit the
            # reason. Keep the attempt diagnostic consistent with that call record.
            if error.reason is None and error.summary in {
                    'total timeout', 'ReadTimeout', 'WriteTimeout', 'ConnectTimeout', 'PoolTimeout'}:
                error.reason = 'timeout'
            raise
        finally:
            if stream is not None:
                stream.cancel()
                for audit in stream.snapshot():
                    outputs.setdefault(audit['purpose'] + '_transport', []).append(audit)
            if owned:
                gateway.close()

    def _check_call(self, purpose, payload, outputs, *, deadline, retry_state):
        """One timeout retry per logical batch; never renew the stage deadline."""
        import time
        monotonic = getattr(self.gateway, 'monotonic', time.monotonic)
        try:
            return self._call(purpose, payload, outputs, deadline=deadline, json_repair=False)
        except ModelError as error:
            if (error.reason != 'timeout' or error.paused
                    or retry_state['timeouts'] >= CHECK_TIMEOUT_RETRIES or monotonic() >= deadline):
                raise
            retry_state['timeouts'] += 1
            retry = {'error_reason': 'timeout', 'stage_deadline': deadline,
                     'remaining_seconds': max(0, deadline - monotonic()), 'outputs': {}}
            outputs.setdefault(purpose + '_timeout_retries', []).append(retry)
            try:
                answer = self._call(purpose, payload, retry['outputs'], deadline=deadline, json_repair=False)
            except ModelError as repeated:
                retry.update(status='failed', retry_error=repeated.reason or repeated.category)
                raise
            retry['status'] = 'complete'
            return answer

    def _correct_batch_coverage(self, purpose, payload, batch, outputs, *,
                                deadline, retry_state, sentence_indices, first_error=None):
        """One batch retry covers its envelope, JSON and sentence identities."""
        import time

        def coverage(answer):
            returned = answer.get('sentences')
            if not isinstance(returned, list):
                return ['sentence results must be an array'], {}, []
            indices, grouped = [], {}
            for row in returned:
                index = row.get('index') if isinstance(row, dict) else None
                indices.append(index)
                if type(index) is int and 1 <= index <= len(payload['candidate']):
                    grouped.setdefault(index, []).append(row)
            aligned = {index: rows[0] for index, rows in grouped.items() if len(rows) == 1}
            complete = (len(returned) == len(payload['candidate'])
                and all(type(index) is int and index == expected
                        for expected, index in enumerate(indices, 1)))
            return ([] if complete else ['model check must cover every sentence, in order']), aligned, indices

        alignment, original_rows, indices = coverage(batch)
        envelope = _fact_envelope_errors(batch)
        errors = [first_error] if first_error else envelope + alignment
        if not errors:
            return batch, {}, set(), None
        audit = _structure_event(outputs, purpose, 'batch', errors,
            returned_indices=indices, stage_deadline=deadline, status='partial', outputs={})
        outputs.setdefault(purpose + '_batch_corrections', []).append(audit)
        adverse = {}
        for index, original in original_rows.items():
            row = copy.deepcopy(original)
            _, issues = _validate_clause_checks({'sentences': [row]},
                {**payload, 'candidate': [payload['candidate'][index - 1]]})
            reasons = _adverse_field_verdicts(original) + issues.get(index, [])
            if reasons:
                adverse[index] = {'reasons': list(dict.fromkeys(reasons)),
                    'fabricated': original.get('fabricated') is True,
                    'violations': [v for v in original.get('violations', [])
                                   if isinstance(v, str) and v.strip()]
                                  if isinstance(original.get('violations'), list) else []}
        # The retry reuses the frozen prompt and exactly the same evidence.
        request = payload
        monotonic = getattr(self.gateway, 'monotonic', time.monotonic)
        reject_batch = bool(first_error or envelope)
        if monotonic() >= deadline:
            audit['status'] = 'infrastructure_failure'
            raise ModelError('timeout', 'check stage budget exhausted', reason='timeout')
        if _check_request_bytes(purpose, request) > MAX_CHECK_REQUEST_BYTES:
            audit['retry_errors'] = ['batch correction exceeds material byte limit']
        else:
            audit['retry_requested'] = True
            try:
                corrected = self._check_call(purpose, request, audit['outputs'],
                                             deadline=deadline, retry_state=retry_state)
            except ModelError as error:
                if not _structure_failure(error):
                    audit['status'] = 'infrastructure_failure'
                    raise
                audit['retry_errors'] = [_parse_structure_error(error)]
                reject_batch = True
            else:
                alignment, aligned, indices = coverage(corrected)
                envelope = _fact_envelope_errors(corrected)
                batch, original_rows = corrected, aligned
                audit['retry_returned_indices'] = indices
                reject_batch = bool(envelope)
                errors = envelope + alignment
                if errors:
                    audit['retry_errors'] = errors
                else:
                    audit['status'] = 'corrected'
        if reject_batch:
            original_rows = {}
        conservative = set(range(1, len(payload['candidate']) + 1)) - original_rows.keys()
        audit['aligned_sentence_indices'] = [sentence_indices[i - 1] for i in sorted(original_rows)]
        audit['conservative_sentence_indices'] = [sentence_indices[i - 1] for i in sorted(conservative)]
        audit['preserved_adverse_reasons'] = [
            {'sentence_index': sentence_indices[i - 1], 'reasons': value['reasons']}
            for i, value in sorted(adverse.items())]
        reason = 'invalid batch structure after correction' if reject_batch else 'unaligned sentence check after batch correction'
        normalized = [copy.deepcopy(original_rows[i]) if i in original_rows else
                      _conservative_check_row(i, reason)
                      for i in range(1, len(payload['candidate']) + 1)]
        normalized_batch = {**batch, 'sentences': normalized}
        if _fact_envelope_errors(batch):
            audit['normalized_fact_envelope'] = True
            normalized_batch.update(change_degree='small', reason='仅查事实与监管')
        return normalized_batch, adverse, conservative, audit

    def _correct_sentence_fields(self, purpose, payload, row, errors, issues, outputs,
                                 *, deadline, retry_state, original_index):
        """Reask only an identified row once, preserving all prior adverse evidence."""
        import time
        local = row['index']
        sentence = payload['candidate'][local - 1]
        errors = list(errors)
        for completion in outputs.get(purpose + '_focus_completions', []):
            if completion['sentence_index'] == local:
                errors.extend(completion.get('errors', []))
        audit = _structure_event(outputs, purpose, 'sentence', errors,
            sentence_index=original_index, local_index=local,
            status='conservative_rejection', outputs={})
        outputs.setdefault(purpose + '_field_corrections', []).append(audit)
        adverse = list(issues.get(local, [])) + _adverse_field_verdicts(row)
        for completion in outputs.get(purpose + '_focus_completions', []):
            if completion['sentence_index'] == local:
                adverse.extend(completion.get('adverse_reasons', []))
        request = {**payload, 'candidate': [{**sentence, 'index': 1}],
                   'evidence_by_ref': {ref: payload['evidence_by_ref'][ref] for ref in sentence['evidence_refs']},
                   'format_retry': {'errors': errors,
                       'instruction': '上次本句判定字段不符合格式。仅重新检查这一句，严格返回原定字段和类型；'
                                      '事实、活动范围和监管规则不变，存疑判不支持。'}}
        monotonic = getattr(self.gateway, 'monotonic', time.monotonic)
        correction_errors = []
        corrected, corrected_issues = None, {}
        if monotonic() >= deadline:
            audit['status'] = 'infrastructure_failure'
            raise ModelError('timeout', 'check stage budget exhausted', reason='timeout')
        elif _check_request_bytes(purpose, request) > MAX_CHECK_REQUEST_BYTES:
            correction_errors = ['field correction exceeds material byte limit']
        else:
            audit['retry_requested'] = True
            try:
                answer = self._check_call(purpose, request, audit['outputs'],
                                          deadline=deadline, retry_state=retry_state)
            except ModelError as error:
                if not _structure_failure(error):
                    audit['status'] = 'infrastructure_failure'
                    raise
                correction_errors = [_parse_structure_error(error)]
            else:
                returned = answer.get('sentences')
                correction_errors = _fact_envelope_errors(answer)
                if not isinstance(returned, list) or len(returned) != 1:
                    correction_errors.append('sentence results must cover every sentence')
                else:
                    correction_errors.extend(_sentence_format_errors(returned[0], 1))
                    if not correction_errors:
                        corrected = copy.deepcopy(returned[0])
                        corrected['index'] = local
                        correction_errors.extend(_proof_format_errors(corrected, local))
                        clause_errors, corrected_issues = _validate_clause_checks(
                            {'sentences': [corrected]}, {**payload, 'candidate': [sentence]})
                        correction_errors.extend(clause_errors)
        if correction_errors:
            audit['correction_errors'] = correction_errors
            audit['retry_errors'] = correction_errors
            reason = 'invalid sentence check after correction: ' + '; '.join(errors + correction_errors)
            return _conservative_check_row(local, reason, row), {local: errors + correction_errors + adverse}, audit
        audit['status'] = 'corrected'
        if adverse:
            corrected['supported'] = False
            corrected['fabricated'] = corrected['fabricated'] or row.get('fabricated') is True
            old_violations = row.get('violations')
            if isinstance(old_violations, list):
                corrected['violations'] = list(dict.fromkeys(corrected['violations'] +
                    [v for v in old_violations if isinstance(v, str) and v.strip()]))
            corrected_issues.setdefault(local, []).extend(adverse)
            audit['preserved_adverse_reasons'] = adverse
        return corrected, corrected_issues, audit

    def _complete_positive_focus(self, purpose, payload, batch, outputs, *, deadline, retry_state):
        """Check an omitted positive suffix once, without replacing prior judgments.

        This is additional proof work, not acceptance of a partial response. Both
        calls share the fixed stage deadline and timeout retry allowance.
        Malformed/negative prefixes cannot use this path. A malformed completion
        goes through the same local field correction and conservative rejection.
        """
        rows = batch.get('sentences')
        candidates = payload['candidate']
        if (not isinstance(rows, list) or len(rows) != len(candidates)
                or batch.get('change_degree') not in ('small', 'medium', 'large')
                or not isinstance(batch.get('reason'), str) or not batch['reason'].strip()
                or not all(_valid_sentence_verdict(row, i, require_kind=True)
                           for i, row in enumerate(rows, 1))):
            return batch
        merged = copy.deepcopy(batch)
        for position, (sentence, row) in enumerate(zip(candidates, rows)):
            parts, requested = row.get('clauses'), sentence['clauses']
            if (sentence['origin'] != 'memory' or sentence.get('initial_setting')
                    or not row['supported'] or row['fabricated'] or row['violations']
                    or row['persona_kind'] in EXCLUDED_KINDS
                    or row.get('stop_after_failure', False) is not False
                    or not isinstance(parts, list) or not 0 < len(parts) < len(requested)
                    or not all(isinstance(part, dict) and part.get('supported') is True
                               for part in parts)):
                continue
            prefix = {**sentence, 'clauses': requested[:len(parts)]}
            errors, problems = _validate_clause_checks(
                {'sentences': [copy.deepcopy(row)]}, {**payload, 'candidate': [prefix]})
            if errors or problems:
                continue
            tail = requested[len(parts):]
            request = {**payload, 'task': 'check_facts_and_rules',
                'candidate': [{**sentence, 'index': 1, 'clauses': tail}],
                'evidence_by_ref': {ref: payload['evidence_by_ref'][ref] for ref in sentence['evidence_refs']}}
            request.pop('previous', None)
            request.pop('candidate_text', None)
            audit = {'sentence_index': sentence['index'],
                'missing_clause_indices': [part['index'] for part in tail],
                'completed': False, 'outputs': {}}
            outputs.setdefault(purpose + '_focus_completions', []).append(audit)
            try:
                answer = self._check_call(purpose, request, audit['outputs'], deadline=deadline, retry_state=retry_state)
            except ModelError as error:
                if not _structure_failure(error):
                    raise
                audit['errors'] = [_parse_structure_error(error)]
                continue
            completion_rows = answer.get('sentences')
            if (isinstance(completion_rows, list) and len(completion_rows) == 1
                    and isinstance(completion_rows[0], dict) and type(completion_rows[0].get('index')) is int
                    and completion_rows[0]['index'] == 1):
                # A bad field must not erase a genuine negative in this response
                # when the outer sentence later takes the field-correction path.
                audit['adverse_reasons'] = _adverse_field_verdicts(completion_rows[0])
                _, adverse_proofs = _validate_clause_checks(copy.deepcopy(answer), request)
                audit['adverse_reasons'].extend(adverse_proofs.get(1, []))
            errors = _fact_envelope_errors(answer)
            if not isinstance(completion_rows, list) or len(completion_rows) != 1:
                errors.append('sentence results must cover every sentence')
            else:
                errors.extend(_sentence_format_errors(completion_rows[0], 1))
                errors.extend(_proof_format_errors(completion_rows[0], 1)
                              if isinstance(completion_rows[0], dict) else [])
            checked = copy.deepcopy(answer)
            clause_errors, problems = _validate_clause_checks(checked, request)
            errors.extend(clause_errors)
            if errors:
                audit['errors'] = errors
                continue
            completion = completion_rows[0]
            # Both responses were checked independently. Keep every original
            # and returned verdict, even repeated IDs, so a later positive can
            # never replace an adverse proof. Projection validates each again.
            proof_parts = sorted(parts + completion['clauses'], key=lambda part: part['index'])
            checked_row = checked['sentences'][0]
            if checked_row.get('unchecked_scope_check'):
                audit['unchecked_scope_check'] = True
            extra_checks = checked_row.get('additional_clause_checks')
            if extra_checks:
                audit['additional_clause_indices'] = [part['index'] for part in extra_checks]
            repeated_checks = checked_row.get('repeated_clause_checks')
            if repeated_checks:
                audit['repeated_clause_indices'] = [part['index'] for part in repeated_checks]
            combined = {**copy.deepcopy(row), 'clauses': copy.deepcopy(proof_parts),
                'supported': row['supported'] and completion['supported'] and not problems,
                'fabricated': row['fabricated'] or completion['fabricated'],
                'scene_qualified': row['scene_qualified'] and completion['scene_qualified'],
                'persona_kind': completion['persona_kind'],
                'violations': list(dict.fromkeys(row['violations'] + completion['violations'])),
                'reason': row['reason'] + '；补查：' + completion['reason']}
            for key in ('scope_check', 'stop_after_failure'):
                if key in completion:
                    combined[key] = copy.deepcopy(completion[key])
            merged['sentences'][position] = combined
            audit['completed'] = True
        return merged

    @staticmethod
    def _remember_check_adverse(batch, payload, adverse):
        # A truncation during a later correction must not erase an earlier
        # aligned negative verdict or a failed literal evidence guard.
        returned = batch.get('sentences')
        if not isinstance(returned, list):
            return
        grouped = {}
        for row in returned:
            index = row.get('index') if isinstance(row, dict) else None
            if type(index) is int and 1 <= index <= len(payload['candidate']):
                grouped.setdefault(index, []).append(row)
        for index, rows in grouped.items():
            if len(rows) != 1:
                continue
            row = rows[0]
            _, problems = _validate_clause_checks({'sentences':[copy.deepcopy(row)]},
                {**payload, 'candidate':[payload['candidate'][index-1]]})
            reasons = _adverse_field_verdicts(row) + problems.get(index, [])
            if reasons:
                previous = adverse.get(index, {})
                adverse[index] = {
                    'reasons':list(dict.fromkeys(previous.get('reasons', []) + reasons)),
                    'fabricated':previous.get('fabricated', False) or row.get('fabricated') is True,
                    'violations':list(dict.fromkeys(previous.get('violations', []) +
                        [v for v in row.get('violations', []) if isinstance(v, str) and v.strip()]))
                        if isinstance(row.get('violations', []), list) else previous.get('violations', [])}

    def _check_fact_batch(self, purpose, material, subset, raw, *, deadline, retry_state,
                          batch_index, diagnostics):
        adverse = {}
        try:
            return self._check_fact_batch_once(purpose, material, subset, raw, deadline=deadline,
                retry_state=retry_state, batch_index=batch_index, diagnostics=diagnostics, adverse=adverse)
        except ModelError as error:
            if error.reason != 'output_limit':
                raise
            # A single-sentence field/focus retry cannot be recovered by asking
            # a larger enclosing batch again. No truncated fragment is parsed.
            if len(subset) <= 1 or getattr(error, 'check_sentence_count', len(subset)) <= 1:
                raise
            split = {'sentence_indices':[s['_check_sentence'] for s in subset],
                     'stage_deadline':deadline, 'status':'splitting', 'children':[]}
            raw.setdefault(purpose + '_output_splits', []).append(split)
            combined, originals, problems, window_problems = [], [], {}, {}
            middle = len(subset) // 2
            try:
                for offset, child in ((0, subset[:middle]), (middle, subset[middle:])):
                    child_raw = {purpose+'_task':'check_facts_and_rules',
                                 purpose+'_prompt_version':PROMPT_VERSIONS[purpose],
                                 'sentence_indices':[s['_check_sentence'] for s in child]}
                    split['children'].append({'outputs':child_raw})
                    checked, original, child_problems, issues = self._check_fact_batch(
                        purpose, material, child, child_raw, deadline=deadline, retry_state=retry_state,
                        batch_index=batch_index, diagnostics=diagnostics)
                    originals.append(original)
                    for row in checked['sentences']:
                        index = row['index'] + offset
                        row = {**row, 'index':index}
                        if index in adverse:
                            prior = adverse[index]
                            row['supported'] = False
                            row['fabricated'] = row['fabricated'] or prior['fabricated']
                            row['violations'] = list(dict.fromkeys(row['violations'] + prior['violations']))
                            row['clause_errors'] = list(dict.fromkeys(
                                row.get('clause_errors', []) + prior['reasons']))
                            problems.setdefault(index, []).extend(prior['reasons'])
                            window_problems.setdefault(index, []).extend(prior['reasons'])
                        combined.append(row)
                    for target, source in ((window_problems, child_problems), (problems, issues)):
                        for index, reasons in source.items():
                            target.setdefault(index + offset, []).extend(reasons)
            except ModelError as repeated:
                split['status'] = 'incomplete_' + (repeated.reason or repeated.category)
                raise
            split['status'] = 'recovered'
            for event in raw.get(purpose + '_structure_events', []):
                if event['status'] == 'infrastructure_failure':
                    event.update(status='partial', recovered_by_output_split=True)
            split['preserved_adverse_sentence_indices'] = [subset[i-1]['_check_sentence'] for i in sorted(adverse)]
            batch = {'sentences':combined, 'change_degree':'small',
                     'reason':'complete sentence checks after output split'}
            return batch, {'output_split_batches':originals}, window_problems, problems

    def _check_fact_batch_once(self, purpose, material, subset, raw, *, deadline, retry_state,
                               batch_index, diagnostics, adverse):
        payload = _fact_payload(material, subset)
        invalid_batches = diagnostics['invalid_batches']
        invalid_fields = diagnostics['invalid_fields']
        normalizations = diagnostics['normalizations']
        short_circuits = diagnostics['short_circuits']
        first_error = None
        try:
            batch = self._check_call(purpose, payload, raw, deadline=deadline, retry_state=retry_state)
        except ModelError as error:
            if not _structure_failure(error):
                raise
            batch = {}
            first_error = _parse_structure_error(error)
        original_batch = copy.deepcopy(batch)
        self._remember_check_adverse(batch, payload, adverse)
        batch, prior_adverse, conservative, batch_audit = self._correct_batch_coverage(
            purpose, payload, batch, raw, deadline=deadline, retry_state=retry_state,
            sentence_indices=[s['_check_sentence'] for s in subset], first_error=first_error)
        if batch_audit is not None:
            invalid_batches.append({'batch': batch_index + 1,
                **{key: value for key, value in batch_audit.items() if key != 'outputs'}})
        else:
            self._remember_check_adverse(batch, payload, adverse)
            batch = self._complete_positive_focus(purpose, payload, batch, raw,
                deadline=deadline, retry_state=retry_state)
        checked_batch = {**batch, 'validation_errors': []}
        returned = batch.get('sentences')
        if isinstance(returned,list):
            checked_batch['sentences'] = [{**row,'clause_errors':[]} if isinstance(row,dict) else row
                                          for row in returned]
        mapped = (isinstance(returned, list) and len(returned) == len(subset)
            and all(isinstance(row, dict) and type(row.get('index')) is int and row['index'] == i
                    for i,row in enumerate(returned,1)))
        problems = {}
        if mapped:
            normalized = []
            for local,(sentence,row) in enumerate(zip(subset,returned),1):
                row = {**copy.deepcopy(row), 'clause_errors': []}  # Preserve the raw model output.
                if local in conservative:
                    # This row was created here, not supplied by the model.
                    # It has no proof and can only enter repair/deletion.
                    errors, issues, malformed = [], {local: [row['reason']]}, []
                else:
                    malformed = _sentence_format_errors(row, local) + _proof_format_errors(row, local)
                    errors, issues = _validate_clause_checks({'sentences':[row]},
                        {**payload, 'candidate':[payload['candidate'][local-1]]})
                    malformed.extend(errors)
                if malformed:
                    if batch_audit is not None:
                        # The one batch reask already covered this row. Never
                        # cascade another structural retry after a bad retry.
                        audit = _structure_event(raw, purpose, 'sentence', malformed,
                            sentence_index=sentence['_check_sentence'], local_index=local,
                            covered_by_batch_retry=True, status='conservative_rejection')
                        batch_audit['status'] = 'partial'
                        batch_audit['conservative_sentence_indices'] = sorted(set(
                            batch_audit['conservative_sentence_indices'] + [sentence['_check_sentence']]))
                        row = _conservative_check_row(local, 'invalid sentence in batch retry', row)
                        issues.setdefault(local, []).extend(malformed)
                    else:
                        row, issues, audit = self._correct_sentence_fields(
                            purpose, payload, row, malformed, issues, raw, deadline=deadline,
                            retry_state=retry_state, original_index=sentence['_check_sentence'])
                    invalid_fields.append({'batch': batch_index + 1,
                        **{key: value for key, value in audit.items() if key != 'outputs'}})
                if local in prior_adverse:
                    prior = prior_adverse[local]
                    row['supported'] = False
                    row['fabricated'] = row['fabricated'] or prior['fabricated']
                    row['violations'] = list(dict.fromkeys(row['violations'] + prior['violations']))
                    issues.setdefault(local, []).extend(prior['reasons'])
                if row.get('additional_clause_checks'):
                    full_sequence = sorted({part['index'] for part in row['clauses'] + row['additional_clause_checks']}) == [
                        part['index'] for part in _clause_parts(sentence['text'])]
                    normalizations.append({'batch':batch_index+1, 'sentence_index':sentence['_check_sentence'],
                        'operation':('full_sentence_focus_checks' if full_sequence else 'additional_focus_checks'),
                        'requested_clause_indices':[part['index'] for part in row['clauses']],
                        'additional_clause_indices':[part['index'] for part in row['additional_clause_checks']]})
                if row.get('repeated_clause_checks'):
                    normalizations.append({'batch':batch_index+1, 'sentence_index':sentence['_check_sentence'],
                        'operation':'repeated_focus_checks',
                        'requested_clause_indices':[part['index'] for part in row['clauses']],
                        'repeated_clause_indices':[part['index'] for part in row['repeated_clause_checks']]})
                if row.get('unchecked_scope_check'):
                    normalizations.append({'batch':batch_index+1,
                        'sentence_index':sentence['_check_sentence'],
                        'operation':'scope_omitted_after_evidence_rejection',
                        'checked_clause_indices':[part['index'] for part in row['clauses']]})
                if row.get('unchecked_clause_indices'):
                    if row.get('guard_rejected_prefix'):
                        normalizations.append({'batch':batch_index+1,
                            'sentence_index':sentence['_check_sentence'],
                            'operation':'positive_prefix_rejected_by_evidence_guard',
                            'checked_clause_indices':[part['index'] for part in row['clauses']],
                            'unchecked_clause_indices':list(row['unchecked_clause_indices'])})
                    elif 'stop_after_failure' not in returned[local-1]:
                        normalizations.append({'batch':batch_index+1,
                            'sentence_index':sentence['_check_sentence'],
                            'operation':'negative_prefix_without_stop_marker',
                            'checked_clause_indices':[part['index'] for part in row['clauses']],
                            'unchecked_clause_indices':list(row['unchecked_clause_indices'])})
                    short_circuits.append({'batch':batch_index+1,
                        'sentence_index':sentence['_check_sentence'],
                        'unchecked_clause_indices':list(row['unchecked_clause_indices'])})
                if isinstance(row.get('persona_kind'), str) and row['persona_kind'] in EXCLUDED_KINDS:
                    issues.setdefault(local, []).append(EXCLUDED_KINDS[row['persona_kind']])
                problems.update(issues)
                # A false/invalid proof cannot be overridden by a true row flag.
                if _valid_sentence_verdict(row,local) and problems.get(local):
                    row['supported'] = False
                normalized.append(row)
            checked_batch['sentences'] = normalized
            if batch_audit is not None:
                invalid_batches[-1].update({key:value for key,value in batch_audit.items() if key != 'outputs'})
        degree, structural, window_problems = _check_verdicts(checked_batch, subset, material=material)
        if structural:
            # Defensive final invariant: no model schema defect may become
            # an incomplete check, or move a judgment to another sentence.
            _structure_event(raw, purpose, 'batch', structural, status='conservative',
                conservative_sentence_indices=[s['_check_sentence'] for s in subset],
                covered_by_batch_retry=batch_audit is not None)
            checked_batch = {'change_degree':'small', 'reason':'invalid batch structure',
                'sentences':[_conservative_check_row(i, 'invalid batch structure')
                             for i in range(1, len(subset) + 1)]}
            problems = {i:['invalid batch structure'] for i in range(1, len(subset) + 1)}
            _, _, window_problems = _check_verdicts(checked_batch, subset, material=material)
        return checked_batch, original_batch, window_problems, problems

    def _check_all(self, purpose, material, sentences, *, full_content, outputs, assess_degree=True):
        try:
            result = self._check_all_stage(purpose, material, sentences, full_content=full_content,
                                          outputs=outputs, assess_degree=assess_degree)
        except ModelError as error:
            if error.reason != 'output_limit':
                if error.reason == 'timeout':
                    outputs[purpose + '_incomplete'] = 'timeout'
                raise
            result = {'sentences':[], 'change_degree':'large', 'reason':'incomplete check: output_limit',
                      'validation_errors':['output_limit; no complete judgment'],
                      'incomplete_checks':[]}
        raw_batches = outputs.get(purpose + '_batches', [])
        if raw_batches and purpose not in outputs:
            outputs.update(raw_batches[0])
        result['output_limit_diagnostics'] = _output_limit_diagnostics(
            outputs.get(purpose + '_batches', []), purpose)
        if result.get('validation_errors'):
            outputs[purpose + '_incomplete'] = 'output_limit'
        return result

    def _check_all_stage(self, purpose, material, sentences, *, full_content, outputs, assess_degree=True):
        """Check small focus windows, then AND facts and union failures per sentence."""
        import time
        subsets = _check_batches(sentences, material=material, purpose=purpose) if sentences else []
        # One fixed stage ceiling includes size splitting, batch/field correction, timeout
        # retry and degree. Even one fact window can retry within this same ceiling.
        stage_seconds = MAX_CHECK_STAGE_SECONDS
        deadline = getattr(self.gateway, 'monotonic', time.monotonic)() + stage_seconds
        raw_batches = outputs.setdefault(purpose + '_batches', [])
        rows, batches, short_circuits = {}, [], []
        invalid_fields, invalid_batches, material_rejections = [], [], []
        outputs[purpose + '_material_rejections'] = material_rejections
        rejected, skipped, normalizations = set(), [], []
        outputs[purpose + '_skipped_windows'] = skipped
        window_map = outputs.setdefault(purpose + '_windows', [])
        for planned_index,planned in enumerate(subsets):
            subset = []
            for sentence in planned:
                original = sentence['_check_sentence']
                if original in rejected:
                    indices = [p['index'] for p in sentence.get('_check_focus',[])]
                    rows[original]['supported'] = False
                    extra_checked = {part['index'] for part in rows[original].get('additional_clause_checks', [])}
                    unchecked = [index for index in indices if index not in extra_checked]
                    if unchecked:
                        rows[original].setdefault('unchecked_clause_indices', []).extend(unchecked)
                    skip = {'planned_batch':planned_index+1, 'sentence_index':original,
                            'clause_indices':indices, 'reason':'sentence_already_rejected'}
                    if extra_checked.intersection(indices):
                        skip['already_checked_clause_indices'] = [i for i in indices if i in extra_checked]
                    skipped.append(skip)
                else:
                    subset.append(sentence)
            if planned and not subset:
                continue  # A failed sentence can never become retained in a later window.
            payload = _fact_payload(material, subset)
            request_bytes = _check_request_bytes(purpose, payload)
            if request_bytes > MAX_CHECK_REQUEST_BYTES:
                # The planner reduced this to one indivisible focus. Dropping
                # source text could erase qualifiers: abstain on the whole sentence.
                sentence = subset[0]
                original = sentence['_check_sentence']
                reason = 'complete check material exceeds byte limit'
                rows[original] = _conservative_check_row(original, reason)
                rows[original]['clause_errors'] = [reason]
                rejected.add(original)
                material_rejections.append({'sentence_index': original, 'request_bytes': request_bytes,
                    'limit_bytes': MAX_CHECK_REQUEST_BYTES, 'reason': reason})
                continue
            batch_index = len(batches)
            window_map.append([{'sentence_index':s['_check_sentence'],
                                'clause_indices':[p['index'] for p in s.get('_check_focus',[])]}
                               for s in subset])
            raw = {purpose+'_task':'check_facts_and_rules',
                   purpose+'_prompt_version':PROMPT_VERSIONS[purpose]}
            raw_batches.append(raw)
            retry_state = {'timeouts': 0}
            checked_batch, original_batch, window_problems, problems = self._check_fact_batch(
                purpose, material, subset, raw, deadline=deadline, retry_state=retry_state,
                batch_index=batch_index, diagnostics={
                    'invalid_batches':invalid_batches, 'invalid_fields':invalid_fields,
                    'normalizations':normalizations, 'short_circuits':short_circuits})
            if batch_index == 0:
                outputs.update(raw)
            batches.append(original_batch)
            for local,(sentence,row) in enumerate(zip(subset,checked_batch['sentences']),1):
                original = sentence['_check_sentence']
                if window_problems.get(local):
                    rejected.add(original)
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
                        'additional_clause_checks':previous.get('additional_clause_checks',[])
                            + part.get('additional_clause_checks',[]),
                        'repeated_clause_checks':previous.get('repeated_clause_checks',[])
                            + part.get('repeated_clause_checks',[]),
                        'unchecked_clause_indices':previous.get('unchecked_clause_indices',[])
                            + part.get('unchecked_clause_indices',[]),
                        'clause_errors':previous['clause_errors']+part['clause_errors']}
                if part.get('unchecked_scope_check'):
                    rows[original]['unchecked_scope_check'] = True
        for row in rows.values():
            extra_checked = {part['index'] for part in row.get('additional_clause_checks', [])}
            if extra_checked and row.get('unchecked_clause_indices'):
                row['unchecked_clause_indices'] = [i for i in row['unchecked_clause_indices'] if i not in extra_checked]
        degree, reason, degree_errors = 'large', 'degree deferred to recovery', []
        assessment = {'status':'deferred_to_recovery'}
        if rejected:
            assessment = {'status':'deferred_after_rejection'}
            reason = 'degree not assessed after factual rejection'
        elif assess_degree:
            # The degree model sees only the completed draft and previous current
            # version. It cannot rejudge facts or replace any adverse proof.
            payload = _check_payload(material, [], full_content=full_content)
            raw = {purpose+'_task':'degree_only', purpose+'_prompt_version':DEGREE_PROMPT_VERSION}
            raw_batches.append(raw)
            window_map.append([])
            retry_state = {'timeouts': 0}
            comparison, degree_event = None, None
            try:
                for attempt in range(2):
                    comparison, errors = None, []
                    try:
                        comparison = self._check_call(purpose, payload, raw if not attempt else degree_event['outputs'],
                            deadline=deadline, retry_state=retry_state)
                    except ModelError as error:
                        if not _structure_failure(error):
                            raise
                        errors = [_parse_structure_error(error)]
                    else:
                        if set(comparison) not in ({'change_degree','reason'}, {'sentences','change_degree','reason'}):
                            errors.append('degree: invalid fields')
                        errors.extend(_fact_envelope_errors(comparison))
                        if 'sentences' in comparison and (not isinstance(comparison['sentences'], list) or comparison['sentences']):
                            errors.append('degree: sentences must be empty')
                    if not errors:
                        if degree_event is not None:
                            degree_event['status'] = 'corrected'
                        degree = {'small':'small','medium':'medium','large':'large',
                                  '小':'small','中':'medium','大':'large'}[comparison['change_degree']]
                        reason = comparison['reason']
                        assessment = {'status':'complete', 'output':comparison}
                        if 'sentences' not in comparison:
                            operation = 'degree_only_empty_sentences'
                            raw.setdefault(purpose+'_normalizations',[]).append(operation)
                            normalizations.append({'batch':len(batches)+1, 'operation':operation})
                        break
                    if not attempt:
                        degree_event = _structure_event(raw, purpose, 'degree', errors,
                            retry_requested=True, status='conservative', stage_deadline=deadline, outputs={})
                    else:
                        degree_event['retry_errors'] = errors
                        degree, reason = 'large', 'conservative degree after invalid structure'
                        assessment = {'status':'conservative', 'output':comparison, 'errors':errors}
                if comparison is not None:
                    batches.append(comparison)
            except ModelError as error:
                if degree_event is not None:
                    degree_event['status'] = 'infrastructure_failure'
                if error.reason != 'output_limit':
                    raise
                degree_errors = ['degree: output_limit; no complete judgment']
                assessment = {'status':'invalid', 'error':'output_limit'}
                degree, reason = 'large', 'incomplete degree judgment'
            finally:
                if not batches or not sentences:
                    outputs.update(raw)
        return {'sentences':[rows[i] for i in sorted(rows)], 'change_degree':degree,
                'reason':reason, 'batches':batches, 'window_map':window_map,
                'degree_assessment':assessment, 'validation_errors':degree_errors,
                'incomplete_checks':[], 'short_circuits':short_circuits,
                'invalid_sentence_checks':invalid_fields, 'invalid_batch_checks':invalid_batches,
                'structure_diagnostics':_structure_diagnostics(raw_batches, purpose),
                'material_rejections':material_rejections,
                'skipped_windows':skipped, 'normalizations':normalizations}

    def _recover_sentences(self, original, initial_model, problems, material, previous, checks, outputs):
        repairable = {i:original[i-1] for i in problems if original[i-1]['origin'] != 'initial_template'}
        replacements = {}
        checks['repair_policy'] = REPAIR_POLICY
        checks['repair_options_rejected'] = []
        for i,sentence in repairable.items():
            replacements[i] = []
            for option in _repair_options(sentence,material):
                _,normalized,deterministic = _deterministic({'sentences':[option]},_editable_material(material),previous)
                issues = (_surface_issues(normalized[0],memories=material['evidence']['memories'])
                          if normalized else [])
                if _repair_claim_key(option['text']) == _repair_claim_key(sentence['text']):
                    issues.append('unchanged failed claim; scene deixis alone is not a repair')
                if not deterministic['passed'] or issues:
                    checks['repair_options_rejected'].append({'index':i,'option':option,
                        'reasons':deterministic['errors']+issues})
                else:
                    replacements[i].append(option)
        # The extractor already has a finite, snapshot-ordered set of complete
        # bound texts. Choose once deterministically, then let the independent
        # semantic recheck accept or delete it. A model selector adds no proof
        # and can consume an entire request budget before that actual check.
        # Do not try a later option after a negative recheck.
        selections = [{'index':i, 'eligible_options':options, 'selected':options[0]}
                      for i,options in replacements.items() if options]
        selected = {item['index']:item['selected'] for item in selections}
        checks['repair_selections'] = selections
        checks['repair_outputs'] = []  # No model selection opinions were requested.
        outputs['persona_repair_selections'] = copy.deepcopy(selections)
        retained, verdicts, modifications, positions = [], [], [], []
        seen_replacements = {}
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
            option = selected.get(i)
            if option is None or sentence['origin']=='initial_template':
                deleted(i,'repair',problems[i]+['immutable templates are never rewritten'])
                continue
            text = option['text']
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
            key = (normalized[0]['text'], tuple((b['memory_id'], b['revision'])
                                               for b in normalized[0]['basis']))
            if key in seen_replacements:
                deleted(i,'repair_duplicate',problems[i]+[
                    f"same complete bound replacement already selected for sentence {seen_replacements[key]}"],text)
                continue
            seen_replacements[key] = i
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
                deleted(original_index,'recheck',problems[original_index]+bad[j],retained[position]['text'])
            else:
                verdicts[position] = recheck['sentences'][j-1]
                checks['repaired_sentences'].append({'index':original_index,
                    'before':original[original_index-1], 'after':retained[position]})
        retained = [s for i,s in enumerate(retained) if i not in discarded]
        verdicts = [{**v,'index':i+1} for i,v in enumerate(v for j,v in enumerate(verdicts) if j not in discarded)]
        if discarded:
            # Rejected repairs defer the comparison. Conservatively require
            # confirmation of the final diff without another model pass.
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
                # Reject only already-known wording/evidence failures before an
                # expensive semantic request. Keep these decisions separate from
                # actual model votes; no unchecked fact receives a fabricated vote.
                problems = {}
                for index, sentence in enumerate(sentences, 1):
                    issues = _surface_issues(sentence, memories=material['evidence']['memories'])
                    if issues:
                        problems[index] = issues
                precheck = [{'index':index, 'reasons':reasons} for index,reasons in problems.items()]
                outputs['persona_precheck_rejections'] = precheck
                positions = [i for i in range(1,len(sentences)+1) if i not in problems]
                eligible = [sentences[i-1] for i in positions]
                checks.update(precheck_rejections=precheck, initial_model_sentence_indices=positions)
                structural, semantic_problems = [], {}
                if eligible:
                    # Its comparison draft must also exclude prechecked failures.
                    # Recovery reassesses the final assembled text after any edit.
                    model = self._check_all('persona_check', material, eligible,
                        full_content=''.join(sentence['text'] for sentence in eligible), outputs=outputs,
                        assess_degree=not bool(problems))
                    degree, structural, semantic_problems = _check_verdicts(model, eligible, material=material)
                problems.update({positions[i-1]:reasons for i,reasons in semantic_problems.items()})
                initial_errors = structural + [f'sentence {i}: {reason}'
                    for i,reasons in sorted(problems.items()) for reason in reasons]
                checks.update(initial_model=model, initial_model_errors=initial_errors)
                model_errors = initial_errors
                if not structural and problems:
                    # Recovery indexes the original proposal. None marks an
                    # unrequested sentence and is never retained or serialized as
                    # a model opinion; every unaffected sentence has a real vote.
                    aligned = [None] * len(sentences)
                    for index,row in zip(positions, model['sentences'] if model else []):
                        aligned[index-1] = {**row, 'index':index}
                    sentences, model, degree, model_errors = self._recover_sentences(
                        sentences, {'sentences':aligned}, problems, material, previous, checks, outputs)
                    content, deterministic = _final_deterministic(sentences, material, previous)
            manual_removed = _admin_removed(previous, sentences)
            if manual_removed:
                degree = 'large'
            conservative_degree = any((checks.get(phase) or {}).get('degree_assessment', {}).get('status') == 'conservative'
                                      for phase in ('initial_model', 'recheck'))
            if conservative_degree:
                degree = 'large'
                checks['degree_override'] = 'invalid_degree_structure_requires_confirmation'
            passed = deterministic['passed'] and not model_errors
            checks.update(passed=passed, deterministic=deterministic, model=model, model_errors=model_errors,
                          admin_content_removed_or_changed=manual_removed)
            mode = settings['publish_mode']
            status = 'rejected' if not passed else ('current' if not conservative_degree and (mode == 'all_auto' or (
                mode == 'small_medium_auto' and degree in ('small','medium'))) else 'pending')
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
        except BaseException:
            # In particular, an evaluator's Ctrl-C must retain completed replies
            # from the active check stage before its temporary database closes.
            self._finish_attempt(attempt, 'failed', 'interrupted', outputs)
            raise
