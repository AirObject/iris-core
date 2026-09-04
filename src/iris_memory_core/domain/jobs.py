"""Durable job-kind registry and coalescing rules (§16.4, §17.4).

The registry is the single authority for which job kinds exist, which may
coalesce (whitelist: state/profile/graph refresh), which lane they run in,
and which kinds have a safe handler enabled in this phase. Kinds whose
handlers arrive in later phases stay registered but disabled: workers must
not claim them (§16 rolling-deploy rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from iris_memory_core.domain.errors import InvalidRequestError

# Every outbox payload and tick envelope carries an explicit format version;
# workers fail closed on unknown versions (leave pending, never mis-execute).
JOB_PAYLOAD_VERSION = 1
TICK_PAYLOAD_VERSION = 1

MAX_JOB_KIND_LENGTH = 128
MAX_PAYLOAD_BYTES = 1_000_000

#: Statuses that still owe work; pressure gauges and coalesce searches only
#: count these (§16.5).
UNSETTLED_STATUSES = ("pending", "leased", "retryable")


class JobLane(StrEnum):
    NORMAL = "normal"
    SAFETY = "safety"


class CoalesceClass(StrEnum):
    STATE = "state"
    PROFILE = "profile"
    GRAPH = "graph"
    FTS = "fts"
    VECTOR = "vector"


@dataclass(frozen=True, slots=True)
class JobKindSpec:
    kind: str
    default_priority: int
    lane: JobLane
    coalesce_class: CoalesceClass | None
    default_catch_up: str
    handler_enabled: bool = False
    notes: str = ""


class UnknownJobKindError(InvalidRequestError):
    """The kind is not registered at all — refused at enqueue time."""


class CoalescingNotAllowedError(InvalidRequestError):
    """A coalesce key was supplied for a kind on the forbidden list (§16.4)."""


def _spec(
    kind: str,
    *,
    priority: int,
    lane: JobLane = JobLane.NORMAL,
    coalesce: CoalesceClass | None = None,
    catch_up: str = "latest",
    enabled: bool = False,
    notes: str = "",
) -> tuple[str, JobKindSpec]:
    if not 0 <= priority <= 9:
        raise ValueError("priority must be within 0..9")
    return kind, JobKindSpec(
        kind=kind,
        default_priority=priority,
        lane=lane,
        coalesce_class=coalesce,
        default_catch_up=catch_up,
        handler_enabled=enabled,
        notes=notes,
    )


# §17.4 periodic kinds plus the Phase 2 spine kinds. Coalescing is a
# whitelist (ADR-0009): only state/profile/graph refresh may drop-and-merge
# pending work; observation refinement, task occurrences, persona proposals,
# forget and audit-adjacent work must never be discarded.
_KINDS: dict[str, JobKindSpec] = dict(
    [
        # Phase 3: enabled with real, safe handlers (rebuild/decay/pointer
        # checks); coalescing merges pending PROJECTION work only.
        _spec(
            "recent_context.maintenance",
            priority=5,
            coalesce=CoalesceClass.STATE,
            enabled=True,
            notes="Phase 3: deterministic rebuild of one target's window.",
        ),
        # §17.4: Focus Maintenance catch-up default is "coalesce", matching
        # Memory Reconciliation (decay/tidy work collapses to the newest run).
        _spec(
            "focus.maintenance",
            priority=5,
            coalesce=CoalesceClass.STATE,
            catch_up="coalesce",
            enabled=True,
            notes="Phase 3: idempotent decay/dormant/expiry sweep per agent.",
        ),
        _spec(
            "state.projection",
            priority=6,
            coalesce=CoalesceClass.STATE,
            enabled=True,
            notes="Phase 3: state current-pointer invariant check (no derived "
            "state table exists yet; the job validates the pointer it names).",
        ),
        _spec("profile.refresh", priority=6, coalesce=CoalesceClass.PROFILE),
        _spec("graph.refresh", priority=6, coalesce=CoalesceClass.GRAPH),
        # Phase 4: enabled with real, safe handlers. note.review sweeps due
        # notes (wake snoozes, associate duplicates, deterministic proposed
        # task promotion); task.trigger_scan computes occurrences and creates
        # due CognitiveEvents idempotently. Both catch_up=all per §17.4.
        _spec(
            "note.review",
            priority=4,
            catch_up="all",
            enabled=True,
            notes="Phase 4: bounded review sweep per agent (wake/duplicate/promote).",
        ),
        _spec(
            "task.trigger_scan",
            priority=4,
            catch_up="all",
            enabled=True,
            notes="Phase 4: occurrence computation + due CognitiveEvent creation.",
        ),
        # Phase 4 pointer invariant checks (the note/task/event coalesced
        # streams' invariant debt, mirroring state.projection). NOT
        # coalescable: occurrences and deliveries must never be drop-merged.
        _spec(
            "note.changed",
            priority=6,
            enabled=True,
            notes="Phase 4: note current-pointer invariant check.",
        ),
        _spec(
            "task.changed",
            priority=6,
            enabled=True,
            notes="Phase 4: task/step/dependency/trigger pointer invariant check.",
        ),
        _spec(
            "cognitive_event.changed",
            priority=6,
            enabled=True,
            notes="Phase 4: event delivery revision invariant check.",
        ),
        # Phase 5: pointer invariant checks for the long-term memory
        # aggregates (same shape as note.changed/task.changed) and the
        # tombstone-effect verifier for forget invalidations. All carry real,
        # idempotent handlers; retention.compaction runs the §19.5 sweep.
        _spec(
            "claim.changed",
            priority=6,
            enabled=True,
            notes="Phase 5: claim current-pointer invariant check.",
        ),
        _spec(
            "episode.changed",
            priority=6,
            enabled=True,
            notes="Phase 5: episode current-pointer invariant check.",
        ),
        _spec(
            "relation.changed",
            priority=6,
            enabled=True,
            notes="Phase 5: relation current-pointer invariant check.",
        ),
        _spec(
            "memory.invalidated",
            priority=1,
            enabled=True,
            notes="Phase 5: verify each invalidated resource is non-current "
            "under the recorded tombstone watermark (fail closed).",
        ),
        _spec(
            "retention.compaction",
            priority=8,
            catch_up="latest",
            enabled=True,
            notes="Phase 5: §19.5 retention sweep (decay/archive/delete via "
            "the Forget machinery; protected resources skipped).",
        ),
        _spec("episode.consolidation", priority=7, catch_up="latest"),
        _spec("memory.reconciliation", priority=7, catch_up="coalesce"),
        # Phase 6: FTS projection maintenance. fts.apply consumes the
        # refs-only change events (claim/episode/note.changed plus
        # memory.invalidated) and coalesces per resource; fts.rebuild runs
        # the shadow rebuild + verified atomic switch; fts.cleanup performs
        # the async physical deletion of logically invalidated documents and
        # retired generations (ADR-0014 §2, §11).
        _spec(
            "fts.apply",
            priority=5,
            coalesce=CoalesceClass.FTS,
            enabled=True,
            notes="Phase 6: upsert/invalidate one resource's FTS document "
            "inside the current verified generation.",
        ),
        _spec(
            "fts.rebuild",
            priority=3,
            catch_up="latest",
            enabled=True,
            notes="Phase 6: full shadow rebuild with checksum verification "
            "and atomic current-pointer switch.",
        ),
        _spec(
            "fts.cleanup",
            priority=8,
            catch_up="latest",
            enabled=True,
            notes="Phase 6: physical cleanup of invalid documents and retired generations.",
        ),
        # Phase 7: vector projection maintenance (ADR-0015 §8). vector.apply
        # consumes the refs-only change events (coalesced per resource, same
        # events FTS consumes) and maintains the id map + delta ledger — the
        # serving FAISS handle is never mutated; vector.rebuild runs the six
        # stage generation pipeline with the provider calls OUTSIDE the
        # fenced transaction (only the switch lands in the commit); vector
        # .cleanup performs the async physical deletion of invalidated id map
        # rows, retired generations and orphan directories.
        _spec(
            "vector.apply",
            priority=5,
            coalesce=CoalesceClass.VECTOR,
            enabled=True,
            notes="Phase 7: id map + delta ledger maintenance for one resource.",
        ),
        _spec(
            "vector.rebuild",
            priority=3,
            catch_up="latest",
            enabled=True,
            notes="Phase 7: FAISS generation build, verification and fenced atomic pointer switch.",
        ),
        _spec(
            "vector.cleanup",
            priority=8,
            catch_up="latest",
            enabled=True,
            notes="Phase 7: physical cleanup of invalidated id map rows, "
            "retired generations and orphan directories.",
        ),
        # Phase 8: profile/graph projection maintenance (ADR-0016 §7).
        # Applies maintain the CURRENT generation in place (per-resource
        # coalesced); rebuilds are deterministic shadow rebuilds with a
        # fenced pointer CAS; cleanup verifies then retires old generations
        # past the rollback window. Payload version 1, refs-only.
        _spec(
            "graph.apply",
            priority=5,
            coalesce=CoalesceClass.GRAPH,
            enabled=True,
            notes="Phase 8: re-derive one canonical resource's edges in the "
            "current graph generation (fail-closed tombstone re-check).",
        ),
        _spec(
            "graph.rebuild",
            priority=3,
            catch_up="latest",
            enabled=True,
            notes="Phase 8: deterministic shadow graph rebuild + fenced pointer CAS.",
        ),
        _spec(
            "graph.cleanup",
            priority=8,
            catch_up="latest",
            enabled=True,
            notes="Phase 8: verify the graph generation then delete retired "
            "generations beyond the rollback window.",
        ),
        _spec(
            "profile.apply",
            priority=5,
            coalesce=CoalesceClass.PROFILE,
            enabled=True,
            notes="Phase 8: re-derive one subject's profile fields in the current generation.",
        ),
        _spec(
            "profile.rebuild",
            priority=3,
            catch_up="latest",
            enabled=True,
            notes="Phase 8: deterministic shadow profile rebuild + fenced pointer CAS.",
        ),
        _spec(
            "profile.cleanup",
            priority=8,
            catch_up="latest",
            enabled=True,
            notes="Phase 8: verify the profile generation then delete "
            "retired generations beyond the rollback window.",
        ),
        # Phase 9: Persona notifications are durable refs-only messages; the
        # handlers verify the referenced immutable revision before marking
        # delivery complete. Transport fan-out arrives in Phase 10. State
        # expiry creates a deterministic baseline revision under fencing.
        _spec(
            "persona.revised",
            priority=2,
            enabled=True,
            notes="Phase 9: persona.revised.v1 notification invariant check.",
        ),
        _spec(
            "persona.revision_invalidated",
            priority=1,
            enabled=True,
            notes="Phase 9: revision-invalidated notification invariant check.",
        ),
        _spec(
            "persona.state_expire",
            priority=4,
            enabled=True,
            notes="Phase 9: deterministically return expired Persona State to baseline.",
        ),
        _spec("reflection.generate", priority=7, catch_up="latest"),
        _spec("persona.evaluation", priority=6, catch_up="latest"),
        _spec("backup.execute", priority=3, catch_up="all"),
        # Phase 2 spine kinds.
        _spec(
            "observation.recorded",
            priority=2,
            enabled=True,
            notes="Emitted atomically with each accepted observation; Phase 3 "
            "handler verifies the committed fact and schedules the target's "
            "recent-context rebuild.",
        ),
        _spec(
            "forget.execute",
            priority=0,
            lane=JobLane.SAFETY,
            notes="Safety lane: bounded priority channel under backpressure.",
        ),
        _spec("correct.apply", priority=0, lane=JobLane.SAFETY),
        _spec(
            "surface.lease_revoked",
            priority=1,
            enabled=True,
            notes=(
                "Revocation notice after fencing (ADR-0010 §2). Enabled: the "
                "preemption transaction enqueues it unconditionally, so a "
                "disabled kind would leave an unclaimable job pinning "
                "oldest_pending_age and the backpressure quota. The handler "
                "verifies the revocation invariant; host push lands with the "
                "Phase 10 transport (ADR-0017 §3)."
            ),
        ),
        _spec(
            "maintenance.selfcheck",
            priority=8,
            enabled=True,
            notes="Phase 2 seed handler: read-only spine consistency check.",
        ),
    ]
)

JOB_KINDS: frozenset[str] = frozenset(_KINDS)

# Kinds a worker may claim in this build: only handlers that already exist
# and are safe (phase-2 doc: 只启用当前已有且安全的 Handler).
ENABLED_JOB_KINDS: frozenset[str] = frozenset(
    kind for kind, spec in _KINDS.items() if spec.handler_enabled
)


def spec_for(kind: str) -> JobKindSpec:
    try:
        return _KINDS[kind]
    except KeyError:
        raise UnknownJobKindError(f"unknown job kind: {kind}") from None


def coalescing_allowed(kind: str) -> bool:
    return spec_for(kind).coalesce_class is not None


def require_coalesce_key(kind: str, coalesce_key: str | None) -> str | None:
    """Validate a coalesce key against the whitelist; None means no merging."""
    spec = spec_for(kind)
    if coalesce_key is None:
        return None
    if spec.coalesce_class is None:
        raise CoalescingNotAllowedError(
            f"job kind {kind} is on the coalescing forbidden list",
            details={"job_kind": kind},
        )
    if not coalesce_key or len(coalesce_key) > 256:
        raise CoalescingNotAllowedError("coalesce_key must be 1..256 characters")
    return coalesce_key


def lane_for(kind: str) -> JobLane:
    return spec_for(kind).lane


@dataclass(frozen=True, slots=True)
class NewOutboxJob:
    """An enqueue request before persistence (payload stays a domain object)."""

    tenant_id: str
    job_kind: str
    aggregate_type: str
    aggregate_id: str
    source_revision: int
    payload: dict[str, object]
    dedupe_key: str
    payload_version: int = JOB_PAYLOAD_VERSION
    agent_id: str | None = None
    coalesce_key: str | None = None
    priority: int = 5
    lane: JobLane = JobLane.NORMAL
    available_at_us: int = 0
    max_attempts: int = 8

    def __post_init__(self) -> None:
        if not 0 <= self.priority <= 9:
            raise ValueError("outbox priority must be within 0..9")
        if not 1 <= self.max_attempts <= 100:
            raise ValueError("max_attempts must be within 1..100")
        if not self.dedupe_key or len(self.dedupe_key) > 512:
            raise ValueError("dedupe_key must be 1..512 characters")
        if self.source_revision < 0:
            raise ValueError("source_revision must be non-negative")


@dataclass(frozen=True, slots=True)
class OutboxJob:
    """The persisted outbox row (§16.2 plus fencing and dedupe fields)."""

    id: str
    tenant_id: str
    job_kind: str
    aggregate_type: str
    aggregate_id: str
    source_revision: int
    payload: dict[str, object]
    payload_version: int
    dedupe_key: str
    coalesce_key: str | None
    priority: int
    lane: str
    status: str
    available_at_us: int
    attempt_count: int
    max_attempts: int
    lease_owner: str | None
    lease_generation: int
    lease_expires_us: int | None
    last_error_code: str | None
    replay_of: str | None
    created_us: int
    completed_us: int | None
    agent_id: str | None = None

    @property
    def leased_by(self) -> bool:
        return self.status == "leased"
