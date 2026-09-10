"""Bounded native values, deterministic payloads and immutable provider outcomes.

Text and floating vector coordinates are restored exactly. Money never uses a
float. Parsing rejects unknown carriers, cycles, deep trees and encoding excess.
"""
from dataclasses import dataclass
from collections.abc import Callable
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import cast

type Data = None | bool | int | float | str | tuple[Data, ...] | MappingProxyType[str, Data]
type Record = MappingProxyType[str, Data]
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
MAX_INTEGER = 2**63-1


class InvalidData(Exception):
    """Safe invalid-input signal, never retaining rejected content."""


class DataLimit(InvalidData):
    """Safe resource-limit signal without the oversized input."""


def is_identifier(value: object) -> bool:
    return type(value) is str and IDENTIFIER.fullmatch(value) is not None


def freeze(value: object, limit: int, *, owned: bool = False, checkpoint: Callable[[], None] | None = None) -> Data:
    """Own one finite exact-native tree before hashing, sending or returning it."""
    nodes = 0
    ancestors: set[int] = set()
    def visit(item: object, depth: int) -> Data:
        nonlocal nodes
        nodes += 1
        if checkpoint is not None and nodes % 128 == 1:
            checkpoint()
        if nodes > 20000 or depth > 12:
            raise DataLimit()
        if item is None or type(item) is bool:
            return item
        if type(item) is int:
            if not -MAX_INTEGER <= item <= MAX_INTEGER:
                raise DataLimit()
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise InvalidData()
            return item
        if type(item) is str:
            if len(item) > limit:
                raise DataLimit()
            try:
                if len(item.encode("utf-8")) > limit:
                    raise DataLimit()
            except UnicodeError:
                raise InvalidData() from None
            return item
        sequence = type(item) in (tuple, list)
        mapping = type(item) is dict or (owned and type(item) is MappingProxyType)
        if not sequence and not mapping:
            raise InvalidData()
        if id(item) in ancestors:
            raise InvalidData()
        if len(cast(list | dict, item)) > 4096:
            raise DataLimit()
        ancestors.add(id(item))
        try:
            if sequence:
                return tuple(visit(child, depth+1) for child in cast(list, item))
            source = cast(dict[str, object], item)
            if any(type(key) is not str or len(key) > 128 for key in source):
                raise InvalidData()
            return MappingProxyType({key: visit(child, depth+1) for key, child in source.items()})
        finally:
            ancestors.remove(id(item))
    result = visit(value, 0)
    if checkpoint is not None:
        checkpoint()
    dump(result, limit)
    if checkpoint is not None:
        checkpoint()
    return result


def plain(value: Data) -> object:
    """Convert an already owned value for deterministic private encoding."""
    if type(value) is MappingProxyType:
        return {key: plain(item) for key, item in sorted(value.items())}
    if type(value) is tuple:
        return [plain(item) for item in value]
    if type(value) is float:
        return {"$float": value.hex()}
    return value


def dump(value: Data, limit: int = 8192) -> str:
    encoded = json.dumps(plain(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
    try:
        if len(encoded.encode("utf-8")) > limit:
            raise DataLimit()
    except UnicodeError:
        raise InvalidData() from None
    return encoded


def load(value: object, limit: int = 8192) -> Record:
    if type(value) is not str or len(value) > limit:
        raise InvalidData()
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise InvalidData()
            result[key] = item
        if set(result) == {"$float"}:
            if type(result["$float"]) is not str or len(result["$float"]) > 32:
                raise InvalidData()
            return float.fromhex(result["$float"])
        return result
    try:
        result = freeze(json.loads(value, object_pairs_hook=pairs), limit)
    except (ValueError, RecursionError, UnicodeError):
        raise InvalidData() from None
    if type(result) is not MappingProxyType:
        raise InvalidData()
    return result


def as_record(value: object) -> Record:
    if type(value) is not MappingProxyType:
        raise InvalidData()
    return cast(Record, value)


def fingerprint(value: Data, limit: int = 1048576) -> str:
    return hashlib.sha256(dump(value, limit).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderError:
    """A fixed safe cause; the outer branch carries persistence evidence."""
    code: str
    operation: str
    field: str
    reason: str
    cleanup_pending: bool = False


@dataclass(frozen=True, slots=True)
class Rejected:
    """Current admission failed; historical effects are not disproved."""
    error: ProviderError


@dataclass(frozen=True, slots=True)
class Pending:
    """A request remains in progress, remotely unknown, or locally unconfirmed."""
    reference: Record
    observation: str
    error: ProviderError | None = None


@dataclass(frozen=True, slots=True)
class Completed:
    """A durable terminal request and its owner-authorized original result."""
    record: Record
    result: Record | None
    source: str


@dataclass(frozen=True, slots=True)
class Found:
    """An immutable committed read observation, without implied replay authority."""
    value: Data


@dataclass(frozen=True, slots=True)
class NotFound:
    """A short read missed; this does not prove an operation never happened."""


@dataclass(frozen=True, slots=True)
class Failed:
    """A safe read failure, distinct from a successful empty result."""
    error: ProviderError


@dataclass(frozen=True, slots=True)
class Ready:
    """Provider recovery completed and its unique owner may admit work."""
    database_id: str


@dataclass(frozen=True, slots=True)
class RecoveryPending:
    """Initialization cannot yet establish a safe completed recovery."""
    error: ProviderError


@dataclass(frozen=True, slots=True)
class CloseReport:
    """The first close observation; late cleanup is visible only through health."""
    status: str
    error: ProviderError | None
    cleanup_pending: bool


@dataclass(frozen=True, slots=True)
class Health:
    """No-I/O view of actual execution ownership and storage readiness."""
    lifecycle: str
    in_flight: int
    unknown_observations: int
    cleanup_pending: bool
    ledger_faulted: bool
    reason: str | None
