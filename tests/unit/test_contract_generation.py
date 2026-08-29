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
