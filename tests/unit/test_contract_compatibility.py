from tools.check_compatibility import find_breaking_changes


def baseline() -> dict[str, object]:
    return {
        "paths": {"/v1/items": ["get", "post"]},
        "required_schema_fields": {"Item": ["id"]},
    }


def current() -> dict[str, object]:
    return {
        "components": {"schemas": {"Item": {"required": ["id", "name"]}}},
        "paths": {"/v1/items": {"get": {}, "post": {}, "patch": {}}},
    }


def test_additive_contract_change_is_compatible() -> None:
    assert find_breaking_changes(baseline(), current()) == ()


def test_removed_operation_and_required_field_are_breaking() -> None:
    candidate = current()
    candidate["paths"] = {"/v1/items": {"get": {}}}
    candidate["components"] = {"schemas": {"Item": {"required": []}}}
    assert find_breaking_changes(baseline(), candidate) == (
        "removed operation: POST /v1/items",
        "removed required field: Item.id",
    )


def test_removed_path_and_schema_are_breaking() -> None:
    candidate: dict[str, object] = {"components": {"schemas": {}}, "paths": {}}
    assert find_breaking_changes(baseline(), candidate) == (
        "removed path: /v1/items",
        "removed schema: Item",
    )
