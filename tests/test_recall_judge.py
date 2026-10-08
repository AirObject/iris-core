"""Recall judgment uses fake transport; no real model or corpus labels."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from conftest import msg
from iris.memory_ops import edit_memory
from iris.model_health import ModelHealth, utc_now
from iris.models import Gateway, ModelConfig, ModelError
from iris.recall_judge import validate_scores
from iris.retrieval import Retrieval
from test_models import response
from test_retrieval import entry, put


def gateway(store, handler, *, health=None):
    config = {'chat': ModelConfig('https://example.invalid/v1', '', 'stub', reasoning_effort='low')}
    return closing(Gateway(config, store, health=health, client=httpx.Client(transport=httpx.MockTransport(handler))))


def scores(ids, values=None):
    return json.dumps({'scores': [{'id': str(mid), 'support': value} for mid, value in
                                zip(ids, values or [100] * len(ids))]})


@pytest.mark.parametrize('raw', [
    '{}', '{"scores":[]}', '{"scores":[{"id":"2","support":100}]}',
    '{"scores":[{"id":"1","support":true}]}', '{"scores":[{"id":"1","support":50.0}]}',
    '{"scores":[{"id":"1","support":101}]}', '{"scores":[{"id":1,"support":50}]}',
    '{"scores":[{"id":"1","support":50,"reason":"extra"}]}',
    '```json\n{"scores":[{"id":"1","support":50}]}\n```',
    '{"scores":[{"id":"1","support":50}]} trailing',
    '{"scores":[{"id":"1","support":1,"support":100}]}',
])
def test_strict_schema(raw):
    with pytest.raises(ValueError):
        validate_scores(raw, ['1'])


def test_order_and_integer_boundaries_and_frozen_prompt():
    assert validate_scores(scores([1, 2, 3], [0, 50, 100]), ['1', '2', '3']) == [0, 50, 100]
    with pytest.raises(ValueError):
        validate_scores(scores([2, 1]), ['1', '2'])
    root = Path(__file__).parents[1]
    assert (root / 'evals/no_answer_probe/judgment_prompt.txt').read_bytes() == (
        root / 'src/iris/prompts/recall_judge_v1.md').read_bytes()


def test_filter_records_feedback_no_refill_or_highlight_judging(store):
    entry(store)
    mids = [put(store, prose) for prose in ['摄影需要赤道仪', '摄影要带备用电池', '摄影记录曝光时间']]
    other = put(store, '来客喜欢烘焙', about=[store._writer.execute("SELECT id FROM subjects WHERE name='来客'").fetchone()[0]], importance=100)
    seen = []
    def handler(request):
        assert not store._writer.in_transaction
        body = json.loads(request.content)
        payload = json.loads(body['messages'][1]['content'])
        seen.append(payload)
        assert body['reasoning_effort'] == 'high'
        assert other not in [int(c['id']) for c in payload['candidates']]
        return httpx.Response(200, json=response(scores([c['id'] for c in payload['candidates']], [49, 50])))
    with gateway(store, handler) as g:
        retrieval = Retrieval(store, g)
        baseline = retrieval.prepare('A', text='摄影', participants=['来客'], memory_limit=3, judge=False)
        relevant = [m['id'] for m in baseline['memories'] if m['reason'] == 'relevant']
        # Limit to two relevant memories, with a separately selected person highlight.
        assert len(relevant) == 3  # Run the narrower selection below using a controlled limit.
        def rank(*args, **kwargs):
            with store.read() as conn:
                items = list(retrieval._hydrate(conn, relevant[:2] + [other]).values())
            for m in items:
                m['reason'] = 'person_highlight' if m['id'] == other else 'relevant'
            return items
        retrieval._rank = rank
        reply = retrieval.prepare('A', text='摄影', participants=['来客'], memory_limit=3)
        assert [m['id'] for m in reply['memories']] == [relevant[1], other]
        assert reply['judgment']['status'] == 'applied'
        assert reply['judgment']['removed_memory_ids'] == [relevant[0]]
        with store.read() as conn:
            assert {r[0] for r in conn.execute('SELECT memory_id FROM recall_items WHERE recall_id=?', (reply['recall_id'],))} == {relevant[1], other}
            assert json.loads(conn.execute('SELECT request_json FROM recalls WHERE id=?', (reply['recall_id'],)).fetchone()[0])['judgment'] == reply['judgment']
        with pytest.raises(ValueError):
            retrieval.feedback(reply['recall_id'], [relevant[0]])
        assert retrieval.feedback(reply['recall_id'], [relevant[1]])['strengthened'] == [relevant[1]]
        assert len(seen) == 1


def test_disabled_search_and_empty_do_not_call(store):
    entry(store)
    put(store, '我喜欢天文摄影')
    with gateway(store, lambda request: pytest.fail('unexpected model call')) as g:
        r = Retrieval(store, g)
        assert r.prepare('A', text='摄影', judge=False)['judgment']['status'] == 'disabled'
        assert r.search(text='摄影')['memories']
        assert r.prepare('A', text='不存在的航班', participants=[])['judgment']['reason'] == 'no_candidates'
        store.set_setting('recall_judge', {'enabled': False})
        assert r.prepare('A', text='摄影')['judgment']['reason'] == 'configuration_disabled'


@pytest.mark.parametrize('change', ['revision', 'forgotten', 'deleted', 'context'])
def test_recheck_after_call(store, change):
    entry(store)
    source = msg(store, 2, '镜头要防潮', entry='A')
    mid = put(store, '摄影器材要防潮', evidence=[source])
    # The source starts outside the returned recent window.
    msg(store, 3, '说说摄影器材', entry='A')
    def handler(request):
        if change == 'revision':
            edit_memory(store, mid, 1, content='摄影器材换了')
        elif change == 'context':
            msg(store, 4, '新的问题', entry='A')
        else:
            with store.write() as conn:
                conn.execute('UPDATE memories SET lifecycle=? WHERE id=?', (change, mid))
        return httpx.Response(200, json=response(scores([mid], [0])))
    with gateway(store, handler) as g:
        reply = Retrieval(store, g).prepare('A', text='摄影器材', participants=[], recent_limit=1)
        assert reply['judgment']['status'] == 'degraded'
        assert [m['id'] for m in reply['memories']] == ([mid] if change == 'context' else [])
        with store.read() as conn:
            assert conn.execute('SELECT COUNT(*) FROM recall_items WHERE recall_id=?', (reply['recall_id'],)).fetchone()[0] == len(reply['memories'])


def test_invalid_json_degrades_without_repair_and_records_usage(store):
    entry(store)
    mid = put(store, '摄影器材要防潮')
    with gateway(store, lambda request: httpx.Response(200, json=response('{}', usage={'prompt_tokens': 10, 'completion_tokens': 2}))) as g:
        reply = Retrieval(store, g).prepare('A', text='摄影器材', participants=[])
        assert [m['id'] for m in reply['memories']] == [mid]
        assert reply['judgment']['reason'] == 'invalid_output'
    with store.read() as conn:
        rows = conn.execute('SELECT purpose,model_kind,result_category,prompt_tokens,completion_tokens FROM model_calls').fetchall()
        assert [tuple(row) for row in rows] == [('recall_judge', 'recall_judge', 'invalid_output', 10, 2)]


def test_rate_limit_immediate_pause_independent_and_probe_recovery(store):
    stamp = [utc_now()]
    configs = {'chat': ModelConfig('https://example.invalid/v1', '', 'stub')}
    health = ModelHealth(store, configs, clock=lambda: stamp[0])
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={'Retry-After': '90'}, json={'error': {'code': 'RateLimitExceeded'}})
        return httpx.Response(200, json=response('{"ok":true}'))
    with gateway(store, handler, health=health) as g:
        with pytest.raises(ModelError):
            g.recall_judge([], ['1'], budget_seconds=1)
        assert len(calls) == 1
        assert health.snapshot()['chat']['state'] == 'normal'
        assert health.snapshot()['recall_judge']['state'] == 'temporarily_unavailable'
        assert 'recall_judge' not in health.due_probes()
        stamp[0] += timedelta(seconds=91)
        assert g.probe('recall_judge')
        assert health.snapshot()['recall_judge']['state'] == 'normal'


def test_concurrency_queue_timeout_and_late_call_keeps_slot(store):
    active = threading.Event()
    release = threading.Event()
    calls = []
    def handler(request):
        calls.append(request)
        active.set()
        release.wait(2)
        return httpx.Response(200, json=response(scores([1])))
    with gateway(store, handler) as g, ThreadPoolExecutor(2) as pool:
        first = pool.submit(g.recall_judge, [], ['1'], budget_seconds=.12)
        assert active.wait(1)
        with pytest.raises(ModelError, match='queue_timeout'):
            g.recall_judge([], ['1'], budget_seconds=.03)
        with pytest.raises(ModelError):
            first.result()
        # Underlying HTTP is still in flight; timeout must not release its slot.
        with pytest.raises(ModelError, match='queue_timeout'):
            g.recall_judge([], ['1'], budget_seconds=.03)
        assert len(calls) == 1
        release.set()


def test_queue_bound_budget_and_account_code_precedence(store):
    store.set_setting('recall_judge', {'concurrency': 1, 'queue_limit': 0})
    release, entered = threading.Event(), threading.Event()
    def handler(request):
        entered.set()
        release.wait(1)
        return httpx.Response(200, json=response(scores([1])))
    with gateway(store, handler) as g, ThreadPoolExecutor(1) as pool:
        first = pool.submit(g.recall_judge, [], ['1'])
        assert entered.wait(1)
        with pytest.raises(ModelError, match='queue_full'):
            g.recall_judge([], ['1'])
        release.set()
        assert first.result()['scores'] == [100]
    configs = {'chat': ModelConfig('https://example.invalid/v1', '', 'stub')}
    health = ModelHealth(store, configs)
    with gateway(store, lambda request: httpx.Response(429, json={'error': {'code': 'QuotaExceeded'}}), health=health) as g:
        with pytest.raises(ModelError) as exc:
            g.recall_judge([], ['1'])
        assert exc.value.category == 'account'
        assert health.snapshot()['recall_judge']['state'] == 'account_problem'
        assert health.learning_allowed()


def test_daily_limit_and_chat_pause_are_independent(store):
    configs = {'chat': ModelConfig('https://example.invalid/v1', '', 'stub', reasoning_effort='low')}
    health = ModelHealth(store, configs)
    token = health.check('chat', 'learning')
    health.observe('chat', token, 'account', 'HTTP 429')
    with gateway(store, lambda request: httpx.Response(200, json=response(scores([1]), usage={'prompt_tokens': 5, 'completion_tokens': 1})), health=health) as g:
        assert g.recall_judge([], ['1'])['scores'] == [100]
        health.set_daily_token_limit(1)
        with pytest.raises(ModelError) as exc:
            g.recall_judge([], ['1'])
        assert exc.value.reason == 'usage_limit'
        assert health.snapshot()['recall_judge']['state'] == 'usage_limit'
        health.set_daily_token_limit(None)
        assert health.snapshot()['recall_judge']['state'] == 'normal'


def test_config_inheritance_and_override_do_not_share_pause(store):
    from dataclasses import replace
    chat = ModelConfig('https://example.invalid/v1', '', 'first', reasoning_effort='low')
    health = ModelHealth(store, {'chat': chat})
    assert health.configs['recall_judge'].reasoning_effort == 'high'
    token = health.check('recall_judge', 'recall_judge')
    health.observe('recall_judge', token, 'authentication', 'HTTP 401')
    assert health.learning_allowed()
    health.replace_config('chat', replace(chat, model='second'))
    assert health.configs['recall_judge'].model == 'second'
    assert health.snapshot()['recall_judge']['state'] == 'normal'
    health.replace_config('recall_judge', replace(chat, model='separate', reasoning_effort='low'))
    health.replace_config('chat', replace(chat, model='third'))
    assert health.configs['recall_judge'].model == 'separate'
    health.replace_config('recall_judge', None)
    assert health.configs['recall_judge'].model == 'third'


def test_host_api_and_management_settings(store):
    from fastapi.testclient import TestClient
    from conftest import login_admin
    from iris.api import create_app
    entry(store)
    mid = put(store, '我喜欢天文摄影')
    with TestClient(create_app(store=store), base_url='http://127.0.0.1', client=('127.0.0.1', 12345)) as client:
        login_admin(client)
        response = client.post('/api/v1/entries/A/prepare', json={'text': '天文摄影', 'judge': False})
        assert response.status_code == 200
        assert response.json()['judgment']['status'] == 'disabled'
        assert response.json()['memories'][0]['id'] == mid
        for value in (0, 11, True, '1'):
            assert client.post('/api/v1/entries/A/prepare', json={'judge_budget_seconds': value}).status_code == 400
        assert client.post('/api/v1/entries/A/prepare', json={'judge': 'false'}).status_code == 400
        response = client.patch('/admin/api/settings/recall-judge', json={'enabled': False, 'queue_limit': 0})
        assert response.status_code == 200
        assert response.json()['recall_judge'] == {'enabled': False, 'concurrency': 1, 'queue_limit': 0}
        response = client.put('/admin/api/settings/models/recall_judge', json={'base_url': 'https://example.invalid/v1', 'model': 'independent'})
        assert response.status_code == 200
        assert response.json()['models']['recall_judge']['reasoning_effort'] == 'high'
        assert not response.json()['models']['recall_judge']['inherited']
        response = client.put('/admin/api/settings/models/recall_judge', json={'enabled': False})
        assert response.json()['models']['recall_judge']['inherited']


def test_fresh_host_context_removes_newly_known_source(store):
    entry(store)
    source = msg(store, 2, '这次摄影地点在南山')
    msg(store, 3, '告诉我摄影地点')
    mid = put(store, '摄影地点在南山', evidence=[source])
    def handler(request):
        # Move source into the one-message host context during model latency.
        with store.write() as conn:
            conn.execute('DELETE FROM messages WHERE id > ?', (source,))
        return httpx.Response(200, json=response(scores([mid])))
    with gateway(store, handler) as g:
        reply = Retrieval(store, g).prepare('A', text='摄影地点', recent_limit=1, participants=[])
        assert reply['memories'] == []
        assert reply['judgment']['status'] == 'degraded'
        assert reply['judgment']['stale_memory_ids'] == [mid]


@pytest.mark.parametrize('body', [[], {'choices': [True]}, {'choices': [{'message': []}]},
                                  {'choices': [{'message': {'content': '{}'}, 'finish_reason': 'length'}]}])
def test_malformed_provider_envelope_degrades(store, body):
    entry(store)
    mid = put(store, '摄影器材要防潮')
    with gateway(store, lambda request: httpx.Response(200, json=body)) as g:
        reply = Retrieval(store, g).prepare('A', text='摄影器材', participants=[])
        assert reply['judgment']['reason'] == 'invalid_output'
        assert [m['id'] for m in reply['memories']] == [mid]


def test_actual_limit_can_be_two_without_affecting_chat_pool(store):
    store.set_setting('recall_judge', {'concurrency': 2, 'queue_limit': 0})
    entered = threading.Barrier(3)
    release = threading.Event()
    def handler(request):
        body = json.loads(request.content)
        if body.get('temperature') == 0:
            entered.wait(2)
            release.wait(2)
            return httpx.Response(200, json=response(scores([1])))
        return httpx.Response(200, json=response('{"ok":true}'))
    with gateway(store, handler) as g, ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(g.recall_judge, [], ['1']) for _ in range(2)]
        entered.wait(2)
        assert g.chat([], 'trial_reply').content == '{"ok":true}'
        with pytest.raises(ModelError, match='queue_full'):
            g.recall_judge([], ['1'])
        release.set()
        assert all(f.result()['scores'] == [100] for f in futures)


def test_network_error_pause_and_daily_limit_skip_probe(store):
    stamp = [utc_now()]
    health = ModelHealth(store, {'chat': ModelConfig('https://example.invalid/v1', '', 'stub')}, clock=lambda: stamp[0])
    def handler(request):
        raise httpx.ConnectError('network')
    with gateway(store, handler, health=health) as g:
        for _ in range(3):
            with pytest.raises(ModelError):
                g.recall_judge([], ['1'])
        assert health.snapshot()['recall_judge']['state'] == 'temporarily_unavailable'
        assert health.learning_allowed()
        stamp[0] += timedelta(seconds=61)
        assert 'recall_judge' in health.due_probes()
        g._record('test', 'stub', 1, 'success', None, {'prompt_tokens': 5})
        health.set_daily_token_limit(1)
        assert 'recall_judge' not in health.due_probes()
        health.set_daily_token_limit(None)
        store.set_setting('recall_judge', {'enabled': False})
        assert 'recall_judge' not in health.due_probes()
