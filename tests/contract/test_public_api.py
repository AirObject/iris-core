"""Reject unreviewed client entry points and missing service mappings."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from tools.check_public_api import WHITELIST, _data_shape, command_surface, validate_mapping
from tools.public_api_policy import PYTHON_OPERATIONS


@pytest.mark.parametrize("change", ["add", "remove", "missing-operation", "typescript"])
def test_public_method_drift_fails_closed(change: str) -> None:
    reviewed = json.loads(WHITELIST.read_text())
    surface = copy.deepcopy(reviewed["surface"])
    methods = surface["python"]["modules"]["iris_memory_sdk.client"]["AsyncIrisMemoryClient"][
        "methods"
    ]
    mapping = {name: list(ops) for name, ops in PYTHON_OPERATIONS.items()}
    if change == "add":
        methods["get_store"] = "(self) -> 'Store'"
    elif change == "remove":
        del methods["recall"]
    elif change == "missing-operation":
        del surface["http"]["openapi"]["operations"]["recall"]
    else:
        surface["typescript"]["methods"].append("getRuntime")
    with pytest.raises(ValueError):
        validate_mapping(surface, mapping)


def test_nested_operator_entry_point_drift_is_visible() -> None:
    from iris_memory_core.cli import build_parser

    parser = build_parser()
    original = command_surface(parser)
    # A new operator flag must be visible even when unrelated business routes
    # are unchanged; the installed wheel comparison uses this same inventory.
    actions: list[Any] = list(parser._actions)
    subparsers = next(
        action
        for action in actions
        if hasattr(action, "choices") and isinstance(action.choices, dict)
    )
    subparsers.choices["init"].add_argument("--raw-sql")
    assert command_surface(parser) != original


def test_exception_attribute_cannot_hide_a_database_connection() -> None:
    import sqlite3

    connection = sqlite3.connect(":memory:")
    try:
        with pytest.raises(ValueError, match="non-data object"):
            _data_shape({"envelope": {"connection": connection}})
    finally:
        connection.close()
