"""Original utterances, not competing summaries, reach persona model requests."""
import copy

import pytest

from iris.memory_ops import edit_memory
from iris.persona import (
    PersonaEngine, _check_payload, _generation_material, _settings, current_persona,
)
from test_persona import FakeGateway, add_self, store


ORIGINAL = '那次配音里，我请求再试一次。'
SUMMARY = '那次配音里，我重录了整段对白。'


def prepare(store):
    mid = add_self(store, ORIGINAL)
    edit_memory(store, mid, 1, content=SUMMARY)
    previous = current_persona(store)
    with store.read() as conn:
        material = _generation_material(conn, _settings(conn), previous, '2026-10-12T12:00:00+00:00')
    memory = next(m for m in material['evidence']['memories'] if m['memory_id'] == mid)
    sentence = {'text': ORIGINAL, 'origin': 'memory', 'initial_setting': False,
        'basis': [{'memory_id': mid, 'revision': 2}], 'dates': memory['dates'], 'date_count': 1}
    return previous, material, memory, sentence


def test_generation_and_check_use_original_without_competing_summary(store):
    previous, _, memory, _ = prepare(store)
    model = FakeGateway(store, {'sentences': [{'text': ORIGINAL, 'basis': [memory['ref']]}]})
    result = PersonaEngine(store, model).regenerate(expected_version=previous['id'])
    assert result['status'] == 'current'
    requests = {purpose: payload for purpose,payload in model.calls
                if payload.get('task') != 'degree_only'}
    generated = next(m for m in requests['persona_generate']['evidence']['memories']
                     if m['memory_id'] == memory['memory_id'])
    checked = requests['persona_check']['evidence_by_ref'][memory['ref']]
    for item in (generated, checked):
        assert 'content' not in item
        assert item['current_revision_text'] == SUMMARY
        assert item['excerpts'][0]['excerpt'] == ORIGINAL
        assert item['excerpts'][0]['speaker_subject_id'] == 'self'
        assert item['event_time'] == memory['event_time']
        assert 'trace_refs' not in item and 'trace_sha256' not in item
    # Audit and revision/source CAS retain the full original snapshot.
    saved = next(m for m in result['version']['material']['evidence']['memories']
                 if m['memory_id'] == memory['memory_id'])
    assert saved['content'] == SUMMARY and saved['revision'] == 2 and saved['trace_sha256']


@pytest.mark.parametrize('proof_choices', [True, False])
def test_check_and_repair_evidence_projection_does_not_mutate_snapshot(store, proof_choices):
    _, material, memory, sentence = prepare(store)
    before = copy.deepcopy(material)
    payload = _check_payload(material, [sentence], full_content=sentence['text'], proof_choices=proof_choices)
    item = payload['evidence_by_ref'][memory['ref']]
    assert 'content' not in item and 'content_spans' not in item
    assert item['current_revision_text'] == SUMMARY
    assert item['excerpts'][0]['excerpt'] == ORIGINAL
    assert material == before


def test_memory_without_utterances_retains_its_own_text_as_fallback(store):
    previous, _, memory, _ = prepare(store)
    with store.write() as conn:
        conn.execute('DELETE FROM sources WHERE memory_id=?', (memory['memory_id'],))
    model = FakeGateway(store, {'sentences': [{'text': SUMMARY, 'basis': [memory['ref']]}]})
    result = PersonaEngine(store, model).regenerate(expected_version=previous['id'])
    assert result['status'] == 'current'
    requests = {purpose: payload for purpose,payload in model.calls
                if payload.get('task') != 'degree_only'}
    generated = next(m for m in requests['persona_generate']['evidence']['memories']
                     if m['memory_id'] == memory['memory_id'])
    checked = requests['persona_check']['evidence_by_ref'][memory['ref']]
    assert generated['content'] == checked['content'] == SUMMARY
    assert not generated['excerpts'] and not checked['excerpts']
    assert checked['content_spans']


def test_first_revision_also_exposes_current_scope_constraint(store):
    mid = add_self(store, ORIGINAL)
    previous = current_persona(store)
    with store.read() as conn:
        material = _generation_material(conn, _settings(conn), previous, '2026-10-13T12:00:00+00:00')
    memory = next(m for m in material['evidence']['memories'] if m['memory_id'] == mid)
    assert memory['revision'] == 1
    sentence = {'text': ORIGINAL, 'origin': 'memory', 'initial_setting': False,
        'basis': [{'memory_id': mid, 'revision': 1}], 'dates': memory['dates'], 'date_count': 1}
    payload = _check_payload(material, [sentence], full_content=ORIGINAL)
    visible = payload['evidence_by_ref'][memory['ref']]
    assert visible['current_revision_text'] == memory['content']
    assert 'content' not in visible and 'content_spans' not in visible
