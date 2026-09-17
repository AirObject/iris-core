"""Synthetic formal learning and manual memory transactions on the same host."""
import json
import time
from types import MappingProxyType
from companion_memory.memory.formats import record, sequence, isolate_object, isolate_links
from companion_memory.persistence import Committed, NotCommitted
from companion_memory.persistence.content_codec import decode_content
from tests.runtime.configuration_support import event


def formal_response(request):
    source = json.loads(request['messages'][1]['content'])['source']
    target = next(member for member in source['ordered_members'] if member['role'] == 'T')
    return {'schema_version': 1, 'kind': 'FINAL', 'actions': [{'action': 'CREATE_MEMORY', 'local_ref': 0,
        'target_anchors': [{'message_id': target['message_id'], 'part': 'EVENT', 'item_index': None,
            'start_utf8': None, 'end_utf8': None, 'occurrence_id': None, 'interpretation_id': None}],
        'auxiliary_refs': [], 'basis_refs': [], 'category': 'FACT', 'body': '合成测试的杯子在桌面。',
        'subject_ids': [{'existing_id': 'self'}], 'speaker_subject_id': None, 'stance': 'ASSERTED',
        'world_scope': {'kind': 'REAL', 'context_id': None}, 'occurred_range': None, 'applicable_range': None,
        'belief': 80, 'belief_reason': '合成目标事件直接陈述。'}]}


def shared_formal_response(request):
    """Two distinct formal objects retain the same actual learned source."""
    value = formal_response(request)
    value['actions'].append(dict(value['actions'][0]) | {'local_ref': 1, 'body': '合成桌面上放着一个杯子。'})
    return value


async def exercise_memory(test, business, dispatch_revision: int = 1, dispatch_key: str = 'resume-after-restart', learning_key: str = 'resume-learning'):
    host = business.host
    assert host is not None and host.runtime is not None and host.assembly.memory.information is not None
    entry = host.bind_entry('entry')
    from companion_memory.ingress.events import event_identity
    from companion_memory.ingress.media_events import isolate_media_event
    from companion_memory.media.service import identity
    upload = host.media.bind_upload('entry')
    media_ids = []
    for index in range(2):
        value = event('formal-input-' + str(index), '合成测试的杯子在桌面。')
        value['event_version'] = 2
        begun = await upload.begin_upload('shared-media-' + str(index), 'IMAGE')
        assert type(begun) is Committed
        uid = record(begun.receipt.result)['upload_id']
        await upload.append_upload(uid, 0, b'synthetic shared media retained by multiple events')
        published = await upload.finish_upload(uid)
        assert type(published) is Committed
        media_ids.append(record(published.receipt.result)['blob_id'])
        mid = event_identity((host.resources.instance_id, 'host', 'entry'),
            isolate_media_event(value, 2048, occurrence_limit=2, text_limit=512))[0]
        value['media'] = [{'reference_id': uid, 'occurrence_id': identity('occurrence', mid, 0), 'modality': 'IMAGE',
            'interpretation': {'status': 'COMPLETE', 'text': '合成外部描述。', 'coverage': 'COMPLETE', 'source_ref': 'synthetic-' + str(index)}}]
        accepted = await entry.accept_event('formal-input-' + str(index), value)
        test.assertIs(type(accepted), Committed, accepted)
    test.assertEqual(media_ids[0], media_ids[1])
    permission = await business.set_dispatch(dispatch_key, dispatch_revision, True, business.dispatch_disclosure()['digest'])
    test.assertIs(type(permission), Committed, permission)
    test.assertIs(type(await host.resume_learning(learning_key)), Committed)
    learned = await entry.run_learning('formal-learning-' + learning_key)
    test.assertIs(type(learned), Committed, learned)
    objects = await host.assembly.memory.information.current_page('', 4)
    test.assertEqual(len(objects), 2, objects)
    current = objects[0]
    shared = objects[1]
    oid = current['object_id']
    links_row = (await host.assembly.memory.rows.read('links_get', {'object_id': oid}))[0]
    links = isolate_links(decode_content(links_row['body'].encode(), 2048), oid, current['revision'])
    now = time.time_ns() // 1000
    old_revision = current['revision']
    proposed = isolate_object(dict(current) | {'revision': old_revision + 1, 'modified_at_us': now,
        'scores': dict(record(current['scores'])) | {'retention': 0}, 'lifecycle': 'FORGOTTEN', 'forgotten_since_us': now}, text_format=True)
    proposed_links = isolate_links({'sources': tuple(dict(record(link)) | {'object_revision': old_revision + 1} for link in sequence(links['sources'])),
        'bases': tuple(dict(record(link)) | {'dependent_revision': old_revision + 1} for link in sequence(links['bases']))}, oid, old_revision + 1)
    port = host.runtime.maintenance.bind((oid,))
    forgotten = await port.set_scores('synthetic-forgetting', {'change_version': 1, 'action': 'SET_SCORES', 'target_id': oid,
        'expected_revision': old_revision, 'proposed_value': proposed, 'links': proposed_links})
    test.assertIs(type(forgotten), Committed, forgotten)
    owner = host.combination.memory_administration
    assert owner is not None
    async def preview(action, revision, key):
        value = {'action': action, 'object_id': oid, 'expected_revision': revision}
        result = await owner.execute(key, value, preview=True)
        test.assertIs(type(result), Committed, result)
        assert type(result) is Committed
        summary = record(result.receipt.result)
        impact = record(summary['impact'])
        return value | {'confirmation_id': summary['confirmation_id'], 'impact_digest': impact['impact_digest']}, impact['release_mask']
    restore, mask = await preview('RESTORE', old_revision + 1, 'preview-restore')
    restored = await owner.execute('restore', restore, preview=False, mask=mask)
    test.assertIs(type(restored), Committed, restored)
    repeated = await owner.execute('restore', restore, preview=False, mask=mask)
    test.assertIs(type(repeated), Committed, repeated)
    assert type(restored) is Committed and type(repeated) is Committed
    test.assertEqual(restored.receipt, repeated.receipt)
    invalid = await owner.execute('reuse-consumed', restore, preview=False, mask=mask)
    test.assertIs(type(invalid), NotCommitted, invalid)
    deletion, mask = await preview('DELETE', old_revision + 2, 'preview-before-shared-release')
    test.assertEqual(mask, 'none')
    shared_input = {'action': 'DELETE', 'object_id': shared['object_id'], 'expected_revision': shared['revision']}
    shared_preview = await owner.execute('preview-shared', shared_input, preview=True)
    assert type(shared_preview) is Committed
    shared_summary = record(shared_preview.receipt.result)
    shared_impact = record(shared_summary['impact'])
    test.assertEqual(shared_impact['released_source_count'], 0)
    test.assertEqual(shared_impact['release_mask'], 'none')
    shared_delete = await owner.execute('delete-shared', shared_input | {
        'confirmation_id': shared_summary['confirmation_id'], 'impact_digest': shared_impact['impact_digest']},
        preview=False, mask='none')
    test.assertIs(type(shared_delete), Committed, shared_delete)
    # The target revision has not changed, but its shared source holder did.
    raced = await owner.execute('delete-stale-impact', deletion, preview=False, mask=mask)
    test.assertIs(type(raced), NotCommitted, raced)
    test.assertEqual(len(await host.assembly.memory.information.current_page('', 4)), 1)
    deletion, mask = await preview('DELETE', old_revision + 2, 'preview-delete')
    test.assertEqual(mask, 'ingress_media')
    clock = host.combination.content.utc_now_us
    try:
        host.combination.content.utc_now_us = lambda: clock() + 301000000
        expired = await owner.execute('delete-expired-confirmation', deletion, preview=False, mask=mask)
        test.assertIs(type(expired), NotCommitted, expired)
    finally:
        host.combination.content.utc_now_us = clock
    deletion, mask = await preview('DELETE', old_revision + 2, 'preview-delete-current')
    deleted = await owner.execute('delete', deletion, preview=False, mask=mask)
    test.assertIs(type(deleted), Committed, deleted)
    test.assertEqual(await host.assembly.memory.information.current_page('', 4), ())
    repeated = await owner.execute('delete', deletion, preview=False, mask=mask)
    test.assertIs(type(repeated), Committed, repeated)
    assert type(deleted) is Committed and type(repeated) is Committed
    test.assertEqual(deleted.receipt, repeated.receipt)
    blob = (await host.media.rows.read('blobs_get', {'blob_id': media_ids[0]}))[0]
    test.assertEqual(blob['state'], 'READY')
    test.assertGreater(blob['reference_count'], 0)
    resources = business.bootstrap.resources
    assert resources is not None
    from contextlib import closing
    import sqlite3
    with closing(sqlite3.connect(resources.root / 'db/memory.sqlite3')) as connection:
        audits = {row[0] for row in connection.execute('SELECT event_slot FROM audit_records WHERE commit_id=?',
            (deleted.receipt.commit_id,))}
        test.assertEqual(audits, {'management_manual', 'memory_manual', 'ingress_manual', 'media_manual', 'object_history'})
        test.assertEqual(connection.execute("SELECT count(*) FROM media_references WHERE owner_kind='SOURCE'").fetchone()[0], 0)
        test.assertEqual(connection.execute("SELECT count(*) FROM media_interpretation_holders WHERE owner_kind='SOURCE'").fetchone()[0], 0)

    from .audit_history_support import exercise_history_audit
    return await exercise_history_audit(test, business, deleted, str(oid))
