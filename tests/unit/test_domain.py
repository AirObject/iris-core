from uuid import UUID

from iris_memory_core.domain import ResourceId


def test_resource_id_round_trip() -> None:
    raw = "018f7f98-3f4a-7f11-8b66-4fd96b2d6671"
    resource_id = ResourceId.parse(raw)
    assert resource_id.value == UUID(raw)
    assert str(resource_id) == raw
