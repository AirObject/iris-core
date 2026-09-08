"""Bounded observation context and evidence-backed grouped summaries (ADR-0054)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from iris_memory_core.domain.errors import InvalidRequestError
from iris_memory_core.domain.observation import StoredObservation


@dataclass(frozen=True, slots=True)
class ObservationContextConfig:
    auto_summary_enabled: bool = False
    summary_min_messages: int = 50
    summary_max_wait_seconds: int = 120
    summary_batch_size: int = 100
    background_retention_days: int = 30

    def __post_init__(self) -> None:
        if type(self.auto_summary_enabled) is not bool:
            raise ValueError("auto_summary_enabled must be boolean")
        for name, value, lower, upper in (
            ("summary_min_messages", self.summary_min_messages, 1, 500),
            ("summary_max_wait_seconds", self.summary_max_wait_seconds, 1, 86400),
            ("summary_batch_size", self.summary_batch_size, 1, 500),
            ("background_retention_days", self.background_retention_days, 0, 3650),
        ):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"{name} must be within {lower}..{upper}")
        if self.summary_min_messages > self.summary_batch_size:
            raise ValueError("summary_min_messages exceeds summary_batch_size")


@dataclass(frozen=True, slots=True)
class SummaryGroup:
    title: str
    summary: str
    observation_ids: tuple[str, ...]


def validate_summary_groups(
    value: Mapping[str, Any], observations: Sequence[StoredObservation]
) -> tuple[SummaryGroup, ...]:
    """Keep legacy providers compatible; grouped outputs must account for every source."""
    if not isinstance(value, Mapping):
        raise InvalidRequestError("summary output must be an object")
    available = {item.id for item in observations}
    if set(value) == {"title", "summary"}:
        raw_groups: object = [{**value, "observation_ids": [item.id for item in observations]}]
        ignored: object = []
    elif set(value) == {"groups", "ignored_observation_ids"}:
        raw_groups = value["groups"]
        ignored = value["ignored_observation_ids"]
    else:
        raise InvalidRequestError("summary output must contain groups and ignored sources")
    if not isinstance(raw_groups, list) or len(raw_groups) > 32:
        raise InvalidRequestError("summary groups must contain at most 32 groups")
    if not isinstance(ignored, list) or any(not isinstance(x, str) for x in ignored):
        raise InvalidRequestError("ignored source ids must be strings")
    covered: set[str] = set()
    result: list[SummaryGroup] = []
    fingerprints: set[tuple[str, ...]] = set()
    for group in raw_groups:
        if not isinstance(group, dict) or set(group) != {"title", "summary", "observation_ids"}:
            raise InvalidRequestError("invalid summary group fields")
        title, summary, ids = group["title"], group["summary"], group["observation_ids"]
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > 500
            or not isinstance(summary, str)
            or not summary.strip()
            or len(summary) > 16000
            or not isinstance(ids, list)
            or not ids
            or any(not isinstance(x, str) for x in ids)
        ):
            raise InvalidRequestError("invalid summary content or sources")
        if len(set(ids)) != len(ids) or not set(ids) <= available:
            raise InvalidRequestError("summary sources must be unique members of this batch")
        ordered = tuple(item.id for item in observations if item.id in ids)
        if ordered in fingerprints:
            raise InvalidRequestError("duplicate summary source group")
        fingerprints.add(ordered)
        covered.update(ids)
        result.append(SummaryGroup(title, summary, ordered))
    if len(ignored) != len(set(ignored)) or set(ignored) & covered:
        raise InvalidRequestError("ignored sources overlap or repeat")
    if covered | set(ignored) != available:
        raise InvalidRequestError("summary must account for every batch source")
    return tuple(result)
