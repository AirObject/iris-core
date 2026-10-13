"""Privacy regression: linked prose, source cleanup and late model writeback."""
import json

import pytest

from conftest import FakeGateway, batch, msg
from iris import admin_data
from iris.db import dumps, now
from iris.learning import LearningEngine, PROMPT_VERSION
from iris.memory_ops import apply_bulk_memories, purge_memory
from iris.queue import form_batch
from iris.retrieval import Retrieval
from test_batches import memory
from test_retrieval import put

PROSE = "待清除的私密原文"


def insert(conn, table, **values):
    return conn.execute(f"INSERT INTO {table}({','.join(values)}) VALUES({','.join('?' for _ in values)})",
                        list(values.values())).lastrowid


def learned(store):
    source = msg(store, 1, PROSE)
    formed, result = batch(store, FakeGateway({'memories': [memory(PROSE)]}), count=1)
    return source, formed.id, result['created'][0]


def record(store, table, key):
    with store.read() as conn:
        return dict(conn.execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone())


def test_single_clears_raw_repair_batch_diagnostics_and_calls_but_keeps_metadata(store):
    source, bid, mid = learned(store)
    with store.write() as conn:
        conn.execute('UPDATE batch_attempts SET repair_output=?,error=? WHERE batch_id=?', (PROSE, PROSE, bid))
        conn.execute('UPDATE batches SET result_json=? WHERE id=?',
                     (dumps({'created': [mid], 'dropped': [{'item': {'content': PROSE}}],
                             'normalizations': [{'before': PROSE}]}), bid))
        call = insert(conn, 'model_calls', purpose='learning', model='fake', duration_ms=17,
                      prompt_tokens=12, completion_tokens=8, result_category='success',
                      error_summary=PROSE, created_at=now(), batch_id=bid)
    result = purge_memory(store, mid, 1, confirm=True)
    assert result['deleted_message_ids'] == [source]
    attempt = record(store, 'batch_attempts', 1)
    assert attempt['raw_output'] is None and attempt['repair_output'] is None and attempt['error'] is None
    assert attempt['parse_status'] == 'direct'
    assert PROSE not in record(store, 'batches', bid)['result_json']
    assert json.loads(record(store, 'batches', bid)['result_json'])['created'] == [mid]
    saved = record(store, 'model_calls', call)
    assert saved['error_summary'] is None
    assert (saved['duration_ms'], saved['prompt_tokens'], saved['completion_tokens']) == (17, 12, 8)
    with store.read() as conn:
        assert conn.execute('SELECT 1 FROM purge_redactions WHERE table_name=? AND row_id=?',
                            ('batch_attempts', str(attempt['id']))).fetchone()
        audit = conn.execute("SELECT details_json FROM admin_operations WHERE action='memory_purge'").fetchone()[0]
        assert PROSE not in audit and str(mid) in audit
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    assert result['redaction_counts']['batch_attempts'] == 1


def seed_derivatives(store, mid, source):
    """Handwritten examples of actual persistence shapes, with an unrelated control."""
    with store.write() as conn:
        run = insert(conn, 'maintenance_runs', trigger='manual', settings_json='{}', timezone='UTC',
                     memory_through=mid, message_through=source, batch_through=1, created_at=now())
        payload = {'memories': [{'id': mid, 'content': PROSE, 'sources': [{'id': source, 'content': PROSE}]}],
                   'pair_ids': [mid], 'resolution': 'report_only_v1'}
        work = insert(conn, 'consolidation_work', fingerprint='privacy-fixture', kind='pair',
                      payload_json=dumps(payload), importance=60, changed_at=now(), created_at=now(),
                      result_json=dumps({'reason': PROSE, 'decision': 'conflict'}))
        insert(conn, 'consolidation_calls', run_id=run, work_id=work, purpose='consolidation_pair',
               created_at=now(), raw_output=PROSE, error=PROSE)
        insert(conn, 'consolidation_run_work', run_id=run, work_id=work, details_json=dumps({'before_memories': payload['memories'], 'report': PROSE}))
        insert(conn, 'maintenance_items', run_id=run, phase='consolidation', item_key=str(work),
               memory_id=mid, object_id=mid, outcome='conflicts', details_json=dumps({'report': PROSE}), created_at=now())
        insert(conn, 'consolidation_annotations', memory_id=mid, work_id=work, text=PROSE,
               evidence_json=dumps([source]), related_ids_json=dumps([mid]), created_at=now())
        evidence = {'evidence': {'memories': [{'memory_id': mid, 'content': PROSE}],
                                 'messages': [{'message_id': source, 'content': PROSE}]}}
        sentences = [{'text': PROSE, 'basis': [{'memory_id': mid, 'revision': 1}]}]
        persona = insert(conn, 'persona_versions', content=PROSE, created_at=now(), is_current=1,
                         status='current', sentences_json=dumps(sentences), material_json=dumps(evidence))
        insert(conn, 'persona_attempts', source='regenerate', base_version_id=persona,
               state='running', material_json=dumps(evidence), outputs_json=dumps({'persona_generate': PROSE}), created_at=now())
        profile = insert(conn, 'entry_profile_versions', entry_id='A', version=1, base_version=0,
                         content=PROSE, sentences_json=dumps(sentences), checks_json=dumps({'reason': PROSE}),
                         change_degree='small', status='candidate', generated_at=now(), author='model',
                         source='regenerate', message_through=source, material_json=dumps(evidence), settings_revision=1)
        insert(conn, 'entry_profile_attempts', entry_id='A', base_version=0, state='running', local_day='2026-10-11',
               started_at=now(), version=1, material_json=dumps(evidence), outputs_json=dumps({'generate': PROSE}))
        conn.execute('INSERT INTO recalls VALUES(?,?,?,?)', ('affected', 'A', dumps({'text': PROSE, 'limit': 8}), now()))
        conn.execute('INSERT INTO recall_items(recall_id,memory_id,revision) VALUES(?,?,1)', ('affected', mid))
        conn.execute('INSERT INTO recalls VALUES(?,?,?,?)', ('unrelated', 'A', dumps({'text': '独立查询'}), now()))
        insert(conn, 'admin_operations', actor='admin', action='legacy', object_type='memory', object_id=str(mid),
               details_json=dumps({'memory_id': mid, 'content': PROSE}), created_at=now())
    return work, persona, profile


def test_clears_linked_snapshots_candidates_and_request_without_erasing_unrelated_query(store):
    source, _, mid = learned(store)
    work, persona, profile = seed_derivatives(store, mid, source)
    purge_memory(store, mid, 1, confirm=True)
    with store.read() as conn:
        for table in ('consolidation_work', 'consolidation_calls', 'consolidation_run_work', 'maintenance_items',
                      'consolidation_annotations', 'persona_versions', 'persona_attempts',
                      'entry_profile_versions', 'entry_profile_attempts', 'recalls', 'admin_operations'):
            assert PROSE not in dumps([dict(r) for r in conn.execute(f'SELECT * FROM {table}')]), table
        assert json.loads(conn.execute("SELECT request_json FROM recalls WHERE id='unrelated'").fetchone()[0]) == {'text': '独立查询'}
        assert conn.execute("SELECT COUNT(*) FROM recall_items WHERE memory_id=?", (mid,)).fetchone()[0] == 1
    assert record(store, 'persona_versions', persona)['content'] == ''
    assert record(store, 'entry_profile_versions', profile)['content'] == ''
    # Model results arriving after the purge must not repopulate retained records.
    with store.write() as conn:
        conn.execute('UPDATE persona_attempts SET outputs_json=?', (dumps({'generate': PROSE}),))
        conn.execute('UPDATE entry_profile_attempts SET outputs_json=?', (dumps({'generate': PROSE}),))
        conn.execute('UPDATE consolidation_calls SET raw_output=?', (PROSE,))
        conn.execute('UPDATE consolidation_work SET result_json=? WHERE id=?', (dumps({'reason': PROSE}), work))
    with store.read() as conn:
        for table in ('persona_attempts', 'entry_profile_attempts', 'consolidation_calls', 'consolidation_work'):
            assert PROSE not in dumps([dict(r) for r in conn.execute(f'SELECT * FROM {table}')])


def test_bulk_uses_same_cleanup_shared_message_and_current_backup_notice(store):
    source, bid, first = learned(store)
    second = put(store, PROSE, evidence=[source], entry='A')
    store.set_setting('scheduled_backup', {'enabled': True, 'keep': 3})
    preview = admin_data.bulk_memory_preview(store, entry_id='A', action='purge')
    assert preview['backup_notice']['scheduled_backup_keep'] == 3
    assert '最多保留 3 份' in preview['backup_notice']['message']
    result = apply_bulk_memories(store, snapshot_token=preview['snapshot_token'], action='purge', confirm=True)
    assert result['memory_ids'] == [first, second]
    assert result['deleted_message_ids'] == [source] and result['retained_messages'] == []
    assert result['backup_notice']['scheduled_backup_keep'] == 3
    assert record(store, 'batch_attempts', 1)['raw_output'] is None
    assert apply_bulk_memories(store, snapshot_token=preview['snapshot_token'], action='purge', confirm=True) == result
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_operations WHERE action='memory_bulk_purge'").fetchone()[0] == 1


def test_source_only_snapshot_is_cleared_but_protected_message_stays(store):
    source, _, mid = learned(store)
    other = put(store, '共享来源的另一条记忆', evidence=[source])
    with store.write() as conn:
        insert(conn, 'persona_versions', content=PROSE, created_at=now(), is_current=1,
               material_json=dumps({'evidence': {'memories': [{'memory_id': other, 'excerpts': [{'message_id': source, 'excerpt': PROSE}]}]}}))
    first = purge_memory(store, mid, 1, confirm=True)
    assert first['deleted_message_ids'] == []
    # A still-protected source is not itself erased by purging one consumer.
    assert record(store, 'messages', source)['content'] == PROSE
    second = purge_memory(store, other, 1, confirm=True)
    assert second['deleted_message_ids'] == [source]
    assert record(store, 'persona_versions', 1)['content'] == ''


def test_inflight_learning_revision_conflict_and_linked_batch_redaction(store):
    source, _, mid = learned(store)
    msg(store, 2, PROSE + '需要修正')
    formed = form_batch(store, 'A', PROMPT_VERSION, target_count=1, history_count=2)
    output = {'updates': [{'ref': 'M1', 'action': '修正', 'content': PROSE + '修正版', 'evidence': [2], 'belief': 90}]}
    def purge_during_call(_):
        result = purge_memory(store, mid, 1, confirm=True)
        assert result['retained_messages'] == [{'message_id': source, 'reasons': ['batch_segment']}]
    result = LearningEngine(store, FakeGateway(output, hook=purge_during_call)).run_batch(formed.id, force=True)
    assert result['updated'] == []
    assert any(item['reason'] == 'memory revision changed' for item in result['dropped'])
    assert record(store, 'memories', mid)['revision'] == 2
    assert record(store, 'memories', mid)['content'] == ''
    with store.read() as conn:
        # This running batch has only a retained history message and transient
        # M1: the new scope explicitly does not taint every batch seeing it.
        attempts = conn.execute('SELECT batch_id,raw_output FROM batch_attempts ORDER BY id').fetchall()
        assert attempts[0]['raw_output'] is None
        assert attempts[-1]['batch_id'] == formed.id and attempts[-1]['raw_output'] is not None


def test_revision_mismatch_has_no_redaction_side_effects(store):
    _, _, mid = learned(store)
    before = record(store, 'batch_attempts', 1)
    assert purge_memory(store, mid, 99, confirm=True) is None
    assert record(store, 'batch_attempts', 1) == before


def test_late_inserts_use_typed_evidence_and_recall_judgment_ids(store):
    source, bid, mid = learned(store)
    purge_memory(store, mid, 1, confirm=True)
    with store.write() as conn:
        call = insert(conn, 'model_calls', purpose='learning_repair', model='fake', duration_ms=9,
                      result_category='invalid_output', error_summary=PROSE, created_at=now(), batch_id=bid)
        persona = insert(conn, 'persona_versions', content=PROSE, created_at=now(), is_current=0,
                         sentences_json=dumps([{'text': PROSE, 'basis': [{'memory_id': mid, 'revision': 1}]}]))
        profile = insert(conn, 'entry_profile_versions', entry_id='A', version=1, base_version=0,
                         content=PROSE, sentences_json='[]', checks_json='{}', change_degree='small',
                         status='candidate', generated_at=now(), author='model', source='regenerate',
                         message_through=source, settings_revision=1,
                         material_json=dumps({'evidence': {'messages': [{'message_id': source, 'content': PROSE}]}}))
        conn.execute('INSERT INTO recalls VALUES(?,?,?,?)', ('late', 'A',
                     dumps({'text': PROSE, 'judgment': {'removed_memory_ids': [mid], 'status': 'applied'}}), now()))
        conn.execute('INSERT INTO recalls VALUES(?,?,?,?)', ('late-item', 'A', dumps({'text': PROSE}), now()))
        conn.execute('INSERT INTO recall_items(recall_id,memory_id,revision) VALUES(?,?,1)', ('late-item', mid))
    assert record(store, 'model_calls', call)['error_summary'] is None
    assert record(store, 'persona_versions', persona)['content'] == ''
    assert record(store, 'entry_profile_versions', profile)['content'] == ''
    with store.read() as conn:
        for rid in ('late', 'late-item'):
            assert PROSE not in conn.execute('SELECT request_json FROM recalls WHERE id=?', (rid,)).fetchone()[0]


def test_failure_rolls_back_tombstone_output_cleanup_and_audit(store, monkeypatch):
    from iris import memory_ops
    _, _, mid = learned(store)
    before = record(store, 'batch_attempts', 1)
    original = memory_ops._purge_linked_outputs
    def fail(*args):
        original(*args)
        raise RuntimeError('interrupted privacy transaction')
    monkeypatch.setattr(memory_ops, '_purge_linked_outputs', fail)
    with pytest.raises(RuntimeError):
        purge_memory(store, mid, 1, confirm=True)
    assert record(store, 'memories', mid)['purged_at'] is None
    assert record(store, 'batch_attempts', 1) == before
    with store.read() as conn:
        assert conn.execute('SELECT COUNT(*) FROM purge_redactions').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM purge_message_tombstones').fetchone()[0] == 0


def test_initial_setting_clears_linked_persona_background_and_preserves_other_memory(store):
    from iris.memory_ops import setup_role
    setup_role(store, 'Iris', PROSE + '。另一个初始事实。')
    with store.read() as conn:
        mid = conn.execute('SELECT id FROM memories ORDER BY id').fetchone()[0]
    purge_memory(store, mid, 1, confirm=True)
    assert store.setting('background') == ''
    assert record(store, 'persona_versions', 1)['content'] == ''
    assert record(store, 'memories', mid + 1)['content'] == '另一个初始事实'


def test_upgrade_scrubs_identifiable_previously_purged_batch(tmp_path, monkeypatch):
    from iris.db import Store
    path = tmp_path / 'upgrade.db'
    from importlib.resources import files
    from types import SimpleNamespace
    root = files('iris')
    class BeforePurge:
        def joinpath(self, name):
            if name == 'migrations':
                return SimpleNamespace(iterdir=lambda: [p for p in root.joinpath(name).iterdir()
                    if p.name < '029'])
            return root.joinpath(name)
    with monkeypatch.context() as patch:
        patch.setattr('iris.db.files', lambda package: BeforePurge())
        old = Store(path)
    # Freeze the pre-029 storage shape; today's learning path requires RS columns.
    source = msg(old, 1, PROSE)
    mid = put(old, PROSE, evidence=[source])
    with old.write() as conn:
        bid = insert(conn, 'batches', entry_id='A', history_ids='[]', target_ids=dumps([source]),
                     future_ids='[]', state='succeeded', prompt_version='historic', created_at=now(),
                     result_json=dumps({'created': [mid]}))
        conn.execute("UPDATE messages SET batch_id=?,learning_state='learned' WHERE id=?", (bid, source))
        insert(conn, 'batch_attempts', batch_id=bid, number=1, started_at=now(), finished_at=now(),
               raw_output=PROSE, parse_status='direct', duration_ms=1)
        conn.execute("UPDATE memories SET content='',lifecycle='deleted',purged_at=?,revision=2 WHERE id=?", (now(), mid))
        conn.execute('DELETE FROM sources WHERE memory_id=?', (mid,))
        conn.execute('DELETE FROM messages WHERE id=?', (source,))
    old.close()
    upgraded = Store(path)
    try:
        assert record(upgraded, 'batch_attempts', 1)['raw_output'] is None
        assert json.loads(record(upgraded, 'batches', bid)['result_json'])['created'] == [mid]
        assert list(tmp_path.glob('upgrade.db.*.bak'))  # Upgrade backup is deliberately unchanged.
    finally:
        upgraded.close()


def test_deleted_record_id_reuse_does_not_clear_unrelated_output(store):
    _, bid, mid = learned(store)
    purge_memory(store, mid, 1, confirm=True)
    with store.write() as conn:
        conn.execute('DELETE FROM batch_attempts WHERE id=1')
        other_batch = insert(conn, 'batches', entry_id='A', history_ids='[]', target_ids='[]', future_ids='[]',
                             state='succeeded', prompt_version=PROMPT_VERSION, created_at=now())
        insert(conn, 'batch_attempts', id=1, batch_id=other_batch, number=1, started_at=now(), finished_at=now(),
               raw_output='无关原始输出', parse_status='direct', duration_ms=1)
    assert record(store, 'batch_attempts', 1)['raw_output'] == '无关原始输出'


def test_new_persona_after_prior_purge_survives_unrelated_purge(store):
    from iris.memory_ops import setup_role
    setup_role(store, 'Iris', PROSE)
    purge_memory(store, 1, 1, confirm=True)
    with store.write() as conn:
        independent = insert(conn, 'persona_versions', content='后来的独立版本', created_at=now(), is_current=0,
               base_version_id=1, sentences_json='[]', material_json='{}')
    other = put(store, '无关记忆')
    purge_memory(store, other, 1, confirm=True)
    assert record(store, 'persona_versions', independent)['content'] == '后来的独立版本'


def test_descendant_redaction_keeps_numeric_evidence_metadata(store):
    _, _, mid = learned(store)
    other = put(store, '另一条独立记忆')
    with store.write() as conn:
        parent = insert(conn, 'persona_versions', content=PROSE, created_at=now(), is_current=0,
                        sentences_json=dumps([{'text': PROSE, 'basis': [{'memory_id': mid, 'revision': 1}]}]))
        child = insert(conn, 'persona_versions', content=PROSE, created_at=now(), is_current=0, source='regenerate', base_version_id=parent,
                       material_json=dumps({'memory_id': other, 'revision': 3, 'content': PROSE}))
    purge_memory(store, mid, 1, confirm=True)
    assert record(store, 'persona_versions', child)['content'] == ''
    with store.read() as conn:
        metadata = json.loads(conn.execute("SELECT metadata_json FROM purge_redactions WHERE table_name='persona_versions' AND row_id=?", (str(child),)).fetchone()[0])
    assert {'field': 'memory_id', 'value': other} in metadata['references']
    assert {'field': 'revision', 'value': 3} in metadata['references']
    assert PROSE not in dumps(metadata)
