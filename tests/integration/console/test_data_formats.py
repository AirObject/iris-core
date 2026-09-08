"""Untrusted byte streams cannot obtain a verified receipt by partial parsing."""

from __future__ import annotations

import hashlib
import io
import json
from typing import Any

import pytest

from iris_memory_core.application.console.data_formats import (
    DataFormatError,
    FormatLimits,
    csv_report_cell,
    parse_data_package,
    strict_json,
    write_data_package,
)

HEADER: dict[str, Any] = {
    "kind": "header",
    "format": "imc-data",
    "format_version": 1,
    "dataset_id": "dataset_example",
    "exported_at": "2026-09-05T08:00:00.000000Z",
    "mode": "data",
    "resources": ["note"],
}
RECORD: dict[str, Any] = {
    "kind": "record",
    "source_id": "legacy-note-12",
    "resource_type": "note",
    "source_scope": {"agent_ref": "old-agent", "space_ref": None},
    "source_occurred_at": None,
    "payload": {"title": "原始便签", "body": "用户记录", "kind": "follow_up"},
    "source_refs": [],
}


def note(payload: dict[str, Any]) -> None:
    if set(payload) != {"title", "body", "kind"} or not all(
        isinstance(value, str) for value in payload.values()
    ):
        raise ValueError("fixture's fixed business whitelist")


VALIDATORS = {"note": note}


def line(row: dict[str, Any]) -> bytes:
    return (json.dumps(row, ensure_ascii=False) + "\n").encode()


def package(records: list[dict[str, Any]], header: dict[str, Any] | None = None) -> bytes:
    raw = b"".join(line(row) for row in records)
    return (
        line(header or HEADER)
        + raw
        + line(
            {
                "kind": "trailer",
                "record_count": str(len(records)),
                "records_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    )


def read(raw: bytes, **kwargs: Any) -> Any:
    return parse_data_package(
        io.BytesIO(raw), validators=VALIDATORS, on_record=lambda row: None, **kwargs
    )


def test_spec_bytes_and_export_roundtrip_preserve_data_and_counts() -> None:
    incoming = package([RECORD])
    staged: list[dict[str, Any]] = []
    receipt = parse_data_package(
        io.BytesIO(incoming), validators=VALIDATORS, on_record=staged.append
    )
    assert staged == [RECORD]
    assert receipt.record_count == 1
    assert receipt.file_bytes == len(incoming)
    assert receipt.file_sha256 == hashlib.sha256(incoming).hexdigest()
    assert receipt.records_sha256 == hashlib.sha256(line(RECORD)).hexdigest()
    output = io.BytesIO()
    written = write_data_package(output, HEADER, iter(staged), validators=VALIDATORS)
    reread: list[dict[str, Any]] = []
    verified = parse_data_package(
        io.BytesIO(output.getvalue()), validators=VALIDATORS, on_record=reread.append
    )
    assert written == verified
    assert reread == staged
    # Hashing is on source bytes, not decoded objects or reserialized JSON.
    assert written.records_sha256 != receipt.records_sha256


@pytest.mark.parametrize(
    "raw,code",
    [
        (b'{"x":1,"x":2}', "duplicate_json_key"),
        (b'{"x":{"a":1,"a":2}}', "duplicate_json_key"),
        (b'{"x":NaN}', "non_finite_number"),
        (b'{"x":Infinity}', "non_finite_number"),
        (b'{"x":-Infinity}', "non_finite_number"),
        (b'{"x":1e999}', "non_finite_number"),
        (b'{"x":"\\u0000"}', "nul_character"),
        (b'{"\\u0000":1}', "nul_character"),
        (b'{"x":"\\ud800"}', "invalid_json"),
        (b'{"x":"\xff"}', "invalid_json"),
        (b"[]", "expected_json_object"),
        (b"SQLite format 3\0", "invalid_json"),
        (b"PK\x03\x04", "invalid_json"),
        (b"!!python/object:foo", "invalid_json"),
    ],
)
def test_invalid_json_is_rejected_without_echoing_input(raw: bytes, code: str) -> None:
    with pytest.raises(DataFormatError, match=f"^{code}$"):
        strict_json(raw)


def test_depth_checked_before_recursive_parser_and_string_brackets_ignored() -> None:
    assert strict_json(b'{"body":"\\\\u0000"}')["body"] == r"\u0000"
    assert strict_json(b'{"body":"[[[[\\"{{{{"}', max_depth=1)["body"]
    strict_json(b'{"x":' * 19 + b"{}" + b"}" * 19)
    with pytest.raises(DataFormatError, match="json_depth_limit"):
        strict_json(b'{"x":' * 20 + b"{}" + b"}" * 20)
    with pytest.raises(DataFormatError, match="json_depth_limit"):
        strict_json(b'{"x":' * 100_000)


@pytest.mark.parametrize(
    "kind",
    [
        "credential",
        "session",
        "permission",
        "provider_config",
        "secret",
        "settings",
        "audit",
        "tombstone",
        "idempotency",
        "outbox",
        "schedule",
        "delivery",
        "usage",
        "fts",
        "vector",
        "profile",
        "graph",
        "recent",
        "cache",
        "sql",
        "unknown",
    ],
)
def test_unknown_or_internal_types_cannot_be_smuggled_in_valid_jsonl(kind: str) -> None:
    with pytest.raises(DataFormatError, match="unsupported_resource"):
        read(package([RECORD | {"resource_type": kind}]))
    with pytest.raises(DataFormatError, match="unsupported_resource"):
        read(package([], HEADER | {"resources": [kind]}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", "foreign"),
        ("admin", True),
        ("secret", "hidden"),
        ("source_scope", {"agent_ref": "a", "space_ref": None, "admin": True}),
        ("source_refs", [{"url": "https://example.test/data"}]),
        ("source_occurred_at", "2026-09-05T08:00:00"),
        ("payload", {"title": "hello", "body": "text", "kind": "follow_up", "token": "hidden"}),
    ],
)
def test_unknown_security_fields_and_invalid_scope_time_fail_closed(field: str, value: Any) -> None:
    with pytest.raises(DataFormatError):
        read(package([RECORD | {field: value}]))


def test_trailer_duplicates_eof_and_stream_limits() -> None:
    raw = package([RECORD])
    for broken in [
        raw[:-1],
        raw + b"\n",
        line(HEADER) + line(RECORD),
        raw.replace(b'"record_count": "1"', b'"record_count": "2"'),
    ]:
        with pytest.raises(DataFormatError):
            read(broken)
    with pytest.raises(DataFormatError, match="duplicate_source_id"):
        read(package([RECORD, RECORD]))
    with pytest.raises(DataFormatError, match="file_size_limit"):
        read(raw, limits=FormatLimits(file_bytes=len(raw) - 1))
    with pytest.raises(DataFormatError, match="record_size_limit"):
        read(raw, limits=FormatLimits(record_bytes=16))
    with pytest.raises(DataFormatError, match="record_count_limit"):
        read(package([RECORD, RECORD | {"source_id": "second"}]), limits=FormatLimits(records=1))
    assert read(raw, limits=FormatLimits(file_bytes=len(raw))).record_count == 1


def test_short_writes_and_untrusted_incomplete_staging_never_receive_receipts() -> None:
    class ShortWriter(io.BytesIO):
        def write(self, value: Any) -> int:
            return super().write(value[:-1])

    with pytest.raises(DataFormatError, match="incomplete_output"):
        write_data_package(ShortWriter(), HEADER, [RECORD], validators=VALIDATORS)
    staged: list[dict[str, Any]] = []
    with pytest.raises(DataFormatError, match="incomplete_package"):
        parse_data_package(
            io.BytesIO(line(HEADER) + line(RECORD)), validators=VALIDATORS, on_record=staged.append
        )
    assert staged == [RECORD]  # Still untrusted; no receipt exists to authorize review/commit.


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_csv_report_formulas_are_escaped_before_csv_quoting(prefix: str) -> None:
    assert csv_report_cell(prefix + "SUM(1,2)") == "'" + prefix + "SUM(1,2)"
    assert csv_report_cell("普通文本") == "普通文本"
