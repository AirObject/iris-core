"""Fixed reply-preparation judgment policy and bounded, purpose-local admission."""
from __future__ import annotations

import json
import math
import threading
import time
from importlib.resources import files

MAX_SECONDS = 10.0
DEFAULTS = {'enabled': True, 'concurrency': 1, 'queue_limit': 8}
PROMPT = files('iris').joinpath('prompts/recall_judge_v1.md').read_text(encoding='utf-8')


def settings(store):
    return {**DEFAULTS, **(store.setting('recall_judge', {}) if store else {})}


def validate_budget(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= MAX_SECONDS:
        raise ValueError('judge_budget_seconds must be greater than 0 and at most 10')
    return float(value)


def validate_scores(raw, ids):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict) or set(value) != {'scores'} or not isinstance(value['scores'], list) or len(value['scores']) != len(ids):
        raise ValueError('invalid scores array')
    scores = []
    for row, mid in zip(value['scores'], ids):
        if (not isinstance(row, dict) or set(row) != {'id', 'support'} or row['id'] != mid
                or type(row['id']) is not str or type(row['support']) is not int or not 0 <= row['support'] <= 100):
            raise ValueError('invalid score or candidate order')
        scores.append(row['support'])
    return scores


class Lease:
    def __init__(self, limiter):
        self.limiter, self.deferred, self.released = limiter, False, False

    def release(self, *_):
        with self.limiter.condition:
            if not self.released:
                self.released = True
                self.limiter.active -= 1
                self.limiter.condition.notify_all()

    def defer(self, future):
        # A timed-out HTTP worker may still run. Keep its slot until it exits.
        self.deferred = True
        future.add_done_callback(self.release)


class Admission:
    def __init__(self):
        self.condition = threading.Condition()
        self.active = 0
        self.waiters = []

    def acquire(self, deadline, concurrency, queue_limit, monotonic=time.monotonic):
        from .models import ModelError
        with self.condition:
            if self.active < concurrency and not self.waiters:
                self.active += 1
                return Lease(self)
            if len(self.waiters) >= queue_limit:
                raise ModelError('queue_full', 'queue_full')
            ticket = object()
            self.waiters.append(ticket)
            try:
                while True:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise ModelError('queue_timeout', 'queue_timeout')
                    if self.waiters[0] is ticket and self.active < concurrency:
                        self.active += 1
                        return Lease(self)
                    self.condition.wait(remaining)
            finally:
                self.waiters.remove(ticket)
                self.condition.notify_all()


def judge(gateway, store, memories, payload, *, enabled, budget_seconds):
    from .models import ModelError
    budget = validate_budget(budget_seconds)
    started = time.monotonic()
    diagnostic = {'status': 'applied', 'reason': None, 'duration_ms': 0.0, 'queue_ms': 0.0,
                  'network_ms': 0.0, 'budget_seconds': budget, 'removed_memory_ids': []}
    candidates = [m for m in memories if m['reason'] == 'relevant'][:8]
    if not enabled or not settings(store)['enabled']:
        diagnostic.update(status='disabled', reason='host_disabled' if not enabled else 'configuration_disabled')
    elif not candidates:
        diagnostic['reason'] = 'no_candidates'
    elif not gateway or not callable(getattr(gateway, 'recall_judge', None)):
        diagnostic.update(status='degraded', reason='unconfigured')
    else:
        payload = {**payload, 'candidates': [dict(id=str(m['id']), content=m['content'], speaker=m['speaker_name'],
                   about=[p['name'] for p in m['about']], kind=m['kind'], stance=m['stance']) for m in candidates]}
        messages = [{'role': 'system', 'content': PROMPT}, {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
        try:
            remaining = budget - (time.monotonic() - started)
            if remaining <= 0:
                raise ModelError('timeout', 'total timeout', reason='timeout')
            result = gateway.recall_judge(messages, [str(m['id']) for m in candidates], budget_seconds=remaining)
            diagnostic.update(queue_ms=result['queue_ms'], network_ms=result['network_ms'])
            diagnostic['removed_memory_ids'] = [m['id'] for m, score in zip(candidates, result['scores']) if score < 50]
        except ModelError as exc:
            diagnostic.update(status='degraded', reason=exc.reason or exc.category,
                              queue_ms=getattr(exc, 'queue_ms', 0.0), network_ms=getattr(exc, 'network_ms', 0.0))
    diagnostic['duration_ms'] = round((time.monotonic() - started) * 1000, 3)
    return diagnostic
