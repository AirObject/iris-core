"""Trace memories and evidence messages back to original messages (design 8.7).

Used by consolidation's automatic handling. Persona keeps its own _trace, which
also collects dates and excerpts for its checks.
"""
from __future__ import annotations

ROLE_SPEECH = 'self_output'


def trace_origins(conn, *, memory_id=None, message_id=None, blocked=(), active_only=True):
    """Original messages reachable through real source edges, keyed by message ID.

    Cycles and repeated paths add nothing. A memory in ``blocked``, or one that
    is no longer active while ``active_only`` is set, is not walked: a lost
    dependency cannot keep supporting anything through its old sources.
    """
    blocked = set(blocked)
    origins, seen, todo = {}, set(), [memory_id] if memory_id is not None else []

    def add(mid):
        row = conn.execute('SELECT id,kind,sender_subject_id,occurred_at FROM messages WHERE id=?', (mid,)).fetchone()
        if row is not None:
            origins[row['id']] = dict(row)

    if message_id is not None:
        add(message_id)
    while todo:
        current = todo.pop()
        if current in seen or current in blocked:
            continue
        seen.add(current)
        row = conn.execute('SELECT lifecycle FROM memories WHERE id=?', (current,)).fetchone()
        if row is None or (active_only and row['lifecycle'] != 'active'):
            continue
        for source in conn.execute('SELECT kind,message_id,source_memory_id FROM sources WHERE memory_id=?', (current,)):
            if source['kind'] == 'memory' and source['source_memory_id'] is not None:
                todo.append(source['source_memory_id'])
            elif source['message_id'] is not None:
                add(source['message_id'])
    return origins


def direct_origins(conn, memory_id):
    """Messages a memory cites itself, without following derived_from."""
    return {row['id']: dict(row) for row in conn.execute(
        '''SELECT m.id,m.kind,m.sender_subject_id,m.occurred_at FROM sources s JOIN messages m ON m.id=s.message_id
        WHERE s.memory_id=? AND s.kind='message' ''', (memory_id,))}


def independent_origins(conn, memory_ids, *, blocked=()):
    """Distinct original messages behind these memories, without the role's own speech.

    A memory that only traces to self_output is the role talking to itself and
    contributes nothing, so repeating a claim cannot manufacture support.
    """
    found = {}
    for memory_id in memory_ids:
        found.update((mid, row) for mid, row in trace_origins(conn, memory_id=memory_id, blocked=blocked).items()
                     if row['kind'] != ROLE_SPEECH)
    return found
