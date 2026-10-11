import json
from pathlib import Path

import pytest

from iris import evaluation
from iris.models import ModelConfig, effective_configs
from test_media import PNG, Vision


CASE = {'id': 'image-unit', 'split': 'dev', 'entry_type': 'private', 'tags': [],
        'messages': [{'type': 'message', 'speaker': '小林', 'content': '',
                      'at': '2026-10-12T08:00:00+08:00',
                      'media': [{'file': 'unit/one.png', 'content_type': 'image/png'}]}],
        'must': [], 'forbidden': [], 'links': [], 'goals': []}


@pytest.fixture
def image_eval(tmp_path, monkeypatch):
    root = tmp_path / 'root'
    media = root / 'evals/media/unit'
    media.mkdir(parents=True)
    (media / 'one.png').write_bytes(PNG)
    corpus = root / 'evals/image-unit.jsonl'
    corpus.write_text(json.dumps(CASE, ensure_ascii=False) + '\n')
    instances = []
    class Fake(Vision):
        def __init__(self, configs, store):
            super().__init__()
            self.configs = effective_configs(configs)
            self.events = []
            instances.append(self)
        def json_chat(self, *args, **kwargs):
            self.events.append('learning')
            return super().json_chat(*args, **kwargs)
        def image_understanding(self, *args, **kwargs):
            self.events.append('description')
            return super().image_understanding(*args, **kwargs)
        def close(self):
            pass
    monkeypatch.setattr(evaluation, 'Gateway', Fake)
    return root, corpus, instances


def configs(vision):
    return {'chat': ModelConfig('https://example.invalid/v1', '', 'fake', vision=vision),
            'embedding': ModelConfig('', '', ''),
            'image_understanding': ModelConfig('https://example.invalid/v1', '', 'fake')}


@pytest.mark.parametrize('vision', [False, True])
def test_image_corpus_reaches_learning_and_exported_originals(image_eval, tmp_path, vision):
    root, corpus, instances = image_eval
    manifest, run = evaluation.run_learning_eval(configs(vision), root, corpus=corpus,
        out=tmp_path / 'out', judge_mode='external')
    assert len(instances) == 1
    gateway = instances[0]
    assert gateway.events == (['learning', 'description'] if vision else ['description', 'learning'])
    assert gateway.images[0][0] == PNG
    assert isinstance(gateway.materials[0], list if vision else str)
    assert (manifest.parent / 'media/unit/one.png').read_bytes() == PNG
    assert run['run']['chat_vision'] is vision
    assert run['run']['media_assets'][0]['sha256']
    evaluation._load_learning_materials(manifest.parent)
    (manifest.parent / 'media/unit/one.png').write_bytes(PNG + b'changed')
    with pytest.raises(ValueError, match='fingerprint'):
        evaluation._load_learning_materials(manifest.parent)


def test_vision_and_original_bytes_change_checkpoint_fingerprint(image_eval, tmp_path):
    root, corpus, instances = image_eval
    out = tmp_path / 'out'
    _, first = evaluation.run_learning_eval(configs(True), root, corpus=corpus, out=out, judge_mode='external')
    _, legacy = evaluation.run_learning_eval(configs(False), root, corpus=corpus, out=out, judge_mode='external')
    (root / 'evals/media/unit/one.png').write_bytes(PNG + b'different-original')
    _, changed = evaluation.run_learning_eval(configs(True), root, corpus=corpus, out=out, judge_mode='external')
    assert len({r['run']['checkpoint_signature'] for r in (first, legacy, changed)}) == 3
    assert len(instances) == 3


@pytest.mark.parametrize('file', ['../private.png', '/tmp/private.png', 'unit/link.png'])
def test_corpus_rejects_paths_outside_media_root_before_models(image_eval, tmp_path, file):
    root, corpus, instances = image_eval
    private = tmp_path / 'private.png'
    private.write_bytes(PNG)
    (root / 'evals/media/unit/link.png').symlink_to(private)
    case = json.loads(json.dumps(CASE))
    case['messages'][0]['media'][0]['file'] = file
    corpus.write_text(json.dumps(case))
    with pytest.raises(ValueError, match='media'):
        evaluation.run_learning_eval(configs(True), root, corpus=corpus,
            out=tmp_path / 'out', judge_mode='external')
    assert not instances
