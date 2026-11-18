"""Reverse the identity associations recorded by a new subject merge."""
from __future__ import annotations

import json
import sqlite3

from .db import dumps, now
from .memory_ops import operation
from .people import PeopleConflict, _subject, _subject_ref, canonical_subject, subject_members


def _later_merge(conn, merge):
    return conn.execute('''SELECT 1 FROM subject_merges WHERE id>? AND split_at IS NULL
        AND (target_id=? OR source_id=?) LIMIT 1''',
        (merge['id'], merge['target_id'], merge['target_id'])).fetchone()


def _undo_rows(conn, changes):
    restorations = {}
    later_denials = []
    for table in ('subject_alias_blocks', 'subject_aliases', 'subject_links'):
        rows = []
        for before, after in changes[table]:
            row_id = (after or before)['id']
            row = conn.execute(f'SELECT * FROM {table} WHERE id=?', (row_id,)).fetchone()
            current = dict(row) if row else None
            if current is not None:
                current.pop('revision', None)
                # Later learning can add provenance or update belief without
                # changing the identity of the existing alias or link.
                if before and after and table == 'subject_aliases' and before['source_message_id'] == after['source_message_id']:
                    before['source_message_id'] = after['source_message_id'] = current['source_message_id']
                if before and after and table == 'subject_links' and before['status'] == after['status']:
                    before['belief'] = after['belief'] = current['belief']
                    if current['status'] == 'denied' and after['status'] == 'possible':
                        # Preserve a denial made against the combined identity,
                        # including one from splitting a subsequent merge.
                        before['status'] = after['status'] = 'denied'
                        if (before['subject_a'], before['subject_b']) != (after['subject_a'], after['subject_b']):
                            later_denials.append(current)
            if table == 'subject_aliases' and after and current is None:
                # An administrator removed this alias after the merge.
                continue
            if current != after:
                raise PeopleConflict('合并涉及的资料已变化，无法安全拆分')
            rows.append((row_id, before, after))
        restorations[table] = rows

    for table in ('subject_alias_blocks', 'subject_aliases'):
        missing = []
        for row_id, before, after in restorations[table]:
            if before is None:
                conn.execute(f'DELETE FROM {table} WHERE id=?', (row_id,))
                continue
            if table == 'subject_aliases' and before['source_message_id'] is not None:
                if not conn.execute('SELECT 1 FROM messages WHERE id=?', (before['source_message_id'],)).fetchone():
                    before['source_message_id'] = None
            if after is None:
                missing.append(before)
            else:
                cols = [key for key in before if key != 'id']
                conn.execute(f'UPDATE {table} SET ' + ','.join(f'{key}=?' for key in cols) + ' WHERE id=?',
                             (*[before[key] for key in cols], row_id))
        if missing:
            # A deleted alias and its folded children form one FK-linked tree.
            columns = list(missing[0])
            values = dumps([[row[key] for key in columns] for row in missing])
            projection = ','.join(f"json_extract(value,'$[{i}]')" for i in range(len(columns)))
            conn.execute(f"INSERT INTO {table}({','.join(columns)}) SELECT {projection} FROM json_each(?)", (values,))

    links = restorations['subject_links']
    for row_id, before, _ in links:
        conn.execute('UPDATE subject_links SET subject_a=?,subject_b=?,folded_into=? WHERE id=?',
                     (before['subject_a'], before['subject_b'], before['folded_into'], row_id))
    for row_id, before, _ in links:
        # Release inherited denials only after their evidence link is unfolded.
        changed = conn.execute('''UPDATE subject_links SET status=?,belief=?,inherited_denial_from=?
            WHERE id=?''', (before['status'], before['belief'], before['inherited_denial_from'], row_id))
        if changed.rowcount != 1:
            raise PeopleConflict('联系的永久否认无法安全还原')
    for row in later_denials:
        conn.execute('''INSERT INTO subject_links(subject_a,subject_b,kind,status,belief,source_message_id,created_at)
            VALUES(?,?,'same_as','denied',?,?,?)
            ON CONFLICT(subject_a,subject_b,kind) DO UPDATE SET status='denied' ''',
            (row['subject_a'], row['subject_b'], row['belief'], row['source_message_id'], row['created_at']))


def _restore_memories(conn, merge, changes):
    source_id, target_id = merge['source_id'], merge['target_id']
    restored_speakers = conn.execute('''UPDATE memories SET speaker_subject_id=?
        WHERE speaker_subject_id=? AND purged_at IS NULL AND id IN (SELECT value FROM json_each(?))''',
        (source_id, target_id, dumps(changes['speaker_memory_ids']))).rowcount
    conn.execute('''INSERT OR IGNORE INTO memory_subjects(memory_id,subject_id)
        SELECT memory_id,? FROM memory_subjects WHERE subject_id=? AND memory_id IN (SELECT value FROM json_each(?))''',
        (source_id, target_id, dumps(changes['about_memory_ids'])))
    source_only = set(changes['about_memory_ids']) - set(changes['about_shared'])
    conn.execute('''DELETE FROM memory_subjects WHERE subject_id=?
        AND memory_id IN (SELECT value FROM json_each(?))''', (target_id, dumps(sorted(source_only))))

    # A new memory has no pre-merge association. A single original author
    # within the split group can identify its side without reading its prose.
    attribution = {}
    members = subject_members(conn, source_id) | subject_members(conn, target_id)
    roots = {sid: canonical_subject(conn, sid) for sid in members}
    rows = conn.execute('''SELECT m.id,COALESCE(q.original_sender_subject_id,q.sender_subject_id) AS sender,
        COALESCE(q.original_quote_author_subject_id,q.quote_author_subject_id) AS quoted
        FROM memories m JOIN sources s ON s.memory_id=m.id AND s.kind='message'
        JOIN messages q ON q.id=s.message_id WHERE m.id>? AND m.speaker_subject_id=? AND m.purged_at IS NULL''',
        (merge['memory_through'], target_id))
    for row in rows:
        attribution.setdefault(row['id'], set()).update(roots[sid] for sid in (row['sender'], row['quoted']) if sid in roots)
    for memory_id, authors in attribution.items():
        if authors != {source_id}:
            continue
        conn.execute('UPDATE memories SET speaker_subject_id=? WHERE id=?', (source_id, memory_id))
        restored_speakers += 1
    return restored_speakers


def split_merge(store, merge_id, *, expected_revision):
    try:
        with store.write() as conn:
            return _split_merge(conn, merge_id, expected_revision)
    except sqlite3.IntegrityError as error:
        raise PeopleConflict('合并涉及的资料已变化，无法安全拆分') from error


def _split_merge(conn, merge_id, expected_revision):
    merge = conn.execute('SELECT * FROM subject_merges WHERE id=?', (merge_id,)).fetchone()
    if merge is None:
        raise KeyError(merge_id)
    source_id, target_id = merge['source_id'], merge['target_id']
    if merge['split_at'] is not None:
        raise PeopleConflict('这次合并已拆分')
    _subject(conn, target_id, expected_revision)
    source = conn.execute('SELECT merged_into,merged_at FROM subjects WHERE id=?', (source_id,)).fetchone()
    if tuple(source) != (target_id, merge['created_at']) or _later_merge(conn, merge):
        raise PeopleConflict('请先拆分涉及这两个人物的后续合并')
    changes = json.loads(merge['changes_json'])
    stamp = now()
    conn.execute('UPDATE subject_merges SET split_at=? WHERE id=?', (stamp, merge_id))
    conn.execute('UPDATE subjects SET merged_into=NULL,merged_at=NULL WHERE id=?', (source_id,))
    for identity in changes['platform_identities']:
        moved = conn.execute('''UPDATE platform_identities SET subject_id=?
            WHERE subject_id=? AND platform=? AND account_id=?''',
            (source_id, target_id, identity['platform'], identity['account_id']))
        if moved.rowcount != 1:
            raise PeopleConflict('合并涉及的平台身份已变化')
    children = changes['child_subject_ids']
    moved = conn.execute('''UPDATE subjects SET parent_id=? WHERE parent_id=?
        AND id IN (SELECT value FROM json_each(?))''', (source_id, target_id, dumps(children)))
    if moved.rowcount != len(children):
        raise PeopleConflict('合并涉及的从属人物已变化')
    _undo_rows(conn, changes)
    conn.execute("UPDATE subject_links SET status='denied',resolved_at=NULL WHERE id=?", (merge['link_id'],))
    members = dumps(sorted(subject_members(conn, source_id)))
    for field, original in (('sender_subject_id', 'original_sender_subject_id'),
                            ('quote_author_subject_id', 'original_quote_author_subject_id')):
        conn.execute(f'''UPDATE messages SET {field}=? WHERE {field}=?
            AND {original} IN (SELECT value FROM json_each(?))''', (source_id, target_id, members))
    restored = _restore_memories(conn, merge, changes)
    details = {'merge_id': merge_id, 'source_id': source_id, 'target_id': target_id, 'link_id': merge['link_id'],
               'restored_speaker_count': restored}
    operation(conn, 'subjects_split', 'subject', target_id, details, stamp=stamp)
    return details


def person_merges(conn, subject_id):
    rows = conn.execute('''SELECT m.id,m.source_id,m.target_id,m.actor,m.created_at,m.split_at,m.link_id,
        m.evidence_ids_json,t.revision AS target_revision,t.merged_into
        FROM subject_merges m JOIN subjects t ON t.id=m.target_id
        WHERE m.target_id=? OR m.source_id=? ORDER BY m.id DESC LIMIT 20''', (subject_id, subject_id))
    results = []
    for row in rows:
        item = {key: row[key] for key in ('id', 'actor', 'created_at', 'split_at', 'link_id', 'target_revision')}
        item['source'] = _subject_ref(conn, row['source_id'])
        item['target'] = _subject_ref(conn, row['target_id'])
        item['can_split'] = row['merged_into'] is None and row['split_at'] is None and not _later_merge(conn, row)
        item['evidence_messages'] = [dict(r) for r in conn.execute('''SELECT m.*,s.name AS sender_name,e.name AS entry_name
            FROM messages m JOIN subjects s ON s.id=COALESCE(m.original_sender_subject_id,m.sender_subject_id)
            JOIN entries e ON e.id=m.entry_id WHERE m.id IN (SELECT value FROM json_each(?)) ORDER BY m.id''',
            (row['evidence_ids_json'],))]
        results.append(item)
    return results
