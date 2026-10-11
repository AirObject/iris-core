"""Native target attachments, fallbacks and durable metadata without image bytes."""
import base64
import json
from dataclasses import replace

import httpx
import pytest

from conftest import learning_output
from iris.learning import LearningEngine, PROMPT_VERSION
from iris.media import DEFAULT_MAX_BYTES, get_media
from iris.models import Gateway, ModelConfig
from iris.queue import form_batch
from test_batches import memory
from test_media import PNG, Vision, file_path, saved, send
from test_models import response


def native_gateway():
    gateway = Vision()
    gateway.configs['chat'] = replace(gateway.configs['chat'], vision=True)
    return gateway


def run(store, gateway, **kwargs):
    batch = form_batch(store, 'A', PROMPT_VERSION, **kwargs)
    assert batch
    result = LearningEngine(store, gateway).run_batch(batch.id)
    return batch, result


def text_parts(material):
    return ''.join(part['text'] for part in material if part['type'] == 'text')


def test_native_images_follow_message_order_without_descriptions_or_sync_calls(store):
    first = saved(store, understanding_text='不得放入学习的描述一')
    second = saved(store, understanding_text='不得放入学习的描述二')
    send(store, [first['id'], second['id']], content='看这两张，顺序如图')
    send(store, key='after', content='后一句')
    gateway = native_gateway()
    _, result = run(store, gateway)
    material = gateway.materials[0]
    assert isinstance(material, list)
    assert not gateway.images
    assert '不得放入学习的描述' not in text_parts(material)
    images = [i for i, part in enumerate(material) if part['type'] == 'image_url']
    assert len(images) == 2
    for number, index in enumerate(images, 1):
        assert f'[图片 {number}]' in material[index - 1]['text']
        assert base64.b64decode(material[index]['image_url']['url'].split(',', 1)[1]) == PNG
    assert text_parts(material).index('看这两张') < text_parts(material).index('[图片 1]')
    assert text_parts(material).index('[图片 2]') < text_parts(material).index('后一句')
    paths = result['media_attempts'][0]['images']
    assert [(p['media_id'], p['path']) for p in paths] == [(first['id'], 'native'), (second['id'], 'native')]


def test_only_targets_use_originals_and_limit_falls_back(store):
    history = saved(store, understanding_text='历史描述')
    send(store, [history['id']])
    run(store, native_gateway(), target_count=1)
    selected = saved(store, understanding_text='原图的描述不得附加')
    limited = saved(store, understanding_text='超限描述')
    placeholder = saved(store)
    send(store, [selected['id'], limited['id'], placeholder['id']], key='target', content='目标')
    future = saved(store, understanding_text='后续描述')
    send(store, [future['id']], key='future', content='后续')
    store.set_setting('learning_image_limit', 1)
    gateway = native_gateway()
    _, result = run(store, gateway, target_count=1, history_count=1, future_count=1)
    material = gateway.materials[0]
    assert len([p for p in material if p['type'] == 'image_url']) == 1
    text = text_parts(material)
    assert all(value in text for value in ('历史描述', '超限描述', '后续描述', '未理解'))
    assert '原图的描述不得附加' not in text
    assert not gateway.images
    paths = {p['media_id']: p for p in result['media_attempts'][0]['images']}
    assert paths[history['id']]['reason'] == paths[future['id']]['reason'] == 'context'
    assert paths[limited['id']]['path'] == 'description'
    assert paths[placeholder['id']]['path'] == 'placeholder'
    assert paths[limited['id']]['reason'] == 'batch_limit'


@pytest.mark.parametrize('failure', ['missing', 'too_large', 'refused', 'symlink'])
def test_unavailable_or_refused_original_is_never_sent(store, failure):
    item = saved(store)
    send(store, [item['id']], content='')
    path = file_path(store, item)
    if failure == 'missing':
        path.unlink()
    elif failure == 'symlink':
        target = store.path.parent / 'replacement.png'
        target.write_bytes(PNG)
        path.unlink()
        path.symlink_to(target)
    else:
        with store.write() as conn:
            if failure == 'too_large':
                conn.execute('UPDATE media_files SET size_bytes=?', (DEFAULT_MAX_BYTES + 1,))
            else:
                conn.execute("UPDATE media_files SET refused_at='2026-10-11T00:00:00Z'")
    gateway = native_gateway()
    _, result = run(store, gateway)
    assert isinstance(gateway.materials[0], str)
    assert not gateway.images
    route = result['media_attempts'][0]['images'][0]
    assert route['path'] == 'placeholder'
    assert route['reason'] == (failure if failure in ('too_large', 'refused') else 'unavailable')


def test_image_only_evidence_preserves_existing_speaker_boundary(store):
    item = saved(store)
    send(store, [item['id']], content='')
    gateway = native_gateway()
    gateway.response = {'memories': [memory('据小林展示的截图，第三人通过考试。', stance='转述'),
                                     memory('第三人通过考试。', speaker='截图署名者')]}
    _, result = run(store, gateway)
    assert len(result['created']) == 1
    assert result['dropped'][0]['reason'] == 'speaker reference is not in evidence'
    assert get_media(store, item['id'])['understanding_source'] == 'unprocessed'


def test_nonvision_still_generates_description_before_learning(store):
    item = saved(store)
    send(store, [item['id']])
    gateway = Vision()
    _, result = run(store, gateway)
    assert len(gateway.images) == 1
    assert isinstance(gateway.materials[0], str)
    assert gateway.result in gateway.materials[0]
    assert result['media_attempts'][0]['images'][0]['path'] == 'description'


@pytest.mark.parametrize('fail_repair', [False, True])
@pytest.mark.parametrize('first_kind', ['syntax', 'shape'])
def test_repair_retains_images_but_attempts_and_calls_do_not_store_base64(store, fail_repair, first_kind):
    item = saved(store)
    send(store, [item['id']], content='')
    seen = []
    encoded = base64.b64encode(PNG).decode()
    def handler(request):
        assert not store._writer.in_transaction
        payload = json.loads(request.content)
        seen.append(payload)
        if len(seen) == 1 or fail_repair:
            echo = 'data:image/png;base64,' + encoded
            output = ('not JSON ' + echo if first_kind == 'syntax'
                      else json.dumps({'memories': echo}))
            return httpx.Response(200, json=response(output))
        return httpx.Response(200, json=response(json.dumps(learning_output({}))))
    configs = {'chat': ModelConfig('https://example.invalid/v1', '', 'fake', vision=True)}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = Gateway(configs, store, client=client)
        try:
            batch, result = run(store, gateway)
        finally:
            gateway.close()
    assert len(seen) == 2
    assert seen[0]['messages'][1] == seen[1]['messages'][1]
    assert encoded not in seen[1]['messages'][-2]['content']
    with store.read() as conn:
        assert encoded not in '\n'.join(conn.iterdump())
        record = json.loads(conn.execute('SELECT result_json FROM batches WHERE id=?', (batch.id,)).fetchone()[0])
    assert record['media_attempts'][0]['images'][0]['sha256'] == item['sha256']
    assert record['media_attempts'][0]['images'][0]['path'] == 'native'


@pytest.mark.parametrize('limit, expected', [(None, 8), (0, 0)])
def test_default_image_limit_and_zero_fallback(store, limit, expected):
    items = [saved(store) for _ in range(9)]
    send(store, [item['id'] for item in items], content='按附件顺序查看')
    if limit is not None:
        store.set_setting('learning_image_limit', limit)
    gateway = native_gateway()
    _, result = run(store, gateway)
    paths = result['media_attempts'][0]['images']
    assert [path['path'] for path in paths] == ['native'] * expected + ['placeholder'] * (9 - expected)
    assert all(path['reason'] == 'batch_limit' for path in paths[expected:])
    assert not gateway.images


def test_image_description_does_not_persist_echoed_original(store):
    item = saved(store)
    send(store, [item['id']])
    encoded = base64.b64encode(PNG).decode()
    def handler(request):
        return httpx.Response(200, json=response('图中文字 data:image/png;base64,' + encoded))
    configs = {'image_understanding': ModelConfig('https://example.invalid/v1', '', 'fake')}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = Gateway(configs, store, client=client)
        try:
            from iris.media import prepare_media
            with store.read() as conn:
                ids = [row[0] for row in conn.execute('SELECT id FROM messages')]
            prepare_media(store, gateway, ids)
        finally:
            gateway.close()
    assert '[图片数据已省略]' in get_media(store, item['id'])['understanding_text']
    with store.read() as conn:
        assert encoded not in '\n'.join(conn.iterdump())


def test_text_request_is_identical_with_or_without_vision(store):
    send(store, content='周四去宁波出差')
    batch = form_batch(store, 'A', PROMPT_VERSION)
    legacy = LearningEngine(store, Vision())
    native = LearningEngine(store, native_gateway())
    assert legacy._material(batch, legacy._snapshot(batch), []) == native._material(batch, native._snapshot(batch), [])


def test_mixed_native_and_fallback_descriptions_share_message_text_budget(store):
    original = saved(store)
    second = saved(store, understanding_text='乙' * 1400)
    third = saved(store, understanding_text='丙' * 1400)
    send(store, [original['id'], second['id'], third['id']], content='甲' * 1300)
    store.set_setting('learning_image_limit', 1)
    gateway = native_gateway()
    _, result = run(store, gateway)
    material = gateway.materials[0]
    text = text_parts(material)
    assert sum(text.count(char) for char in '甲乙丙') <= 1500
    assert len([part for part in material if part['type'] == 'image_url']) == 1
    paths = result['media_attempts'][0]['images']
    assert paths[0]['path'] == 'native'
    assert paths[1]['description_truncated'] is True
    assert paths[2]['path'] == 'placeholder'
    assert paths[2]['description_omitted'] == 'message_budget'
