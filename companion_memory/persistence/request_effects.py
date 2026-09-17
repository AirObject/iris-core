"""Observe possible durable effects without granting or changing storage authority.

Request adapters may reject input only while no enclosed storage operation has
crossed its commit boundary. A lost reply after that boundary requires original
operation confirmation. The observation is deliberately conservative: it cannot
declare rollback, reveal a receipt, or resolve an unknown transaction.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Iterator


@dataclass
class RequestEffects:
    """Shared monotonic flag retained by storage workers beyond caller timeout."""

    possible_commit: bool = False


_current: ContextVar[RequestEffects | None] = ContextVar('request_effects', default=None)


def current_effects() -> RequestEffects | None:
    """Capture the current observer when an owned storage job is admitted."""
    return _current.get()


@contextmanager
def observe_effects(effects: RequestEffects) -> Iterator[None]:
    """Bind an observer to this request and inherited background operations."""
    token = _current.set(effects)
    try:
        yield
    finally:
        _current.reset(token)
