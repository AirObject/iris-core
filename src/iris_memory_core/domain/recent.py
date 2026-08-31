"""RecentContextProjection domain rules (§9.1, Phase 3.1).

The projection is a deterministic, rebuildable window over COMMITTED
observations — never a fact source (ADR-0001). The builder below is a pure
function: the same observation set, source watermark, builder version and
token estimator always produce byte-identical refs, segments, token estimate
and result hash. Summary segments carry SOURCE REFS ONLY: without a provider
there is no free-text summarization, only deterministic window compaction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol

from iris_memory_core.domain.hashing import content_hash
from iris_memory_core.domain.observation import StoredObservation

#: Bump on any change to window selection, ordering, segments or hashing;
#: a new version rebuilds through the shadow path before the pointer swaps.
RECENT_BUILDER_VERSION = 1

RECENT_PROJECTION_TTL_US = 3_600_000_000  # one hour
DEFAULT_RECENT_TOKEN_BUDGET = 4_000
DEFAULT_RECENT_MAX_OBSERVATIONS = 200
DEFAULT_RECENT_SEGMENT_SOURCES = 40


class InvalidRecentTargetError(ValueError):
    """Raised when a projection target cannot form a legal scope."""


class TokenEstimator(Protocol):
    """Injectable deterministic token estimator (§18.6)."""

    def estimate(self, text: str) -> int: ...


class DefaultTokenEstimator:
    """Characters-over-four heuristic; deterministic and dependency-free."""

    def estimate(self, text: str) -> int:
        return max(1, math.ceil(len(text) / 4)) if text else 0


@dataclass(frozen=True, slots=True)
class RecentWindowPolicy:
    token_budget: int = DEFAULT_RECENT_TOKEN_BUDGET
    max_observations: int = DEFAULT_RECENT_MAX_OBSERVATIONS
    segment_sources: int = DEFAULT_RECENT_SEGMENT_SOURCES
    ttl_us: int = RECENT_PROJECTION_TTL_US


@dataclass(frozen=True, slots=True)
class ObservationRef:
    observation_id: str
    revision: int
    occurred_us: int
    token_estimate: int


@dataclass(frozen=True, slots=True)
class SummarySegment:
    """Compacted tail of the window: source refs only, never free text.

    A segment states WHICH observations it covers and how many tokens the
    covered window represented; it cannot fabricate a summary and can never
    serve as independent Claim Evidence (§9.1).
    """

    segment_id: str
    source_refs: tuple[ObservationRef, ...]
    token_estimate: int

    @property
    def covers(self) -> int:
        return len(self.source_refs)


@dataclass(frozen=True, slots=True)
class BuiltProjection:
    """The deterministic output of one builder run over committed observations."""

    builder_version: int
    source_watermark: int
    head_observation_id: str | None
    tail_observation_id: str | None
    hot_observation_refs: tuple[ObservationRef, ...]
    summary_segments: tuple[SummarySegment, ...]
    token_estimate: int
    result_hash: str

    def referenced_refs(self) -> tuple[ObservationRef, ...]:
        refs: list[ObservationRef] = []
        for segment in self.summary_segments:
            refs.extend(segment.source_refs)
        refs.extend(self.hot_observation_refs)
        return tuple(refs)

    def referenced_ids(self) -> tuple[str, ...]:
        return tuple(ref.observation_id for ref in self.referenced_refs())


@dataclass(frozen=True, slots=True)
class StoredGeneration:
    """A persisted, verified generation row plus its pointer metadata."""

    generation_id: str
    target_key: str
    tenant_id: str
    agent_id: str
    space_group_id: str | None
    space_id: str
    session_id: str | None
    projection: BuiltProjection
    status: str
    created_us: int
    expires_us: int | None


@dataclass(frozen=True, slots=True)
class RecentContextView:
    """What readers see: the projection plus its provenance (§9.1)."""

    tenant_id: str
    agent_id: str
    space_id: str
    session_id: str | None
    space_group_id: str | None = None
    projection: BuiltProjection | None = None
    #: "generation" — served from a verified stored generation; "canonical" —
    #: the stored generation was missing/stale/broken and the window was read
    #: straight from committed observations (the safe fallback).
    source: str = "canonical"
    expires_us: int | None = None
    degraded_reason: str | None = field(default=None)


def recent_target_key(
    tenant_id: str,
    agent_id: str,
    space_id: str,
    session_id: str | None,
    space_group_id: str | None = None,
) -> str:
    """Canonical identity of one projection target (pointer primary key).

    A stable, NULL-free string keeps SQLite uniqueness exact: composite
    UNIQUE keys treat NULLs as distinct, which would allow duplicate pointers
    for the session-less target.
    """
    if not tenant_id or not agent_id or not space_id:
        raise InvalidRecentTargetError("recent context target needs tenant, agent and space")
    return "|".join((tenant_id, agent_id, space_group_id or "", space_id, session_id or ""))


def validate_target(space_id: str | None, session_id: str | None) -> tuple[str, str]:
    if space_id is None or not space_id:
        raise InvalidRecentTargetError("recent context target requires space_id")
    if session_id is not None and not session_id:
        raise InvalidRecentTargetError("session_id must be non-empty when present")
    return space_id, session_id  # type: ignore[return-value]


def _window_order(item: StoredObservation) -> tuple[int, str]:
    """Stable ordering key; independent of outbox arrival order (§9.1)."""
    return (item.occurred_us, item.id)


def projection_result_hash(
    *,
    builder_version: int,
    source_watermark: int,
    head_observation_id: str | None,
    tail_observation_id: str | None,
    hot_observation_refs: tuple[ObservationRef, ...],
    summary_segments: tuple[SummarySegment, ...],
    token_estimate: int,
) -> str:
    """Hash every persisted field that determines projection semantics."""
    return content_hash(
        {
            "builder_version": builder_version,
            "source_watermark": source_watermark,
            "head": head_observation_id,
            "tail": tail_observation_id,
            "hot": [
                {
                    "id": ref.observation_id,
                    "rev": ref.revision,
                    "occurred_us": ref.occurred_us,
                    "tok": ref.token_estimate,
                }
                for ref in hot_observation_refs
            ],
            "segments": [
                {
                    "segment_id": segment.segment_id,
                    "refs": [
                        {
                            "id": ref.observation_id,
                            "rev": ref.revision,
                            "occurred_us": ref.occurred_us,
                            "tok": ref.token_estimate,
                        }
                        for ref in segment.source_refs
                    ],
                    "token_estimate": segment.token_estimate,
                }
                for segment in summary_segments
            ],
            "token_estimate": token_estimate,
        }
    )


def build_projection(
    observations: list[StoredObservation],
    *,
    watermark: int,
    policy: RecentWindowPolicy,
    estimator: TokenEstimator,
    builder_version: int = RECENT_BUILDER_VERSION,
) -> BuiltProjection:
    """Deterministically build the hot window plus compacted tail segments.

    Newest observations fill the hot window until the token budget or the
    observation cap is reached; older observations within the fetched window
    are compacted into segments of bounded source-ref lists. Everything is a
    pure function of the inputs — no clock, no randomness.
    """
    ordered = sorted(observations, key=_window_order)
    tokened: list[tuple[StoredObservation, int]] = [
        (item, estimator.estimate(item.content or "")) for item in ordered
    ]
    hot: list[ObservationRef] = []
    used = 0
    cursor = len(tokened) - 1
    while cursor >= 0 and len(hot) < policy.max_observations:
        item, tokens = tokened[cursor]
        if used + tokens > policy.token_budget and hot:
            break
        hot.append(
            ObservationRef(
                observation_id=item.id,
                revision=item.revision,
                occurred_us=item.occurred_us,
                token_estimate=tokens,
            )
        )
        used += tokens
        cursor -= 1
    hot.reverse()  # oldest-first inside the hot window
    # Remaining older observations become bounded source-ref segments. The
    # chunk endpoint is clamped to the un-windowed prefix — a plain slice
    # would run past `cursor` and re-cover hot-window observations.
    segments: list[SummarySegment] = []
    for start in range(0, cursor + 1, policy.segment_sources):
        chunk = tokened[start : min(start + policy.segment_sources, cursor + 1)]
        refs = tuple(
            ObservationRef(
                observation_id=item.id,
                revision=item.revision,
                occurred_us=item.occurred_us,
                token_estimate=tokens,
            )
            for item, tokens in chunk
        )
        segments.append(
            SummarySegment(
                segment_id=f"seg:{start // policy.segment_sources}",
                source_refs=refs,
                token_estimate=sum(ref.token_estimate for ref in refs),
            )
        )
    tail_id = hot[-1].observation_id if hot else None
    head_id = None
    if hot:
        head_id = hot[0].observation_id
    elif segments:
        head_id = segments[0].source_refs[0].observation_id if segments[0].source_refs else None
    token_estimate = sum(ref.token_estimate for ref in hot) + sum(
        segment.token_estimate for segment in segments
    )
    result_hash = projection_result_hash(
        builder_version=builder_version,
        source_watermark=watermark,
        head_observation_id=head_id,
        tail_observation_id=tail_id,
        hot_observation_refs=tuple(hot),
        summary_segments=tuple(segments),
        token_estimate=token_estimate,
    )
    return BuiltProjection(
        builder_version=builder_version,
        source_watermark=watermark,
        head_observation_id=head_id,
        tail_observation_id=tail_id,
        hot_observation_refs=tuple(hot),
        summary_segments=tuple(segments),
        token_estimate=token_estimate,
        result_hash=result_hash,
    )


def projection_invariants(projection: BuiltProjection) -> tuple[str, ...]:
    """Structural checks a built projection must pass before it can be stored.

    The shadow path builds first, validates with this function, and only then
    swaps the pointer atomically; a failed validation never touches storage.
    """
    problems: list[str] = []
    if projection.builder_version < 1:
        problems.append("builder_version must be positive")
    if projection.source_watermark < 0:
        problems.append("source_watermark must be non-negative")
    hot_ids = [ref.observation_id for ref in projection.hot_observation_refs]
    if len(set(hot_ids)) != len(hot_ids):
        problems.append("duplicate hot observation refs")
    refs = projection.referenced_refs()
    all_covered = [ref.observation_id for ref in refs]
    if any(not ref.observation_id for ref in refs):
        problems.append("observation ref id must be non-empty")
    if len(set(all_covered)) != len(all_covered):
        problems.append("observation referenced by both hot window and segments")
    if any(ref.revision < 1 for ref in refs):
        problems.append("observation ref revision must be positive")
    if any(ref.occurred_us < 0 for ref in refs):
        problems.append("observation ref occurred_us must be non-negative")
    if any(ref.token_estimate < 0 for ref in refs):
        problems.append("observation ref token_estimate must be non-negative")
    segment_ids = [segment.segment_id for segment in projection.summary_segments]
    if any(
        not segment.segment_id or not segment.source_refs for segment in projection.summary_segments
    ):
        problems.append("summary segments need an id and at least one source ref")
    if len(set(segment_ids)) != len(segment_ids):
        problems.append("duplicate summary segment ids")
    if any(
        segment.token_estimate != sum(ref.token_estimate for ref in segment.source_refs)
        for segment in projection.summary_segments
    ):
        problems.append("summary segment token_estimate does not equal its source refs")
    recomputed = sum(ref.token_estimate for ref in projection.hot_observation_refs) + sum(
        segment.token_estimate for segment in projection.summary_segments
    )
    if recomputed != projection.token_estimate:
        problems.append("token_estimate does not equal the sum of its parts")
    if projection.tail_observation_id is not None and (
        not hot_ids or projection.tail_observation_id != hot_ids[-1]
    ):
        problems.append("tail ref must be the newest hot ref")
    if projection.token_estimate < 0:
        problems.append("token_estimate must be non-negative")
    expected_hash = projection_result_hash(
        builder_version=projection.builder_version,
        source_watermark=projection.source_watermark,
        head_observation_id=projection.head_observation_id,
        tail_observation_id=projection.tail_observation_id,
        hot_observation_refs=projection.hot_observation_refs,
        summary_segments=projection.summary_segments,
        token_estimate=projection.token_estimate,
    )
    if projection.result_hash != expected_hash:
        problems.append("result_hash does not match projection contents")
    return tuple(problems)
