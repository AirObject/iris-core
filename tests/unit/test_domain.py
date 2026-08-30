import json
from uuid import UUID

import pytest

from iris_memory_core.domain import ResourceId


def test_resource_id_round_trip() -> None:
    raw = "018f7f98-3f4a-7f11-8b66-4fd96b2d6671"
    resource_id = ResourceId.parse(raw)
    assert resource_id.value == UUID(raw)
    assert str(resource_id) == raw


def test_idempotency_snapshots_are_versioned_and_forward_compatible() -> None:
    """Replay snapshots carry a format version and tolerate schema growth:
    unknown keys are ignored, missing optional fields fall back to defaults,
    and only a missing required field raises (naming the contract)."""
    from dataclasses import dataclass

    from iris_memory_core.domain.model import (
        SNAPSHOT_FORMAT_VERSION,
        record_restore,
        record_snapshot,
        snapshot_json,
    )

    @dataclass(frozen=True, slots=True)
    class _FutureRecord:
        id: str
        revision: int
        new_optional_field: str = "default"

    body = snapshot_json(_FutureRecord(id="a", revision=2, new_optional_field="set"))
    assert json.loads(body)["snapshot_version"] == SNAPSHOT_FORMAT_VERSION

    legacy = json.loads(body)["record"]
    del legacy["new_optional_field"]  # snapshot taken before the field existed
    legacy["unknown_future_key"] = "ignored"
    restored = record_restore(_FutureRecord, legacy)
    assert restored.new_optional_field == "default"
    assert restored.id == "a" and restored.revision == 2

    @dataclass(frozen=True, slots=True)
    class _StrictRecord:
        id: str
        required_later: str

    with pytest.raises(KeyError, match="required_later"):
        record_restore(_StrictRecord, {"id": "a"})

    with pytest.raises(ValueError, match="unsupported idempotency snapshot version"):
        record_restore(
            _FutureRecord,
            {"snapshot_version": SNAPSHOT_FORMAT_VERSION + 1, "record": legacy},
        )
    with pytest.raises(ValueError, match="record must be a JSON object"):
        record_restore(
            _FutureRecord,
            {"snapshot_version": SNAPSHOT_FORMAT_VERSION, "record": None},
        )

    # Bare (legacy, unenveloped) snapshots still restore.
    assert record_restore(_FutureRecord, {"id": "b", "revision": 1}).id == "b"
    # Envelope round trip for a real record is exercised end to end by the
    # identity idempotency replay tests.
    del record_snapshot
