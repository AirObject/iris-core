"""Streaming confirmation of complete runtime JSONL record boundaries.

Recovery accepts the runtime object shape (root plus context/attributes objects)
without retaining record bodies. Chunks are bounded by the current event budget;
existing records may be larger than the new emit limit. The parser retains only
bounded schema fields and scalar syntax, then confirms the complete runtime shape.
It never repairs bytes or accepts a trailing partial line, NaN, or infinity.
"""

import codecs
from datetime import datetime
from typing import cast
import json
import os
import re

from ._events import _Header, _UUID_TEXT, _normalize_fields
from ._results import _Failure
from ._rules import _ATTRIBUTE_FIELDS, _CONTEXT_FIELDS, _LEVEL_NUMBERS, _MESSAGES, _MODULES

_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")


class _Line:
    """Incremental object grammar with bounded nesting and field identifiers."""

    def __init__(self):
        self.stack: list[str] = []
        self.fields: list[int] = []
        self.mode = "start"
        self.token = ""
        self.capture = False
        self.string_key = False
        self.escape = False
        self.unicode_left = 0
        self.key = ""
        self.objects: list[dict[str, object]] = []
        self.keys: list[str] = []
        self.record: dict[str, object] = {}
        self.done = False

    def feed(self, char: str) -> None:
        if self.mode == "string":
            if ord(char) < 32:
                raise ValueError("Invalid runtime JSONL.")
            if self.capture:
                self.token += char
                if len(self.token) > 770:
                    raise ValueError("Invalid runtime JSONL.")
            if self.unicode_left:
                if char not in "0123456789abcdefABCDEF":
                    raise ValueError("Invalid runtime JSONL.")
                self.unicode_left -= 1
            elif self.escape:
                self.escape = False
                if char == "u":
                    self.unicode_left = 4
                elif char not in '"\\/bfnrt':
                    raise ValueError("Invalid runtime JSONL.")
            elif char == "\\":
                self.escape = True
            elif char == '"':
                if self.string_key:
                    key = json.loads(self.token)
                    if type(key) is not str or len(key) > 128:
                        raise ValueError("Invalid runtime JSONL.")
                    self.key = key
                    self.keys[-1] = key
                    self.stack[-1] = "colon"
                else:
                    if self.capture:
                        self._store(json.loads(self.token))
                    self._value_done()
                self.token = ""
                self.mode = "structural"
            return
        if self.mode == "scalar":
            if char not in " \t\r,}":
                self.token += char
                if len(self.token) > 32:
                    raise ValueError("Invalid runtime JSONL.")
                return
            if self.token not in ("true", "false", "null") and not _NUMBER.fullmatch(self.token):
                raise ValueError("Invalid runtime JSONL.")
            if self.capture:
                self._store(json.loads(self.token))
            self.token = ""
            self._value_done()
            self.mode = "structural"
        if char in " \t\r":
            return
        if self.done:
            raise ValueError("Invalid runtime JSONL.")
        if self.mode == "start":
            if char != "{":
                raise ValueError("Invalid runtime JSONL.")
            self.stack.append("key_or_end")
            self.fields.append(0)
            self.objects.append({})
            self.keys.append("")
            self.mode = "structural"
            return
        state = self.stack[-1]
        if state in ("key", "key_or_end"):
            if char == "}" and state == "key_or_end":
                self._end_object()
            elif char == '"':
                self.fields[-1] += 1
                if self.fields[-1] > 32:
                    raise ValueError("Invalid runtime JSONL.")
                self.mode = "string"
                self.string_key = self.capture = True
                self.token = '"'
            else:
                raise ValueError("Invalid runtime JSONL.")
        elif state == "colon":
            if char != ":":
                raise ValueError("Invalid runtime JSONL.")
            self.stack[-1] = "value"
        elif state == "value":
            self.capture = True
            if char == "{":
                if len(self.stack) >= 2 or self.keys[-1] not in ("context", "attributes"):
                    raise ValueError("Invalid runtime JSONL.")
                self.stack.append("key_or_end")
                self.fields.append(0)
                self.objects.append({})
                self.keys.append("")
            elif char == '"':
                self.mode = "string"
                self.string_key = False
                self.token = '"' if self.capture else ""
            elif char in "-0123456789tfn":
                self.mode = "scalar"
                self.token = char
            else:
                raise ValueError("Invalid runtime JSONL.")
        elif state == "comma_or_end":
            if char == ",":
                self.stack[-1] = "key"
            elif char == "}":
                self._end_object()
            else:
                raise ValueError("Invalid runtime JSONL.")

    def _store(self, value: object) -> None:
        key = self.keys[-1]
        if key in self.objects[-1]:
            raise ValueError("Invalid runtime JSONL.")
        self.objects[-1][key] = value

    def _value_done(self) -> None:
        self.stack[-1] = "comma_or_end"

    def _end_object(self) -> None:
        self.stack.pop()
        self.fields.pop()
        value = self.objects.pop()
        self.keys.pop()
        if self.stack:
            self._store(value)
            self._value_done()
        else:
            self.record = value
            self.done = True

    def complete(self) -> bool:
        return self.done and self.mode == "structural" and _valid_record(self.record)


def _valid_record(record: dict[str, object]) -> bool:
    """Confirm ownership by the fixed runtime schema without rebuilding an event."""
    required = {"schema_version", "category", "event_id", "timestamp", "level_name", "level_number",
                "logger", "event_code", "message", "redacted", "exception_omitted"}
    if not required <= record.keys() or record.keys() - required - {"context", "attributes"}:
        return False
    if type(record["schema_version"]) is not int or record["schema_version"] != 1 or record["category"] != "runtime":
        return False
    for key in ("event_id", "timestamp", "level_name", "logger", "event_code", "message"):
        if type(record[key]) is not str:
            return False
    identity, timestamp, level, logger, code = (cast(str, record[key]) for key in
                                               ("event_id", "timestamp", "level_name", "logger", "event_code"))
    if not _UUID_TEXT.fullmatch(identity) or logger not in _MODULES or level not in _LEVEL_NUMBERS:
        return False
    if type(record["level_number"]) is not int or record["level_number"] != _LEVEL_NUMBERS[level]:
        return False
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z", timestamp):
        return False
    try:
        datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    if type(record["redacted"]) is not bool or type(record["exception_omitted"]) is not bool:
        return False
    context, attributes = record.get("context", {}), record.get("attributes", {})
    if type(context) is not dict or type(attributes) is not dict:
        return False
    if context.keys() - set(_CONTEXT_FIELDS) or attributes.keys() - set(_ATTRIBUTE_FIELDS):
        return False
    fallback = code == "DIAGNOSTIC_FORMAT_FAILED"
    if fallback:
        if record["message"] != "诊断记录格式化失败。" or context or attributes or record["redacted"] is not True:
            return False
    elif code not in _MESSAGES or record["message"] != _MESSAGES[code]:
        return False
    header = _Header(level, _LEVEL_NUMBERS[level], code, cast(bool, record["redacted"]),
                     cast(bool, record["exception_omitted"]))
    return not isinstance(_normalize_fields({"context": context, "attributes": attributes}, header), _Failure)


def _check_jsonl(descriptor: int, chunk_bytes: int) -> None:
    """Consume an owned read descriptor without buffering a whole historical line."""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    line = _Line()
    since_lf = False
    while data := os.read(descriptor, chunk_bytes):
        text = decoder.decode(data)
        for char in text:
            if char == "\n":
                if not line.complete():
                    raise ValueError("Invalid runtime JSONL.")
                line = _Line()
                since_lf = False
            else:
                line.feed(char)
                since_lf = True
    decoder.decode(b"", final=True)
    if since_lf:
        raise ValueError("Invalid runtime JSONL.")
