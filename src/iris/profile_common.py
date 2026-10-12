"""Pure profile helpers and the shared chat health gate; no scope or content policy."""
from __future__ import annotations

import hashlib
import json
from importlib.resources import files

from .db import dumps
from .queue import truncate_material


def evidence_hash(value):
    return hashlib.sha256(dumps(value).encode('utf-8')).hexdigest()


def setting(conn, key, default):
    row = conn.execute('SELECT value_json FROM runtime_settings WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def whitespace(text):
    return ' '.join(text.split())


def excerpt(text, limit):
    value = truncate_material(text, limit)
    return value.removesuffix(' [已截断；原文仍完整保存]') if value != text else value


def json_object(content):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate_key')
            value[key] = item
        return value
    value = json.loads(content, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError('invalid_output')
    return value


class ChatHealthGate:
    """Apply the shared chat daily limit on every HTTP attempt, including retries."""
    def __init__(self, health):
        self.real = health
    def check(self, kind, purpose, *, probe=False):
        return self.real.check(kind, 'learning' if kind == 'chat' else purpose, probe=probe)
    def __getattr__(self, key):
        return getattr(self.real, key)


def load_profile_prompts(family, version, allowed):
    """Snapshot the two UTF-8 prompts; provenance hashes the exact sent text."""
    if version not in allowed:
        raise ValueError('unsupported profile prompt version')
    result = {}
    for kind in ('generate', 'check'):
        name = f'{family}_{kind}_{version}'
        text = files('iris').joinpath(f'prompts/{name}.md').read_text(encoding='utf-8')
        result[kind] = {'prompt_version': name, 'prompt_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
                        'text': text}
    return result


def profile_prompt_metadata(prompts):
    return {'prompt_versions': [p['prompt_version'] for p in prompts.values()],
            'prompt_sha256': {p['prompt_version']: p['prompt_sha256'] for p in prompts.values()}}
