from tools.generate_contracts import (
    build_openapi,
    canonical_json,
    generated_documents,
    load_source,
)


def test_contract_generation_is_deterministic() -> None:
    source = load_source()
    first = generated_documents(source)
    second = generated_documents(source)
    assert {path: canonical_json(value) for path, value in first.items()} == {
        path: canonical_json(value) for path, value in second.items()
    }


def test_openapi_uses_v31_and_stable_surfaces() -> None:
    document = build_openapi(load_source())
    assert document["openapi"] == "3.1.0"
    assert "/v1/capabilities" in document["paths"]
    assert "/v1/negotiation" in document["paths"]
    assert "ErrorEnvelope" in document["components"]["schemas"]


def test_declarative_changes_drive_openapi_and_standalone_schemas() -> None:
    source = load_source()
    schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    source["schemas"]["AddedView"] = schema
    source["schema_files"]["added-view.schema.json"] = "AddedView"
    source["paths"]["/v1/added"] = {"get": {"operationId": "added", "responses": {}}}
    before = canonical_json(source)
    documents = generated_documents(source)
    openapi = build_openapi(source)
    assert "/v1/added" in openapi["paths"]
    assert openapi["components"]["schemas"]["AddedView"] == schema
    assert any(
        path.name == "added-view.schema.json" and value == schema
        for path, value in documents.items()
    )
    openapi["components"]["schemas"]["AddedView"]["properties"].clear()
    assert canonical_json(source) == before


def test_schema_output_cannot_escape_generated_directory() -> None:
    import pytest

    source = load_source()
    source["schema_files"]["../outside.schema.json"] = "ErrorEnvelope"
    with pytest.raises(ValueError, match="invalid JSON Schema filename"):
        generated_documents(source)
