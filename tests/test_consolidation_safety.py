"""Write rejection, candidate isolation and conservative historical treatment."""
import json

import pytest

from iris.consolidation import snapshot
from iris.memory_ops import delete_memory
from test_consolidation import Model, pair, run
from test_lifecycle import Clock
from test_retrieval import put
from conftest import msg


def resolution(store, name='rewrite_v2'):
    store.set_setting('consolidation', {'resolution': name})


def conflict(a, sources, **update):
    return {'decision': 'conflict', 'reason': '依据原话复核摘要', 'evidence': sources,
            'updates': [{'id': a, 'annotation': '摘要存在争议，保留原始事实范围', **update}]}


@pytest.mark.parametrize('body,reason', [
    ('她每周教口琴', 'gendered_pronoun'),
    ('他每周教口琴', 'gendered_pronoun'),
    ('P1每周教口琴', 'participant_reference'),
    ('我每周教口琴，见M1', 'numbered_reference'),
    ('我每周教口琴，见消息1', 'numbered_reference'),
    ('wrong_subject每周教口琴', 'unsupported_identifier'),
    ('我每周教2次口琴', 'claim_sequences'),
    ('我不教口琴', 'claim_sequences'),
])
def test_unsafe_body_is_individual_failure_without_rewrite_retry(store, body, reason):
    resolution(store)
    a, b = pair(store)
    model = Model(store, lambda p: conflict(a, [1, 2], content=body, belief=1))
    _, _, report = run(store, model)
    with store.read() as conn:
        after = snapshot(conn, a)
        assert after['content'] == '我每周教口琴' and after['belief'] == 70
        assert not after['annotations'] and not after['merged_into']
        assert snapshot(conn, b)['annotations']  # Independent safe item still commits.
    failures = [i for i in report['items'] if i['outcome'] == 'failed']
    assert len(failures) == 1 and failures[0]['details']['rejection'] == reason
    assert len(model.requests) == 1
    run(store, model)
    assert len(model.requests) == 1  # Failure has a receipt until the inputs change.


@pytest.mark.parametrize('field,value', [('evidence', [99999]), ('subject_ids', ['unknown_subject']), ('id', 99999)])
def test_unknown_material_ids_do_not_trigger_semantic_repair(store, field, value):
    resolution(store)
    a, _ = pair(store)
    answer = conflict(a, [1, 2], content='我固定每周教口琴')
    if field == 'evidence':
        answer[field] = value
    else:
        answer['updates'][0][field] = value
    model = Model(store, answer)
    _, _, report = run(store, model)
    assert len(model.requests) == 1 and report['summary']['failed']['count'] >= 1
    with store.read() as conn:
        assert snapshot(conn, a)['content'] == '我每周教口琴'


def test_source_sequences_must_match_even_when_original_summary_agrees(store):
    resolution(store)
    sid = msg(store, 1, '我只教1次口琴。')
    a = put(store, '我教2次口琴', evidence=[sid], importance=80)
    put(store, '我教1次口琴', evidence=[sid], importance=80)
    model = Model(store, conflict(a, [sid], content='我固定教2次口琴'))
    _, _, report = run(store, model)
    assert any(i['details'].get('rejection') == 'claim_sequences' for i in report['items'])
    with store.read() as conn:
        assert snapshot(conn, a)['content'] == '我教2次口琴'


@pytest.mark.parametrize('body', ['我过去喜欢口琴', '我原定每周教口琴', '我去年教口琴'])
def test_conservative_historical_fact_never_loses_belief(store, body):
    resolution(store, 'conservative_v2')
    sid = msg(store, 1, body)
    newer = msg(store, 2, '我现在不喜欢口琴，也不教口琴了。')
    a = put(store, body, evidence=[sid], importance=80)
    put(store, '我现在不教口琴', evidence=[newer], importance=80)
    answer = conflict(a, [sid, newer], belief=1, assessment='source_error', annotation='旧安排已被后来说法替代')
    run(store, Model(store, answer))
    with store.read() as conn:
        after = snapshot(conn, a)
    assert after['content'] == body and after['belief'] == 70
    assert after['annotations']


def test_conservative_unmarked_old_preference_needs_explicit_error_for_belief_drop(store):
    resolution(store, 'conservative_v2')
    sid = msg(store, 1, '我喜欢口琴。')
    later = msg(store, 2, '现在我不喜欢口琴了。')
    a = put(store, '我喜欢口琴', evidence=[sid], importance=80)
    put(store, '我现在不喜欢口琴', evidence=[later], importance=80)
    run(store, Model(store, conflict(a, [sid, later], belief=1, assessment='source_error')))
    with store.read() as conn:
        assert snapshot(conn, a)['belief'] == 70


def test_conservative_explicit_original_error_can_lower_belief(store):
    resolution(store, 'conservative_v2')
    sid = msg(store, 1, '订了12箱。')
    correction = msg(store, 2, '更正，12箱是笔误，实际是21箱。')
    with store.read() as conn:
        speaker = conn.execute('SELECT sender_subject_id FROM messages WHERE id=?', (sid,)).fetchone()[0]
    a = put(store, '我订了12箱', evidence=[sid], speaker=speaker, about=(speaker,), importance=80)
    put(store, '我订了21箱', evidence=[correction], speaker=speaker, about=(speaker,), importance=80)
    run(store, Model(store, conflict(a, [correction], belief=20, assessment='source_error', annotation='旧数量为笔误，正文保留为旧说法')))
    with store.read() as conn:
        after = snapshot(conn, a)
    assert after['belief'] == 20 and after['content'] == '我订了12箱'


@pytest.mark.parametrize('kind', ['pair', 'dependency'])
def test_conservative_forbids_model_body_even_when_source_supported(store, kind):
    resolution(store, 'conservative_v2')
    a, b = pair(store)
    if kind == 'dependency':
        with store.write() as conn:
            conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,1,?)", (a, b, Clock()().isoformat()))
        delete_memory(store, b, 1)
    answer = conflict(a, [1], content='我固定每周教口琴')
    if kind == 'dependency':
        answer['decision'] = 'review'
    model = Model(store, answer)
    _, _, report = run(store, model)
    assert any(i['details'].get('rejection') == 'conservative_body_write' for i in report['items'])
    with store.read() as conn:
        assert snapshot(conn, a)['content'] == '我每周教口琴'
    assert len(model.requests) == 1


def test_annotation_ids_are_checked_without_saving_invalid_claims(store):
    resolution(store)
    a, _ = pair(store)
    model = Model(store, conflict(a, [1, 2], annotation='依据消息99999，wrong_subject更正了摘要'))
    _, _, report = run(store, model)
    assert report['summary']['failed']['count'] == 1
    with store.read() as conn:
        assert not snapshot(conn, a)['annotations']


def test_resolution_switch_does_not_reuse_old_decisions_or_receipts(store):
    a, _ = pair(store)
    resolution(store, 'original_v1')
    model = Model(store)
    run(store, model, stop=lambda: bool(model.requests))
    assert len(model.requests) == 1
    resolution(store, 'conservative_v2')
    run(store, model)  # Resume the original run under its frozen configuration.
    assert len(model.requests) == 1
    run(store, model)
    assert len(model.requests) == 2
    with store.read() as conn:
        settings = [json.loads(r[0]) for r in conn.execute('SELECT settings_json FROM consolidation_runs ORDER BY run_id')]
    assert [s['resolution'] for s in settings] == ['original_v1', 'conservative_v2']


@pytest.mark.parametrize('old,source,proposed', [
    ('我每周教1.5小时口琴', '我每周教1.5小时口琴', '我每周教15小时口琴'),
    ('I do not teach flute', 'I do not teach flute', 'I do teach flute'),
    ('我每周教口琴', '我不教口琴', '我固定每周教口琴'),
])
def test_rewrite_does_not_hide_decimal_or_english_negation_changes(store, old, source, proposed):
    from iris.consolidation import UnsafeWrite, validate_write
    sid = msg(store, 1, source)
    a = put(store, old, evidence=[sid])
    with store.read() as conn:
        m = snapshot(conn, a)
    with pytest.raises(UnsafeWrite, match='claim_sequences'):
        validate_write(m, {'content': proposed}, {'memories': [m]}, [sid], 'rewrite_v2')


def test_uncited_and_truncated_sources_cannot_authorize_body(store):
    from iris.consolidation import UnsafeWrite, validate_write
    ids = [msg(store, 1, '有1节课'), msg(store, 2, '口琴' * 200 + '有2节课')]
    a = put(store, '我教2节课', evidence=ids)
    with store.read() as conn:
        m = snapshot(conn, a)
    for evidence in ([ids[0]], [ids[1]]):
        with pytest.raises(UnsafeWrite, match='claim_sequences'):
            validate_write(m, {'content': '我固定教2节课'}, {'memories': [m]}, evidence, 'rewrite_v2')


def test_partial_failure_does_not_block_prepare_or_repeat_good_item(store):
    from iris.retrieval import Retrieval
    resolution(store)
    a, b = pair(store)
    model = Model(store, conflict(a, [1, 2], content='她教口琴'))
    run(store, model)
    assert Retrieval(store).prepare('A', text='口琴', recent_limit=0, judge=False)['memories']
    with store.read() as conn:
        revision = snapshot(conn, b)['revision']
    run(store, model)
    with store.read() as conn:
        assert snapshot(conn, b)['revision'] == revision
    assert len(model.requests) == 1


def test_gender_guard_recognizes_pronouns_without_treating_guitar_as_person(store):
    from iris.consolidation import validate_write
    sid = msg(store, 1, '我只参加吉他维修课。')
    a = put(store, '我参加吉他维修课', evidence=[sid])
    with store.read() as conn:
        memory = snapshot(conn, a)
    validate_write(memory, {'content':'我只参加吉他维修课', 'annotation':'其他来源不能证明更宽的概括'},
                   {'memories':[memory]}, [sid], 'rewrite_v2')


def test_root_evidence_rejection_is_complete_not_deferred(store):
    resolution(store)
    a, _ = pair(store)
    model = Model(store, conflict(a, [99999]))
    _, _, report = run(store, model)
    assert report['consolidation']['deferred'] == 0
    run(store, model)
    assert len(model.requests) == 1


def test_invalid_subject_in_report_is_not_saved_as_revision_reason(store):
    resolution(store)
    a, _ = pair(store)
    answer=conflict(a,[1,2],annotation='安全的争议说明')
    answer['reason']='wrong_subject确认了消息1'
    model=Model(store,answer)
    _,_,report=run(store,model)
    assert len(model.requests)==1 and report['summary']['failed']['count']==1
    with store.read() as conn:
        assert not conn.execute("SELECT 1 FROM memory_revisions WHERE actor='consolidation'").fetchone()


def test_existing_body_history_and_vector_survive_rejected_write(store):
    resolution(store)
    sid=msg(store,1,'我参加吉他维修课')
    a=put(store,'我参加吉他维修课',evidence=[sid],vector=[1.,0.],importance=80)
    put(store,'我不再参加吉他维修课',evidence=[sid],importance=80)
    index=store.vector_index('fake-vector')
    model=Model(store,conflict(a,[sid],content='她参加吉他维修课'))
    run(store,model)
    with store.read() as conn:
        assert snapshot(conn,a)['revision']==1
        assert not conn.execute('SELECT 1 FROM memory_revisions WHERE memory_id=?',(a,)).fetchone()
    assert index.contains(a)


@pytest.mark.parametrize('invalid', ['weaken_content','empty_content','invalid_belief','invalid_keep_id'])
def test_semantic_write_errors_never_request_rewriting(store, invalid):
    resolution(store)
    a,b=pair(store)
    answer=conflict(a,[1,2],content='我固定每周教口琴')
    if invalid=='weaken_content':
        with store.write() as conn:
            conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,1,?)",(a,b,Clock()().isoformat()))
        delete_memory(store,b,1)
        answer.update(decision='weaken',evidence=[1])
    elif invalid=='empty_content':
        answer['updates'][0]['content']=''
    elif invalid=='invalid_belief':
        answer['updates'][0]['belief']=101
    else:
        answer.update(decision='merge',keep_id=99999,updates=[])
    model=Model(store,answer)
    _,_,report=run(store,model)
    assert len(model.requests)==1
    assert report['summary']['model_calls']['count']==1
    assert report['summary']['failed']['count']==1 and report['consolidation']['deferred']==0
    with store.read() as conn:
        assert snapshot(conn,a)['content']=='我每周教口琴'
