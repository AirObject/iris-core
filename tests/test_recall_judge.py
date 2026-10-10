"""Recall judgment uses fake transport; no real model or corpus labels."""
from conftest import authorize_host
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
    return closing(Gateway(config, store, health=health, clock=health.clock if health else None,
                           client=httpx.Client(transport=httpx.MockTransport(handler))))


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


@pytest.mark.parametrize('change', ['revision', 'forgotten', 'deleted', 'context', 'alias'])
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
        elif change == 'alias':
            with store.write() as conn:
                conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES('self','星点')")
        else:
            with store.write() as conn:
                conn.execute('UPDATE memories SET lifecycle=? WHERE id=?', (change, mid))
        return httpx.Response(200, json=response(scores([mid], [0])))
    with gateway(store, handler) as g:
        reply = Retrieval(store, g).prepare('A', text='摄影器材', participants=[], recent_limit=1)
        assert reply['judgment']['status'] == 'applied'
        assert reply['memories'] == []
        assert reply['judgment'].get('stale_memory_ids', []) == ([] if change in ('context', 'alias') else [mid])
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
    from fake_openai import Clock
    configs = {'chat': ModelConfig('https://example.invalid/v1', '', 'stub', reasoning_effort='low')}
    health = ModelHealth(store, configs, clock=Clock())
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
        authorize_host(client)
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
        assert reply['judgment']['status'] == 'applied'
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
    from fake_openai import Clock
    clock = Clock()
    health = ModelHealth(store, {'chat': ModelConfig('https://example.invalid/v1', '', 'stub')}, clock=clock)
    def handler(request):
        raise httpx.ConnectError('network')
    with gateway(store, handler, health=health) as g:
        for _ in range(3):
            with pytest.raises(ModelError):
                g.recall_judge([], ['1'])
        assert health.snapshot()['recall_judge']['state'] == 'temporarily_unavailable'
        assert health.learning_allowed()
        clock.advance(61)  # Cross role midnight before recording the usage.
        assert 'recall_judge' in health.due_probes()
        g._record('test', 'stub', 1, 'success', None, {'prompt_tokens': 5})
        health.set_daily_token_limit(1)
        assert 'recall_judge' not in health.due_probes()
        health.set_daily_token_limit(None)
        store.set_setting('recall_judge', {'enabled': False})
        assert 'recall_judge' not in health.due_probes()


@pytest.mark.parametrize(('kind', 'message'), [
    ('chat', '学习已暂停，消息仍正常接收。'),
    ('embedding', 'embedding 已暂停，使用全文检索。'),
    ('recall_judge', '召回判断已暂停，保留原召回。'),
])
def test_status_budget_and_pause_hints_keep_purposes_separate(store, kind, message):
    from types import SimpleNamespace
    from iris.service_status import add_health_hints, service_status
    configs = {purpose: ModelConfig('https://example.invalid/v1', '', 'stub')
               for purpose in ('chat', 'embedding')}
    health = ModelHealth(store, configs)
    token = health.check(kind, kind)
    health.observe(kind, token, 'authentication', 'HTTP 401')
    result = add_health_hints({'hints': []}, health)
    assert len(result['hints']) == 1
    assert result['hints'][0]['kind'] == kind
    assert result['hints'][0]['message'] == message
    assert result['hints'][0]['state'] == 'invalid_key'
    status = service_status(store, SimpleNamespace(running=True, last_error=None), health)
    assert status['model_health'][kind]['state'] == 'invalid_key'
    assert all(state['state'] == 'normal' for purpose, state in status['model_health'].items()
               if purpose != kind)
    assert status['timeouts_seconds']['recall_judge'] == 10
    assert status['timeouts_seconds']['learning'] == 180
    assert status['timeouts_seconds']['judge'] == 240


@pytest.mark.parametrize('failure', ['rate_limit', 'invalid_output'])
def test_api_reports_recall_pause_and_per_request_degradation(store, failure):
    from fastapi.testclient import TestClient
    from iris.api import create_app
    entry(store)
    mid = put(store, '摄影器材要防潮')
    health = ModelHealth(store, {'chat': ModelConfig('https://example.invalid/v1', '', 'stub')})
    calls = []
    def handler(request):
        calls.append(request)
        if failure == 'rate_limit':
            return httpx.Response(429, json={'error': {'code': 'RateLimitExceeded'}})
        return httpx.Response(200, json=response('{}'))
    with gateway(store, handler, health=health) as g:
        with TestClient(create_app(store=store, gateway=g), base_url='http://127.0.0.1') as client:
            authorize_host(client)
            reply = client.post('/api/v1/entries/A/prepare', json={'text': '摄影器材', 'participants': []})
            assert reply.status_code == 200
            result = reply.json()
            assert [m['id'] for m in result['memories']] == [mid]
            hint = next(h for h in result['hints'] if h['code'] == 'recall_judgment')
            assert hint['status'] == 'degraded'
            assert hint['reason'] == result['judgment']['reason']
            assert hint['budget_seconds'] == 10
            assert hint['message'] == '召回判断降级，保留仍有效的原候选。'
            paused = [h for h in result['hints'] if h['code'] == 'model_paused' and h['kind'] == 'recall_judge']
            if failure == 'rate_limit':
                assert len(paused) == 1
                assert paused[0]['message'] == '召回判断限流退避中，保留原召回。'
                second = client.post('/api/v1/entries/A/prepare', json={'text': '摄影器材', 'participants': []}).json()
                assert second['judgment']['reason'] == 'rate_limited'
            else:
                assert not paused
                assert hint['reason'] == 'invalid_output'
            assert all('学习已暂停' not in h.get('message', '') for h in result['hints'])
            status = client.get('/api/v1/status').json()
            assert status['model_health']['chat']['state'] == 'normal'
            assert status['model_health']['recall_judge']['state'] == (
                'rate_limited' if failure == 'rate_limit' else 'normal')
            assert status['timeouts_seconds']['recall_judge'] == 10
            assert len(calls) == 1


@pytest.mark.parametrize('change', ['revision', 'forgotten', 'deleted', 'source', 'disabled'])
def test_stale_candidate_does_not_restore_other_rejected_candidates(store, change):
    entry(store)
    source = msg(store, 2, '摄影支架放在储藏室')
    msg(store, 3, '摄影有什么要提醒的')
    stale = put(store, '摄影支架放在储藏室', evidence=[source])
    rejected = put(store, '摄影灯的电池需要更换')
    kept = put(store, '摄影出行计划周六讨论')
    def handler(request):
        payload = json.loads(json.loads(request.content)['messages'][1]['content'])
        ids = [c['id'] for c in payload['candidates']]
        assert set(ids) == {str(stale), str(rejected), str(kept)}
        if change in ('revision', 'disabled'):
            edit_memory(store, stale, 1, content='摄影支架已经送修')
        elif change == 'source':
            with store.write() as conn:
                conn.execute('DELETE FROM messages WHERE id > ?', (source,))
        else:
            with store.write() as conn:
                conn.execute('UPDATE memories SET lifecycle=? WHERE id=?', (change, stale))
        if change == 'disabled':
            store.set_setting('recall_judge', {'enabled': False})
        return httpx.Response(200, json=response(scores(ids, [0 if mid == str(rejected) else 100 for mid in ids])))
    with gateway(store, handler) as g:
        r = Retrieval(store, g)
        reply = r.prepare('A', text='摄影', participants=[], recent_limit=1)
        diagnostic = reply['judgment']
        assert diagnostic['status'] == ('disabled' if change == 'disabled' else 'applied')
        assert diagnostic['stale_memory_ids'] == [stale]
        assert diagnostic['removed_memory_ids'] == ([] if change == 'disabled' else [rejected])
        assert {m['id'] for m in reply['memories']} == ({kept, rejected} if change == 'disabled' else {kept})
        with store.read() as conn:
            recorded = {row[0] for row in conn.execute('SELECT memory_id FROM recall_items WHERE recall_id=?', (reply['recall_id'],))}
        assert recorded == {m['id'] for m in reply['memories']}
        with pytest.raises(ValueError):
            r.feedback(reply['recall_id'], [stale])
        if change != 'disabled':
            with pytest.raises(ValueError):
                r.feedback(reply['recall_id'], [rejected])


@pytest.mark.parametrize('text', [None, '摄影器材怎么保管'])
def test_arriving_messages_keep_frozen_query_and_judgment(store, text):
    entry(store)
    msg(store, 2, '摄影器材怎么保管')
    mid = put(store, '摄影器材放在干燥箱')
    seen = []
    def handler(request):
        seen.append(json.loads(json.loads(request.content)['messages'][1]['content']))
        msg(store, 3, '来客说今晚改聊烘焙')
        return httpx.Response(200, json=response(scores([mid], [0])))
    with gateway(store, handler) as g:
        r = Retrieval(store, g)
        frozen, _ = r.prepare_query('A', text)
        result = r.prepare('A', text=text, participants=[])
        assert seen[0]['retrieval_query'] == frozen
        assert result['recent_messages'][-1]['content'] == '来客说今晚改聊烘焙'
        assert result['judgment']['status'] == 'applied'
        assert result['judgment']['removed_memory_ids'] == [mid]
        assert result['memories'] == []


@pytest.mark.parametrize('automatic', [False, True])
def test_judgment_alias_payload_only_contains_related_subjects(store, automatic):
    entry(store)
    people = [('speaker', '讲述者', '讲讲'), ('about', '涉及者', '阿及'),
              ('participant', '在场者', '候场'), ('named1', '点名甲', '小舟'),
              ('named2', '点名乙', '小舟'), ('highlight', '背景人物', '背景'),
              ('unrelated', '无关人物', '陌生')]
    with store.write() as conn:
        for sid, name, alias in people:
            conn.execute("INSERT INTO subjects(id,kind,name,created_at) VALUES(?,'person',?,?)", (sid, name, utc_now().isoformat()))
            conn.execute('INSERT INTO subject_aliases(subject_id,alias) VALUES(?,?)', (sid, alias))
        sender = conn.execute("SELECT sender_subject_id FROM messages WHERE entry_id='A'").fetchone()[0]
        conn.execute("INSERT INTO subject_aliases(subject_id,alias) VALUES(?,'访客')", (sender,))
    msg(store, 2, '小舟对摄影有什么建议', sender='来客')
    mid = put(store, '小舟与我们讨论过摄影', speaker='speaker', about=['about'])
    highlight = put(store, '背景人物喜欢烘焙', speaker='highlight', about=['highlight'])
    seen = []
    def handler(request):
        seen.append(json.loads(json.loads(request.content)['messages'][1]['content']))
        return httpx.Response(200, json=response(scores([mid])))
    with gateway(store, handler) as g:
        r = Retrieval(store, g)
        def rank(*args, **kwargs):
            with store.read() as conn:
                rows = r._hydrate(conn, [mid, highlight])
            rows[mid]['reason'], rows[highlight]['reason'] = 'relevant', 'person_highlight'
            return [rows[mid], rows[highlight]]
        r._rank = rank
        r.prepare('A', text=None if automatic else '小舟对摄影有什么建议', participants=None if automatic else ['候场'])
    actual = {row['name']: row['aliases'] for row in seen[0]['subject_aliases']}
    expected = {'讲述者': ['讲讲'], '涉及者': ['阿及'],
                '来客': ['访客'], '点名甲': ['小舟'], '点名乙': ['小舟']}
    if not automatic:
        expected['在场者'] = ['候场']
    assert actual == expected


@pytest.mark.parametrize(('header', 'delay'), [(None, 2), ('7', 7), ('90', 90), ('date', 7), ('invalid', 2), ('0', 0)])
def test_rate_limit_cooldown_honors_header_and_skips_admission(store, header, delay):
    from email.utils import format_datetime
    from fake_openai import Clock
    clock = Clock()
    entry(store)
    mid = put(store, '摄影器材要防潮')
    configs = {'chat': ModelConfig('https://example.invalid/v1', '', 'stub')}
    health = ModelHealth(store, configs, clock=clock)
    calls = []
    if header == 'date':
        header = format_datetime(clock() + timedelta(seconds=delay), usegmt=True)
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={'Retry-After': header} if header is not None else {},
                                  json={'error': {'code': 'RateLimitExceeded'}})
        return httpx.Response(200, json=response(scores([mid])))
    with closing(Gateway(configs, store, health=health, clock=clock,
                         client=httpx.Client(transport=httpx.MockTransport(handler)))) as g:
        first = Retrieval(store, g).prepare('A', text='摄影器材', participants=[])
        assert first['judgment']['reason'] == 'rate_limited'
        assert health.snapshot()['chat']['state'] == 'normal'
        assert 'recall_judge' not in health.due_probes()
        original = g._judge_admission.acquire
        g._judge_admission.acquire = lambda *a, **kw: pytest.fail('cooldown must not queue')
        if delay:
            state = health.snapshot()['recall_judge']
            assert state['state'] == 'rate_limited'
            assert state['retry_at'] == (clock() + timedelta(seconds=delay)).isoformat()
            restored = ModelHealth(store, configs, clock=clock)
            assert restored.snapshot()['recall_judge']['retry_at'] == state['retry_at']
            second = Retrieval(store, g).prepare('A', text='摄影器材', participants=[])
            assert second['judgment']['reason'] == 'rate_limited'
            assert second['judgment']['queue_ms'] == 0
            assert len(calls) == 1
        clock.advance(delay)
        assert health.snapshot()['recall_judge']['state'] == 'normal'
        assert not g.probe('recall_judge')  # A new business call resumes without a probe.
        g._judge_admission.acquire = original
        assert g.recall_judge([], [str(mid)])['scores'] == [100]
        assert health.snapshot()['recall_judge']['consecutive_rate_limits'] == 0
        assert len(calls) == 2


@pytest.mark.parametrize('third_header', [None, '3', '120'])
def test_repeated_rate_limits_enter_existing_probe_backoff(store, third_header):
    from fake_openai import Clock
    clock = Clock()
    configs = {'chat': ModelConfig('https://example.invalid/v1', '', 'stub')}
    health = ModelHealth(store, configs, clock=clock)
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) <= 4:
            headers = {'Retry-After': third_header} if len(calls) == 3 and third_header else {}
            return httpx.Response(429, headers=headers, json={'error': {'code': 'RateLimitExceeded'}})
        return httpx.Response(200, json=response('{"ok":true}'))
    with closing(Gateway(configs, store, health=health, clock=clock,
                         client=httpx.Client(transport=httpx.MockTransport(handler)))) as g:
        for delay in (2, 4):
            with pytest.raises(ModelError):
                g.recall_judge([], ['1'])
            state = health.snapshot()['recall_judge']
            assert state['state'] == 'rate_limited'
            assert state['retry_at'] == (clock() + timedelta(seconds=delay)).isoformat()
            clock.advance(delay)
        with pytest.raises(ModelError):
            g.recall_judge([], ['1'])
        delay = max(60, int(third_header or 0))
        state = health.snapshot()['recall_judge']
        assert state['state'] == 'temporarily_unavailable'
        assert state['consecutive_errors'] == 3
        assert state['next_probe_at'] == (clock() + timedelta(seconds=delay)).isoformat()
        assert health.learning_allowed()
        clock.advance(delay)
        assert not g.probe('recall_judge')
        assert health.snapshot()['recall_judge']['probe_delay_seconds'] == 120
        clock.advance(120)
        assert g.probe('recall_judge')
        assert health.snapshot()['recall_judge']['state'] == 'normal'
        assert health.snapshot()['recall_judge']['consecutive_rate_limits'] == 0


def test_inflight_success_cannot_cancel_rate_limit_cooldown(store):
    from fake_openai import Clock
    clock = Clock()
    configs = {'chat': ModelConfig('https://example.invalid/v1', '', 'stub')}
    health = ModelHealth(store, configs, clock=clock)
    token = health.check('recall_judge', 'recall_judge')
    health.observe('recall_judge', token, 'retryable', 'HTTP 429', rate_limited=True)
    health.observe('recall_judge', token, 'success')
    assert health.snapshot()['recall_judge']['state'] == 'rate_limited'
    assert health.snapshot()['recall_judge']['consecutive_rate_limits'] == 1
    with pytest.raises(ModelError, match='HTTP 429'):
        health.check('recall_judge', 'recall_judge')
    clock.advance(2)
    token = health.check('recall_judge', 'recall_judge')
    health.observe('recall_judge', token, 'success')
    assert health.snapshot()['recall_judge']['state'] == 'normal'
    assert health.snapshot()['recall_judge']['consecutive_errors'] == 0


def test_later_inflight_rate_limits_cannot_shorten_retry_after(store):
    from fake_openai import Clock
    clock = Clock()
    health = ModelHealth(store, {'chat': ModelConfig('https://example.invalid/v1', '', 'stub')}, clock=clock)
    token = health.check('recall_judge', 'recall_judge')
    health.observe('recall_judge', token, 'retryable', 'HTTP 429', rate_limited=True, retry_after=90)
    until = health.snapshot()['recall_judge']['retry_at']
    clock.advance(1)
    health.observe('recall_judge', token, 'retryable', 'HTTP 429', rate_limited=True, retry_after=1)
    assert health.snapshot()['recall_judge']['retry_at'] == until
    health.observe('recall_judge', token, 'retryable', 'HTTP 429', rate_limited=True, retry_after=1)
    assert health.snapshot()['recall_judge']['state'] == 'temporarily_unavailable'
    assert health.snapshot()['recall_judge']['next_probe_at'] == until


@pytest.mark.parametrize('category', ['invalid_output', 'content_rejection'])
def test_nonretryable_result_resets_limit_streak_without_ending_cooldown(store, category):
    from fake_openai import Clock
    clock = Clock()
    health = ModelHealth(store, {'chat': ModelConfig('https://example.invalid/v1', '', 'stub')}, clock=clock)
    token = health.check('recall_judge', 'recall_judge')
    health.observe('recall_judge', token, 'retryable', 'HTTP 429', rate_limited=True)
    until = health.snapshot()['recall_judge']['retry_at']
    health.observe('recall_judge', token, category)
    state = health.snapshot()['recall_judge']
    assert state['state'] == 'rate_limited' and state['retry_at'] == until
    assert state['consecutive_errors'] == state['consecutive_rate_limits'] == 0
    clock.advance(2)
    token = health.check('recall_judge', 'recall_judge')
    health.observe('recall_judge', token, 'retryable', 'HTTP 429', rate_limited=True)
    assert health.snapshot()['recall_judge']['retry_at'] == (clock() + timedelta(seconds=2)).isoformat()
