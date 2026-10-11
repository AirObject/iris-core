"""Pure profile helpers and the shared chat health gate; no scope or content policy."""
from __future__ import annotations

import hashlib
import json

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
