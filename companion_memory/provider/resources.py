"""Explicit trusted gate, cancellation and simulation resources for provider calls.

Bindings authorize supported code, not hostile Python in the same process.
No default gate, endpoint, secret resolver or background retry is provided.
"""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import time
import uuid
from typing import cast

from .values import Data, Record, as_record, freeze

_ISSUER = object()


def native_issued(value: object, kind: type) -> bool:
    """Check native construction without invoking submitted attribute hooks."""
    if type(value) is not kind:
        return False
    try:
        return object.__getattribute__(value, "_issuer") is _ISSUER
    except AttributeError:
        return False


@dataclass(frozen=True, slots=True)
class WorkGrant:
    """Trusted caller identity and finite authorized business references."""
    caller_module: str
    caller_scope: str
    extension_id: str | None
    task_role: str
    profiles: tuple[str, ...]
    capabilities: tuple[str, ...]
    result_owner: str
    actor_ref: str
    run_ids: tuple[str, ...]
    entry_ids: tuple[str, ...] = ()
    batch_ids: tuple[str, ...] = ()
    dream_run_ids: tuple[str, ...] = ()
    internal_dream: bool = False
    parent_request_ids: tuple[str, ...] = ()
    trace_ids: tuple[str, ...] = ()
    prompt_revisions: tuple[str, ...] = ()


class CancellationToken:
    """Read-only observation of one explicit native cancellation source."""
    __slots__ = ("_event", "_issuer")
    def __new__(cls):
        raise TypeError("Cancellation tokens are issued by a cancellation source.")
    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Cancellation tokens cannot be rebound.")
    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


class CancellationSource:
    """Thread-safe irreversible cancellation; it makes no remote rollback claim."""
    def __init__(self) -> None:
        self._event = threading.Event()
        self._token = object.__new__(CancellationToken)
        object.__setattr__(self._token, "_event", self._event)
        object.__setattr__(self._token, "_issuer", _ISSUER)
    @property
    def token(self) -> CancellationToken:
        return self._token
    def cancel(self) -> None:
        self._event.set()


class GateBinding:
    """Native trusted callbacks check current authority and atomically start work.

    The start callback only starts an already bounded worker. The injected gate
    must serialize that callback with epoch/revocation changes, not hold its lock
    for model execution. No mode is inferred from a request's role text.
    """
    __slots__ = ("_check", "_dispatch", "_authorize", "_issuer")
    def __new__(cls):
        raise TypeError("Gate bindings are issued by trusted assembly.")
    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Gate bindings are immutable.")
    def authorized(self, grant: WorkGrant) -> bool:
        return self._authorize(grant) is True
    def check(self, grant: WorkGrant) -> bool:
        return self._check(grant) is True
    def dispatch(self, grant: WorkGrant, start: Callable[[], None]) -> bool:
        return self._dispatch(grant, start) is True


def bind_gate(check: Callable[[WorkGrant], bool], dispatch: Callable[[WorkGrant, Callable[[], None]], bool], authorize: Callable[[WorkGrant], bool]) -> GateBinding:
    """Trusted mode authority supplies a deny-capable current-epoch gate."""
    if not all(callable(value) for value in (check, dispatch, authorize)):
        raise TypeError("Trusted gate callbacks are required.")
    binding = object.__new__(GateBinding)
    object.__setattr__(binding, "_issuer", _ISSUER)
    object.__setattr__(binding, "_check", check)
    object.__setattr__(binding, "_dispatch", dispatch)
    object.__setattr__(binding, "_authorize", authorize)
    return binding


@dataclass(frozen=True, slots=True)
class Scenario:
    """Finite explicit simulation data and optional test synchronization events."""
    outcome: str
    payload: object
    usage: object
    started: threading.Event | None = None
    release: threading.Event | None = None
    interim_usage: object = None
    malformed: str | None = None


@dataclass(frozen=True, slots=True)
class AdapterResponse:
    """Model-side result evidence, independent of any database transaction."""
    outcome: str
    payload: Data
    usage: Data


class SimulationAdapter:
    """One bounded scenario per invocation; never retries or accesses a ledger."""
    def __init__(self, scenarios: tuple[Scenario, ...]):
        if type(scenarios) is not tuple or not 1 <= len(scenarios) <= 1024 or any(type(item) is not Scenario for item in scenarios):
            raise ValueError("A finite native simulation sequence is required.")
        for item in scenarios:
            if (type(item.outcome) is not str or item.outcome not in ("SUCCEEDED", "TRANSIENT_FAILURE", "RATE_LIMITED", "AUTHENTICATION_FAILED", "SENSITIVE_REFUSAL", "OTHER_REFUSAL", "INVALID_RESPONSE", "TIMED_OUT", "CANCELLED", "REMOTE_RESULT_UNKNOWN", "EXCEPTION")
                    or any(value is not None and type(value) is not threading.Event for value in (item.started, item.release))
                    or item.malformed is not None and (type(item.malformed) is not str or item.malformed not in ("NON_FINITE", "INVALID_PAYLOAD"))):
                raise ValueError("A closed native simulation scenario is required.")
        self._scenarios = tuple((item.outcome, freeze(item.payload, 1048576), self._usage(item.usage), item.started, item.release, self._usage(item.interim_usage) if item.interim_usage is not None else None, item.malformed) for item in scenarios)
        self._issuer = _ISSUER
        self._lock = threading.Lock()
        self._position = 0
        self._calls: list[str] = []

    @staticmethod
    def _usage(value: object) -> Data:
        if type(value) is not dict:
            return None
        from .normalization import USAGE_FIELDS, BILLING_FIELDS
        return freeze({key: value[key] for key in (*USAGE_FIELDS, *BILLING_FIELDS, "coverage") if key in value}, 8192)

    @property
    def calls(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._calls)

    def invoke(self, attempt_id: str, request: Record, cancellation: CancellationToken, observe: Callable[[AdapterResponse], None] | None = None) -> AdapterResponse:
        """Consume one scenario, optionally block; no hidden retry or timeout reset."""
        with self._lock:
            if self._position == len(self._scenarios):
                raise RuntimeError("The finite simulation sequence was exhausted.")
            outcome, payload, usage, started, release, interim, malformed = self._scenarios[self._position]
            self._position += 1
            self._calls.append(attempt_id)
        if interim is not None and observe is not None:
            observe(AdapterResponse("REMOTE_RESULT_UNKNOWN", None, interim))
        if started is not None:
            started.set()
        if release is not None:
            release.wait()
        if outcome == "EXCEPTION":
            raise RuntimeError("Synthetic private adapter detail.")
        if malformed == "NON_FINITE":
            payload = float("nan")
        elif malformed == "INVALID_PAYLOAD":
            payload = 42
        return AdapterResponse(outcome, payload, usage)


@dataclass(frozen=True, slots=True)
class ProviderResources:
    """Borrowed trusted gate, bounded simulator and explicit infrastructure clocks."""
    gate: GateBinding
    adapter: SimulationAdapter
    monotonic: Callable[[], float] = time.monotonic
    utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    new_id: Callable[[], str] = lambda: str(uuid.uuid4())
    logger: object = None


class AuthorizedMedia:
    """Opaque in-memory media reference issued only by trusted provider assembly."""
    __slots__ = ("_issuer", "_scope", "_owner", "_artifact", "_content", "_modality")
    def __new__(cls):
        raise TypeError("Media access is issued by trusted assembly.")
    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Media access is immutable.")
