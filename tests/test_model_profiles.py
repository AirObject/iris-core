"""Profile selection and capability flags use synthetic configurations only."""
import json
from dataclasses import replace

import httpx
import pytest

from iris.models import Gateway, ModelConfig, effective_configs, load_test_models
from test_models import response


@pytest.fixture
def profile_file(tmp_path, monkeypatch):
    monkeypatch.delenv('IRIS_TEST_MODEL_PROFILE', raising=False)
    path = tmp_path / 'profiles.toml'
    path.write_text('''[chat]
base_url = "https://first.invalid/v1"
api_key = "fake-first"
model = "first-chat"
reasoning_effort = "high"
vision = true
[chat2]
base_url = "https://second.invalid/v1"
api_key_env = "IRIS_FAKE_PROFILE_KEY"
model = "second-chat"
vision = true
[embedding]
base_url = "https://vectors.invalid/v1"
api_key = "fake-vector"
model = "vector"
dimensions = 2
[recall_judge]
base_url = "https://independent.invalid/v1"
model = "own-recall"
[goal_dedup_judge]
base_url = "https://independent.invalid/v1"
model = "own-goal"
[image_understanding]
base_url = "https://independent.invalid/v1"
model = "own-image"
''', encoding='utf-8')
    monkeypatch.setenv('IRIS_FAKE_PROFILE_KEY', 'fake-second')
    return path


def test_profile_replaces_all_chat_purposes_but_keeps_embedding(profile_file, monkeypatch):
    default = load_test_models(profile_file)
    assert default['chat'].model == 'first-chat' and default['chat'].vision
    assert effective_configs(default)['recall_judge'].model == 'own-recall'
    assert effective_configs(default)['goal_dedup_judge'].model == 'own-goal'
    assert effective_configs(default)['image_understanding'].model == 'own-image'
    monkeypatch.setenv('IRIS_TEST_MODEL_PROFILE', 'chat2')
    selected = load_test_models(profile_file)
    assert selected['embedding'] == default['embedding']
    assert selected['chat'].api_key == 'fake-second'
    for kind in ('chat', 'recall_judge', 'goal_dedup_judge', 'image_understanding'):
        config = effective_configs(selected)[kind]
        assert config.model == 'second-chat'
        assert config.base_url == 'https://second.invalid/v1'
        assert config.reasoning_effort is None and config.vision
    # Ignored purpose sections must not even resolve invalid credential refs.
    profile_file.write_text(profile_file.read_text().replace('model = "own-recall"',
        'model = "own-recall"\napi_key_env = "IRIS_MISSING_UNUSED_KEY"'), encoding='utf-8')
    assert load_test_models(profile_file) == selected


@pytest.mark.parametrize('section, match', [
    ('', 'chat2'),
    ('[chat2]\nmodel="second"\napi_key="fake-do-not-echo"', 'base_url'),
    ('[chat2]\nbase_url="https://second.invalid"\napi_key="fake-do-not-echo"', 'model'),
    ('[chat2]\nbase_url="https://second.invalid"\nmodel="second"', 'api_key'),
    ('chat2="fake-do-not-echo"', 'chat2'),
])
def test_invalid_profile_has_safe_field_error(tmp_path, monkeypatch, section, match):
    path = tmp_path / 'invalid-profile.toml'
    path.write_text(section, encoding='utf-8')
    monkeypatch.setenv('IRIS_TEST_MODEL_PROFILE', 'chat2')
    with pytest.raises(ValueError, match=match) as caught:
        load_test_models(path)
    assert 'fake-do-not-echo' not in str(caught.value)


@pytest.mark.parametrize('effort', [None, 'low', 'high'])
def test_derived_purposes_only_inherit_explicit_reasoning(effort):
    chat = ModelConfig('https://chat.invalid/v1', '', 'chat', dimensions=17, reasoning_effort=effort, vision=True)
    configs = effective_configs({'chat': chat})
    for kind in ('recall_judge', 'goal_dedup_judge', 'image_understanding'):
        assert configs[kind] == replace(chat, dimensions=None)
    explicit = ModelConfig('https://own.invalid/v1', '', 'own')
    assert effective_configs({'chat': chat, 'recall_judge': explicit})['recall_judge'].reasoning_effort == 'high'
    assert effective_configs({'chat': chat, 'image_understanding': explicit})['image_understanding'] == explicit
    assert not effective_configs({'chat': replace(chat, vision=False)}).get('image_understanding')


@pytest.mark.parametrize('effort', [None, 'high'])
def test_vision_never_changes_text_request_bytes_and_reasoning_is_not_invented(store, effort):
    requests = []
    def handler(request):
        assert not store._writer.in_transaction
        requests.append(request.content)
        body = json.loads(request.content)
        assert 'vision' not in body
        assert body.get('reasoning_effort') == effort
        assert ('reasoning_effort' in body) == (effort is not None)
        return httpx.Response(200, json=response())
    chat = ModelConfig('https://chat.invalid/v1', '', 'chat', reasoning_effort=effort)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = Gateway({'chat': chat}, store, client=client)
        try:
            messages = [{'role': 'user', 'content': '原样保留学习文本。'}]
            gateway.chat(messages, 'learning')
            gateway.replace_config('chat', replace(chat, vision=True))
            gateway.chat(messages, 'learning')
            assert requests[0] == requests[1]
            for kind in ('recall_judge', 'goal_dedup_judge', 'image_understanding'):
                gateway._call(kind, kind, {'messages': messages, 'max_tokens': 32})
        finally:
            gateway.close()
    with store.read() as conn:
        rows = conn.execute('SELECT model,model_kind,reasoning_effort FROM model_calls ORDER BY id').fetchall()
        assert [tuple(row) for row in rows] == [
            ('chat', kind, effort) for kind in ('chat', 'chat', 'recall_judge', 'goal_dedup_judge', 'image_understanding')]


@pytest.mark.parametrize('bad', ['true', 1, None])
def test_vision_must_be_a_boolean(bad):
    with pytest.raises(ValueError, match='vision'):
        ModelConfig('https://chat.invalid', '', 'chat', vision=bad)


def test_e2e_child_uses_resolved_profile_without_selecting_it_again(profile_file, monkeypatch, tmp_path):
    import os
    from iris.e2e_evaluation import ServeProcess
    monkeypatch.setenv('IRIS_TEST_MODEL_PROFILE', 'chat2')
    selected = load_test_models(profile_file)
    service = ServeProcess(tmp_path / 'service', selected)
    try:
        assert os.environ['IRIS_TEST_MODEL_PROFILE'] == 'chat2'
        profile_present = 'IRIS_TEST_MODEL_PROFILE' in service.env
        assert not profile_present  # Never include the environment in assertion output.
        with monkeypatch.context() as child:
            child.delenv('IRIS_TEST_MODEL_PROFILE')
            for name, value in service.env.items():
                child.setenv(name, value)
            restored = load_test_models(service.env['IRIS_TEST_MODELS'])
        assert restored == selected
        assert restored['chat'].vision
        assert effective_configs(restored)['recall_judge'].reasoning_effort is None
        assert effective_configs(restored)['image_understanding'].model == 'second-chat'
    finally:
        service.stop()
