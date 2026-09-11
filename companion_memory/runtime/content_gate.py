"""Finite runtime authority serializes mode/protection changes with dispatch.

Only native admitted grant objects may start a Provider worker. The short lock is
never held for model/file/database I/O. A protection write closes matching sends
before persistence; uncertainty keeps those sends closed until owner resolution.
"""
from __future__ import annotations
from collections.abc import Callable
import threading
from companion_memory.provider import WorkGrant, bind_gate


class ContentGate:
    """Bounded active grants and a monotonic protection publication revision."""
    def __init__(self, capacity: int):
        self.lock = threading.RLock()
        self.capacity = capacity
        self.state = 'RECOVERING'; self.epoch = 0; self.protection_revision = 0
        self._grants: dict[int, tuple[WorkGrant, int, str | None, bool]] = {}
        self._guard_writers: set[str] = set()
        self._mode_cutoff = False
        self.integrity_pending: Callable[[], bool] = lambda: False
        self.binding = bind_gate(self.check, self.dispatch, self.authorized)

    def publish_mode(self, state: str, epoch: int) -> None:
        """Publish only confirmed original mode facts, never a local desired state."""
        with self.lock:
            if epoch >= self.epoch: self.state, self.epoch = state, epoch

    def admit(self, grant: WorkGrant, expected_epoch: int, guard_key: str | None = None, expected_protection_revision: int | None = None) -> bool:
        """Install one currently authorized grant after persistent owner checks."""
        with self.lock:
            if (self.integrity_pending() or type(grant) is not WorkGrant or len(self._grants) >= self.capacity or expected_epoch != self.epoch
                    or (self._mode_cutoff or self.state not in ('NORMAL', 'DRAINING')) or guard_key in self._guard_writers
                    or expected_protection_revision is not None and expected_protection_revision != self.protection_revision): return False
            self._grants[id(grant)] = grant, self.epoch, guard_key, False
            return True

    def admit_recovery(self, grant: WorkGrant) -> bool:
        """Issue bounded original-query authority with an unconditional send denial."""
        with self.lock:
            if type(grant) is not WorkGrant or len(self._grants) >= self.capacity: return False
            self._grants[id(grant)] = grant, self.epoch, None, True
            return True

    def authorized(self, grant: WorkGrant) -> bool:
        with self.lock:
            entry = self._grants.get(id(grant))
            return entry is not None and entry[0] is grant

    def check(self, grant: WorkGrant) -> bool:
        with self.lock:
            entry = self._grants.get(id(grant))
            return (entry is not None and entry[0] is grant and entry[1] == self.epoch and not entry[3]
                and not self.integrity_pending() and not self._mode_cutoff and self.state in ('NORMAL', 'DRAINING') and entry[2] not in self._guard_writers)

    def dispatch(self, grant: WorkGrant, start: Callable[[], None]) -> bool:
        """Consume a current permit in the same serialization boundary as closure."""
        with self.lock:
            if not self.check(grant): return False
            start()
            return True

    def close_guard(self, key: str) -> None:
        """Close affected attempts before a sensitive-result transaction begins."""
        with self.lock:
            if key not in self._guard_writers:
                if len(self._guard_writers) >= self.capacity: raise ValueError('Protection writer capacity is occupied.')
                self._guard_writers.add(key); self.protection_revision += 1
            for identity, (grant, epoch, guard, blocked) in tuple(self._grants.items()):
                if guard == key: self._grants[identity] = grant, epoch, guard, True

    def guard_committed(self, key: str) -> None:
        """Durable guard replaces the finite pending writer; old grants stay closed."""
        with self.lock: self._guard_writers.discard(key)

    def revoke(self, grant: WorkGrant) -> None:
        """The work owner calls after all actual Provider consumers have ended."""
        with self.lock: self._grants.pop(id(grant), None)

    def close_ordinary(self) -> None:
        """Close new starts immediately while the persisted mode transition resolves."""
        with self.lock:
            self._mode_cutoff = True
            self.state = 'DREAM_PREPARING'

    def resolve_cutoff(self, state: str, epoch: int) -> None:
        """Release a temporary cutoff only from reliable current persistent facts."""
        with self.lock:
            self.publish_mode(state, epoch)
            self._mode_cutoff = False

    def close(self) -> None:
        """Permanently deny dispatch while completed owner cleanup proceeds."""
        with self.lock: self.state = 'CLOSED'

    def invalidate_current(self) -> None:
        """A newly discovered integrity failure revokes all previously checked sends."""
        with self.lock:
            self.protection_revision += 1
            for identity, (grant, epoch, guard, blocked) in tuple(self._grants.items()):
                self._grants[identity] = grant, epoch, guard, True
