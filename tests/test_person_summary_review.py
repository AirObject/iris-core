"""Regression probes for the PS1 review; synthetic mechanism fixtures only."""
from contextlib import contextmanager
from datetime import timedelta

import pytest

from iris import person_summary as ps
from iris.db import dumps
from iris.memory_ops import Visibility, confirm_retention, purge_memory
from iris.models import ModelError
from test_person_summary import clock, seed, memory, Model, generate
from test_person_summary_consolidation import prepare


def alias(store, clock, name='夜猫', entry='restricted'):
    with store.write() as conn:
        message = conn.execute("""INSERT INTO messages(entry_id,kind,sender_subject_id,content,
            occurred_at,received_at,dedupe_key) VALUES(?,'message','a','',?,?,?)""",
            (entry, clock().isoformat(), clock().isoformat(), 'alias-' + name)).lastrowid
        conn.execute('INSERT INTO subject_aliases(subject_id,alias,source_message_id) VALUES(?,?,?)',
                     ('a', name, message))
    return message


@pytest.mark.parametrize('entry', ['restricted', 'shared', None])
def test_alias_provenance_controls_relatedness(store, clock, entry):
    seed(store, clock)
    if entry:
        alias(store, clock, entry=entry)
    else:
        with store.write() as conn:
            conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES('a','夜猫')")
    mid = memory(store, clock, '夜猫最近在学日语', speaker='self', importance=100)
    material = ps.select_material(store, 'a', clock=clock)
    assert (mid in {m['memory_id'] for m in material['memories']}) == (entry != 'restricted')
    assert ('夜猫' in material['subject']['aliases']) == (entry != 'restricted')


def test_restricting_alias_source_invalidates_identity_at_read_and_write(store, clock):
    seed(store, clock); alias(store, clock, entry='shared')
    memory(store, clock, '夜猫最近在学日语', speaker='self', importance=100)
    generate(store, clock)
    with store.write() as conn:
        conn.execute("UPDATE entries SET visibility='entry_only' WHERE id='shared'")
    assert ps.current_summary(store, 'a', clock=clock)['possibly_stale']
    assert ps.current_summary(store, 'a', entry_id='shared', clock=clock) is None
    assert ps.current_summary(store, 'a', entry_id='restricted', clock=clock) is None
    assert ps.select_material(store, 'a', clock=clock)['memory_count'] == 8


def test_alias_change_during_generation_is_conflict(store, clock):
    seed(store, clock); alias(store, clock, entry='shared')
    memory(store, clock, '夜猫最近在学日语', speaker='self', importance=100)
    def restrict():
        with store.write() as conn:
            conn.execute("UPDATE entries SET visibility='entry_only' WHERE id='shared'")
    with pytest.raises(ps.PersonSummaryConflict):
        generate(store, clock, hook=restrict)


def test_confirmation_does_not_change_due_or_inflight_snapshot(store, clock):
    mids = seed(store, clock); generate(store, clock)
    clock.advance(days=15)
    confirm_retention(store, mids[0], current=clock())
    assert not ps.current_summary(store, 'a', clock=clock)['possibly_stale']
    assert ps.summary_due(store, 'a', clock=clock)['changed_memories'] == 0
    assert not ps.summary_due(store, 'a', clock=clock)['due']
    model = Model(store, hook=lambda: confirm_retention(store, mids[0], current=clock() + timedelta(seconds=1)))
    assert ps.PersonSummaryEngine(store, model, clock=clock).regenerate('a', expected_version=1)['status'] == 'published'


@pytest.mark.parametrize('lifecycle', ['deleted', 'forgotten', 'purged'])
def test_unavailable_basis_is_filtered_per_item_before_host_return(store, clock, lifecycle):
    mids = seed(store, clock)
    items = [{'kind':'fact','text':'第一条公开事项。','memory_refs':['M1']},
             {'kind':'fact','text':'第二条公开事项。','memory_refs':['M2']}]
    generate(store, clock, items=items)
    if lifecycle == 'purged':
        # Also cover reads before the future CF hook has cleared entire fields.
        purge_memory(store, mids[0], 1, confirm=True)
    else:
        with store.write() as conn:
            conn.execute('UPDATE memories SET lifecycle=? WHERE id=?', (lifecycle, mids[0]))
    value = ps.current_summary(store, 'a', entry_id='shared', clock=clock)
    assert '第一条' not in value['text'] and '第二条' in value['text']
    assert value['possibly_stale']
    with store.write() as conn:
        conn.execute("UPDATE memories SET lifecycle='forgotten' WHERE id=?", (mids[1],))
    assert ps.current_summary(store, 'a', entry_id='shared', clock=clock) is None


def test_default_recent_expiry_uses_event_time_filters_only_expired_and_triggers_due(store, clock):
    mids = seed(store, clock)
    with store.write() as conn:
        conn.execute('UPDATE memories SET event_time=?,created_at=? WHERE id=?',
                     ((clock()-timedelta(days=2)).isoformat(), (clock()-timedelta(days=60)).isoformat(), mids[0]))
    items = [{'kind':'recent','text':'正在筹备展示。','time_range':'本月','memory_refs':['M1']},
             {'kind':'fact','text':'仍然有效的事实。','memory_refs':['M2']}]
    result, _ = generate(store, clock, items=items)
    assert result['status'] == 'published'
    clock.advance(days=28)
    value = ps.current_summary(store, 'a', entry_id='shared', clock=clock)
    assert '筹备展示' not in value['text'] and '有效的事实' in value['text']
    assert value['possibly_stale'] and ps.summary_due(store, 'a', clock=clock)['due']


def test_default_recent_expiry_falls_back_to_creation_and_rejects_already_expired(store, clock):
    seed(store, clock)
    with store.write() as conn:
        conn.execute('UPDATE memories SET created_at=?', ((clock()-timedelta(days=31)).isoformat(),))
    result, _ = generate(store, clock, items=[{'kind':'recent','text':'旧的临时事项。',
        'time_range':'近期','memory_refs':['M1']}])
    assert result['status'] == 'rejected'


@pytest.mark.parametrize('failure', ['server', 'rejected'])
def test_failed_or_rejected_attempt_waits_for_related_changes(store, clock, failure):
    mids = seed(store, clock)
    class Broken(Model):
        def chat(self, *args, **kwargs):
            raise ModelError('server_error', 'fake service failure', reason='temporary_unavailable')
    broken = Broken(store) if failure == 'server' else Model(store, bad=[0])
    engine = ps.PersonSummaryEngine(store, broken, clock=clock)
    assert engine.regenerate('a', expected_version=0)['status'] in ('failed', 'rejected')
    assert ps.summary_due(store, 'a', clock=clock)['reason'] == 'no_changes_since_attempt'
    model = Model(store); engine = ps.PersonSummaryEngine(store, model, clock=clock)
    assert engine.regenerate('a', expected_version=0)['status'] == 'skipped'
    assert not model.calls
    with store.write() as conn:
        conn.execute('UPDATE memories SET revision=revision+1 WHERE id=?', (mids[0],))
    assert ps.summary_due(store, 'a', clock=clock)['due']
    assert engine.update('a', expected_version=0)['status'] == 'published'


def test_due_and_selection_never_run_inside_writer_and_stop_at_limit(store, monkeypatch):
    clock, model, gateway, run, config = prepare(store, limit=1)
    due = ps._due; select = ps._select; calls = []
    def inspect_due(conn, sid, *args, **kwargs):
        assert not store._writer.in_transaction
        calls.append(sid)
        return due(conn, sid, *args, **kwargs)
    def inspect_select(conn, *args, **kwargs):
        assert not store._writer.in_transaction
        return select(conn, *args, **kwargs)
    monkeypatch.setattr(ps, '_due', inspect_due); monkeypatch.setattr(ps, '_select', inspect_select)
    ps.update_summaries_for_run(store, gateway, run, config, clock=clock)
    assert set(calls) == {'a'}
    ps.admin_edit(store, 'a', '管理员记录。', expected_version=1, clock=clock)


def test_selection_and_host_read_use_bounded_ancestry_without_embedding(store, clock, monkeypatch):
    seed(store, clock); generate(store, clock)
    def forbid(*args, **kwargs):
        raise AssertionError('global visibility closure must not be built')
    monkeypatch.setattr(Visibility, '_load', forbid)
    statements = []; original = store.read
    @contextmanager
    def read():
        with original() as conn:
            conn.set_trace_callback(statements.append)
            yield conn
    monkeypatch.setattr(store, 'read', read)
    assert ps.select_material(store, 'a', clock=clock)['memory_count'] == 8
    assert ps.current_summary(store, 'a', entry_id='shared', clock=clock)
    assert not any('SELECT * FROM memories' in s or 'SELECT m.* FROM memories' in s for s in statements)
    assert not any('embedding' in s.lower() for s in statements)


def test_incremental_material_budget_stops_reading_prose_after_full(store, clock, monkeypatch):
    seed(store, clock, 30)
    store.set_setting('person_summary', {**store.setting('person_summary'), 'memory_tokens':200})
    original = ps.excerpt; seen = []
    def record(text, limit):
        seen.append(text)
        return original(text, limit)
    monkeypatch.setattr(ps, 'excerpt', record)
    material = ps.select_material(store, 'a', clock=clock)
    assert len(seen) <= len(material['memories']) + 1
    assert material['memory_tokens'] <= 200


def test_merge_without_timestamp_uses_nonnull_fallback(store, clock):
    seed(store, clock); generate(store, clock)
    with store.write() as conn:
        conn.execute("UPDATE subjects SET merged_into='b',merged_at=NULL WHERE id='a'")
    assert ps.summary_settings(store, 'a')['updated_at']


def test_administrator_rejection_also_waits_for_changed_evidence(store, clock):
    seed(store, clock)
    ps.set_settings(store, 'a', expected_version=0, publish_mode='all_manual', clock=clock)
    result, _ = generate(store, clock)
    ps.reject_candidate(store, 'a', result['version']['version'], expected_version=0, clock=clock)
    assert ps.summary_due(store, 'a', clock=clock)['reason'] == 'no_changes_since_attempt'


def test_run_limit_is_rechecked_at_admission_not_only_during_discovery(store):
    clock, model, gateway, run, config = prepare(store, limit=1)
    ps.update_summaries_for_run(store, gateway, run, config, clock=clock)
    with pytest.raises(ps.PersonSummaryConflict, match='run_limit'):
        ps.PersonSummaryEngine(store, gateway, clock=clock).update('b', expected_version=0, run_id=run['id'])
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM person_summary_attempts WHERE run_id=?', (run['id'],)).fetchone()[0] == 1


def test_index_candidates_keep_name_only_subjects_and_word_boundaries(store, clock):
    seed(store, clock, 0)
    with store.write() as conn:
        conn.execute("UPDATE subjects SET name='公开人物甲' WHERE id='a'")
        conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES('a','Ann')")
    named = [memory(store, clock, f'公开人物甲的第{i}条公开事实', speaker='self') for i in range(8)]
    alias_mid = memory(store, clock, 'Ann is learning woodworking.', speaker='self')
    unrelated = memory(store, clock, 'annual public report', speaker='self')
    assert ps.summary_due(store, 'a', clock=clock)['due']
    material = ps.select_material(store, 'a', clock=clock)
    assert material['memory_count'] == 9
    assert unrelated not in {m['memory_id'] for m in material['memories']}
    assert set(named[:2]) <= {m['memory_id'] for m in material['memories']}


def test_edit_cannot_restore_evidence_cleared_after_read_snapshot(store, clock, monkeypatch):
    mids = seed(store, clock); generate(store, clock)
    old = ps.current_summary(store, 'a', clock=clock)
    original = store.write; armed = True
    @contextmanager
    def write():
        nonlocal armed
        if armed:
            armed = False
            with original() as conn:
                purge_memory(store, mids[0], 1, confirm=True, _conn=conn)
                ps.purge_memory_references(conn, [mids[0]], clock=clock)
        with original() as conn:
            yield conn
    monkeypatch.setattr(store, 'write', write)
    with pytest.raises(ps.PersonSummaryConflict, match='evidence_changed'):
        ps.admin_edit(store, 'a', old['content'], expected_version=1, clock=clock)
    assert len(ps.list_versions(store, 'a', clock=clock)) == 1
    assert ps.current_summary(store, 'a', clock=clock)['content'] == ''
