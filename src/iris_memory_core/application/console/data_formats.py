"""Bounded imc-data/v1 framing for staging and explicitly mapped exports.

This module never authorizes or commits Canonical records. A staging caller may
retain rows only after receiving a verified receipt; payload validators must be
fixed server-owned business-field allowlists, never supplied by an upload.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, BinaryIO

PayloadValidator = Callable[[dict[str, Any]], None]
RecordConsumer = Callable[[dict[str, Any]], None]
_TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)\Z")


class DataFormatError(ValueError):
    """Low-sensitivity error: never include uploaded keys, values or filenames."""


@dataclass(frozen=True)
class FormatLimits:
    file_bytes: int = 50 * 1024 * 1024
    record_bytes: int = 256 * 1024
    records: int = 100_000
    depth: int = 20

    def __post_init__(self) -> None:
        if any(type(v) is not int or v < 1 for v in vars(self).values()):
            raise ValueError("format limits must be positive integers")


@dataclass(frozen=True)
class DataReceipt:
    dataset_id: str
    record_count: int
    records_sha256: str
    file_sha256: str
    file_bytes: int


IMPORT_LIMITS = FormatLimits()
EXPORT_LIMITS = FormatLimits(file_bytes=1024 * 1024 * 1024)


def _identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 256
        and not any(ord(char) < 32 for char in value)
    )


def _time(value: Any) -> bool:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        return False
    try:
        return datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DataFormatError("duplicate_json_key")
        result[key] = value
    return result


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise DataFormatError("non_finite_number")
    return number


def _constant(_value: str) -> Any:
    raise DataFormatError("non_finite_number")


def strict_json(raw: bytes, *, max_depth: int = 20) -> dict[str, Any]:
    """Check nesting before json.loads to avoid recursive parser exhaustion."""
    depth, quoted, escaped = 0, False, False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > max_depth:
                raise DataFormatError("json_depth_limit")
        elif byte in (93, 125):
            depth -= 1
    try:
        text = raw.decode("utf-8", errors="strict")
        result = json.loads(
            text, object_pairs_hook=_pairs, parse_float=_float, parse_constant=_constant
        )
        if not isinstance(result, dict):
            raise DataFormatError("expected_json_object")
        # Reject escaped NUL and unpaired surrogates in values and keys too.
        json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        pending: list[Any] = [result]
        while pending:
            item = pending.pop()
            if isinstance(item, str) and "\x00" in item:
                raise DataFormatError("nul_character")
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        return result
    except DataFormatError:
        raise
    except (ValueError, UnicodeError, RecursionError) as error:
        raise DataFormatError("invalid_json") from error


def _header(row: dict[str, Any], validators: Mapping[str, PayloadValidator]) -> None:
    if set(row) != {
        "kind",
        "format",
        "format_version",
        "dataset_id",
        "exported_at",
        "mode",
        "resources",
    } or (
        row["kind"] != "header"
        or row["format"] != "imc-data"
        or type(row["format_version"]) is not int
        or row["format_version"] != 1
        or row["mode"] != "data"
        or not _identifier(row["dataset_id"])
        or not _time(row["exported_at"])
    ):
        raise DataFormatError("invalid_header")
    resources = row["resources"]
    if (
        not isinstance(resources, list)
        or not resources
        or not all(isinstance(item, str) and item in validators for item in resources)
        or len(set(resources)) != len(resources)
    ):
        raise DataFormatError("unsupported_resource")


def _record(
    row: dict[str, Any], resources: list[str], validators: Mapping[str, PayloadValidator]
) -> tuple[str, str]:
    if (
        set(row)
        != {
            "kind",
            "source_id",
            "resource_type",
            "source_scope",
            "source_occurred_at",
            "payload",
            "source_refs",
        }
        or row["kind"] != "record"
    ):
        raise DataFormatError("invalid_record")
    kind, identifier = row["resource_type"], row["source_id"]
    if not isinstance(kind, str) or kind not in resources or not _identifier(identifier):
        raise DataFormatError("unsupported_resource")
    scope = row["source_scope"]
    if (
        not isinstance(scope, dict)
        or set(scope) != {"agent_ref", "space_ref"}
        or any(value is not None and not _identifier(value) for value in scope.values())
    ):
        raise DataFormatError("invalid_source_scope")
    if row["source_occurred_at"] is not None and not _time(row["source_occurred_at"]):
        raise DataFormatError("invalid_source_time")
    refs = row["source_refs"]
    if not isinstance(refs, list) or len(refs) > 1000:
        raise DataFormatError("invalid_source_refs")
    for ref in refs:
        if (
            not isinstance(ref, dict)
            or set(ref) != {"resource_type", "source_id"}
            or not isinstance(ref["resource_type"], str)
            or ref["resource_type"] not in validators
            or not _identifier(ref["source_id"])
        ):
            raise DataFormatError("invalid_source_refs")
    if not isinstance(row["payload"], dict):
        raise DataFormatError("invalid_payload")
    try:
        validators[kind](row["payload"])
    except (ValueError, TypeError, KeyError) as error:
        raise DataFormatError("invalid_payload") from error
    return kind, identifier


def parse_data_package(
    stream: BinaryIO,
    *,
    validators: Mapping[str, PayloadValidator],
    on_record: RecordConsumer,
    limits: FormatLimits = IMPORT_LIMITS,
) -> DataReceipt:
    """Stream into untrusted staging; return a receipt only after trailer and EOF.

    Read sizes are bounded even without Content-Length. Errors leave staging
    unverified; this routine never invokes a business write or Provider.
    """
    total, count = 0, 0
    file_hash, records_hash = hashlib.sha256(), hashlib.sha256()
    header: dict[str, Any] | None = None
    trailer = False
    keys: set[tuple[str, str]] = set()
    while True:
        raw = stream.readline(min(limits.record_bytes, limits.file_bytes - total) + 1)
        if not raw:
            break
        total += len(raw)
        if total > limits.file_bytes:
            raise DataFormatError("file_size_limit")
        if len(raw) > limits.record_bytes:
            raise DataFormatError("record_size_limit")
        file_hash.update(raw)
        if not raw.endswith(b"\n") or raw.endswith(b"\r\n"):
            raise DataFormatError("expected_lf_line")
        if trailer:
            raise DataFormatError("data_after_trailer")
        row = strict_json(raw, max_depth=limits.depth)
        if header is None:
            _header(row, validators)
            header = row
        elif row.get("kind") == "trailer":
            if (
                set(row) != {"kind", "record_count", "records_sha256"}
                or row["record_count"] != str(count)
                or row["records_sha256"] != records_hash.hexdigest()
            ):
                raise DataFormatError("trailer_mismatch")
            trailer = True
        else:
            count += 1
            if count > limits.records:
                raise DataFormatError("record_count_limit")
            key = _record(row, header["resources"], validators)
            if key in keys:
                raise DataFormatError("duplicate_source_id")
            keys.add(key)
            records_hash.update(raw)
            on_record(row)
    if header is None or not trailer:
        raise DataFormatError("incomplete_package")
    return DataReceipt(
        header["dataset_id"], count, records_hash.hexdigest(), file_hash.hexdigest(), total
    )


def write_data_package(
    stream: BinaryIO,
    header: dict[str, Any],
    records: Iterable[dict[str, Any]],
    *,
    validators: Mapping[str, PayloadValidator],
    limits: FormatLimits = EXPORT_LIMITS,
) -> DataReceipt:
    """Encode already-authorized business records; no internal row dumping."""
    _header(header, validators)
    total, count = 0, 0
    file_hash, records_hash = hashlib.sha256(), hashlib.sha256()
    keys: set[tuple[str, str]] = set()

    def emit(row: dict[str, Any]) -> bytes:
        nonlocal total
        try:
            raw = (
                json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
        except (ValueError, UnicodeError) as error:
            raise DataFormatError("invalid_json") from error
        if len(raw) > limits.record_bytes or total + len(raw) > limits.file_bytes:
            raise DataFormatError("output_size_limit")
        strict_json(raw, max_depth=limits.depth)
        written = stream.write(raw)
        if written != len(raw):
            raise DataFormatError("incomplete_output")
        total += len(raw)
        file_hash.update(raw)
        return raw

    emit(header)
    for row in records:
        count += 1
        if count > limits.records:
            raise DataFormatError("record_count_limit")
        key = _record(row, header["resources"], validators)
        if key in keys:
            raise DataFormatError("duplicate_source_id")
        keys.add(key)
        records_hash.update(emit(row))
    emit(
        {"kind": "trailer", "record_count": str(count), "records_sha256": records_hash.hexdigest()}
    )
    return DataReceipt(
        header["dataset_id"], count, records_hash.hexdigest(), file_hash.hexdigest(), total
    )


def csv_report_cell(value: str) -> str:
    """Formula escaping precedes standard csv.writer quoting; reports are lossy."""
    return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
