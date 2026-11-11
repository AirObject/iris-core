"""The handwritten review sees later corrections, without widening its permission."""
import json
import time
from datetime import datetime, timezone

import pytest

import iris.persona as persona
from iris.db import Store, dumps
from iris.memory_ops import edit_memory, setup_role
from iris.persona import (HANDWRITTEN_REVIEW_MAX_PAGES, PersonaConflict, PersonaEngine, _generation_material,
                          _settings, admin_edit, confirm_candidate)
from test_persona import add_self
from test_persona_handwritten_revision import ReviewModel


WRITTEN = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
TEXT = '我不喜欢合奏。'
CORRECTION = '此前说不喜欢合奏并不准确，我喜欢合奏排练。'
LATER = '2026-10-02T12:00:00+00:00'
FILLER = ('这是一段用来占满生成预算的长记录，讲述我在山路边整理邮包、抄写地址、核对路牌、'
          '归还雨具与清点信封时反复做过的琐碎事情，内容彼此不同：第{}段。')


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / 'coverage.db')
    setup_role(result, 'Iris', '我来自云城。')
    yield result
    result.close()


def write(store, text=TEXT):
    return admin_edit(store, text, expected_version=1, clock=lambda: WRITTEN)


def fill_budget(store, count=70, stamp='2026-09-20T12:00:00+00:00'):
    for index in range(count):
        add_self(store, FILLER.format(index), stamp=stamp, importance=90)


def material_for(store, previous):
    with store.read() as conn:
        return _generation_material(conn, _settings(conn), previous, NOW.isoformat())


def test_over_budget_later_correction_reaches_the_review_and_licenses_deletion(store):
    before = write(store)
    fill_budget(store)
    mid = add_self(store, CORRECTION, stamp=LATER, importance=10)
    material = material_for(store, before)
    assert mid not in {m['memory_id'] for m in material['evidence']['memories']}
    assert [m['memory_id'] for m in material['handwritten_evidence']['memories']] == [mid]
    assert 'over_budget' not in material['evidence']
    model = ReviewModel(store, TEXT, 'explicit_correction')
    version = PersonaEngine(store, model, clock=lambda: NOW).regenerate(expected_version=before['id'])['version']
    assert TEXT not in version['content']
    change, = version['checks']['handwritten_changes']
    assert [b['memory_id'] for b in change['basis']] == [mid]
    assert version['checks']['handwritten_review']['over_budget_candidates'] == 1
    # The generator never receives memories outside its budget.
    generated = next(payload for purpose, payload in model.calls if purpose == 'persona_generate')
    assert mid not in {m['memory_id'] for m in generated['evidence']['memories']}


def test_over_budget_deletion_evidence_keeps_trace_protection(store):
    before = write(store)
    fill_budget(store)
    mid = add_self(store, CORRECTION, stamp=LATER, importance=10)
    result = PersonaEngine(store, ReviewModel(store, TEXT, 'explicit_correction'),
                           clock=lambda: NOW).regenerate(expected_version=before['id'])
    assert result['status'] == 'pending'
    assert mid in {m['memory_id'] for m in result['version']['material']['handwritten_evidence']['memories']}
    edit_memory(store, mid, 1, content='更正已撤回。')
    with pytest.raises(PersonaConflict):
        confirm_candidate(store, result['version']['id'], expected_version=before['id'], clock=lambda: NOW)


def test_over_budget_memories_must_be_later_and_share_a_term(store):
    before = write(store)
    fill_budget(store)
    earlier = add_self(store, '早先我也说过合奏的事。', stamp='2026-09-30T12:00:00+00:00', importance=10)
    unrelated = add_self(store, '后来我换了一只新的雨靴。', stamp=LATER, importance=10)
    related = add_self(store, CORRECTION, stamp=LATER, importance=10)
    kept = [m['memory_id'] for m in material_for(store, before)['handwritten_evidence']['memories']]
    assert earlier not in kept and unrelated not in kept and related in kept


def test_stored_over_budget_memories_are_bounded_and_keep_the_most_related(store, monkeypatch):
    monkeypatch.setattr(persona, 'HANDWRITTEN_MAX_OVER_BUDGET', 2)
    before = write(store)
    fill_budget(store)
    add_self(store, '合奏这件事我又想了想。', stamp=LATER, importance=10)
    newest = add_self(store, '合奏这件事我再想了想。', stamp='2026-10-03T12:00:00+00:00', importance=10)
    best = add_self(store, CORRECTION, stamp=LATER, importance=10)
    kept = material_for(store, before)['handwritten_evidence']['memories']
    assert [m['memory_id'] for m in kept] == [best, newest]
    assert len({m['ref'] for m in kept}) == 2


def test_no_handwritten_sentence_adds_no_review_evidence(store):
    with store.read() as conn:
        current = persona._decode(conn, conn.execute('SELECT * FROM persona_versions WHERE is_current=1').fetchone())
    fill_budget(store)
    assert material_for(store, current)['handwritten_evidence'] == {'memories': []}


def review(store, before, model):
    return PersonaEngine(store, model, clock=lambda: NOW)._review_handwritten(
        material_for(store, before), {}, deadline=time.monotonic() + 900)


def test_same_topic_memories_come_first_and_newest_first(store):
    before = write(store)
    add_self(store, '后来我换了一只新的雨靴。', stamp=LATER)
    one = add_self(store, '合奏这件事我又想了想。', stamp=LATER)
    newest = add_self(store, '合奏这件事我再想了想。', stamp='2026-10-03T12:00:00+00:00')
    two = add_self(store, CORRECTION, stamp=LATER)
    model = ReviewModel(store, TEXT)
    review(store, before, model)
    assert [m['memory_id'] for m in model.reviews[0]['evidence_by_ref'].values()][:3] == [two, newest, one]


def test_pages_are_bounded_and_unreviewed_memories_are_audited(store, monkeypatch):
    monkeypatch.setattr(persona, 'MAX_CHECK_REQUEST_BYTES', 8000)
    before = write(store)
    for index in range(30):
        add_self(store, f'关于合奏的第{index}条记录，'+'内容足够长以便占满请求页面，'*4, stamp=LATER)
    model = ReviewModel(store, TEXT)
    _, audit = review(store, before, model)
    assert len(model.reviews) == HANDWRITTEN_REVIEW_MAX_PAGES
    assert audit['unreviewed_memories'][0]['sentence_ref'] == 'A1'
    assert audit['unreviewed_memories'][0]['count'] > 0


def invalid_quote_model(store, *, persistent=False):
    model = ReviewModel(store, TEXT, 'explicit_correction')
    original = model.chat
    def chat(messages, purpose, **kwargs):
        reply = original(messages, purpose, **kwargs)
        if json.loads(messages[-1]['content']).get('task') == 'handwritten_review' and (persistent or len(model.reviews) == 1):
            value = json.loads(reply.content)
            value['evidence'][0]['quote'] = '并不存在的原话。'
            reply.content = dumps(value)
        return reply
    model.chat = chat
    return model


def test_reask_names_the_defect_and_keeps_the_same_evidence(store):
    before = write(store)
    add_self(store, CORRECTION, stamp=LATER)
    model = invalid_quote_model(store)
    changes, audit = review(store, before, model)
    assert 'format_retry' not in model.reviews[0]
    assert 'exact quote' in model.reviews[1]['format_retry']['errors'][0]
    assert model.reviews[0]['evidence_by_ref'] == model.reviews[1]['evidence_by_ref']
    assert audit['retries'] == 1 and len(changes) == 1


def test_reask_falls_back_when_the_feedback_would_exceed_the_byte_limit(store, monkeypatch):
    before = write(store)
    add_self(store, CORRECTION, stamp=LATER)
    real = persona._check_request_bytes
    monkeypatch.setattr(persona, '_check_request_bytes',
        lambda purpose, payload: persona.MAX_CHECK_REQUEST_BYTES + 1 if 'format_retry' in payload else real(purpose, payload))
    model = invalid_quote_model(store)
    review(store, before, model)
    assert len(model.reviews) == 2 and 'format_retry' not in model.reviews[1]


@pytest.mark.parametrize('content', ['我好像不再讨厌合奏了。', '我可能不是不喜欢合奏。'])
def test_uncertain_statement_never_licenses_deleting_handwriting(store, content):
    before = write(store)
    add_self(store, content, stamp=LATER)
    model = ReviewModel(store, TEXT, 'explicit_correction')
    changes, audit = review(store, before, model)
    assert changes == [] and audit['conservative_keeps'] == 1 and len(model.reviews) == 2


def test_no_later_memory_still_means_a_single_empty_page_and_keep(store):
    before = write(store)
    fill_budget(store)
    model = ReviewModel(store, TEXT)
    changes, _ = review(store, before, model)
    assert changes == [] and len(model.reviews) == 1 and model.reviews[0]['evidence_by_ref'] == {}
