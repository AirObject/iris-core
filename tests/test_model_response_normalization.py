"""Provider reasoning prefixes must never reach raw-output persistence."""
import json

import httpx
import pytest

from conftest import batch, learning_output, msg
from iris.models import Gateway, ModelConfig, ModelError, parse_json_object
from test_models import response


@pytest.mark.parametrize('content, native, expected, stripped, chars', [
    (' \n{"ok":true}\n ', 'private-native', ' \n{"ok":true}\n ', False, 14),
    ('<think>private-prefix</think>\n{"ok":true}', None, '{"ok":true}', True, 14),
    (' \n<think>秘密推理</think> \n{"ok":true}', 'native', '{"ok":true}', True, 10),
    ('<think></think>{"ok":true}', None, '{"ok":true}', True, 0),
    ('<think>unfinished {"ok":true}', None, '<think>unfinished {"ok":true}', False, 0),
    ('正文中间 <think>literal</think> 不剥离', None, '正文中间 <think>literal</think> 不剥离', False, 0),
    ('<think>first</think><think>second</think>{"ok":true}', None,
     '<think>second</think>{"ok":true}', True, 5),
])
def test_reasoning_prefix_only_changes_one_complete_leading_block(store, content, native, expected, stripped, chars):
    body = response(content)
    if native is not None:
        body['choices'][0]['message']['reasoning_content'] = native
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))) as client:
        gateway = Gateway({'chat': ModelConfig('https://fake.invalid/v1', '', 'fake')}, store, client=client)
        try:
            assert gateway.chat([{'role': 'user', 'content': 'test'}], 'test').content == expected
        finally:
            gateway.close()
    with store.read() as conn:
        call = dict(conn.execute('SELECT * FROM model_calls').fetchone())
        assert call['reasoning_present'] == bool(native is not None or stripped)
        assert call['reasoning_chars'] == chars
        assert call['leading_think_stripped'] == stripped
        saved = '\n'.join(conn.iterdump())
        assert 'private-native' not in saved and 'private-prefix' not in saved


def test_json_string_with_think_in_the_middle_is_preserved():
    value = {'content': '正文 <think>普通引用</think> 保留'}
    assert parse_json_object(json.dumps(value, ensure_ascii=False)) == value


@pytest.mark.parametrize('kind', ['recall_judge', 'goal_dedup_judge', 'image_understanding'])
def test_prefix_is_removed_before_purpose_validation(store, kind):
    contents = {'recall_judge': '{"scores":[{"id":"1","support":100}]}',
                'goal_dedup_judge': '{"decisions":[{"id":1,"verdict":"different"}]}',
                'image_understanding': '红色方块。'}
    raw = '<think>private-prefix</think>\n' + contents[kind]
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response(raw)))) as client:
        gateway = Gateway({'chat': ModelConfig('https://fake.invalid/v1', '', 'fake', vision=True)}, store, client=client)
        try:
            if kind == 'recall_judge':
                assert gateway.recall_judge([], ['1'])['scores'] == [100]
            elif kind == 'goal_dedup_judge':
                assert gateway.goal_dedup_judge([], [1])['decisions'] == {1: 'different'}
            else:
                assert gateway.image_understanding(b'image', 'image/png') == contents[kind]
        finally:
            gateway.close()
    with store.read() as conn:
        call = dict(conn.execute('SELECT * FROM model_calls').fetchone())
        assert call['result_category'] == 'success'
        assert call['model_kind'] == kind and call['leading_think_stripped'] == 1


def test_unclosed_prefix_still_fails_strict_judgment(store):
    raw = '<think>unfinished {"scores":[{"id":"1","support":100}]}'
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response(raw)))) as client:
        gateway = Gateway({'chat': ModelConfig('https://fake.invalid/v1', '', 'fake')}, store, client=client)
        try:
            with pytest.raises(ModelError) as caught:
                gateway.recall_judge([], ['1'])
            assert caught.value.category == 'invalid_output'
        finally:
            gateway.close()
    with store.read() as conn:
        assert conn.execute('SELECT leading_think_stripped FROM model_calls').fetchone()[0] == 0


def test_learning_raw_and_repair_outputs_and_requests_never_retain_stripped_reasoning(store):
    msg(store, 1, '我喜欢茶。')
    msg(store, 2, '下次再一起喝茶。')
    outputs = ['<think>private-first {"bad":true}</think>not-json',
               '<think>private-repair</think>\n' + json.dumps(learning_output({}))]
    requests = []
    def handler(request):
        assert not store._writer.in_transaction
        requests.append(request.content.decode())
        return httpx.Response(200, json=response(outputs.pop(0)))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = Gateway({'chat': ModelConfig('https://fake.invalid/v1', '', 'fake')}, store, client=client)
        try:
            formed, _ = batch(store, gateway)
        finally:
            gateway.close()
    assert len(requests) == 2 and all('private-' not in r for r in requests)
    with store.read() as conn:
        row = conn.execute('SELECT raw_output,repair_output,parse_status FROM batch_attempts').fetchone()
        assert row['raw_output'] == 'not-json'
        assert json.loads(row['repair_output']) == learning_output({})
        assert row['parse_status'] == 'repaired'
        calls = conn.execute('SELECT batch_id,leading_think_stripped,reasoning_chars FROM model_calls ORDER BY id').fetchall()
        assert [tuple(c) for c in calls] == [(formed.id, 1, 26), (formed.id, 1, 14)]
        assert 'private-' not in '\n'.join(conn.iterdump())
    from iris.evaluation import _case_data
    assert all(call['leading_think_stripped'] == 1 for call in _case_data(store)['calls'])
