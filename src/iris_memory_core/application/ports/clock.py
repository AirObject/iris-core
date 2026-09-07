"""Application ports for clock; storage and provider adapters implement these contracts."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    def now_us(self) -> int: ...


class SystemClock:
    """Wall clock in UTC; microseconds come from the same reading (§4.2)."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def now_us(self) -> int:
        return time.time_ns() // 1000


class MonotonicClock(Protocol):
    """Injectable monotonic clock for schedule/tick observations (§17.3)."""

    def monotonic_us(self) -> int: ...


class SystemMonotonicClock:
    def monotonic_us(self) -> int:
        return time.monotonic_ns() // 1000


class FixedMonotonicClock:
    """Test double: manually advanced monotonic time (sleep/restart tests)."""

    def __init__(self, start_us: int = 0) -> None:
        self._us = start_us

    def monotonic_us(self) -> int:
        return self._us

    def advance(self, delta_us: int) -> None:
        self._us += delta_us


class IdentifierGenerator(Protocol):
    def new(self) -> uuid.UUID: ...


class Uuid7Generator:
    """UUIDv7 identifiers per §4.1, stdlib-only.

    Millisecond-sorted with random tail; strict intra-millisecond ordering is
    provided by revisions and watermarks, not by ids.
    """

    def new(self) -> uuid.UUID:
        buffer = uuid.uuid4().bytes  # 16 random bytes as the base
        timestamp_ms = time.time_ns() // 1_000_000
        packed = timestamp_ms.to_bytes(6, "big")
        buffer = packed + buffer[6:]
        raw = bytearray(buffer)
        raw[6] = (raw[6] & 0x0F) | 0x70  # version 7
        raw[8] = (raw[8] & 0x3F) | 0x80  # RFC 4122 variant
        return uuid.UUID(bytes=bytes(raw))
