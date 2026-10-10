"""Free-form timestamps and durable item failures; no network models."""
import json

import pytest

from iris import consolidation, goals, persona
from iris.consolidation import Consolidation, merge_exclusion, snapshot
from iris.maintenance import Maintenance
from iris.memory_ops import setup_role
from test_consolidation import Model, pair, merge_answer
from test_lifecycle import Clock
from test_maintenance_cognition import goal_with_lost_basis, persona_case
from test_persona import add_self


@pytest.mark.parametrize('left,right,excluded', [
    ('2026-11-23 至 2026-11-29', '下个月', True),
    ('下个月', '2026-11-01T00:00:00+08:00', True),
    ('2026-11-23 至 2026-11-29', '2026-11-23 至 2026-11-29', False),
    ('下个月', '下个月', False),
    ('2026-02-30', '2026-03-02', True),
    ('2026-02-30', '2026-02-30', False),
    ('2026-11-01T08:00:00+08:00', '2026-11-01T00:00:00+00:00', False),
    ('2026-11-01', '2026-11-02', True),
    (None, '下个月', False),
])
def test_event_time_iso_or_exact_text_comparison(store, left, right, excluded):
    a, b = pair(store)
    with store.read() as conn:
        first, second = snapshot(conn, a), snapshot(conn, b)
    first['event_time'], second['event_time'] = left, right
    assert merge_exclusion(first, second) == ('event_time' if excluded else None)
    assert first['event_time'] == left and second['event_time'] == right


@pytest.mark.parametrize('same', [False, True])
def test_non_iso_times_complete_maintenance_and_survive_restart(store, same):
    a, b = pair(store)
    with store.write() as conn:
        conn.execute('UPDATE memories SET event_time=? WHERE id=?', ('下个月', a))
        conn.execute('UPDATE memories SET event_time=? WHERE id=?', ('下个月' if same else '2026-11-23 至 2026-11-29', b))
    model = Model(store, lambda p: merge_answer(p) if p['merge_allowed'] else None)
    engine = Maintenance(store, gateway=model, clock=Clock())
    rid = engine.request()
    engine.run(rid)
    report = engine.report(rid)
    assert report['state'] == 'completed'
    assert report['summary']['failed']['count'] == 0
    assert report['summary'].get('merged', {}).get('count', 0) == int(same)
    assert model.requests[0][1]['merge_allowed'] == same
    calls = len(model.requests)
    engine.run(rid)
    assert len(model.requests) == calls


@pytest.mark.parametrize('raw_time', ['2026-11-23 至 2026-11-29', '下个月'])
def test_persona_evidence_preserves_text_and_tolerates_text_source_times(store, raw_time):
    setup_role(store, 'Iris', '我来自云城。')
    mid = add_self(store)
    with store.write() as conn:
        conn.execute('UPDATE memories SET event_time=? WHERE id=?', (raw_time, mid))
        conn.execute('UPDATE messages SET occurred_at=? WHERE id IN (SELECT message_id FROM sources WHERE memory_id=?)', (raw_time, mid))
    memory = next(m for m in persona.select_evidence(store)['memories'] if m['memory_id'] == mid)
    assert memory['event_time'] == raw_time
    assert memory['dates'] == []
    assert memory['excerpts'][0]['occurred_at'] == raw_time
    assert memory['excerpts'][0]['date'] is None
    assert persona.persona_due(store, clock=Clock())['changed_memory_ids'] == [mid]


@pytest.mark.parametrize('raw_time', ['2026-11-23 至 2026-11-29', '下个月'])
def test_goal_review_preserves_text_times_and_legacy_deadline(store, raw_time):
    gid = goal_with_lost_basis(store)
    with store.write() as conn:
        conn.execute('UPDATE memories SET event_time=?', (raw_time,))
        conn.execute('UPDATE goals SET deadline=? WHERE id=?', (raw_time, gid))
    result = goals.review_goal_basis_items(store, Clock()())
    assert result['changed_goal_ids'] == [gid]
    assert not goals.review_goal_basis_items(store, Clock()())['changed_goal_ids']
    with store.read() as conn:
        assert conn.execute('SELECT deadline FROM goals WHERE id=?', (gid,)).fetchone()[0] == raw_time
        assert conn.execute('SELECT event_time FROM memories').fetchone()[0] == raw_time


def independent_pairs(store):
    first = pair(store)
    second = pair(store)
    with store.write() as conn:
        conn.execute("UPDATE memories SET world='other',importance=70 WHERE id IN (?,?)", second)
    return first, second


def failed_items(report):
    return [i for i in report['items'] if i['outcome'] == 'failed']


@pytest.mark.parametrize('point', ['snapshot', 'pair', 'enqueue'])
def test_planning_failure_is_one_item_and_restart_skips_it(store, monkeypatch, point):
    first, second = independent_pairs(store)
    original = getattr(consolidation, {'snapshot':'snapshot', 'pair':'merge_exclusion', 'enqueue':'work_fingerprint'}[point])
    attempts = []
    def fail(*args):
        affected = args[1] == first[0] if point == 'snapshot' else (
            args[0]['id'] == first[0] if point == 'pair' else first[0] in args[1].get('pair_ids', []))
        if affected:
            attempts.append(1)
            raise RuntimeError('private provider detail must not enter report')
        return original(*args)
    monkeypatch.setattr(consolidation, original.__name__, fail)
    engine = Maintenance(store, gateway=Model(store, merge_answer), clock=Clock())
    rid = engine.request()
    # Simulate termination after planning has committed item checkpoints but
    # before the phase's planned bit commits; rescan must skip failed items.
    original_plan = Consolidation.plan
    def interrupt(owner, run, settings):
        original_plan(owner, run, settings)
        with store.write() as conn:
            conn.execute('UPDATE consolidation_runs SET planned=0 WHERE run_id=?', (rid,))
        raise KeyboardInterrupt()
    monkeypatch.setattr(Consolidation, 'plan', interrupt)
    with pytest.raises(KeyboardInterrupt):
        engine.run(rid)
    before = len(attempts)
    monkeypatch.setattr(Consolidation, 'plan', original_plan)
    engine.run(rid)
    engine.run(rid)
    report = engine.report(rid)
    assert report['state'] == 'completed'
    assert len(attempts) == before == 1
    assert len(failed_items(report)) == 1
    assert 'private provider detail' not in json.dumps(report)
    assert report['summary']['merged']['count'] == 1
    with store.read() as conn:
        assert snapshot(conn, second[0])['merged_into'] == second[1]
        assert all(snapshot(conn, i)['lifecycle'] == 'active' for i in first)


@pytest.mark.parametrize('point', ['call', 'stale', 'apply'])
def test_unexpected_work_exception_rolls_back_and_is_not_retried(store, monkeypatch, point):
    first, second = independent_pairs(store)
    method = {'call':'call', 'stale':'_stale', 'apply':'apply'}[point]
    original = getattr(Consolidation, method)
    attempts = []
    def fail(owner, *args, **kwargs):
        payload = args[1] if point in ('call','stale') else args[2]
        if first[0] in payload['pair_ids']:
            attempts.append(1)
            if point == 'apply':
                original(owner, *args, **kwargs)
            raise RuntimeError('private provider detail must not enter report')
        return original(owner, *args, **kwargs)
    monkeypatch.setattr(Consolidation, method, fail)
    engine = Maintenance(store, gateway=Model(store, merge_answer), clock=Clock())
    rid = engine.request()
    engine.run(rid)
    engine.run(rid)
    report = engine.report(rid)
    assert report['state'] == 'completed'
    assert report['summary']['merged']['count'] == 1
    assert len(failed_items(report)) == 1
    assert failed_items(report)[0]['details']['error_type'] == 'RuntimeError'
    assert 'private provider detail' not in json.dumps(report)
    assert report['consolidation']['deferred'] == 0
    # Same unchanged input also has a durable terminal work receipt next day.
    engine.run(engine.request())
    assert attempts == [1]
    with store.read() as conn:
        assert all(snapshot(conn, i)['lifecycle'] == 'active' for i in first)
        assert snapshot(conn, second[0])['merged_into'] == second[1]
        assert not conn.execute('SELECT 1 FROM memory_revisions WHERE memory_id IN (?,?)', first).fetchone()


def test_persona_due_exception_is_reported_and_goals_continue(store, monkeypatch):
    engine, model, _ = persona_case(store)
    gid = goal_with_lost_basis(store)
    attempts = []
    def fail(*args, **kwargs):
        attempts.append(1)
        raise RuntimeError('private detail')
    monkeypatch.setattr(persona, 'persona_due', fail)
    rid = engine.request()
    engine.run(rid)
    engine.run(rid)
    report = engine.report(rid)
    assert report['state'] == 'completed'
    assert report['persona']['status'] == 'failed'
    assert report['summary']['goals_reviewed']['object_ids'] == [gid]
    assert len(failed_items(report)) == 1
    assert not model.calls and attempts == [1]
    assert 'private detail' not in json.dumps(report)


@pytest.mark.parametrize('phase', ['decay', 'goals'])
def test_deterministic_item_failure_advances_cursor_and_rolls_back(store, monkeypatch, phase):
    if phase == 'goals':
        first, second = goal_with_lost_basis(store), goal_with_lost_basis(store)
    else:
        first, second = pair(store)
    engine = Maintenance(store, clock=Clock())
    original = engine._apply
    attempts = []
    def fail(conn, run, stage, candidate):
        result = original(conn, run, stage, candidate)
        if stage == phase and candidate['id'] == first:
            attempts.append(1)
            raise RuntimeError('private detail')
        return result
    monkeypatch.setattr(engine, '_apply', fail)
    rid = engine.request()
    engine.run(rid)
    engine.run(rid)
    report = engine.report(rid)
    assert report['state'] == 'completed' and attempts == [1]
    assert len(failed_items(report)) == 1
    assert failed_items(report)[0]['object_id'] == first
    if phase == 'goals':
        assert report['summary']['goals_reviewed']['object_ids'] == [second]
        with store.read() as conn:
            assert not conn.execute('SELECT 1 FROM goal_basis_annotations WHERE goal_id=?', (first,)).fetchone()


def test_format_failure_has_only_one_later_daily_retry(store):
    from iris.models import ModelReply
    pair(store)
    class Malformed(Model):
        def chat(self, *args, **kwargs):
            self.requests.append(1)
            return ModelReply('{', 'stop', {})
    model = Malformed(store)
    engine = Maintenance(store, gateway=model, clock=Clock())
    first_run = None
    for deferred, total_calls in ((1,2), (0,4), (0,4)):
        rid = engine.request()
        engine.run(rid)
        engine.run(rid)
        assert engine.report(rid)['consolidation']['deferred'] == deferred
        assert len(model.requests) == total_calls
        first_run = first_run or rid
    assert engine.report(first_run)['consolidation']['deferred'] == 1


def test_dependency_planning_failure_keeps_unrelated_merge_and_goals(store, monkeypatch):
    from test_retrieval import put
    a, b = pair(store)
    basis = put(store, '厨房已经退租', importance=80)
    derived = put(store, '我可能继续烘焙', stance='推断', importance=80)
    with store.write() as conn:
        conn.execute("INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) VALUES(?,'memory',?,1,?)",
                     (derived,basis,Clock()().isoformat()))
    gid = goal_with_lost_basis(store)
    def fail(*args):
        raise IndexError('private detail')
    monkeypatch.setattr(consolidation,'dependency_material',fail)
    engine = Maintenance(store,gateway=Model(store,merge_answer),clock=Clock())
    rid = engine.request()
    engine.run(rid)
    report = engine.report(rid)
    assert report['state'] == 'completed'
    assert report['summary']['merged']['count'] == 1
    assert report['summary']['goals_reviewed']['object_ids'] == [gid]
    failures = failed_items(report)
    assert len(failures) == 1 and failures[0]['memory_id'] == derived
    assert failures[0]['details']['error_type'] == 'IndexError'


def test_interruption_after_work_failure_receipt_resumes_other_items(store, monkeypatch):
    first, second = independent_pairs(store)
    calls = []
    def answer(payload):
        calls.append(payload['pair_ids'])
        if first[0] in payload['pair_ids']:
            return RuntimeError('private detail')
        return merge_answer(payload)
    model = Model(store,answer)
    engine = Maintenance(store,gateway=model,clock=Clock())
    rid = engine.request()
    original = Consolidation.fail_work
    def interrupt(owner, *args, **kwargs):
        original(owner,*args,**kwargs)
        raise KeyboardInterrupt()
    monkeypatch.setattr(Consolidation,'fail_work',interrupt)
    with pytest.raises(KeyboardInterrupt):
        engine.run(rid)
    monkeypatch.setattr(Consolidation,'fail_work',original)
    restarted = Maintenance(store,gateway=model,clock=Clock())
    restarted.run(rid)
    report = restarted.report(rid)
    assert report['state'] == 'completed' and len(failed_items(report)) == 1
    assert report['summary']['merged']['count'] == 1
    assert sum(first[0] in ids for ids in calls) == 1
    assert sum(second[0] in ids for ids in calls) == 1


def test_corrupt_persisted_decision_isolated_from_later_merge(store):
    first, second = independent_pairs(store)
    model = Model(store,merge_answer)
    engine = Maintenance(store,gateway=model,clock=Clock())
    rid = engine.request()
    engine.run(rid,stop=lambda:len(model.requests)==1)
    with store.write() as conn:
        conn.execute("UPDATE consolidation_work SET result_json='[]' WHERE id=(SELECT MIN(id) FROM consolidation_work)")
    engine.run(rid)
    report = engine.report(rid)
    assert report['state'] == 'completed' and len(failed_items(report)) == 1
    assert report['summary']['merged']['count'] == 1
    with store.read() as conn:
        assert all(snapshot(conn,i)['lifecycle']=='active' for i in first)
        assert snapshot(conn,second[0])['merged_into']==second[1]


def test_persona_failure_checkpoint_survives_stop_before_phase_advance(store,monkeypatch):
    engine,model,_ = persona_case(store)
    gid = goal_with_lost_basis(store)
    failed = []
    def fail(*args,**kwargs):
        failed.append(1)
        raise RuntimeError('private detail')
    monkeypatch.setattr(persona,'persona_due',fail)
    rid=engine.request()
    engine.run(rid,stop=lambda:bool(failed))
    assert engine.report(rid)['persona']['status']=='failed'
    Maintenance(store,gateway=model,clock=engine.clock).run(rid)
    assert failed==[1] and not model.calls
    assert engine.report(rid)['summary']['goals_reviewed']['object_ids']==[gid]
