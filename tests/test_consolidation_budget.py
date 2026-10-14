"""Consolidation's request-group deadline, exercised without real model calls."""
import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from iris import consolidation
from iris.consolidation import snapshot
from iris.memory_ops import delete_memory
from iris.models import Gateway, ModelConfig, ModelError
from fake_openai import completion
from test_consolidation import merge_answer, pair, run
from test_lifecycle import Clock


@contextmanager
def scripted_gateway(store, monkeypatch, steps):
    script = SimpleNamespace(now=1000.0, timeouts=[], deadlines=[], sleeps=[])

    def advance(seconds):
        script.now += seconds

    def sleep(seconds):
        script.sleeps.append(seconds)
        advance(seconds)

    def respond(request):
        assert not store._writer.in_transaction
        timeout = request.extensions['timeout']['read']
        script.timeouts.append(timeout)
        script.deadlines.append(script.now + timeout)
        elapsed, answer = steps[len(script.timeouts) - 1]
        advance(elapsed)
        if isinstance(answer, int):
            return httpx.Response(answer, json={'error': {'code': 'InternalServerError'}})
        if answer == 'merge':
            payload = json.loads(json.loads(request.content)['messages'][1]['content'])
            answer = merge_answer(payload)
        content = json.dumps(answer, ensure_ascii=False) if isinstance(answer, dict) else answer
        return httpx.Response(200, json=completion(content))

    # Keep the virtual clock local to consolidation; do not replace global time.
    monkeypatch.setattr(consolidation, 'time', SimpleNamespace(monotonic=lambda: script.now))
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        gateway = Gateway(
            {'chat': ModelConfig('https://example.invalid', '', 'fake', reasoning_effort='high')},
            store, client, sleeper=sleep, monotonic=lambda: script.now + 5000,
            jitter=lambda low, high: 0,
        )
        try:
            yield gateway, script
        finally:
            gateway.close()


@pytest.mark.parametrize('kind', ['merge', 'conflict', 'dependency'])
def test_consolidation_requests_allow_180_seconds(store, monkeypatch, kind):
    a, b = pair(store)
    answer = 'merge'
    if kind == 'conflict':
        with store.write() as conn:
            conn.execute('UPDATE memories SET pinned=1 WHERE id=?', (a,))
        answer = {'decision': 'separate', 'reason': 'Different facts', 'evidence': [], 'updates': []}
    elif kind == 'dependency':
        with store.write() as conn:
            conn.execute(
                "INSERT INTO sources(memory_id,kind,source_memory_id,source_revision,created_at) "
                "VALUES(?,'memory',?,1,?)", (b, a, Clock()().isoformat()),
            )
        delete_memory(store, a, 1)
        answer = {'decision': 'keep', 'reason': 'Independent source remains', 'evidence': [], 'updates': []}

    with scripted_gateway(store, monkeypatch, [(150, answer)]) as (gateway, script):
        _, _, report = run(store, gateway)

    assert script.timeouts == [180]
    assert report['summary'].get('failed', {}).get('count', 0) == 0
    assert report['summary'].get('merged', {}).get('count', 0) == (1 if kind == 'merge' else 0)
    with store.read() as conn:
        assert [tuple(row) for row in conn.execute(
            'SELECT purpose,result_category,timed_out FROM model_calls ORDER BY id'
        )] == [(f'consolidation_{kind}', 'success', 0)]


def test_initial_retry_and_json_repair_share_one_deadline(store, monkeypatch):
    pair(store)
    # 40 s failed HTTP call + 2 s backoff + 60 s malformed reply + 70 s repair.
    with scripted_gateway(store, monkeypatch, [(40, 503), (60, '{'), (70, 'merge')]) as (gateway, script):
        _, _, report = run(store, gateway)

    assert script.timeouts == [180, 138, 78]
    assert script.deadlines == [1180, 1180, 1180]
    assert script.sleeps == [2]
    assert report['summary'].get('merged', {}).get('count', 0) == 1
    assert report['summary']['model_calls']['count'] == 3
    with store.read() as conn:
        assert [tuple(row) for row in conn.execute(
            'SELECT purpose,result_category FROM model_calls ORDER BY id'
        )] == [
            ('consolidation_merge', 'retryable'),
            ('consolidation_merge', 'success'),
            ('consolidation_merge_repair', 'success'),
        ]
        assert conn.execute('SELECT COUNT(*) FROM consolidation_calls').fetchone()[0] == 3


@pytest.mark.parametrize('steps,remaining', [
    ([(180.001, 'merge')], [180]),
    ([(40, 503), (138.001, 'merge')], [180, 138]),
    ([(100, '{'), (80.001, 'merge')], [180, 80]),
], ids=['initial', 'retry', 'repair'])
def test_late_success_never_writes_memories(store, monkeypatch, steps, remaining):
    ids = pair(store)
    with store.read() as conn:
        before = [snapshot(conn, mid) for mid in ids]

    with scripted_gateway(store, monkeypatch, steps) as (gateway, script):
        _, _, report = run(store, gateway)

    assert script.timeouts == remaining
    assert script.deadlines == [1180] * len(remaining)
    assert report['summary'].get('merged', {}).get('count', 0) == 0
    assert report['summary'].get('failed', {}).get('count', 0) == 1
    with store.read() as conn:
        for original in before:
            after = snapshot(conn, original['id'])
            for field in ('content', 'belief', 'revision', 'merged_into', 'sources'):
                assert after[field] == original[field]
        assert not conn.execute(
            "SELECT 1 FROM memory_revisions WHERE actor='consolidation'"
        ).fetchone()
        last = conn.execute(
            'SELECT result_category,timed_out,error_summary FROM model_calls ORDER BY id DESC LIMIT 1'
        ).fetchone()
        assert tuple(last) == ('retryable', 1, 'total timeout')


def test_only_one_json_repair_is_allowed(store, monkeypatch):
    pair(store)
    with scripted_gateway(store, monkeypatch, [(1, '{'), (1, '{')]) as (gateway, script):
        _, _, report = run(store, gateway)

    assert script.timeouts == [180, 179]
    assert report['summary'].get('merged', {}).get('count', 0) == 0
    assert [item['reason'] for item in report['items'] if item['outcome'] == 'failed'] == ['invalid_output']


@pytest.mark.parametrize('purpose,budget', [
    ('trial_reply', 120),
    ('consolidation_persona_generate', 120),
    ('consolidation_entry_profile_generate', 120),
    ('consolidation_person_summary_generate', 120),
    ('learning', 180),
    ('learning_judge', 240),
])
def test_other_gateway_budgets_are_unchanged(store, monkeypatch, purpose, budget):
    with scripted_gateway(store, monkeypatch, [(budget + 0.001, '{}')]) as (gateway, script):
        with pytest.raises(ModelError, match='total timeout'):
            gateway.chat([{'role': 'user', 'content': 'Local deadline check'}], purpose)

    assert script.timeouts == [budget]
    with store.read() as conn:
        assert conn.execute('SELECT timed_out FROM model_calls').fetchone()[0] == 1
