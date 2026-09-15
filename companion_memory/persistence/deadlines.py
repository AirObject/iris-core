"""Inherited absolute I/O budgets without cancellation or connection ownership.

The caller supplies a monotonic deadline. Storage converts its remaining time
into the configured resource clock, so every child read and write consumes the
same original budget. An exhausted caller still retains actual completion.
"""
from contextvars import ContextVar, Token
from dataclasses import dataclass
import time

_current: ContextVar[float | None] = ContextVar('persistence_absolute_deadline', default=None)


@dataclass(slots=True)
class DeadlineScope:
    deadline: float
    _token: Token[float | None] | None = None

    def __enter__(self) -> 'DeadlineScope':
        previous = _current.get()
        self._token = _current.set(self.deadline if previous is None else min(previous, self.deadline))
        return self

    def __exit__(self, *args: object) -> None:
        if self._token is None:
            raise RuntimeError('A deadline scope must be entered before release.')
        _current.reset(self._token); self._token = None


def bounded_deadline(resource_now: float, configured_seconds: float) -> float:
    original = _current.get()
    remaining = configured_seconds if original is None else min(configured_seconds, max(0, original - time.monotonic()))
    return resource_now + remaining


def current_deadline(configured_seconds: float) -> float:
    """Return the inherited monotonic upper bound, never extending its budget."""
    now = time.monotonic()
    original = _current.get()
    return min(original, now + configured_seconds) if original is not None else now + configured_seconds


def check_deadline() -> None:
    """Stop the next local action after the original scoped budget expires."""
    original = _current.get()
    if original is not None and time.monotonic() >= original:
        from .owned_statements import OwnerFailure
        raise OwnerFailure('TIMEOUT', 'state', 'DEADLINE_EXCEEDED')
