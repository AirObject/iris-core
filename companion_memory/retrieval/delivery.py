"""A host-owned first-write callback inherited by one query and its actual work."""
from __future__ import annotations
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from companion_memory.information.records import Record
import time
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.runtime.content_gate import ContentGate

_current: ContextVar[DeliveryScope | None] = ContextVar('information_delivery', default=None)


@dataclass(slots=True)
class DeliveryScope:
    prepare: Callable[[Record], bytes]
    authorized: Callable[[], bool]
    start: Callable[[bytes], None]
    _token: Token[DeliveryScope | None] | None = None

    def __enter__(self) -> DeliveryScope:
        if self._token is not None: raise RuntimeError('Delivery scope already entered.')
        self._token = _current.set(self)
        return self

    def __exit__(self, *args: object) -> None:
        if self._token is None: raise RuntimeError('Delivery scope not entered.')
        _current.reset(self._token); self._token = None


def current_delivery() -> DeliveryScope | None:
    return _current.get()


def deliver(gate: ContentGate, checkpoint: tuple[int, int], response: Record, deadline: float,
            authorize: Callable[[], None], check_mode: Callable[[], tuple[int, int]]) -> Record:
    """Encode fully before the shared short permission and first-write barrier."""
    encode_content(response, 131072)
    delivery = current_delivery()
    wire = delivery.prepare(response) if delivery is not None else None
    def authorized() -> bool:
        authorize()
        if time.monotonic() >= deadline:
            raise OwnerFailure('TIMEOUT', 'state', 'DEADLINE_EXCEEDED')
        if delivery is not None and not delivery.authorized():
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        return True
    def start() -> None:
        if delivery is not None and wire is not None: delivery.start(wire)
    if not gate.start_information_delivery(checkpoint, authorized, start):
        check_mode()
        raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
    return response
