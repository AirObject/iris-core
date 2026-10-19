"""Second planner review: maintenance provenance and purge publication lifecycle."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from iris import admin_data, entry_profile, memory_ops, persona
from iris.db import Store, dumps, now
from iris.memory_ops import purge_memory, setup_role
from test_purge_raw_outputs import PROSE, insert, record
from test_purge_review import before_purge_store, run_row
from test_retrieval import put


def seed_versions(store, *, manual=True):
    setup_role(store, 'Iris', PROSE)
    mid = 1
    erased = {'text': PROSE, 'origin': 'model', 'author': 'model',
              'basis': [{'memory_id': mid, 'revision': 1}]}
    kept = {'text': '管理员改写后保留的独立句子。\n', 'origin': 'admin', 'author': 'admin', 'basis': []}
    sentences = [erased, kept] if manual else [erased]
    with store.write() as conn:
        current = persona._insert(conn, content=''.join(s['text'] for s in sentences), sentences=sentences,
            checks={'passed': True}, degree='small', status='current', source='regenerate', stamp=now(), base=1)
        pending = persona._insert(conn, content=PROSE, sentences=[erased], checks={'passed': True},
            degree='large', status='pending', source='regenerate', stamp=now(), base=current['id'])
        for entry in ('A', 'B'):
            conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES(?,?,'test','group')", (entry, entry))
            setting = entry_profile._ensure(conn, entry, now())
            items = json.loads(dumps(sentences if entry == 'A' else [kept]))
            for item in items:
                for basis in item['basis']:
                    row = dict(conn.execute('SELECT * FROM memories WHERE id=?', (basis['memory_id'],)).fetchone())
                    basis['sha256'] = entry_profile._memory_hash(row, entry_profile._direct_sources(conn, basis['memory_id'], entry))
            entry_profile._insert(conn, entry, previous=0, content=''.join(s['text'] for s in items),
                sentences=items, checks={'passed': True, 'format_version': 2}, degree='small', status='published',
                stamp=now(), author='model', source='regenerate', material={}, message_through=0,
                settings_revision=setting['revision'])
            entry_profile._insert(conn, entry, previous=1, content=''.join(s['text'] for s in items),
                sentences=items, checks={'passed': True, 'format_version': 2}, degree='large', status='candidate',
                stamp=now(), author='model', source='regenerate', material={}, message_through=0,
                settings_revision=setting['revision'])
    return mid, current, pending, kept


@pytest.mark.parametrize('upgrade', [False, True])
def test_purge_publication_supersedes_candidates_and_keeps_old_snapshot(tmp_path, monkeypatch, upgrade):
    path = tmp_path / 'publication.db'
    store = before_purge_store(path, monkeypatch) if upgrade else Store(path)
    try:
        mid, previous, pending, kept = seed_versions(store)
        if upgrade:
            with store.write() as conn:
                conn.execute("UPDATE memories SET content='',purged_at=?,lifecycle='deleted',revision=2 WHERE id=?", (now(), mid))
                conn.execute('DELETE FROM memory_subjects WHERE memory_id=?', (mid,))
            store.close()
            store = Store(path)
        else:
            purge_memory(store, mid, 1, confirm=True)
        current = persona.current_persona(store)
        assert current['source'] == 'purge'
        assert current['self_snapshot'] == previous['self_snapshot']
        assert current['sentences'] == [kept]
        assert record(store, 'persona_versions', pending['id'])['status'] == 'superseded'
        candidate = entry_profile.get_version(store, 'A', 2)
        assert candidate['status'] == 'rejected'
        assert candidate['checks']['reason'] == 'superseded'
        assert candidate['checks']['superseded_by'] == 3
        assert candidate['checks']['redacted'] is True
        assert candidate['content'] == ''
        assert entry_profile.get_version(store, 'B', 2)['status'] == 'candidate'
        assert entry_profile.current_profile(store, 'A')['sentences'] == [kept]
        assert persona.persona_due(store)['changed_memory_ids'] == [mid]
    finally:
        store.close()


def test_purge_preview_counts_kept_unbased_sentences_without_guessing_text(store):
    mid, previous, pending, kept = seed_versions(store)
    preview = admin_data.memory_detail(store, mid)['purge_preview']
    assert preview['persona']['retained_unbased_sentences'] == 1
    assert preview['entry_profiles']['retained_unbased_sentences'] == 2  # Both current entries retain their admin sentence.
    assert preview['persona']['sentences'] == preview['entry_profiles']['sentences'] == 1
    bulk = admin_data.bulk_memory_preview(store, subject_id='self', action='purge', include_pinned=True)
    assert bulk['purge_preview'] == preview
    purge_memory(store, mid, 1, confirm=True)
    assert persona.current_persona(store)['content'] == kept['text']
    assert entry_profile.current_profile(store, 'A')['content'] == kept['text']


@pytest.mark.parametrize('marker', ['redacted', 'empty'])
def test_persona_rollback_rejects_erased_or_empty_target_without_side_effects(store, marker):
    setup_role(store, 'Iris')
    with store.write() as conn:
        old = insert(conn, 'persona_versions', content='' if marker == 'empty' else '残留占位文字',
            is_current=0, status='history', source='regenerate', created_at=now(),
            checks_json=dumps({'passed': False, 'redacted': marker == 'redacted'}))
    before = persona.current_persona(store)
    with pytest.raises(ValueError, match='rollback'):
        persona.rollback(store, old, expected_version=before['id'])
    assert persona.current_persona(store) == before


def test_empty_purge_persona_is_due_and_regenerates_as_manual_candidate(store):
    from test_persona import FakeGateway, add_self
    mid, previous, pending, kept = seed_versions(store, manual=False)
    safe = add_self(store, '我在这次交流中介绍了新的灯具。')
    purge_memory(store, mid, 1, confirm=True)
    current = persona.current_persona(store)
    assert current['checks']['needs_regeneration'] and current['content'] == ''
    due = persona.persona_due(store)
    assert due['due'] and due['reason'] == 'needs_regeneration'
    material = persona.select_evidence(store)
    ref = next(item['ref'] for item in material['memories'] if item['memory_id'] == safe)
    model = FakeGateway(store, {'sentences': [{'text': '在这次交流中，我介绍了新的灯具。', 'basis': [ref]}]})
    result = persona.PersonaEngine(store, model).update(expected_version=current['id'])
    assert result['status'] == 'pending'
    assert result['version']['content'] and len(model.calls) == 3
    assert [payload['task'] for purpose,payload in model.calls if purpose=='persona_check'] == [
        'check_facts_and_rules', 'degree_only']
    assert persona.current_persona(store)['id'] == current['id']


@pytest.mark.parametrize('link', ['attempt_id', 'version_id'])
@pytest.mark.parametrize('late', [False, True])
def test_persona_report_item_links_real_ids_and_clears_late_writes(store, link, late):
    from iris.consolidation import _persona_result
    setup_role(store, 'Iris', PROSE)
    with store.write() as conn:
        run = run_row(conn)
        conn.execute("UPDATE maintenance_runs SET state='completed' WHERE id=?", (run,))
        attempt = insert(conn, 'persona_attempts', source='periodic', base_version_id=1, state='running',
                         material_json=dumps({'memory_id': 1, 'revision': 1, 'content': PROSE}), created_at=now())
        # No maintenance_persona.attempt_id link: prove the details link is sufficient.
        conn.execute("INSERT INTO maintenance_persona(run_id,status) VALUES(?,'running')", (run,))
    details = {link: attempt if link == 'attempt_id' else 1, 'content': PROSE, 'checks': {'reason': PROSE}}
    if late:
        purge_memory(store, 1, 1, confirm=True)
    _persona_result(store, run, 'published', None, details, lambda: datetime.now(timezone.utc))
    if not late:
        purge_memory(store, 1, 1, confirm=True)
    with store.read() as conn:
        row = conn.execute("SELECT * FROM maintenance_items WHERE run_id=? AND phase='persona'", (run,)).fetchone()
        assert row['details_json'] == '{}'
        assert row['reason'] == memory_ops.PURGE_REASON
        assert conn.execute("SELECT 1 FROM purge_redactions WHERE table_name='maintenance_items' AND row_id=?",
                            (dumps([run, 'persona', 'update']),)).fetchone()
    with store.write() as conn:
        conn.execute("UPDATE maintenance_items SET details_json=? WHERE run_id=? AND phase='persona'", (dumps(details), run))
    with store.read() as conn:
        assert conn.execute('SELECT details_json FROM maintenance_items WHERE run_id=?', (run,)).fetchone()[0] == '{}'


@pytest.mark.parametrize('link', ['attempt_id', 'version_id'])
def test_upgrade_redacts_persona_report_by_details_link(tmp_path, monkeypatch, link):
    from iris.consolidation import _persona_result
    path = tmp_path / 'report-upgrade.db'
    store = before_purge_store(path, monkeypatch)
    setup_role(store, 'Iris', PROSE)
    with store.write() as conn:
        run = run_row(conn)
        attempt = insert(conn, 'persona_attempts', source='periodic', base_version_id=1, state='current',
                         material_json=dumps({'memory_id': 1, 'revision': 1}), created_at=now())
        conn.execute("INSERT INTO maintenance_persona(run_id,status) VALUES(?,'running')", (run,))
    _persona_result(store, run, 'published', None, {link: attempt if link == 'attempt_id' else 1, 'content': PROSE},
                    lambda: datetime.now(timezone.utc))
    with store.write() as conn:
        conn.execute("UPDATE memories SET content='',purged_at=?,lifecycle='deleted',revision=2 WHERE id=1", (now(),))
    store.close()
    store = Store(path)
    try:
        with store.read() as conn:
            assert conn.execute("SELECT details_json FROM maintenance_items WHERE run_id=?", (run,)).fetchone()[0] == '{}'
    finally:
        store.close()


def test_profile_calls_keep_the_original_running_attempt_across_entries(store):
    mid = put(store, PROSE)
    with store.write() as conn:
        run = run_row(conn)
        calls = {}
        attempts = {}
        for entry in ('A', 'B'):
            conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES(?,?,'test','group')", (entry, entry))
            attempt = insert(conn, 'entry_profile_attempts', entry_id=entry, run_id=run, base_version=0,
                state='running', local_day='2026-10-12', started_at=now(),
                material_json=dumps({'memory_id': mid, 'revision': 1}) if entry == 'A' else '{}')
            attempts[entry] = attempt
            calls[entry] = insert(conn, 'consolidation_calls', run_id=run,
                purpose='consolidation_entry_profile_generate', created_at=now(), raw_output=PROSE if entry == 'A' else '入口 B 独立输出')
            conn.execute("UPDATE entry_profile_attempts SET state='published',finished_at=? WHERE id=?", (now(), attempt))
        # Finishing/redacting an older call must not rebind it to the newer entry.
        conn.execute('UPDATE consolidation_calls SET duration_ms=10 WHERE id=?', (calls['A'],))
    purge_memory(store, mid, 1, confirm=True)
    assert record(store, 'consolidation_calls', calls['A'])['raw_output'] is None
    assert record(store, 'consolidation_calls', calls['B'])['raw_output'] == '入口 B 独立输出'
    with store.write() as conn:
        conn.execute('UPDATE consolidation_calls SET raw_output=? WHERE id=?', (PROSE, calls['A']))
        conn.execute('UPDATE consolidation_calls SET duration_ms=20 WHERE id=?', (calls['B'],))
    assert record(store, 'consolidation_calls', calls['A'])['raw_output'] is None
    assert record(store, 'consolidation_calls', calls['B'])['raw_output'] == '入口 B 独立输出'


def test_bulk_purge_row_budget_splits_fanout_and_revision_history(store, monkeypatch):
    ids = [put(store, '关联行数预算机制夹具') for _ in range(4)]
    with store.write() as conn:
        for mid in ids:
            for _ in range(24):
                insert(conn, 'admin_operations', action='memory_edit', actor='admin', object_type='memory', object_id=str(mid),
                       details_json=dumps({'content': PROSE}), created_at=now())
                insert(conn, 'memory_revisions', memory_id=mid, revision_before=1, revision_after=2,
                       before_json=dumps({'content': PROSE}), after_json='{}', reason='test', actor='admin', created_at=now())
    chunks = []
    original = memory_ops._purge_memories
    def observe(conn, expected, **kwargs):
        chunks.append(list(expected))
        return original(conn, expected, **kwargs)
    monkeypatch.setattr(memory_ops, '_purge_memories', observe)
    monkeypatch.setattr(memory_ops, 'PURGE_ROW_BUDGET', 80, raising=False)
    preview = admin_data.bulk_memory_preview(store, subject_id='self', action='purge')
    result = memory_ops.apply_bulk_memories(store, snapshot_token=preview['snapshot_token'], action='purge', confirm=True)
    assert result['memory_ids'] == ids
    assert chunks == [[mid] for mid in ids]
    with store.read() as conn:
        plan = conn.execute('EXPLAIN QUERY PLAN DELETE FROM memory_revisions WHERE memory_id=?', (ids[0],)).fetchall()
        assert any('SEARCH' in r[3] and 'memory_revisions_by_memory' in r[3] for r in plan)
        assert conn.execute('SELECT COUNT(*) FROM memory_revisions').fetchone()[0] == 0


def test_unbased_current_persona_is_reported_even_without_any_memory_link(store):
    setup_role(store, 'Iris')
    text = '管理员手写的句子仍需人工核对。'
    persona.admin_edit(store, text, expected_version=persona.current_persona(store)['id'])
    mid = put(store, PROSE)
    impact = admin_data.memory_detail(store, mid)['purge_preview']['persona']
    assert impact == {'versions': 0, 'sentences': 0, 'retained_unbased_sentences': 1}
    purge_memory(store, mid, 1, confirm=True)
    assert persona.current_persona(store)['content'] == text


def test_persona_report_never_treats_object_id_fallback_as_attempt_id(store):
    from iris.consolidation import _persona_result
    setup_role(store, 'Iris', PROSE)
    with store.write() as conn:
        run = run_row(conn)
        attempt = insert(conn, 'persona_attempts', source='periodic', base_version_id=1,
                         state='running', material_json='{}', created_at=now())
        assert run == attempt  # These are unrelated integer namespaces.
        conn.execute("INSERT INTO maintenance_persona(run_id,status) VALUES(?,'running')", (run,))
    details = {'content': '未引用这个版本或尝试的独立记录'}
    _persona_result(store, run, 'published', None, details, lambda: datetime.now(timezone.utc))
    purge_memory(store, 1, 1, confirm=True)
    with store.read() as conn:
        assert json.loads(conn.execute('SELECT details_json FROM maintenance_items WHERE run_id=?', (run,)).fetchone()[0])['content'] == details['content']


def test_upgrade_profile_calls_bind_only_an_unambiguous_attempt_interval(tmp_path, monkeypatch):
    path = tmp_path / 'profile-calls-upgrade.db'
    store = before_purge_store(path, monkeypatch)
    mid = put(store, PROSE)
    with store.write() as conn:
        run = run_row(conn)
        calls = {}
        for entry, start, finish in [('A', '2026-10-12T00:00:00+00:00', '2026-10-12T00:01:00+00:00'),
                                      ('B', '2026-10-12T00:02:00+00:00', '2026-10-12T00:03:00+00:00')]:
            conn.execute("INSERT INTO entries(id,name,platform,kind) VALUES(?,?,'test','group')", (entry, entry))
            insert(conn, 'entry_profile_attempts', entry_id=entry, run_id=run, base_version=0,
                   state='published', local_day='2026-10-12', started_at=start, finished_at=finish,
                   material_json=dumps({'memory_id': mid, 'revision': 1}) if entry == 'A' else '{}')
            calls[entry] = insert(conn, 'consolidation_calls', run_id=run,
                purpose='consolidation_entry_profile_generate', created_at=start,
                raw_output=PROSE if entry == 'A' else '入口 B 独立历史输出')
        conn.execute("UPDATE memories SET content='',purged_at=?,lifecycle='deleted',revision=2 WHERE id=?", (now(), mid))
    store.close()
    store = Store(path)
    try:
        assert record(store, 'consolidation_calls', calls['A'])['raw_output'] is None
        assert record(store, 'consolidation_calls', calls['B'])['raw_output'] == '入口 B 独立历史输出'
        with store.write() as conn:
            conn.execute('UPDATE consolidation_calls SET duration_ms=1 WHERE id=?', (calls['B'],))
        assert record(store, 'consolidation_calls', calls['B'])['raw_output'] == '入口 B 独立历史输出'
    finally:
        store.close()


def test_real_profile_run_preserves_second_entry_calls_after_first_entry_purge(store):
    from iris.consolidation import Consolidation, _PersonaGateway
    from iris.maintenance import Maintenance
    from test_entry_profile import Clock, Model, memory, messages
    clock = Clock()
    a = messages(store, clock, entry='A')
    b = messages(store, clock, entry='B')
    mid = memory(store, clock, a[:1], text=PROSE)
    memory(store, clock, b[:1], text='入口 B 的独立记忆')
    model = Model(store)
    run = Maintenance(store, gateway=model, clock=clock).request()
    owner = Consolidation(store, model, clock=clock)
    owner.run_id = run
    assert entry_profile.update_profiles_for_run(store, _PersonaGateway(owner), {'id': run},
                                                 {'entry_profile_enabled': True, 'max_calls': 50}, clock=clock)
    with store.read() as conn:
        calls = list(conn.execute('SELECT * FROM consolidation_calls WHERE run_id=? ORDER BY id', (run,)))
        assert len(calls) == 4 and all(c['raw_output'] for c in calls)
    purge_memory(store, mid, 1, confirm=True)
    assert all(record(store, 'consolidation_calls', c['id'])['raw_output'] is None for c in calls[:2])
    assert all(record(store, 'consolidation_calls', c['id'])['raw_output'] == c['raw_output'] for c in calls[2:])


# These regressions exercise the retained legacy projection/purge machinery.
from profile_legacy import legacy_schema
