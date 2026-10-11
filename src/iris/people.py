"""Administrator-controlled identities and read-only subject annotations.

canonical_subject is the common transaction-local resolver for intake, recall,
and learning commits. It never guesses by name.
"""
from __future__ import annotations

from urllib.parse import urlencode

from .db import now
from .memory_ops import operation


class PeopleConflict(Exception):
    """The administrator's identity/link snapshot is no longer current."""


def canonical_subject(conn, subject_id: str) -> str:
    seen = set()
    while subject_id not in seen:
        seen.add(subject_id)
        row = conn.execute('SELECT merged_into FROM subjects WHERE id=?', (subject_id,)).fetchone()
        if row is None:
            raise KeyError(subject_id)
        if row['merged_into'] is None:
            return subject_id
        subject_id = row['merged_into']
    raise PeopleConflict('主体合并链异常')


def _subject(conn, subject_id, expected_revision=None):
    row = conn.execute('SELECT * FROM subjects WHERE id=?', (subject_id,)).fetchone()
    if row is None:
        raise KeyError(subject_id)
    if row['merged_into'] is not None or expected_revision is not None and row['revision'] != expected_revision:
        raise PeopleConflict('人物已合并或资料已变化，请刷新后重试')
    if subject_id in ('self', 'scene'):
        raise ValueError('此操作只适用于人物主体')
    return row


def _link(conn, link_id, expected_revision):
    row = conn.execute('SELECT * FROM subject_links WHERE id=?', (link_id,)).fetchone()
    if row is None:
        raise KeyError(link_id)
    if (row['revision'] != expected_revision or row['folded_into'] is not None or row['resolved_at'] is not None
            or row['status'] == 'denied'):
        raise PeopleConflict('联系已处理或已变化，请刷新后重试')
    if row['kind'] != 'same_as':
        raise ValueError('只有可能是同一人的联系可以确认或否认')
    return row


def _tree_ids(conn, table, root):
    # table is selected only by this module, never by a request.
    return [r[0] for r in conn.execute(f'''WITH RECURSIVE tree(id) AS (
        SELECT id FROM {table} WHERE id=? UNION
        SELECT a.id FROM {table} a JOIN tree t ON a.folded_into=t.id)
        SELECT id FROM tree ORDER BY id''', (root,))]


def _alias(conn, alias_id):
    row = conn.execute('SELECT id,alias,source_message_id FROM subject_aliases WHERE id=?', (alias_id,)).fetchone()
    result = dict(row)
    ids = _tree_ids(conn, 'subject_aliases', alias_id)
    result['evidence_message_ids'] = [r[0] for r in conn.execute(
        'SELECT DISTINCT source_message_id FROM subject_aliases WHERE source_message_id IS NOT NULL AND id IN ('
        + ','.join('?' for _ in ids) + ') ORDER BY source_message_id', ids)]
    return result


def add_alias(store, subject_id, alias, *, expected_revision):
    alias = alias.strip()
    if not alias or len(alias) > 100:
        raise ValueError('别名需为 1 至 100 个字符')
    with store.write() as conn:
        subject = _subject(conn, subject_id, expected_revision)
        if alias.casefold() == subject['name'].strip().casefold():
            raise ValueError('别名不能与人物名字相同')
        conn.execute('DELETE FROM subject_alias_blocks WHERE subject_id=? AND alias=?', (subject_id, alias))
        conn.execute('INSERT OR IGNORE INTO subject_aliases(subject_id,alias) VALUES(?,?)', (subject_id, alias))
        row = conn.execute('SELECT id FROM subject_aliases WHERE subject_id=? AND alias=? AND folded_into IS NULL',
                           (subject_id, alias)).fetchone()
        result = _alias(conn, row['id'])
        operation(conn, 'subject_alias_added', 'subject', subject_id, {'alias_id': row['id']})
        return result


def _remove_alias(conn, alias_id):
    ids = _tree_ids(conn, 'subject_aliases', alias_id)
    if ids:
        conn.execute('DELETE FROM subject_aliases WHERE id IN (' + ','.join('?' for _ in ids) + ')', ids)
    return ids


def delete_alias(store, subject_id, alias_id, *, expected_revision):
    with store.write() as conn:
        _subject(conn, subject_id, expected_revision)
        row = conn.execute('SELECT * FROM subject_aliases WHERE id=? AND subject_id=? AND folded_into IS NULL',
                           (alias_id, subject_id)).fetchone()
        if row is None:
            raise KeyError(alias_id)
        conn.execute('INSERT OR IGNORE INTO subject_alias_blocks(subject_id,alias,created_at) VALUES(?,?,?)',
                     (subject_id, row['alias'], now()))
        ids = _remove_alias(conn, alias_id)
        operation(conn, 'subject_alias_removed', 'subject', subject_id, {'alias_id': alias_id, 'removed_alias_ids': ids})
        return {'subject_id': subject_id, 'removed_alias_ids': ids}


def deny_link(store, link_id, *, expected_revision):
    with store.write() as conn:
        row = _link(conn, link_id, expected_revision)
        if any(canonical_subject(conn, sid) != sid for sid in (row['subject_a'], row['subject_b'])):
            raise PeopleConflict('联系中的人物已合并，请刷新后重试')
        conn.execute("UPDATE subject_links SET status='denied' WHERE id=?", (link_id,))
        operation(conn, 'subject_link_denied', 'subject_link', link_id,
                  {'subject_ids': [row['subject_a'], row['subject_b']], 'previous_revision': expected_revision})
        return dict(conn.execute('SELECT id,status,revision FROM subject_links WHERE id=?', (link_id,)).fetchone())


def _ancestors(conn, sid):
    seen = set()
    while sid and sid not in seen:
        seen.add(sid)
        row = conn.execute('SELECT parent_id FROM subjects WHERE id=?', (sid,)).fetchone()
        sid = row['parent_id'] if row else None
    return seen


def _move_aliases(conn, source, target, moved):
    blocks = list(conn.execute('SELECT * FROM subject_alias_blocks WHERE subject_id=? ORDER BY id', (source['id'],)))
    moved['alias_block_ids'] = [r['id'] for r in blocks]
    for row in blocks:
        existing = conn.execute('SELECT id FROM subject_alias_blocks WHERE subject_id=? AND alias=?',
                                (target['id'], row['alias'])).fetchone()
        if existing:
            conn.execute('DELETE FROM subject_alias_blocks WHERE id=?', (row['id'],))
        else:
            conn.execute('UPDATE subject_alias_blocks SET subject_id=? WHERE id=?', (target['id'], row['id']))
    aliases = list(conn.execute('SELECT * FROM subject_aliases WHERE subject_id=? AND folded_into IS NULL ORDER BY id',
                               (source['id'],)))
    moved['alias_ids'] = [r['id'] for r in aliases]
    moved['folded_aliases'], moved['removed_alias_ids'] = [], []
    # Manual removal survives an identity merge. Confirming the identity itself
    # explicitly authorizes the old display name as an alias of the survivor.
    conn.execute('DELETE FROM subject_alias_blocks WHERE subject_id=? AND alias=?', (target['id'], source['name']))
    blocked = {r[0] for r in conn.execute('SELECT alias FROM subject_alias_blocks WHERE subject_id=?', (target['id'],))}
    for row in list(conn.execute('SELECT id,alias FROM subject_aliases WHERE subject_id=? AND folded_into IS NULL', (target['id'],))):
        if row['alias'] in blocked:
            moved['removed_alias_ids'].extend(_remove_alias(conn, row['id']))
    for row in aliases:
        if row['alias'] in blocked or row['alias'].strip().casefold() == target['name'].strip().casefold():
            moved['removed_alias_ids'].extend(_remove_alias(conn, row['id']))
            continue
        existing = conn.execute('SELECT id FROM subject_aliases WHERE subject_id=? AND alias=? AND folded_into IS NULL',
                                (target['id'], row['alias'])).fetchone()
        if existing:
            conn.execute('UPDATE subject_aliases SET folded_into=? WHERE id=?', (existing['id'], row['id']))
            moved['folded_aliases'].append({'alias_id': row['id'], 'into_alias_id': existing['id']})
        else:
            conn.execute('UPDATE subject_aliases SET subject_id=? WHERE id=?', (target['id'], row['id']))
    if source['name'].strip().casefold() != target['name'].strip().casefold():
        conn.execute('INSERT OR IGNORE INTO subject_aliases(subject_id,alias) VALUES(?,?)', (target['id'], source['name']))
        moved['name_alias_id'] = conn.execute('SELECT id FROM subject_aliases WHERE subject_id=? AND alias=?',
                                             (target['id'], source['name'])).fetchone()[0]


def _move_links(conn, source_id, target_id, moved):
    links = list(conn.execute('''SELECT * FROM subject_links WHERE (subject_a=? OR subject_b=?)
        AND folded_into IS NULL AND resolved_at IS NULL ORDER BY id''', (source_id, source_id)))
    moved['links'] = []
    priority = {'possible': 0, 'confirmed': 1, 'denied': 2}
    for row in links:
        a, b = [target_id if sid == source_id else sid for sid in (row['subject_a'], row['subject_b'])]
        if row['kind'] == 'same_as':
            a, b = sorted((a, b))
        existing = conn.execute('''SELECT * FROM subject_links WHERE subject_a=? AND subject_b=? AND kind=?
            AND folded_into IS NULL AND resolved_at IS NULL AND id!=?''', (a, b, row['kind'], row['id'])).fetchone()
        change = {'link_id': row['id'], 'from_subject_ids': [row['subject_a'], row['subject_b']],
                  'to_subject_ids': [a, b], 'source_message_id': row['source_message_id']}
        if existing:
            if priority.get(row['status'], 0) > priority.get(existing['status'], 0):
                conn.execute('UPDATE subject_links SET status=?,belief=? WHERE id=?',
                             (row['status'], row['belief'], existing['id']))
            conn.execute('UPDATE subject_links SET folded_into=? WHERE id=?', (existing['id'], row['id']))
            change['folded_into'] = existing['id']
        else:
            conn.execute('UPDATE subject_links SET subject_a=?,subject_b=? WHERE id=?', (a, b, row['id']))
        moved['links'].append(change)


def confirm_link(store, link_id, *, target_id, expected_revision, expected_source_revision, expected_target_revision):
    """Confirm by merging the other endpoint into target, atomically and once."""
    with store.write() as conn:
        rel = _link(conn, link_id, expected_revision)
        if target_id not in (rel['subject_a'], rel['subject_b']):
            raise ValueError('保留的人物必须是联系的一方')
        source_id = rel['subject_b'] if target_id == rel['subject_a'] else rel['subject_a']
        source = _subject(conn, source_id, expected_source_revision)
        target = _subject(conn, target_id, expected_target_revision)
        if source_id in _ancestors(conn, target_id) or target_id in _ancestors(conn, source_id):
            raise ValueError('不能合并从属人物与其祖先')
        if conn.execute('''SELECT 1 FROM subject_links WHERE kind='roleplay' AND folded_into IS NULL
            AND resolved_at IS NULL AND ((subject_a=? AND subject_b=?) OR (subject_a=? AND subject_b=?))''',
            (source_id, target_id, target_id, source_id)).fetchone():
            raise ValueError('扮演者与虚构角色不能合并')
        moved = {
            'platform_identities': [dict(r) for r in conn.execute('SELECT platform,account_id FROM platform_identities WHERE subject_id=? ORDER BY platform,account_id', (source_id,))],
            'speaker_memory_ids': [r[0] for r in conn.execute('SELECT id FROM memories WHERE speaker_subject_id=? ORDER BY id', (source_id,))],
            'about_memory_ids': [r[0] for r in conn.execute('SELECT memory_id FROM memory_subjects WHERE subject_id=? ORDER BY memory_id', (source_id,))],
            'child_subject_ids': [r[0] for r in conn.execute('SELECT id FROM subjects WHERE parent_id=? ORDER BY id', (source_id,))],
        }
        stamp = now()
        conn.execute("UPDATE subject_links SET status='confirmed',resolved_at=? WHERE id=?", (stamp, link_id))
        _move_aliases(conn, source, target, moved)
        conn.execute('UPDATE platform_identities SET subject_id=? WHERE subject_id=?', (target_id, source_id))
        conn.execute('UPDATE memories SET speaker_subject_id=? WHERE speaker_subject_id=?', (target_id, source_id))
        conn.execute('INSERT OR IGNORE INTO memory_subjects(memory_id,subject_id) SELECT memory_id,? FROM memory_subjects WHERE subject_id=?',
                     (target_id, source_id))
        conn.execute('DELETE FROM memory_subjects WHERE subject_id=?', (source_id,))
        conn.execute('UPDATE subjects SET parent_id=? WHERE parent_id=?', (target_id, source_id))
        _move_links(conn, source_id, target_id, moved)
        conn.execute('UPDATE subjects SET merged_into=?,merged_at=? WHERE id=?', (target_id, stamp, source_id))
        details = {'source_id': source_id, 'target_id': target_id, 'link_id': link_id,
                   'source_revision_before': expected_source_revision, 'target_revision_before': expected_target_revision,
                   'moved': moved}
        operation(conn, 'subjects_merged', 'subject', target_id, details)
        return {'source_id': source_id, 'target_id': target_id, 'link_id': link_id,
                'revision': conn.execute('SELECT revision FROM subjects WHERE id=?', (target_id,)).fetchone()[0]}


def _live_links(conn, subject_ids):
    ids = sorted(set(subject_ids))
    if not ids:
        return []
    marks = ','.join('?' for _ in ids)
    return [dict(r) for r in conn.execute(f'''SELECT l.* FROM subject_links l
        JOIN subjects a ON a.id=l.subject_a JOIN subjects b ON b.id=l.subject_b
        WHERE (l.subject_a IN ({marks}) OR l.subject_b IN ({marks})) AND l.folded_into IS NULL
        AND l.resolved_at IS NULL AND a.merged_into IS NULL AND b.merged_into IS NULL ORDER BY l.id''', [*ids, *ids])]


def _subject_ref(conn, sid):
    return dict(conn.execute('SELECT id,name FROM subjects WHERE id=?', (sid,)).fetchone())


def _link_details(conn, row):
    result = dict(row)
    result['subjects'] = [_subject_ref(conn, row['subject_a']), _subject_ref(conn, row['subject_b'])]
    ids = _tree_ids(conn, 'subject_links', row['id'])
    marks = ','.join('?' for _ in ids)
    result['evidence_messages'] = [dict(r) for r in conn.execute(f'''SELECT DISTINCT m.*,s.name AS sender_name,
        e.name AS entry_name FROM subject_links l JOIN messages m ON m.id=l.source_message_id
        JOIN subjects s ON s.id=m.sender_subject_id JOIN entries e ON e.id=m.entry_id
        WHERE l.id IN ({marks}) ORDER BY m.id''', ids)]
    result['folded_link_ids'] = [i for i in ids if i != row['id']]
    if row['kind'] == 'roleplay':
        result.update(_roleplay_annotation(conn, row, ids))
    return result


def _memory_counts(conn, sid):
    counts = {r['lifecycle']: r['n'] for r in conn.execute('''SELECT m.lifecycle,COUNT(*) AS n FROM memories m
        WHERE m.purged_at IS NULL AND (m.speaker_subject_id=? OR EXISTS(
            SELECT 1 FROM memory_subjects ms WHERE ms.memory_id=m.id AND ms.subject_id=?)) GROUP BY m.lifecycle''', (sid, sid))}
    return counts


def person_detail(store, subject_id):
    with store.read() as conn:
        row = conn.execute('SELECT * FROM subjects WHERE id=?', (subject_id,)).fetchone()
        if row is None or subject_id in ('self', 'scene'):
            raise KeyError(subject_id)
        result = dict(row)
        result['canonical_id'] = canonical_subject(conn, subject_id)
        result['platform_identities'] = [dict(r) for r in conn.execute('SELECT platform,account_id,display_name FROM platform_identities WHERE subject_id=? ORDER BY platform,account_id', (subject_id,))]
        result['aliases'] = [_alias(conn, r[0]) for r in conn.execute('SELECT id FROM subject_aliases WHERE subject_id=? AND folded_into IS NULL ORDER BY alias,id', (subject_id,))]
        result['memory_counts'] = _memory_counts(conn, subject_id)
        result['memory_count'] = sum(result['memory_counts'].values())
        result['memories_url'] = '/admin/api/memories?' + urlencode({'person_id': result['canonical_id'], 'lifecycle': 'all'})
        links = [_link_details(conn, r) for r in _live_links(conn, [subject_id])]
        result['same_as'] = [r for r in links if r['kind'] == 'same_as']
        result['roleplay'] = [r for r in links if r['kind'] == 'roleplay']
        return result


def list_people(store, *, text='', pending_only=False, include_merged=False, limit=30, offset=0):
    where, args = ["s.id NOT IN ('self','scene')"], []
    if not include_merged:
        where.append('s.merged_into IS NULL')
    if text.strip():
        where.append('''(instr(lower(s.name),lower(?))>0 OR EXISTS(SELECT 1 FROM subject_aliases a
            WHERE a.subject_id=s.id AND a.folded_into IS NULL AND instr(lower(a.alias),lower(?))>0))''')
        args.extend([text.strip(), text.strip()])
    pending = '''(SELECT COUNT(*) FROM subject_links l WHERE l.kind='same_as' AND l.status='possible'
        AND l.folded_into IS NULL AND l.resolved_at IS NULL AND (l.subject_a=s.id OR l.subject_b=s.id)
        AND NOT EXISTS(SELECT 1 FROM subjects x WHERE x.id IN (l.subject_a,l.subject_b) AND x.merged_into IS NOT NULL))'''
    if pending_only:
        where.append(pending + '>0')
    clause = ' AND '.join(where)
    with store.read() as conn:
        total = conn.execute('SELECT COUNT(*) FROM subjects s WHERE ' + clause, args).fetchone()[0]
        items = [dict(r) for r in conn.execute(f'''SELECT s.id,s.name,s.kind,s.parent_id,s.merged_into,s.merged_at,
            s.revision,{pending} AS pending_links FROM subjects s WHERE {clause} ORDER BY s.name,s.id LIMIT ? OFFSET ?''',
            [*args, limit, offset])]
        for item in items:
            item['canonical_id'] = canonical_subject(conn, item['id'])
            item['aliases'] = [_alias(conn, r[0]) for r in conn.execute('SELECT id FROM subject_aliases WHERE subject_id=? AND folded_into IS NULL ORDER BY alias,id', (item['id'],))]
            item['memory_count'] = sum(_memory_counts(conn, item['id']).values())
    return {'items': items, 'total': total, 'limit': limit, 'offset': offset}


def _roleplay_annotation(conn, row, folded_ids=None):
    ids = folded_ids or _tree_ids(conn, 'subject_links', row['id'])
    worlds = list(dict.fromkeys(r[0] for r in conn.execute('SELECT world FROM subject_links WHERE id IN ('
                               + ','.join('?' for _ in ids) + ') ORDER BY id', ids)))
    return {'link_id': row['id'], 'actor': _subject_ref(conn, row['subject_a']),
            'character': _subject_ref(conn, row['subject_b']), 'worlds': worlds,
            'belief': row['belief'], 'fictional': True}


def annotate_memories(conn, memories, *, entry_id=None):
    """Append metadata only AFTER selection/budget/judgment. Never expose evidence prose."""
    involved = [{m['speaker_subject_id'], *(p['id'] for p in m['about'])} for m in memories]
    links = _live_links(conn, set().union(*involved) if involved else set())
    from .memory_ops import Visibility
    visibility = Visibility(conn)
    annotations = {}
    for row in links:
        link_ids = _tree_ids(conn, 'subject_links', row['id'])
        marks = ','.join('?' for _ in link_ids)
        evidence = [r[0] for r in conn.execute(
            f'SELECT source_message_id FROM subject_links WHERE id IN ({marks})', link_ids) if r[0] is not None]
        if not visibility.allows(visibility.evidence(evidence), entry_id):
            continue
        if row['status'] == 'denied':
            continue
        if row['kind'] == 'same_as' and row['status'] == 'possible':
            annotations[row['id']] = ('possible_same_as', {'link_id': row['id'], 'belief': row['belief'],
                'subjects': [_subject_ref(conn, row['subject_a']), _subject_ref(conn, row['subject_b'])]})
        elif row['kind'] == 'roleplay':
            annotations[row['id']] = ('roleplay', _roleplay_annotation(conn, row))
    for memory, subjects in zip(memories, involved):
        metadata = {'possible_same_as': [], 'roleplay': []}
        for row in links:
            if row['id'] in annotations and subjects.intersection((row['subject_a'], row['subject_b'])):
                key, annotation = annotations[row['id']]
                metadata[key].append(annotation)
        memory['subject_annotations'] = metadata
