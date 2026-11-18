"""Daily model review of tentative same-person links."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from importlib.resources import files
from itertools import combinations
from zoneinfo import ZoneInfo

from .claim_sequences import normalize_claim
from .db import dumps
from .memory_ops import lifecycle_settings
from .models import ModelError
from .people import canonical_subject, merge_blocker, merge_link, subject_members
from .state import role_zone

SETTING = 'people_auto_merge_enabled'
PROMPT = files('iris').joinpath('prompts', 'people_merge_v1.md').read_text(encoding='utf-8')
PURPOSE = 'people_merge'
REVIEWS_PER_RUN = 32
RECENT_MESSAGES = 1500
MATERIAL_LIMIT = 56
TEXT_LIMIT = 1400
CLAUSES = re.compile(r'[。！？；!?;\n]')
PLAYING = r'(?:扮演|饰演|(?:今晚|今天|正在|我)演|roleplay(?:ing)?\s+as\s+|playing\s+as\s+)'
CHARACTER = r'(?:用来跑团|跑团专用|角色剧情|角色设定|虚构角色|是.{0,8}的角色号|\bis my character\b)'


def _instant(value):
    try:
        instant = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return instant if instant.tzinfo is not None else None


def _source_ids(conn, rel, a, b, through):
    folded = conn.execute('''WITH RECURSIVE t(id) AS (
        SELECT ? UNION SELECT l.id FROM subject_links l JOIN t ON l.folded_into=t.id)
        SELECT source_message_id FROM subject_links JOIN t USING(id)
        WHERE source_message_id<=? ORDER BY source_message_id DESC LIMIT 24''', (rel['id'], through))
    direct = {row[0] for row in folded}
    related = conn.execute('''SELECT DISTINCT s.message_id FROM memory_subjects own
        JOIN memory_subjects other USING(memory_id) JOIN sources s USING(memory_id)
        JOIN memories m ON m.id=own.memory_id
        WHERE own.subject_id=? AND other.subject_id=? AND s.kind='message'
          AND s.message_id<=? AND m.lifecycle!='deleted' ORDER BY s.message_id DESC LIMIT 32''', (a, b, through))
    return direct, direct | {row[0] for row in related}


def _messages(conn, rel, a, b, members, names, through):
    direct, source_ids = _source_ids(conn, rel, a, b, through)
    columns = '''id,entry_id,kind,sender_subject_id,quote_author_subject_id,content,quote_content,
        occurred_at,original_sender_subject_id,original_quote_author_subject_id'''
    rows = {row['id']: row for row in conn.execute(f'''SELECT {columns} FROM messages
        WHERE id<=? ORDER BY id DESC LIMIT ?''', (through, RECENT_MESSAGES))}
    for sid in sorted(members):
        for row in conn.execute(f'''SELECT {columns} FROM messages WHERE sender_subject_id=? AND id<=?
            ORDER BY id DESC LIMIT 24''', (sid, through)):
            rows[row['id']] = row
    # A source outside the recent window still brings its immediate context.
    for source in conn.execute('SELECT id,entry_id FROM messages WHERE id IN (SELECT value FROM json_each(?))',
                               (dumps(sorted(source_ids)),)):
        for bounds, order in (('id<=?', 'DESC'), ('id>? AND id<=?', 'ASC')):
            args = (source['id'],) if order == 'DESC' else (source['id'], through)
            for row in conn.execute(f'''SELECT {columns} FROM messages WHERE entry_id=? AND {bounds}
                ORDER BY id {order} LIMIT 4''', (source['entry_id'], *args)):
                rows[row['id']] = row
    scores = {}
    previous_by_entry = {}
    adjacent = set()
    prev_anchor_by_entry = set()
    for row in sorted(rows.values(), key=lambda row: row['id']):
        mid, eid = row['id'], row['entry_id']
        text = (row['content'] + '\n' + (row['quote_content'] or '')).casefold()
        mentions = sum(any(name in text for name in side) for side in names)
        from_pair = bool({row['sender_subject_id'], row['quote_author_subject_id'],
                          row['original_sender_subject_id'], row['original_quote_author_subject_id']} & members)
        score = 4 if mid in direct else 3 if mid in source_ids or mentions == 2 else 2 if mentions else int(from_pair)
        if score:
            scores[mid] = score
            adjacent.add(previous_by_entry.get(eid))
        if eid in prev_anchor_by_entry:
            adjacent.add(mid)
        if score:
            prev_anchor_by_entry.add(eid)
        else:
            prev_anchor_by_entry.discard(eid)
        previous_by_entry[eid] = mid
    adjacent.discard(None)
    chosen = sorted(set(scores) | adjacent, key=lambda mid: (scores.get(mid, 0), mid), reverse=True)[:MATERIAL_LIMIT]
    return [rows[mid] for mid in sorted(chosen)]


def _participants(conn, subjects):
    info = {}
    names = []
    members = set()
    for subject in subjects:
        group = subject_members(conn, subject['id'])
        aliases = [r[0] for r in conn.execute('''SELECT alias FROM subject_aliases WHERE subject_id=?
            AND folded_into IS NULL ORDER BY alias LIMIT 40''', (subject['id'],))]
        group_names = [r[0] for r in conn.execute('SELECT name FROM subjects WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id',
                                                (dumps(sorted(group)),))]
        info[subject['id']] = {'name': subject['name'], 'aliases': sorted(set(aliases + group_names) - {subject['name']})}
        names.append({name.casefold() for name in [*aliases, *group_names] if name.strip()})
        members.update(group)
    return info, names, members


def _render_messages(conn, rows, zone):
    names = {}
    roots = {}

    def person(sid):
        if sid is None:
            return None
        if sid not in roots:
            roots[sid] = canonical_subject(conn, sid)
            names[sid] = conn.execute('SELECT name FROM subjects WHERE id=?', (sid,)).fetchone()[0]
        return {'id': roots[sid], 'name': names[sid]}

    rendered = []
    for row in rows:
        occurred = _instant(row['occurred_at'])
        item = {'id': row['id'], 'entry': row['entry_id'], 'kind': row['kind'],
                'author': person(row['original_sender_subject_id'] or row['sender_subject_id']),
                'local_time': occurred.astimezone(zone).isoformat() if occurred else None,
                'text': row['content'][:TEXT_LIMIT]}
        if len(row['content']) > TEXT_LIMIT:
            item['truncated'] = True
        if row['quote_content'] or row['quote_author_subject_id']:
            item['quote'] = {'author': person(row['original_quote_author_subject_id'] or row['quote_author_subject_id']),
                             'text': (row['quote_content'] or '')[:TEXT_LIMIT]}
        rendered.append(item)
    return rendered


def _direct(row):
    return (row['kind'] == 'message' and row['author']['id'] not in ('self', 'scene') and
            row['local_time'] and row['text'].strip() and not row.get('truncated'))


def _independent(left, right, pair):
    a, b = left['author']['id'], right['author']['id']
    return (left['id'] != right['id'] and left['local_time'][:10] != right['local_time'][:10]
            and (a in pair or b in pair or a != b))


def _roleplay_evidence(rows, pair, names):
    """Recognize explicit actor/character statements in the direct dialogue."""
    patterns = []
    for actor, character in ((0, 1), (1, 0)):
        if not names[character]:
            continue
        character_name = '(?:' + '|'.join(re.escape(name) for name in sorted(names[character])) + ')'
        cites_character = re.compile(PLAYING + r'[“「" ]*' + character_name + '|' +
                                    character_name + r'.{0,24}' + CHARACTER)
        patterns.append((actor, cites_character))
    for row in rows:
        for clause in CLAUSES.split(row['text'].casefold()):
            for actor, cites_character in patterns:
                if ((row['author']['id'] == pair[actor] or any(name in clause for name in names[actor]))
                        and cites_character.search(clause)):
                    return True
    return False


def material_for_link(conn, link_id, *, through, zone):
    rel = conn.execute('''SELECT * FROM subject_links WHERE id=? AND kind='same_as' AND status='possible'
        AND folded_into IS NULL AND resolved_at IS NULL''', (link_id,)).fetchone()
    if rel is None:
        return None, 'link_changed'
    pair = (rel['subject_a'], rel['subject_b'])
    subjects = [conn.execute("SELECT * FROM subjects WHERE id=? AND kind='person' AND merged_into IS NULL",
                             (sid,)).fetchone() for sid in pair]
    if None in subjects or {'self', 'scene'} & set(pair):
        return None, 'not_two_people'
    if reason := merge_blocker(conn, *pair, automatic=True):
        return None, reason
    created = [_instant(subject['created_at']) for subject in subjects]
    if None in created:
        return None, 'invalid_creation_time'
    target, source = sorted(zip(created, subjects), key=lambda item: (item[0], item[1]['id']))
    info, names, members = _participants(conn, subjects)
    rows = _render_messages(conn, _messages(conn, rel, *pair, members, names, through), zone)
    direct = [row for row in rows if _direct(row)]
    if _roleplay_evidence(direct, pair, names):
        return None, 'roleplay_evidence'
    if not any(_independent(left, right, pair) for left, right in combinations(direct, 2)):
        return None, 'insufficient_independent_messages'
    payload = {'people': info, 'messages': rows}
    fingerprint = hashlib.sha256(dumps([PROMPT, str(zone), link_id, rel['revision'],
                                        [s['revision'] for s in subjects], payload]).encode()).hexdigest()
    return {'link': rel, 'source': source[1], 'target': target[1], 'pair': pair,
            'payload': payload, 'fingerprint': fingerprint}, None


def judge(gateway, material):
    messages = [{'role': 'system', 'content': PROMPT}, {'role': 'user', 'content': dumps(material['payload'])}]
    return gateway.json_chat(messages, PURPOSE, 16000)[0]


def verified_evidence(material, output):
    if not isinstance(output, dict) or output.keys() != {'decision', 'evidence', 'counter_evidence'}:
        return [], 'invalid_output'
    decision, evidence, counter = (output[key] for key in ('decision', 'evidence', 'counter_evidence'))
    if decision not in ('same_person', 'keep_possible') or not isinstance(evidence, list) or not isinstance(counter, list):
        return [], 'invalid_output'
    if decision == 'keep_possible' or counter:
        return [], 'counter_evidence' if counter else 'model_kept_possible'
    if not 2 <= len(evidence) <= 6:
        return [], 'insufficient_independent_evidence'
    rows = {row['id']: row for row in material['payload']['messages']}
    selected = {}
    for citation in evidence:
        if (not isinstance(citation, dict) or citation.keys() != {'id', 'excerpt'}
                or type(citation['id']) is not int or not isinstance(citation['excerpt'], str)):
            return [], 'invalid_citation'
        mid, excerpt = citation['id'], citation['excerpt'].strip()
        row = rows.get(mid)
        normalized = normalize_claim(excerpt)
        if (row is None or mid in selected or not _direct(row) or not 5 <= len(normalized) <= 200
                or excerpt not in row['text'] or normalized in normalize_claim(row.get('quote', {}).get('text', ''))):
            return [], 'invalid_citation'
        selected[mid] = normalized
    for a, b in combinations(selected, 2):
        if (_independent(rows[a], rows[b], material['pair'])
                and selected[a] not in selected[b] and selected[b] not in selected[a]
                and normalize_claim(rows[a]['text']) != normalize_claim(rows[b]['text'])):
            return sorted((a, b)), 'verified_independent_evidence'
    return [], 'insufficient_independent_evidence'


def _review_item(conn, run_id, link_id):
    return conn.execute('''SELECT 1 FROM maintenance_items WHERE run_id=? AND phase='people' AND item_key=?''',
                        (run_id, str(link_id))).fetchone()


def _record(conn, run_id, link_id, outcome, reason, details, clock):
    conn.execute('''INSERT INTO maintenance_items(run_id,phase,item_key,object_id,outcome,reason,details_json,created_at)
        VALUES(?,'people',?,?,?,?,?,?)''', (run_id, str(link_id), link_id, outcome, reason, dumps(details), clock().isoformat()))


def review_for_run(store, gateway, run, *, link_through, clock, stop=None):
    if gateway is None or not hasattr(gateway, 'json_chat'):
        return True
    run_id, zone = run['id'], ZoneInfo(run['timezone'])
    with store.read() as conn:
        previous = conn.execute("SELECT COUNT(*) FROM maintenance_items WHERE run_id=? AND phase='people'", (run_id,)).fetchone()[0]
        link_ids = [row[0] for row in conn.execute('''SELECT l.id FROM subject_links l
            WHERE l.id<=? AND l.kind='same_as' AND l.status='possible' AND l.folded_into IS NULL AND l.resolved_at IS NULL
                AND NOT EXISTS(SELECT 1 FROM maintenance_items i WHERE i.run_id=? AND i.phase='people'
                    AND i.item_key=CAST(l.id AS TEXT))
            ORDER BY COALESCE((SELECT MAX(i.run_id) FROM maintenance_items i WHERE i.phase='people' AND i.object_id=l.id),0), l.id
            LIMIT ?''', (link_through, run_id, max(0, REVIEWS_PER_RUN - previous)))]
    for link_id in link_ids:
        if stop and stop():
            return False
        with store.read() as conn:
            if not lifecycle_settings(conn).get(SETTING):
                return True
            material, reason = material_for_link(conn, link_id, through=run['message_through'], zone=zone)
            prior = conn.execute('''SELECT details_json FROM maintenance_items WHERE phase='people' AND object_id=?
                AND outcome='kept_possible' ORDER BY run_id DESC LIMIT 1''', (link_id,)).fetchone()
        details = {}
        output = None
        outcome = 'skipped'
        if material is not None:
            details['fingerprint'] = material['fingerprint']
            if prior and details['fingerprint'] == json.loads(prior[0]).get('fingerprint'):
                reason = 'already_reviewed'
            else:
                try:
                    output = judge(gateway, material)
                    outcome = 'kept_possible'
                except ModelError as error:
                    outcome, reason = 'failed', error.reason or error.category
        if stop and stop():
            return False
        try:
            with store.write() as conn:
                if _review_item(conn, run_id, link_id):
                    continue
                if not lifecycle_settings(conn).get(SETTING):
                    return True
                if outcome == 'kept_possible':
                    through = conn.execute('SELECT COALESCE(MAX(id),0) FROM messages').fetchone()[0]
                    live, _ = material_for_link(conn, link_id, through=through, zone=role_zone(conn))
                    if live is None or live['fingerprint'] != material['fingerprint']:
                        outcome, reason = 'skipped', 'stale'
                    else:
                        evidence, reason = verified_evidence(live, output)
                        if reason in ('invalid_output', 'invalid_citation'):
                            outcome = 'failed'
                        if evidence:
                            result = merge_link(conn, live['link'], live['source'], live['target'],
                                                actor='maintenance', stamp=clock().isoformat(), evidence_ids=evidence)
                            outcome = 'people_merged'
                            details.update(result, evidence_message_ids=evidence)
                _record(conn, run_id, link_id, outcome, reason, details, clock)
        except sqlite3.IntegrityError:
            with store.write() as conn:
                if not _review_item(conn, run_id, link_id):
                    _record(conn, run_id, link_id, 'failed', 'write_conflict', details, clock)
        if outcome == 'failed' and output is None:
            break
    return True
