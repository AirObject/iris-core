"""Persistent per-purpose circuit state. No model requests run under these locks."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import Store
from .models import JUDGMENT_KINDS, MODEL_KINDS, effective_configs


def utc_now():
    return datetime.now(timezone.utc)


def usage_window(store, current):
    local = current.astimezone(ZoneInfo(str(store.setting("timezone", "Asia/Shanghai"))))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def fingerprint(config):
    if config is None:
        return "unconfigured"
    values = [config.base_url, config.model, config.api_key, config.dimensions]
    # Preserve existing pauses across upgrades when the optional setting is absent.
    if config.reasoning_effort is not None:
        values.append(config.reasoning_effort)
    return hashlib.sha256(json.dumps(values).encode("utf-8")).hexdigest()


class ModelHealth:
    def __init__(self, store: Store, configs, *, clock=utc_now):
        self.store, self.clock = store, clock
        self._raw_configs = dict(configs)
        self.configs = effective_configs(configs)
        self._lock = threading.RLock()
        self._states = {}
        for kind in MODEL_KINDS:
            saved = store.setting("model_health." + kind, {})
            self._states[kind] = saved if saved.get("fingerprint") == fingerprint(self.configs.get(kind)) else self._fresh(kind)
            self._save(kind)

    def _fresh(self, kind):
        config = self.configs.get(kind)
        configured = config and config.base_url and config.model
        state = {"state": "normal" if configured else "configuration_error",
                "last_error": None if configured else "模型尚未配置",
                "consecutive_errors": 0, "next_probe_at": None, "probe_delay_seconds": 60,
                "fingerprint": fingerprint(config)}
        if kind in JUDGMENT_KINDS:
            state.update(retry_at=None, consecutive_rate_limits=0)
        return state

    def _cooling(self, state):
        return (state['state'] == 'rate_limited' and state.get('retry_at') is not None
                and self.clock() < datetime.fromisoformat(state['retry_at']))

    def _available(self, state):
        return state['state'] == 'normal' or (state['state'] == 'rate_limited' and not self._cooling(state))

    def _save(self, kind):
        self.store.set_setting("model_health." + kind, self._states[kind])

    def budget(self):
        limit = self.store.setting("daily_token_limit")
        start, end = usage_window(self.store, self.clock())
        with self.store.read() as conn:
            used = conn.execute("""SELECT COALESCE(SUM(COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0)),0)
                FROM model_calls WHERE julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?)""",
                (start.isoformat(), end.isoformat())).fetchone()[0]
        return {"limit": limit, "used": used, "reset_at": end.isoformat(),
                "exhausted": limit is not None and used >= limit}

    def set_daily_token_limit(self, limit):
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("daily token limit must be a positive integer or None")
        self.store.set_setting("daily_token_limit", limit)

    def snapshot(self):
        with self._lock:
            result = {kind: {k: v for k, v in state.items() if k != "fingerprint"}
                      for kind, state in self._states.items()}
        for state in result.values():
            if state['state'] == 'rate_limited' and not self._cooling(state):
                state.update(state='normal', retry_at=None)
        budget = self.budget()
        for kind in ('chat', *JUDGMENT_KINDS):
            if budget['exhausted'] and result[kind]['state'] == 'normal':
                result[kind].update(state='usage_limit', last_error='达到每日 token 上限', next_probe_at=budget['reset_at'])
        return result

    def learning_allowed(self):
        return self.snapshot()["chat"]["state"] == "normal"

    def allowed(self, kind):
        with self._lock:
            return self._available(self._states[kind])

    def check(self, kind, purpose, *, probe=False):
        from .models import ModelError
        with self._lock:
            state = self._states[kind]
            if not probe and not self._available(state):
                raise ModelError("paused", state["last_error"] or state["state"], paused=True, reason=state["state"])
            token = state["fingerprint"]
        if ((not probe and purpose in ("learning", "learning_repair")) or kind in JUDGMENT_KINDS) and self.budget()["exhausted"]:
            raise ModelError("paused", "达到每日 token 上限", paused=True, reason="usage_limit")
        return token

    def observe(self, kind, token, category, summary=None, *, probe=False, rate_limited=False, retry_after=None):
        """Returns whether the caller must wait without spending a batch attempt."""
        with self._lock:
            state = self._states[kind]
            if state["fingerprint"] != token:
                return True  # A response from a replaced configuration cannot restore it.
            previous = state["state"]
            if previous in ("invalid_key", "configuration_error"):
                return True  # Only replacing configuration may clear these states.
            cooldown_until = datetime.fromisoformat(state['retry_at']) if kind in JUDGMENT_KINDS and self._cooling(state) else None
            if category == "success":
                # Only a probe may close an open circuit; an older in-flight call may finish later.
                if probe or self._available(state):
                    self._states[kind] = self._fresh(kind)
            elif category == "retryable":
                state["consecutive_errors"] += 1
                state["last_error"] = summary
                if kind in JUDGMENT_KINDS:
                    state['consecutive_rate_limits'] = state.get('consecutive_rate_limits', 0) + 1 if rate_limited else 0
                if (probe or state["consecutive_errors"] >= 3
                        or (kind in JUDGMENT_KINDS and previous in ('temporarily_unavailable', 'account_problem'))):
                    state["state"] = "account_problem" if previous == "account_problem" else "temporarily_unavailable"
                    if kind in JUDGMENT_KINDS:
                        state['retry_at'] = None
                    if probe:
                        state["probe_delay_seconds"] = min(600, state["probe_delay_seconds"] * 2)
                    delay = max(state["probe_delay_seconds"], retry_after or 0)
                    resume_at = self.clock() + timedelta(seconds=delay)
                    state["next_probe_at"] = max(resume_at, cooldown_until or resume_at).isoformat()
                elif kind in JUDGMENT_KINDS and rate_limited:
                    # First two consecutive limits: Retry-After exactly, or 2/4 s.
                    # New prepares fail before admission; expiry needs no probe.
                    delay = retry_after if retry_after is not None else 2 ** state['consecutive_rate_limits']
                    resume_at = self.clock() + timedelta(seconds=delay)
                    state.update(state='rate_limited', next_probe_at=None,
                                 retry_at=max(resume_at, cooldown_until or resume_at).isoformat())
                elif previous == 'rate_limited' and not self._cooling(state):
                    state.update(state='normal', retry_at=None)
            elif category in ("authentication", "configuration", "account"):
                if kind in JUDGMENT_KINDS:
                    state.update(retry_at=None, consecutive_rate_limits=0)
                state.update(state={"authentication": "invalid_key", "configuration": "configuration_error",
                                    "account": "account_problem"}[category], last_error=summary, consecutive_errors=0)
                delay = min(600, state["probe_delay_seconds"] * 2) if probe else 60
                state["probe_delay_seconds"] = delay
                state["next_probe_at"] = (self.clock() + timedelta(seconds=delay)).isoformat() if category == "account" else None
            else:
                # A content refusal demonstrates availability; it is not a circuit failure.
                state["consecutive_errors"] = 0
                if kind in JUDGMENT_KINDS:
                    state['consecutive_rate_limits'] = 0
                    if self._available(state):
                        state.update(state='normal', retry_at=None)
                if probe:
                    state["probe_delay_seconds"] = min(600, state["probe_delay_seconds"] * 2)
                    state["next_probe_at"] = (self.clock() + timedelta(seconds=state["probe_delay_seconds"])).isoformat()
            self._save(kind)
            current = self._states[kind]["state"]
            if previous != current:
                logging.getLogger("iris.models").info("model state kind=%s state=%s", kind, current)
            return not self._available(self._states[kind])

    def due_probes(self):
        exhausted = self.budget()['exhausted']
        from .goal_dedup_judge import settings as goal_judge_settings
        judge_available = {'recall_judge':self.store.setting('recall_judge', {}).get('enabled',True) and not exhausted,
                           'goal_dedup_judge':goal_judge_settings(self.store)['enabled'] and not exhausted}
        with self._lock:
            return [kind for kind, state in self._states.items()
                    if (kind not in JUDGMENT_KINDS or judge_available[kind])
                    and state["state"] in ("temporarily_unavailable", "account_problem")
                    and state["next_probe_at"] and datetime.fromisoformat(state["next_probe_at"]) <= self.clock()]

    def replace_config(self, kind, config):
        if kind not in self._states:
            raise ValueError("unknown model purpose")
        with self._lock:
            self._raw_configs[kind] = config
            updated = effective_configs(self._raw_configs)
            changed = False
            for purpose in MODEL_KINDS:
                candidate = updated.get(purpose)
                if fingerprint(candidate) != self._states[purpose]['fingerprint']:
                    self.configs[purpose] = candidate
                    self._states[purpose] = self._fresh(purpose)
                    self._save(purpose)
                    changed = True
            return changed

    def retry_now(self, kind):
        with self._lock:
            if self._states[kind]["state"] in ("temporarily_unavailable", "account_problem"):
                self._states[kind] = self._fresh(kind)
                self._save(kind)
