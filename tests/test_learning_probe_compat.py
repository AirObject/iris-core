"""The development probe must preserve the current gateway's request contract."""
import importlib.util
import json
from pathlib import Path
import sys

import httpx
import pytest

from iris import evaluation, models


@pytest.mark.parametrize(("variant", "configured", "expected"), [
    ("low", None, "low"), ("default", "low", "low"), ("high", "low", "high"),
])
def test_probe_preserves_deadlines_and_records_copied_payloads(monkeypatch, tmp_path, variant, configured, expected):
    path = Path(__file__).parents[1] / 'evals/glm_probe/probe.py'
    spec = importlib.util.spec_from_file_location('learning_probe_test', path)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    sent = []

    class Client:
        def post(self, url, **kwargs):
            sent.append(kwargs)
            body = ({'choices': [{'message': {'content': '{}'}, 'finish_reason': 'stop'}]}
                    if url.endswith('/chat/completions') else {'data': [{'embedding': [1.0, 0.0]}]})
            return httpx.Response(200, json=body)

        def close(self):
            pass

    monkeypatch.setattr(probe.httpx, 'Client', Client)
    monkeypatch.setattr(models, 'load_test_models', lambda: {
        'chat': models.ModelConfig('https://test.invalid', 'fixture-key', 'fake-chat', reasoning_effort=configured),
        'embedding': models.ModelConfig('https://test.invalid', 'fixture-key', 'fake-embedding', 2)})
    for name in ('__init__', '_call', '_record', 'close'):
        monkeypatch.setattr(models.Gateway, name, getattr(models.Gateway, name))
    monkeypatch.setattr(evaluation, '_run_case', evaluation._run_case)
    monkeypatch.setattr(evaluation, 'ThreadPoolExecutor', evaluation.ThreadPoolExecutor)
    monkeypatch.setattr(models, 'CHAT_TOTAL_TIMEOUT', models.CHAT_TOTAL_TIMEOUT)
    monkeypatch.setattr(evaluation, 'CHAT_TOTAL_TIMEOUT', getattr(evaluation, 'CHAT_TOTAL_TIMEOUT', None), raising=False)

    def run(configs, root, **kwargs):
        gateway = models.Gateway(configs)
        try:
            assert gateway.json_chat([{'role': 'user', 'content': 'empty'}], 'learning')[0] == {}
            assert gateway.embedding('text') == [1.0, 0.0]
        finally:
            gateway.close()
        return tmp_path / 'manifest.json', {}

    monkeypatch.setattr(evaluation, 'run_learning_eval', run)
    corpus = tmp_path / 'corpus.jsonl'
    corpus.write_text('{}\n')
    out = tmp_path / 'probe'
    monkeypatch.setattr(sys, 'argv', ['probe', '--root', str(tmp_path), '--corpus', str(corpus),
                                    '--out', str(out), '--variant', variant, '--workers', '1'])
    probe.main()
    assert sent[0]['json']['reasoning_effort'] == expected
    assert 'reasoning_effort' not in sent[1]['json']
    assert 170 < sent[0]['timeout'].read <= models.LEARNING_TOTAL_TIMEOUT
    metadata = json.loads((out / 'probe-metadata.json').read_text())
    assert metadata['learning_timeout_seconds'] == models.LEARNING_TOTAL_TIMEOUT
    attempts = [json.loads(line) for line in (out / 'http-attempts.jsonl').read_text().splitlines()]
    assert [a['kind'] for a in attempts] == ['chat', 'embedding']
    assert all(a['status_code'] == 200 for a in attempts)
