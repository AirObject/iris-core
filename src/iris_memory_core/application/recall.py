"""Structured Recall Orchestrator and the Phase 6 recall protocol (§18).

Phase 3/4 built the structured skeleton (recent/state/focus, then due
tasks); Phase 6 completes it (ADR-0014) without forking a second semantic:

- routes: ``tasks``, ``recent_context``, ``state``, ``focus``, ``claims``,
  ``relations``, ``fts`` — Persona is NEVER a route or candidate, it is
  returned as top-level revision/hash only;
- bounded-parallel route execution: every parallel route opens its OWN read
  unit of work (SQLite connections/transactions are never shared across
  threads), results merge in fixed route order for replay determinism;
- the FINAL canonical rehydrate runs in a FRESH read unit of work opened
  AFTER collection — it can observe Forget/Tombstone/Correct commits that
  landed while candidates were being collected (the stale-snapshot barrier);
- ranker v2 (versioned, deterministic, missing-score aware) with conflict/
  redundancy marking and protected-budget trimming;
- RecallService publishes the `/v1/recall` envelope (actors, purpose,
  deadline conversion, partial policy, usage persistence) and
  RecallUsageService validates/merges host usage reports.

Route naming: the wire enum freezes the INTERNAL names (ADR-0014 §3) —
``tasks`` (not the baseline's example ``task``) keeps its ADR-0011/0012
identity; no silently changed semantics.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from iris_memory_core.application.events import CognitiveEventService
from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.ports import (
    Clock,
    MonotonicClock,
    SystemMonotonicClock,
    Transaction,
    UnitOfWork,
)
from iris_memory_core.application.recent import RecentContextService
from iris_memory_core.application.state import StateService
from iris_memory_core.application.tasks import TaskService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    ConflictError,
    DeadlineExceededError,
    IdentityNotFoundError,
    InvalidRequestError,
    MinimumWatermarkUnavailableError,
    NotReadyError,
    RevisionMismatchError,
    ScopeViolationError,
)
from iris_memory_core.domain.focus import FocusStatus
from iris_memory_core.domain.fts import build_fts_query
from iris_memory_core.domain.hashing import canonical_json, content_hash
from iris_memory_core.domain.identity import ExternalIdentityKey
from iris_memory_core.domain.memory import (
    CLAIM_CURRENT_VISIBLE_STATUSES,
    SOURCE_AUTHORITY_RANK,
    SourceAuthority,
)
from iris_memory_core.domain.note import NoteStatus
from iris_memory_core.domain.observation import StoredObservation
from iris_memory_core.domain.privacy import evaluate_privacy
from iris_memory_core.domain.recall import (
    CATEGORY_PRIORITY,
    RECALL_RANKER_V3,
    ROUTE_CLAIMS,
    ROUTE_FOCUS,
    ROUTE_FTS,
    ROUTE_RECENT,
    ROUTE_RELATIONS,
    ROUTE_STATE,
    ROUTE_TASKS,
    ROUTE_VECTOR,
    TOKEN_ESTIMATOR_VERSION,
    RankerV3,
    apply_token_budgets,
)
from iris_memory_core.domain.recent import DefaultTokenEstimator, TokenEstimator
from iris_memory_core.domain.scope import Scope, scope_allows
from iris_memory_core.domain.task import TaskStatus
from iris_memory_core.domain.vector import (
    VECTOR_INDEXABLE_RESOURCE_TYPES,
    VECTOR_NON_RETRYABLE_REASONS,
    VECTOR_REASON_AS_OF_UNSUPPORTED,
    VECTOR_REASON_UNAVAILABLE,
    EmbeddingProviderError,
    VectorDegradedError,
)
from iris_memory_core.indexing.fts import FtsDegradedError, FtsProjectionService
from iris_memory_core.indexing.vector import VectorProjectionService

#: Bump when scoring weights, category priorities or budget logic change.
#: v1 was the Phase 3/4 skeleton; v2 added missing-score semantics, conflict
#: marking and protected budgets (ADR-0014 §5); v3 registers the vector
#: route in the hybrid fusion (ADR-0015 §6) — existing candidates rank
#: identically under v2 and v3.
RECALL_RANKER_VERSION = RECALL_RANKER_V3

DEFAULT_ROUTES = (
    ROUTE_TASKS,
    ROUTE_RECENT,
    ROUTE_STATE,
    ROUTE_FOCUS,
    ROUTE_CLAIMS,
    ROUTE_RELATIONS,
    ROUTE_FTS,
    ROUTE_VECTOR,
)

#: Equal share of the total budget as each route's sub-deadline. Parallel
#: routes all start at (approximately) the same instant; a route that
#: exceeds its share degrades without blocking the others (§18.4).
_ROUTE_SHARE = 1.0 / 3.0

#: Upper bound of concurrently running routes (ADR-0014 §3).
DEFAULT_ROUTE_CONCURRENCY = 4

DEFAULT_TASKS_CANDIDATES = 10
DEFAULT_RECENT_CANDIDATES = 12
DEFAULT_STATE_CANDIDATES = 8
DEFAULT_FOCUS_CANDIDATES = 10
DEFAULT_CLAIMS_CANDIDATES = 20
DEFAULT_RELATIONS_CANDIDATES = 12
DEFAULT_FTS_CANDIDATES = 20
DEFAULT_VECTOR_CANDIDATES = 20
#: Upper bound of pending event ids advertised on one recall response (§12).
DEFAULT_PENDING_EVENT_IDS = 50

#: Recency reference window for score normalization — deterministic given the
#: injected clock; replay tests pin the clock.
RECENCY_WINDOW_US = 3_600_000_000

#: Purposes accepted by the public request (§18.1).
RECALL_PURPOSES = frozenset({"reply", "planning", "reflection", "tool"})

#: Wire route names (ADR-0014 §3): internal names are the frozen contract.
FTS_AS_OF_REASON = "fts_as_of_unsupported"

#: Privacy labels a caller may name in requested_privacy_labels without the
#: candidate carrying them being excluded (structural tenant visibility).
_NON_NARROWING_LABELS = frozenset({"tenant"})


def _hash_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class RouteDeadlineExceeded(Exception):
    """Internal cooperative-cancel signal raised inside a route."""


@dataclass(frozen=True, slots=True)
class StructuredRecallRequest:
    """Internal request shape (the public /v1/recall request maps onto it)."""

    request_id: str
    agent_id: str
    space_id: str
    deadline_monotonic_us: int
    session_id: str | None = None
    #: Optional space-group narrowing: group-scoped documents become
    #: positively recallable; authorized against the access envelope.
    space_group_id: str | None = None
    token_budget: int = 2_000
    layer_budgets: dict[str, int] | None = None
    candidate_limits: dict[str, int] = field(default_factory=dict)
    minimum_watermark: int | None = None
    include_trace: bool = False
    allow_partial: bool = True
    state_namespaces: tuple[str, ...] = ("runtime", "environment", "topic")
    # --- Phase 6 public surface -------------------------------------------
    topic: str = ""
    purpose: str = "reply"
    categories: frozenset[str] | None = None
    resource_types: frozenset[str] | None = None
    requested_privacy_labels: frozenset[str] | None = None
    as_of_us: int | None = None
    #: Server-resolved current speaker entity (never caller-supplied).
    speaker_entity_id: str | None = None
    max_route_concurrency: int = DEFAULT_ROUTE_CONCURRENCY

    def __post_init__(self) -> None:
        # Negative numbers must not reach SQL: SQLite treats LIMIT -1 as
        # UNLIMITED, which would turn a malformed cap into an unbounded read.
        for route, limit in self.candidate_limits.items():
            if limit < 0:
                raise InvalidRequestError(
                    "candidate_limits must be non-negative",
                    details={"route": route, "limit": limit},
                )
        if self.token_budget < 0:
            raise InvalidRequestError("token_budget must be non-negative")
        for route, budget in (self.layer_budgets or {}).items():
            if budget < 0:
                raise InvalidRequestError(
                    "layer_budgets must be non-negative", details={"route": route}
                )
        if self.max_route_concurrency < 1:
            raise InvalidRequestError("max_route_concurrency must be >= 1")


@dataclass(frozen=True, slots=True)
class RecallCandidate:
    """Unified internal candidate (the §18.2 projection used everywhere).

    ``scores`` may carry ``None`` values: a missing component is EXCLUDED
    from the weighted mean (and reported via ``missing_components``) —
    missing is never zero (ADR-0014 §5).
    """

    candidate_id: str
    route: str
    resource_type: str
    resource_id: str
    resource_revision: int
    text: str
    scores: dict[str, float | None]
    final_score: float
    token_estimate: int
    occurred_us: int
    scope: Scope
    privacy_labels: tuple[str, ...]
    expires_us: int | None = None
    # --- Phase 6 additions ---------------------------------------------------
    content_hash: str = ""
    subject_entity_id: str | None = None
    category: str = ""
    conflict_group: str | None = None
    conflict_state: str | None = None
    missing_components: tuple[str, ...] = ()
    placement: str = "memory"

    @property
    def category_label(self) -> str:
        return self.category or self.route


@dataclass(frozen=True, slots=True)
class DegradedRoute:
    route: str
    reason_code: str
    retryable: bool
    fallback: str


@dataclass(frozen=True, slots=True)
class RouteTrace:
    route: str
    outcome: str
    candidate_count: int
    duration_us: int
    fallback: str | None = None


@dataclass(frozen=True, slots=True)
class RecallTrace:
    """Safe trace: id hashes, counts, versions and durations only (§31.3)."""

    request_hash: str
    ranker_version: int
    total_duration_us: int
    routes: tuple[RouteTrace, ...]
    rehydrated_out: int
    missing_score_components: int = 0


@dataclass(frozen=True, slots=True)
class StructuredRecallResult:
    request_id: str
    source_watermark: int
    completed_routes: tuple[str, ...]
    degraded_routes: tuple[DegradedRoute, ...]
    partial: bool
    candidates: tuple[RecallCandidate, ...]
    dropped_by_rehydrate: int
    trace: RecallTrace | None = None
    #: Undelivered CognitiveEvents visible at the request scope (§12). The
    #: host pulls them via the events endpoint; recall only advertises ids.
    pending_event_ids: tuple[str, ...] = ()
    # --- Phase 6 envelope additions ---------------------------------------
    persona_revision: int = 0
    persona_content_hash: str = ""
    #: Stage 1 of the usage accounting: every candidate the routes retrieved
    #: BEFORE rehydrate dropped any of them.
    retrieved_count: int = 0
    tombstone_watermark: int = 0


class RecallRoute(Protocol):
    """Route port — Phase 6 adds routes without touching existing semantics."""

    name: str

    def collect(
        self,
        tx: Transaction,
        request: StructuredRecallRequest,
        access: AccessContext,
        deadline_us: int,
        now_us: int,
    ) -> tuple[RecallCandidate, ...]: ...


def check_deadline(monotonic: MonotonicClock, deadline_us: int) -> None:
    if monotonic.monotonic_us() >= deadline_us:
        raise RouteDeadlineExceeded()


def _candidate_id(route: str, resource_id: str, revision: int) -> str:
    """Deterministic candidate id — identical across replays of one snapshot."""
    digest = hashlib.sha256(f"{route}:{resource_id}:{revision}".encode()).hexdigest()[:16]
    return f"cand:{digest}"


def stable_sort_key(candidate: RecallCandidate) -> tuple[float, int, int, str]:
    """§18.6: (-final_score, category_priority, occurred_at DESC, id ASC).

    Category priorities are the domain v2 mapping (ADR-0014 §5): due tasks
    first, then focus, claims, recent context, relations, state, FTS.
    """
    return (
        -candidate.final_score,
        CATEGORY_PRIORITY.get(candidate.route, 99),
        -candidate.occurred_us,
        candidate.resource_id,
    )


def _recency(now_us: int, occurred_us: int) -> float:
    age = max(0, now_us - occurred_us)
    return max(0.0, 1.0 - age / RECENCY_WINDOW_US)


def _authority_component(source_authority: str) -> float:
    try:
        rank = SOURCE_AUTHORITY_RANK[SourceAuthority(source_authority)]
    except ValueError:
        return 0.0
    return min(1.0, rank / 60.0)


def _passes_request_filters(
    request: StructuredRecallRequest,
    *,
    resource_type: str,
    category: str,
    privacy_labels: tuple[str, ...],
) -> bool:
    """Request-side narrowing filters (categories/resource types/privacy).

    ``requested_privacy_labels`` NARROWS: when provided, every label a
    candidate carries must be structural-tenant or explicitly requested —
    it can never widen what ``evaluate_privacy`` grants (ADR-0014 §5).
    """
    if request.resource_types is not None and resource_type not in request.resource_types:
        return False
    if request.categories is not None and category not in request.categories:
        return False
    if request.requested_privacy_labels is not None:
        allowed = request.requested_privacy_labels | _NON_NARROWING_LABELS
        if not all(label in allowed for label in privacy_labels):
            return False
    return True


class RecentContextRoute:
    """Hot observation window from the projection (or canonical fallback)."""

    def __init__(
        self,
        service: RecentContextService,
        monotonic: MonotonicClock,
        estimator: TokenEstimator | None = None,
    ) -> None:
        self.name = ROUTE_RECENT
        self._service = service
        self._monotonic = monotonic
        self._estimator = estimator or DefaultTokenEstimator()

    def collect(
        self,
        tx: Transaction,
        request: StructuredRecallRequest,
        access: AccessContext,
        deadline_us: int,
        now_us: int,
    ) -> tuple[RecallCandidate, ...]:
        # Even an EMPTY window must respect the deadline: a route whose
        # budget is already spent is degraded, never "completed with zero".
        check_deadline(self._monotonic, deadline_us)
        view = self._service.get_for_route(
            tx,
            access,
            agent_id=request.agent_id,
            space_id=request.space_id,
            session_id=request.session_id,
            minimum_watermark=request.minimum_watermark,
        )
        check_deadline(self._monotonic, deadline_us)
        assert view.projection is not None
        limit = request.candidate_limits.get(ROUTE_RECENT, DEFAULT_RECENT_CANDIDATES)
        refs = list(view.projection.hot_observation_refs)
        candidates: list[RecallCandidate] = []
        # Cap the newest first, present oldest-first (the window's own stable
        # order); the deadline is checked cooperatively per candidate.
        for ref in list(reversed(refs))[:limit]:
            check_deadline(self._monotonic, deadline_us)
            observation = tx.observations.get(ref.observation_id)
            recency = _recency(now_us, observation.occurred_us)
            text = observation.content or ""
            if not _passes_request_filters(
                request, resource_type="observation", category="recent", privacy_labels=()
            ):
                continue
            candidates.append(
                RecallCandidate(
                    candidate_id=_candidate_id(ROUTE_RECENT, observation.id, ref.revision),
                    route=ROUTE_RECENT,
                    resource_type="observation",
                    resource_id=observation.id,
                    resource_revision=ref.revision,
                    text=text,
                    scores={"recency": round(recency, 6)},
                    final_score=round(0.5 + 0.5 * recency, 6),
                    token_estimate=self._estimator.estimate(text),
                    occurred_us=observation.occurred_us,
                    scope=_observation_scope(observation),
                    privacy_labels=observation.privacy_labels,
                    placement="working",
                    content_hash=content_hash({"content": text})[:16],
                )
            )
        check_deadline(self._monotonic, deadline_us)
        candidates.reverse()
        return tuple(candidates)


class StateRoute:
    """Current, non-expired state values visible at the request scope."""

    def __init__(
        self,
        service: StateService,
        estimator: TokenEstimator | None = None,
        *,
        monotonic: MonotonicClock | None = None,
    ) -> None:
        self.name = ROUTE_STATE
        self._service = service
        self._estimator = estimator or DefaultTokenEstimator()
        self._monotonic = monotonic or SystemMonotonicClock()

    def collect(
        self,
        tx: Transaction,
        request: StructuredRecallRequest,
        access: AccessContext,
        deadline_us: int,
        now_us: int,
    ) -> tuple[RecallCandidate, ...]:
        check_deadline(self._monotonic, deadline_us)
        limit = request.candidate_limits.get(ROUTE_STATE, DEFAULT_STATE_CANDIDATES)
        candidates: list[RecallCandidate] = []
        for namespace in request.state_namespaces:
            # Cooperative deadline: one namespace list read per check.
            check_deadline(self._monotonic, deadline_us)
            entries = self._service.list_scope_in_tx(
                tx,
                access,
                agent_id=request.agent_id,
                namespace=namespace,
                space_id=request.space_id,
                session_id=request.session_id,
                limit=limit,
            )
            check_deadline(self._monotonic, deadline_us)
            for entry in entries:
                check_deadline(self._monotonic, deadline_us)
                text = entry.value_json
                recency = _recency(now_us, entry.observed_us)
                candidates.append(
                    RecallCandidate(
                        candidate_id=_candidate_id(
                            ROUTE_STATE, entry.record.id, entry.record.current_revision
                        ),
                        route=ROUTE_STATE,
                        resource_type="state_record",
                        resource_id=entry.record.id,
                        resource_revision=entry.record.current_revision,
                        text=text,
                        scores={"recency": round(recency, 6)},
                        final_score=round(0.6 + 0.4 * recency, 6),
                        token_estimate=self._estimator.estimate(text),
                        occurred_us=entry.observed_us,
                        scope=Scope(
                            tenant_id=entry.record.tenant_id,
                            agent_id=entry.record.agent_id,
                            space_group_id=entry.record.space_group_id,
                            space_id=entry.record.space_id,
                            session_id=entry.record.session_id,
                        ),
                        privacy_labels=(),
                        expires_us=entry.expires_us,
                    )
                )
            if len(candidates) >= limit:
                break
        check_deadline(self._monotonic, deadline_us)
        return tuple(candidates[:limit])


class FocusRoute:
    """Active focus items (dormant/dismissed/expired/promoted excluded)."""

    def __init__(
        self,
        service: FocusService,
        estimator: TokenEstimator | None = None,
        *,
        monotonic: MonotonicClock | None = None,
    ) -> None:
        self.name = ROUTE_FOCUS
        self._service = service
        self._estimator = estimator or DefaultTokenEstimator()
        self._monotonic = monotonic or SystemMonotonicClock()

    def collect(
        self,
        tx: Transaction,
        request: StructuredRecallRequest,
        access: AccessContext,
        deadline_us: int,
        now_us: int,
    ) -> tuple[RecallCandidate, ...]:
        check_deadline(self._monotonic, deadline_us)
        limit = request.candidate_limits.get(ROUTE_FOCUS, DEFAULT_FOCUS_CANDIDATES)
        pairs = self._service.list_items_in_tx(
            tx,
            access,
            agent_id=request.agent_id,
            statuses=(FocusStatus.ACTIVE.value,),
            space_id=request.space_id,
            session_id=request.session_id,
            limit=limit,
        )
        check_deadline(self._monotonic, deadline_us)
        candidates: list[RecallCandidate] = []
        for item, revision in pairs:
            check_deadline(self._monotonic, deadline_us)
            if not _passes_request_filters(
                request,
                resource_type="focus_item",
                category=revision.kind,
                privacy_labels=revision.privacy_labels,
            ):
                continue
            scores: dict[str, float | None] = {
                "activation": round(revision.activation, 6),
                "salience": round(revision.salience, 6),
                "importance": round(revision.importance, 6),
            }
            final = round(
                0.4 * revision.activation
                + 0.3 * revision.salience
                + 0.2 * revision.importance
                + 0.1 * 0.5,
                6,
            )
            candidates.append(
                RecallCandidate(
                    candidate_id=_candidate_id(ROUTE_FOCUS, item.id, revision.revision),
                    route=ROUTE_FOCUS,
                    resource_type="focus_item",
                    resource_id=item.id,
                    resource_revision=revision.revision,
                    text=revision.summary,
                    scores=scores,
                    final_score=final,
                    token_estimate=self._estimator.estimate(revision.summary),
                    occurred_us=revision.created_us,
                    scope=Scope(
                        tenant_id=item.tenant_id,
                        agent_id=item.agent_id,
                        space_group_id=item.space_group_id,
                        space_id=item.space_id,
                        session_id=item.session_id,
                    ),
                    privacy_labels=revision.privacy_labels,
                    expires_us=revision.expires_us,
                    category=revision.kind,
                )
            )
        check_deadline(self._monotonic, deadline_us)
        return tuple(candidates)


class DueTaskRoute:
    """Due, non-terminal tasks — the highest-priority structured route.

    Phase 4 addition (§18/§11): prospective memory outranks recency. Only
    active/waiting tasks whose due time has arrived; the final rehydrate
    re-checks status, due time, scope, privacy and tombstone again.
    """

    def __init__(
        self,
        service: TaskService,
        estimator: TokenEstimator | None = None,
        *,
        monotonic: MonotonicClock | None = None,
    ) -> None:
        self.name = ROUTE_TASKS
        self._service = service
        self._estimator = estimator or DefaultTokenEstimator()
        self._monotonic = monotonic or SystemMonotonicClock()

    def collect(
        self,
        tx: Transaction,
        request: StructuredRecallRequest,
        access: AccessContext,
        deadline_us: int,
        now_us: int,
    ) -> tuple[RecallCandidate, ...]:
        check_deadline(self._monotonic, deadline_us)
        limit = request.candidate_limits.get(ROUTE_TASKS, DEFAULT_TASKS_CANDIDATES)
        pairs = self._service.due_tasks_for_route(
            tx,
            access,
            agent_id=request.agent_id,
            space_id=request.space_id,
            session_id=request.session_id,
            limit=limit,
        )
        check_deadline(self._monotonic, deadline_us)
        candidates: list[RecallCandidate] = []
        for task, revision in pairs:
            check_deadline(self._monotonic, deadline_us)
            if not _passes_request_filters(
                request,
                resource_type="task",
                category="task",
                privacy_labels=revision.privacy_labels,
            ):
                continue
            overdue = 0.0
            if task.due_at_us is not None and task.due_at_us < now_us:
                overdue = min(1.0, (now_us - task.due_at_us) / RECENCY_WINDOW_US)
            final = round(0.7 + 0.2 * (1 - task.priority / 9) + 0.1 * overdue, 6)
            text = revision.next_action or revision.title
            candidates.append(
                RecallCandidate(
                    candidate_id=_candidate_id(ROUTE_TASKS, task.id, revision.revision),
                    route=ROUTE_TASKS,
                    resource_type="task",
                    resource_id=task.id,
                    resource_revision=revision.revision,
                    text=text,
                    scores={"overdue": round(overdue, 6), "task_urgency": round(overdue, 6)},
                    final_score=final,
                    token_estimate=self._estimator.estimate(text),
                    occurred_us=task.due_at_us or task.created_us,
                    scope=Scope(
                        tenant_id=task.tenant_id,
                        agent_id=task.agent_id,
                        space_group_id=task.space_group_id,
                        space_id=task.space_id,
                        session_id=task.session_id,
                    ),
                    privacy_labels=revision.privacy_labels,
                    category="task",
                )
            )
        check_deadline(self._monotonic, deadline_us)
        return tuple(candidates[:limit])


class ClaimsRoute:
    """Structured long-term memory route: canonical claims at the request
    scope (Phase 6, §18.4 route group 2)."""

    def __init__(
        self,
        estimator: TokenEstimator | None = None,
        *,
        monotonic: MonotonicClock | None = None,
    ) -> None:
        self.name = ROUTE_CLAIMS
        self._estimator = estimator or DefaultTokenEstimator()
        self._monotonic = monotonic or SystemMonotonicClock()

    def collect(
        self,
        tx: Transaction,
        request: StructuredRecallRequest,
        access: AccessContext,
        deadline_us: int,
        now_us: int,
    ) -> tuple[RecallCandidate, ...]:
        check_deadline(self._monotonic, deadline_us)
        limit = request.candidate_limits.get(ROUTE_CLAIMS, DEFAULT_CLAIMS_CANDIDATES)
        request_scope = Scope(
            tenant_id=access.tenant_id,
            agent_id=request.agent_id,
            space_group_id=request.space_group_id,
            space_id=request.space_id,
            session_id=request.session_id,
        )
        if request.resource_types is not None and "claim" not in request.resource_types:
            return ()
        candidates: list[RecallCandidate] = []
        cursor_updated: int | None = None
        cursor_id: str | None = None
        page_size = min(100, max(limit * 2, 50))
        while len(candidates) < limit:
            check_deadline(self._monotonic, deadline_us)
            # as_of is pushed INTO the keyset query: search_page reconstructs
            # the revision current at as_of and applies the status filter to
            # THAT revision — a claim active historically but retracted now
            # stays a candidate (a current-status prefilter would drop it
            # before rehydrate could ever see it). Valid time is likewise
            # evaluated AT as_of for historical reads: "valid then, expired
            # now" must stay a candidate and "not yet effective then" must
            # not leak into a historical result.
            page = tx.claims.search_page(
                tenant_id=access.tenant_id,
                agent_id=request.agent_id,
                space_group_id=request.space_group_id,
                space_id=request.space_id,
                session_id=request.session_id,
                statuses=list(CLAIM_CURRENT_VISIBLE_STATUSES),
                cursor_updated_us=cursor_updated,
                cursor_id=cursor_id,
                limit=page_size,
                as_of_us=request.as_of_us,
                valid_at_us=request.as_of_us if request.as_of_us is not None else now_us,
            )
            if not page:
                break
            for claim, revision in page:
                cursor_updated = claim.updated_us
                cursor_id = claim.id
                data_scope = _claim_scope(claim)
                if not scope_allows(data_scope, request_scope):
                    continue
                if not evaluate_privacy(revision.privacy_labels, data_scope, request_scope, access):
                    continue
                if not _passes_request_filters(
                    request,
                    resource_type="claim",
                    category=revision.category,
                    privacy_labels=revision.privacy_labels,
                ):
                    continue
                check_deadline(self._monotonic, deadline_us)
                candidates.append(self._candidate(claim, revision, now_us))
                if len(candidates) >= limit:
                    break
            if len(page) < page_size:
                break
        candidates.sort(key=lambda c: c.resource_id)
        return tuple(candidates)

    def _candidate(self, claim: Any, revision: Any, now_us: int) -> RecallCandidate:
        scores: dict[str, float | None] = {
            "confidence": round(claim.confidence, 6),
            "importance": round(claim.importance, 6),
            "accessibility": round(claim.accessibility, 6),
            "authority": round(_authority_component(claim.source_authority), 6),
            "recency": round(_recency(now_us, revision.recorded_at_us), 6),
        }
        conflict_group = (
            f"{revision.subject_entity_id}:{revision.predicate}"
            if revision.subject_entity_id
            else None
        )
        if claim.status == "disputed":
            conflict_group = conflict_group or "disputed"
        return RecallCandidate(
            candidate_id=_candidate_id(ROUTE_CLAIMS, claim.id, revision.revision),
            route=ROUTE_CLAIMS,
            resource_type="claim",
            resource_id=claim.id,
            resource_revision=revision.revision,
            text=revision.canonical_text,
            scores=scores,
            final_score=0.0,
            token_estimate=self._estimator.estimate(revision.canonical_text),
            occurred_us=revision.recorded_at_us,
            scope=_claim_scope(claim),
            privacy_labels=revision.privacy_labels,
            content_hash=revision.content_hash,
            subject_entity_id=revision.subject_entity_id,
            category=revision.category,
            conflict_group=conflict_group,
        )


class RelationsRoute:
    """Canonical relations involving the speaker (Phase 6, §18.4)."""

    def __init__(
        self,
        estimator: TokenEstimator | None = None,
        *,
        monotonic: MonotonicClock | None = None,
    ) -> None:
        self.name = ROUTE_RELATIONS
        self._estimator = estimator or DefaultTokenEstimator()
        self._monotonic = monotonic or SystemMonotonicClock()

    def collect(
        self,
        tx: Transaction,
        request: StructuredRecallRequest,
        access: AccessContext,
        deadline_us: int,
        now_us: int,
    ) -> tuple[RecallCandidate, ...]:
        check_deadline(self._monotonic, deadline_us)
        if request.speaker_entity_id is None:
            return ()
        if request.resource_types is not None and "relation" not in request.resource_types:
            return ()
        limit = request.candidate_limits.get(ROUTE_RELATIONS, DEFAULT_RELATIONS_CANDIDATES)
        request_scope = Scope(
            tenant_id=access.tenant_id,
            agent_id=request.agent_id,
            space_group_id=request.space_group_id,
            space_id=request.space_id,
            session_id=request.session_id,
        )
        ids = tx.relations.relations_for_entity(
            access.tenant_id, request.speaker_entity_id, agent_id=request.agent_id
        )
        candidates: list[RecallCandidate] = []
        # Valid time at the request's evaluation instant (as_of for
        # historical reads) — same rule the claim rehydrate applies.
        valid_at_us = request.as_of_us if request.as_of_us is not None else now_us
        for relation_id in ids:
            check_deadline(self._monotonic, deadline_us)
            if len(candidates) >= limit:
                break
            relation = tx.relations.get(relation_id)
            if relation.status not in ("active", "disputed"):
                continue
            revision = tx.relations.current_revision_row(relation.id)
            data_scope = Scope(
                tenant_id=relation.tenant_id,
                agent_id=relation.agent_id,
                space_group_id=relation.space_group_id,
                space_id=relation.space_id,
                session_id=relation.session_id,
            )
            if not scope_allows(data_scope, request_scope):
                continue
            if not evaluate_privacy(revision.privacy_labels, data_scope, request_scope, access):
                continue
            if relation.valid_from_us is not None and relation.valid_from_us > valid_at_us:
                continue
            if relation.valid_until_us is not None and relation.valid_until_us <= valid_at_us:
                continue
            if not _passes_request_filters(
                request,
                resource_type="relation",
                category="relationship",
                privacy_labels=revision.privacy_labels,
            ):
                continue
            text = (
                f"{relation.source_entity_id} {relation.relation_type} {relation.target_entity_id}"
            )
            scores: dict[str, float | None] = {
                "confidence": round(relation.confidence, 6),
                "importance": round(relation.importance, 6),
                "accessibility": round(relation.accessibility, 6),
                "recency": round(_recency(now_us, relation.created_us), 6),
                "relevance": None,
            }
            candidates.append(
                RecallCandidate(
                    candidate_id=_candidate_id(ROUTE_RELATIONS, relation.id, revision.revision),
                    route=ROUTE_RELATIONS,
                    resource_type="relation",
                    resource_id=relation.id,
                    resource_revision=revision.revision,
                    text=text,
                    scores=scores,
                    final_score=0.0,
                    token_estimate=self._estimator.estimate(text),
                    occurred_us=relation.created_us,
                    scope=data_scope,
                    privacy_labels=revision.privacy_labels,
                    content_hash=revision.content_hash,
                    subject_entity_id=relation.source_entity_id,
                    category="relationship",
                )
            )
        return tuple(candidates)


class FtsRoute:
    """FTS route over the trusted current generation (Phase 6, §22.1).

    Hits only contribute resource refs plus a relevance component; the
    candidate text is read from the CANONICAL row in the route's own
    transaction, and the final rehydrate re-verifies everything again.
    """

    def __init__(
        self,
        projection: FtsProjectionService,
        estimator: TokenEstimator | None = None,
        *,
        monotonic: MonotonicClock | None = None,
    ) -> None:
        self.name = ROUTE_FTS
        self._projection = projection
        self._estimator = estimator or DefaultTokenEstimator()
        self._monotonic = monotonic or SystemMonotonicClock()

    def collect(
        self,
        tx: Transaction,
        request: StructuredRecallRequest,
        access: AccessContext,
        deadline_us: int,
        now_us: int,
    ) -> tuple[RecallCandidate, ...]:
        check_deadline(self._monotonic, deadline_us)
        if request.as_of_us is not None:
            # The FTS projection indexes CURRENT revisions only; it cannot
            # answer historical visibility questions (ADR-0014 §3).
            raise FtsDegradedError(FTS_AS_OF_REASON, retryable=False)
        if not request.topic.strip():
            return ()
        if request.resource_types is not None and not (
            request.resource_types & {"claim", "episode", "note"}
        ):
            return ()
        try:
            match = build_fts_query(request.topic)
        except InvalidRequestError:
            # A topic made only of stopwords has no usable tokens: the FTS
            # route completes with zero candidates rather than degrading —
            # this is a query-shape outcome, not a projection failure.
            return ()
        limit = request.candidate_limits.get(ROUTE_FTS, DEFAULT_FTS_CANDIDATES)
        check_deadline(self._monotonic, deadline_us)
        hits = self._projection.search_in_tx(
            tx,
            tenant_id=access.tenant_id,
            agent_id=request.agent_id,
            match_expression=match,
            space_group_id=request.space_group_id,
            space_id=request.space_id,
            session_id=request.session_id,
            valid_at_us=now_us,
            limit=limit,
        )
        request_scope = Scope(
            tenant_id=access.tenant_id,
            agent_id=request.agent_id,
            space_group_id=request.space_group_id,
            space_id=request.space_id,
            session_id=request.session_id,
        )
        candidates: list[RecallCandidate] = []
        for hit in hits:
            check_deadline(self._monotonic, deadline_us)
            document = hit.document
            canonical = _canonical_projection_row(tx, document.resource_type, document.resource_id)
            if canonical is None:
                continue
            current, revision, text, category, occurred_us = canonical
            if current.current_revision != document.resource_revision:
                # The projection is behind: an expired revision must never
                # become a trustworthy return value — skip and let the
                # rehydrate discipline hold for the rest.
                continue
            data_scope = Scope(
                tenant_id=document.tenant_id,
                agent_id=document.agent_id,
                space_group_id=document.space_group_id,
                space_id=document.space_id,
                session_id=document.session_id,
            )
            if not scope_allows(data_scope, request_scope):
                continue
            if not evaluate_privacy(revision.privacy_labels, data_scope, request_scope, access):
                continue
            if not _passes_request_filters(
                request,
                resource_type=document.resource_type,
                category=category,
                privacy_labels=revision.privacy_labels,
            ):
                continue
            base = _fts_base_scores(document.resource_type, current)
            scores: dict[str, float | None] = dict(base)
            scores["relevance"] = hit.relevance
            scores["recency"] = round(_recency(now_us, occurred_us), 6)
            candidates.append(
                RecallCandidate(
                    candidate_id=_candidate_id(
                        ROUTE_FTS, document.resource_id, document.resource_revision
                    ),
                    route=ROUTE_FTS,
                    resource_type=document.resource_type,
                    resource_id=document.resource_id,
                    resource_revision=document.resource_revision,
                    text=text,
                    scores=scores,
                    final_score=0.0,
                    token_estimate=self._estimator.estimate(text),
                    occurred_us=occurred_us,
                    scope=data_scope,
                    privacy_labels=revision.privacy_labels,
                    content_hash=revision.content_hash,
                    subject_entity_id=document.subject_entity_id,
                    category=category,
                    conflict_group=(
                        f"{revision.subject_entity_id}:{revision.predicate}"
                        if document.resource_type == "claim" and revision.subject_entity_id
                        else None
                    ),
                )
            )
        return tuple(candidates)


def _fts_base_scores(resource_type: str, current: Any) -> dict[str, float | None]:
    if resource_type == "claim":
        return {
            "confidence": round(current.confidence, 6),
            "importance": round(current.importance, 6),
            "accessibility": round(current.accessibility, 6),
            "authority": round(_authority_component(current.source_authority), 6),
        }
    if resource_type == "episode":
        return {"importance": round(current.importance, 6)}
    return {"importance": round(current.importance, 6)}


def _canonical_projection_row(
    tx: Transaction, resource_type: str, resource_id: str
) -> tuple[Any, Any, str, str, int] | None:
    """Read the canonical (current, revision, text, category, occurred)."""
    try:
        if resource_type == "claim":
            current = tx.claims.get(resource_id)
            if current.status not in CLAIM_CURRENT_VISIBLE_STATUSES:
                return None
            revision = tx.claims.current_revision_row(current.id)
            return (
                current,
                revision,
                revision.canonical_text,
                revision.category,
                revision.recorded_at_us,
            )
        if resource_type == "episode":
            episode = tx.episodes.get(resource_id)
            if episode.status not in ("open", "sealed"):
                return None
            episode_revision = tx.episodes.current_revision_row(episode.id)
            text = f"{episode_revision.title}\n{episode_revision.summary}"
            return episode, episode_revision, text, "episode", episode_revision.created_us
        if resource_type == "note":
            note = tx.notes.get(resource_id)
            if note.status not in (
                NoteStatus.INBOX.value,
                NoteStatus.PINNED.value,
                NoteStatus.SNOOZED.value,
            ):
                return None
            note_revision = tx.notes.current_revision_row(note.id)
            text = f"{note_revision.title}\n{note_revision.body}"
            return note, note_revision, text, note.kind, note_revision.created_us
    except Exception:
        return None
    return None


def _claim_scope(claim: Any) -> Scope:
    return Scope(
        tenant_id=claim.tenant_id,
        agent_id=claim.agent_id,
        space_group_id=claim.space_group_id,
        space_id=claim.space_id,
        session_id=claim.session_id,
    )


def _cosine_relevance(score: float) -> float:
    """Map a cosine in [-1, 1] to a deterministic relevance in [0, 1]."""
    return round(max(0.0, min(1.0, (score + 1.0) / 2.0)), 6)


class VectorRoute:
    """Vector route over the trusted current FAISS generation (Phase 7,
    ADR-0015 §6).

    The query embedding goes through the provider FIRST (bounded by the
    route's cooperative deadline checks — a stuck provider degrades this
    route only, never the request), then the trusted search runs inside the
    route's own read transaction: pointer, freshness gate and handle all
    resolve against one consistent snapshot. Hits carry refs + score only;
    the candidate text is read from the CANONICAL row and the final
    rehydrate re-verifies everything again."""

    def __init__(
        self,
        projection: VectorProjectionService,
        estimator: TokenEstimator | None = None,
        *,
        monotonic: MonotonicClock | None = None,
    ) -> None:
        self.name = ROUTE_VECTOR
        self._projection = projection
        self._estimator = estimator or DefaultTokenEstimator()
        self._monotonic = monotonic or SystemMonotonicClock()

    def collect(
        self,
        tx: Transaction,
        request: StructuredRecallRequest,
        access: AccessContext,
        deadline_us: int,
        now_us: int,
    ) -> tuple[RecallCandidate, ...]:
        check_deadline(self._monotonic, deadline_us)
        if request.as_of_us is not None:
            # The FAISS projection indexes CURRENT revisions only; without a
            # trusted historical generation it must not answer as_of with
            # present-day vectors (ADR-0015 §6).
            raise VectorDegradedError(VECTOR_REASON_AS_OF_UNSUPPORTED, retryable=False)
        if not request.topic.strip():
            return ()
        if request.resource_types is not None and not (
            request.resource_types & VECTOR_INDEXABLE_RESOURCE_TYPES
        ):
            return ()
        # Provider call bounded by the route's remaining deadline: the
        # provider caps its transport timeout at the remaining budget (the
        # socket aborts — the call cannot outlive the deadline in a stranded
        # thread) and fails fast once it has passed. This runs inside the
        # route's READ snapshot (never a writer transaction) — a WAL reader
        # does not block canonical writes.
        try:
            query_vector = self._projection.embed_query(
                request.topic, deadline_monotonic_us=deadline_us
            )
        except EmbeddingProviderError:
            raise VectorDegradedError(VECTOR_REASON_UNAVAILABLE, retryable=True) from None
        check_deadline(self._monotonic, deadline_us)
        limit = request.candidate_limits.get(ROUTE_VECTOR, DEFAULT_VECTOR_CANDIDATES)
        hits = self._projection.search_in_tx(
            tx,
            tenant_id=access.tenant_id,
            agent_id=request.agent_id,
            query_vector=query_vector,
            limit=limit,
            minimum_watermark=request.minimum_watermark,
        )
        request_scope = Scope(
            tenant_id=access.tenant_id,
            agent_id=request.agent_id,
            space_group_id=request.space_group_id,
            space_id=request.space_id,
            session_id=request.session_id,
        )
        candidates: list[RecallCandidate] = []
        for hit in hits:
            check_deadline(self._monotonic, deadline_us)
            canonical = _canonical_projection_row(tx, hit.resource_type, hit.resource_id)
            if canonical is None:
                continue
            current, revision, text, category, occurred_us = canonical
            if current.current_revision != hit.resource_revision:
                # The projection is behind: an expired revision must never
                # become a trustworthy return value.
                continue
            data_scope = Scope(
                tenant_id=current.tenant_id,
                agent_id=current.agent_id,
                space_group_id=current.space_group_id,
                space_id=current.space_id,
                session_id=current.session_id,
            )
            if not scope_allows(data_scope, request_scope):
                continue
            if not evaluate_privacy(revision.privacy_labels, data_scope, request_scope, access):
                continue
            if not _passes_request_filters(
                request,
                resource_type=hit.resource_type,
                category=category,
                privacy_labels=revision.privacy_labels,
            ):
                continue
            base = _fts_base_scores(hit.resource_type, current)
            scores: dict[str, float | None] = dict(base)
            scores["relevance"] = _cosine_relevance(hit.score)
            scores["recency"] = round(_recency(now_us, occurred_us), 6)
            candidates.append(
                RecallCandidate(
                    candidate_id=_candidate_id(
                        ROUTE_VECTOR, hit.resource_id, hit.resource_revision
                    ),
                    route=ROUTE_VECTOR,
                    resource_type=hit.resource_type,
                    resource_id=hit.resource_id,
                    resource_revision=hit.resource_revision,
                    text=text,
                    scores=scores,
                    final_score=0.0,
                    token_estimate=self._estimator.estimate(text),
                    occurred_us=occurred_us,
                    scope=data_scope,
                    privacy_labels=revision.privacy_labels,
                    content_hash=revision.content_hash,
                    subject_entity_id=getattr(revision, "subject_entity_id", None),
                    category=category,
                    conflict_group=(
                        f"{revision.subject_entity_id}:{revision.predicate}"
                        if hit.resource_type == "claim" and revision.subject_entity_id
                        else None
                    ),
                )
            )
        return tuple(candidates)


def _observation_scope(observation: StoredObservation) -> Scope:
    return Scope(
        tenant_id=observation.tenant_id,
        agent_id=observation.agent_id,
        space_group_id=observation.space_group_id,
        space_id=observation.space_id,
        session_id=observation.session_id,
    )


class StructuredRecallOrchestrator:
    """Runs the structured routes under one deadline and budget.

    Phase 6 three-stage execution (ADR-0014 §3):

    1. Control read transaction: request authorization, watermark check and
       the Persona top-level revision/hash read.
    2. Bounded-parallel route stage: every route opens its OWN read unit of
       work (connections are never shared across threads); results merge in
       fixed route order.
    3. Fresh rehydrate transaction opened AFTER collection: every candidate
       is re-read canonically; a Forget that committed during collection is
       visible here and removes the candidate. pending_event_ids read here.
    """

    def __init__(
        self,
        uow: UnitOfWork,
        recent: RecentContextService,
        states: StateService,
        focus: FocusService,
        *,
        clock: Clock,
        tasks: TaskService | None = None,
        events: CognitiveEventService | None = None,
        relations_enabled: bool = False,
        claims_enabled: bool = False,
        fts: FtsProjectionService | None = None,
        vector: VectorProjectionService | None = None,
        monotonic: MonotonicClock | None = None,
        estimator: TokenEstimator | None = None,
        routes: tuple[RecallRoute, ...] | None = None,
        max_route_concurrency: int = DEFAULT_ROUTE_CONCURRENCY,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._events = events
        self._monotonic = monotonic or SystemMonotonicClock()
        self._estimator = estimator or DefaultTokenEstimator()
        self._ranker = RankerV3()
        self._max_route_concurrency = max_route_concurrency
        if routes is not None:
            self._routes: tuple[RecallRoute, ...] = routes
        else:
            # Default composition: due tasks FIRST (prospective memory is the
            # highest-priority structured signal), then the Phase 3 routes,
            # then the Phase 6 structured long-term and FTS routes, then the
            # Phase 7 vector route (semantic recall supplements the rest).
            composed: list[RecallRoute] = []
            if tasks is not None:
                composed.append(DueTaskRoute(tasks, self._estimator, monotonic=self._monotonic))
            composed.extend(
                (
                    RecentContextRoute(recent, self._monotonic, self._estimator),
                    StateRoute(states, self._estimator, monotonic=self._monotonic),
                    FocusRoute(focus, self._estimator, monotonic=self._monotonic),
                )
            )
            if claims_enabled:
                composed.append(ClaimsRoute(self._estimator, monotonic=self._monotonic))
            if relations_enabled:
                composed.append(RelationsRoute(self._estimator, monotonic=self._monotonic))
            if fts is not None:
                composed.append(FtsRoute(fts, self._estimator, monotonic=self._monotonic))
            if vector is not None:
                composed.append(VectorRoute(vector, self._estimator, monotonic=self._monotonic))
            self._routes = tuple(composed)

    # -- route execution ------------------------------------------------------

    def _run_route(
        self,
        route: RecallRoute,
        request: StructuredRecallRequest,
        access: AccessContext,
        route_deadline: int,
        now_us: int,
    ) -> tuple[str, tuple[RecallCandidate, ...], RouteTrace]:
        """Run one route inside its OWN read unit of work (thread-local)."""
        route_start = self._monotonic.monotonic_us()
        try:
            with self._uow.read() as route_tx:
                candidates = route.collect(route_tx, request, access, route_deadline, now_us)
            # The port boundary is authoritative even for a custom Route:
            # a provider that returns only after its sub-deadline must not be
            # recorded as completed merely because it omitted its own
            # cooperative post-I/O check.
            check_deadline(self._monotonic, route_deadline)
            return (
                "completed",
                tuple(candidates),
                RouteTrace(
                    route.name,
                    "completed",
                    len(candidates),
                    self._monotonic.monotonic_us() - route_start,
                ),
            )
        except RouteDeadlineExceeded:
            return (
                "degraded",
                (),
                RouteTrace(
                    route.name,
                    "degraded",
                    0,
                    self._monotonic.monotonic_us() - route_start,
                    "route_deadline_exceeded",
                ),
            )
        except FtsDegradedError as error:
            return (
                "degraded",
                (),
                RouteTrace(
                    route.name,
                    "degraded",
                    0,
                    self._monotonic.monotonic_us() - route_start,
                    error.reason_code,
                ),
            )
        except VectorDegradedError as error:
            return (
                "degraded",
                (),
                RouteTrace(
                    route.name,
                    "degraded",
                    0,
                    self._monotonic.monotonic_us() - route_start,
                    error.reason_code,
                ),
            )
        except (AccessDeniedError, ScopeViolationError):
            # Authorization failures are REQUEST-level errors: a route must
            # never swallow a scope violation into degradation.
            raise
        except Exception:
            # Provider/projection failure degrades this route only;
            # observation writes and the other routes are unaffected.
            return (
                "degraded",
                (),
                RouteTrace(
                    route.name,
                    "degraded",
                    0,
                    self._monotonic.monotonic_us() - route_start,
                    "route_failed",
                ),
            )

    def _run_routes(
        self,
        request: StructuredRecallRequest,
        access: AccessContext,
        total_deadline: int,
        total_budget: int,
        now_us: int,
    ) -> tuple[
        list[str],
        list[DegradedRoute],
        list[RecallCandidate],
        list[RouteTrace],
    ]:
        completed: list[str] = []
        degraded: list[DegradedRoute] = []
        collected: list[RecallCandidate] = []
        traces: list[RouteTrace] = []

        def route_deadline_for(start_us: int) -> int:
            share_us = max(1, int(total_budget * _ROUTE_SHARE))
            return min(total_deadline, start_us + share_us)

        # Every execution path is bounded by the deadline, including the
        # single-route / max_route_concurrency=1 case: a synchronous call
        # could park the request on one stuck route forever, so even one
        # executor goes through the pool and its timed future.result().
        executors = max(1, min(request.max_route_concurrency, len(self._routes)))

        # Bounded parallel execution. Each future is waited on with AT MOST
        # the remaining request budget: a route stuck inside a provider call
        # or a long scan cannot hold the response past its deadline. The
        # worker thread is abandoned, not joined — its own read unit of work
        # closes itself when the call returns, and its result is discarded
        # (shutdown below neither waits nor blocks on it).
        merge_start = self._monotonic.monotonic_us()
        pool = ThreadPoolExecutor(max_workers=executors)
        try:
            futures = []
            for route in self._routes:
                futures.append(
                    pool.submit(
                        self._run_route,
                        route,
                        request,
                        access,
                        route_deadline_for(self._monotonic.monotonic_us()),
                        now_us,
                    )
                )
            # Fixed route order merge: determinism does not depend on
            # completion order.
            for route, future in zip(self._routes, futures, strict=True):
                remaining_us = total_deadline - self._monotonic.monotonic_us()
                try:
                    outcome, candidates, trace = future.result(
                        timeout=max(0.0, remaining_us / 1_000_000.0)
                    )
                except FuturesTimeoutError:
                    outcome = "degraded"
                    candidates = ()
                    trace = RouteTrace(
                        route.name,
                        "degraded",
                        0,
                        self._monotonic.monotonic_us() - merge_start,
                        "route_deadline_exceeded",
                    )
                except (AccessDeniedError, ScopeViolationError):
                    # Authorization failures stay REQUEST-level errors even
                    # when they surface on a worker thread.
                    raise
                except Exception:
                    # Anything else that escapes a route thread (teardown,
                    # cancellation) degrades the route, not the request.
                    outcome = "degraded"
                    candidates = ()
                    trace = RouteTrace(
                        route.name,
                        "degraded",
                        0,
                        self._monotonic.monotonic_us() - merge_start,
                        "route_failed",
                    )
                self._record_route(
                    outcome, route, candidates, trace, completed, degraded, collected, traces
                )
        finally:
            # Never join the pool here: joining would reintroduce the
            # unbounded wait this bounded merge exists to prevent.
            pool.shutdown(wait=False, cancel_futures=True)
        return completed, degraded, collected, traces

    @staticmethod
    def _record_route(
        outcome: str,
        route: RecallRoute,
        candidates: tuple[RecallCandidate, ...],
        trace: RouteTrace,
        completed: list[str],
        degraded: list[DegradedRoute],
        collected: list[RecallCandidate],
        traces: list[RouteTrace],
    ) -> None:
        traces.append(trace)
        if outcome == "completed":
            collected.extend(candidates)
            completed.append(route.name)
            return
        fallback = trace.fallback or ""
        if fallback.startswith("fts_"):
            reason = fallback
            retryable = fallback not in (
                FTS_AS_OF_REASON,
                "fts_unavailable",
                "fts_builder_unknown",
            )
        elif fallback.startswith("vector_"):
            reason = fallback
            retryable = fallback not in VECTOR_NON_RETRYABLE_REASONS
        elif fallback == "route_deadline_exceeded":
            reason = "route_deadline_exceeded"
            retryable = True
        else:
            reason = "route_failed"
            retryable = True
        degraded.append(
            DegradedRoute(route.name, reason, retryable, "route_skipped_canonical_intact")
        )

    # -- public entry ---------------------------------------------------------

    def recall(
        self, access: AccessContext, request: StructuredRecallRequest
    ) -> StructuredRecallResult:
        started = self._monotonic.monotonic_us()
        request_hash = _hash_id(
            f"{request.request_id}:{request.agent_id}:{request.space_id}:{request.session_id}"
        )
        with self._uow.read() as tx:
            # Authenticate the whole request before any early watermark or
            # deadline outcome. Otherwise an unreachable watermark could
            # short-circuit before the first route's authorization and turn
            # an unauthorized probe into `not_ready` instead of access_denied.
            self._authorize_request(tx, access, request)
            watermark_state = tx.watermark(access.tenant_id, request.agent_id)
            source_watermark = watermark_state.current_seq if watermark_state is not None else 0
            if (
                request.minimum_watermark is not None
                and source_watermark < request.minimum_watermark
            ):
                # Read-your-writes cannot be satisfied: the store has not
                # seen the claimed watermark. No key Route can succeed, so
                # §18.3 requires a stable error rather than an all-degraded
                # envelope.
                raise MinimumWatermarkUnavailableError(
                    "minimum watermark not reachable at this time",
                    details={
                        "minimum_watermark": request.minimum_watermark,
                        "degraded_routes": [route.name for route in self._routes],
                    },
                )
            tombstone_watermark = tx.tombstone_watermark()
            persona_revision, persona_content_hash = _persona_metadata(tx, request.agent_id)
            now_us = self._clock.now_us()

        total_budget = max(0, request.deadline_monotonic_us - started)
        completed, degraded, collected, route_traces = self._run_routes(
            request, access, request.deadline_monotonic_us, total_budget, now_us
        )
        retrieved_count = len(collected)

        # FINAL canonical rehydrate in a FRESH read unit of work (ADR-0014
        # §3): commits that landed while routes were collecting — Forget,
        # Correct, tombstones — are visible here. A candidate collected from
        # any projection (recent/state/focus/claims/relations/FTS) that no
        # longer passes every hard filter is dropped with an internal
        # reason code.
        dropped = 0
        rehydrated: list[RecallCandidate] = []
        pending_event_ids: tuple[str, ...] = ()
        with self._uow.read() as tx:
            for candidate in collected:
                if self._rehydrate(tx, access, request, candidate) is None:
                    rehydrated.append(candidate)
                else:
                    dropped += 1
            # Pending CognitiveEvents at this scope (§12): ids only — the
            # body stays behind the events endpoint's own authorization.
            if self._events is not None:
                # The request scope — not the wider access envelope — decides
                # which pending ids this recall may broadcast: agent-level
                # events enter a space request, the matching space's events
                # enter it, other spaces/sessions never do.
                pending_event_ids = self._events.pending_event_ids_for_request_scope(
                    tx,
                    access,
                    agent_id=request.agent_id,
                    space_id=request.space_id,
                    session_id=request.session_id,
                    limit=DEFAULT_PENDING_EVENT_IDS,
                )

        ranked = self._rank_and_trim(rehydrated, request)
        missing_components = sum(len(c.missing_components) for c in ranked)

        # A partial response requires at least one successful key Route (§18.3).
        # All-degraded is a stable error even when partial results were
        # allowed: there is no result whose partiality could be useful or
        # trustworthy.
        partial = bool(degraded)
        if degraded:
            all_deadline = all(d.reason_code == "route_deadline_exceeded" for d in degraded)
            if not completed:
                if all_deadline:
                    raise DeadlineExceededError(
                        "every recall route missed its deadline",
                        details={"degraded_routes": [d.route for d in degraded]},
                    )
                raise NotReadyError(
                    "structured recall has no acceptable complete route result",
                    details={"degraded_routes": [d.route for d in degraded]},
                )
            if not request.allow_partial:
                # Fail closed: the caller asked for complete-or-error, so a
                # degraded route is a stable request-level error even when
                # other routes completed.
                raise NotReadyError(
                    "structured recall has degraded routes and partial results are not allowed",
                    details={"degraded_routes": [d.route for d in degraded]},
                )
        return StructuredRecallResult(
            request_id=request.request_id,
            source_watermark=source_watermark,
            completed_routes=tuple(completed),
            degraded_routes=tuple(degraded),
            partial=partial,
            candidates=tuple(ranked),
            dropped_by_rehydrate=dropped,
            trace=self._trace(
                request_hash, started, tuple(route_traces), dropped, missing_components
            )
            if request.include_trace
            else None,
            pending_event_ids=pending_event_ids,
            persona_revision=persona_revision,
            persona_content_hash=persona_content_hash,
            retrieved_count=retrieved_count,
            tombstone_watermark=tombstone_watermark,
        )

    def _rank_and_trim(
        self, candidates: list[RecallCandidate], request: StructuredRecallRequest
    ) -> list[RecallCandidate]:
        """Ranker v2 fusion + budget trimming (ADR-0014 §5)."""
        from iris_memory_core.domain.recall import ScoredCandidate

        scored: list[ScoredCandidate] = []
        for candidate in candidates:
            scored.append(
                ScoredCandidate(
                    candidate_id=candidate.candidate_id,
                    route=candidate.route,
                    resource_type=candidate.resource_type,
                    resource_id=candidate.resource_id,
                    resource_revision=candidate.resource_revision,
                    subject_entity_id=candidate.subject_entity_id,
                    category=candidate.category or candidate.route,
                    content_hash=candidate.content_hash,
                    occurred_us=candidate.occurred_us,
                    token_estimate=candidate.token_estimate,
                    scores=candidate.scores,
                    conflict_group=candidate.conflict_group,
                )
            )
        ordered = self._ranker.score(scored)
        outcome = apply_token_budgets(
            ordered,
            token_budget=request.token_budget,
            layer_budgets=request.layer_budgets or {},
            speaker_entity_id=request.speaker_entity_id,
        )
        by_id = {candidate.candidate_id: candidate for candidate in candidates}
        kept: list[RecallCandidate] = []
        for entry in outcome.kept:
            source = by_id[entry.candidate_id]
            kept.append(
                replace(
                    source,
                    final_score=entry.final_score,
                    conflict_state=entry.conflict_state,
                    missing_components=entry.missing_components,
                )
            )
        return kept

    @staticmethod
    def _authorize_request(
        tx: Transaction, access: AccessContext, request: StructuredRecallRequest
    ) -> None:
        # Purpose authorization: the server-derived access context names the
        # data purposes this caller was granted; recall outside that grant
        # is denied even though the purpose itself is a known enum value
        # (§18.1 — the enum check alone is NOT authorization).
        if access.data_purposes and request.purpose not in access.data_purposes:
            raise AccessDeniedError(
                "recall purpose is outside this access context",
                details={"purpose": request.purpose},
            )
        agent = tx.get_agent(request.agent_id)
        if agent.tenant_id != access.tenant_id:
            raise AccessDeniedError("agent belongs to another tenant")
        if request.agent_id not in access.agent_ids:
            raise AccessDeniedError("agent is outside the access context")
        if (
            request.space_group_id is not None
            and request.space_group_id not in access.allowed_space_group_ids
        ):
            raise AccessDeniedError("space group is outside the access context")
        space = tx.get_space(request.space_id)
        if space.tenant_id != access.tenant_id:
            raise AccessDeniedError("space belongs to another tenant")
        if request.space_id not in access.allowed_space_ids:
            raise AccessDeniedError("space is outside the access context")
        if space.agent_id is not None and space.agent_id != request.agent_id:
            raise AccessDeniedError("space belongs to a different agent")
        if request.space_group_id is not None:
            # Membership in each authorized set is necessary but not
            # sufficient: the two named dims must describe the SAME
            # hierarchy, or a caller could pair an approved group with an
            # unrelated approved space and read the union.
            binding = tx.get_active_group_binding(request.space_id)
            if (
                binding is None
                or binding.tenant_id != access.tenant_id
                or binding.space_group_id != request.space_group_id
            ):
                raise AccessDeniedError("space is not bound to the requested space group")
        if request.session_id is not None:
            session = tx.get_session(request.session_id)
            if session.tenant_id != access.tenant_id:
                raise AccessDeniedError("session belongs to another tenant")
            if session.space_id != request.space_id:
                raise InvalidRequestError("session does not belong to the given space")

    def _trace(
        self,
        request_hash: str,
        started_us: int,
        routes: tuple[RouteTrace, ...],
        dropped: int,
        missing_components: int = 0,
    ) -> RecallTrace:
        return RecallTrace(
            request_hash=request_hash,
            ranker_version=RECALL_RANKER_VERSION,
            total_duration_us=self._monotonic.monotonic_us() - started_us,
            routes=routes,
            rehydrated_out=dropped,
            missing_score_components=missing_components,
        )

    def _rehydrate(
        self,
        tx: Transaction,
        access: AccessContext,
        request: StructuredRecallRequest,
        candidate: RecallCandidate,
    ) -> str | None:
        """Re-read one candidate canonically; a reason string means DROP."""
        request_scope = Scope(
            tenant_id=access.tenant_id,
            agent_id=request.agent_id,
            space_group_id=request.space_group_id,
            space_id=request.space_id,
            session_id=request.session_id,
        )
        now_us = self._clock.now_us()
        if candidate.resource_type == "task":
            return self._rehydrate_task(tx, access, request, request_scope, candidate, now_us)
        if candidate.resource_type == "observation":
            return self._rehydrate_observation(tx, access, request_scope, candidate)
        if candidate.resource_type == "state_record":
            return self._rehydrate_state(tx, request, request_scope, candidate, now_us)
        if candidate.resource_type == "focus_item":
            return self._rehydrate_focus(tx, access, request_scope, candidate, now_us)
        if candidate.resource_type == "claim":
            return self._rehydrate_claim(tx, access, request, request_scope, candidate, now_us)
        if candidate.resource_type == "episode":
            return self._rehydrate_episode(tx, access, request_scope, candidate, now_us)
        if candidate.resource_type == "note":
            return self._rehydrate_note(tx, access, request_scope, candidate, now_us)
        if candidate.resource_type == "relation":
            return self._rehydrate_relation(tx, access, request, request_scope, candidate, now_us)
        return "unknown_resource_type"

    def _rehydrate_task(
        self,
        tx: Transaction,
        access: AccessContext,
        request: StructuredRecallRequest,
        request_scope: Scope,
        candidate: RecallCandidate,
        now_us: int,
    ) -> str | None:
        del request
        try:
            task = tx.tasks.get_task(candidate.resource_id)
        except Exception:
            return "task_missing"
        if task.current_revision != candidate.resource_revision:
            return "stale_revision"
        if tx.is_tombstoned(access.tenant_id, "task", task.id):
            return "tombstoned"
        if task.status not in (TaskStatus.ACTIVE.value, TaskStatus.WAITING.value):
            return "status_not_due"
        if task.due_at_us is None or task.due_at_us > now_us:
            return "not_due"
        task_revision = tx.tasks.current_task_revision_row(task.id)
        data_scope = Scope(
            tenant_id=task.tenant_id,
            agent_id=task.agent_id,
            space_group_id=task.space_group_id,
            space_id=task.space_id,
            session_id=task.session_id,
        )
        if not scope_allows(data_scope, request_scope):
            return "scope_mismatch"
        if not evaluate_privacy(task_revision.privacy_labels, data_scope, request_scope, access):
            return "privacy_blocked"
        return None

    def _rehydrate_observation(
        self,
        tx: Transaction,
        access: AccessContext,
        request_scope: Scope,
        candidate: RecallCandidate,
    ) -> str | None:
        try:
            observation = tx.observations.get(candidate.resource_id)
        except Exception:
            return "observation_missing"
        if observation.revision != candidate.resource_revision:
            return "stale_revision"
        if tx.is_tombstoned(access.tenant_id, "observation", observation.id):
            return "tombstoned"
        if not scope_allows(_observation_scope(observation), request_scope):
            return "scope_mismatch"
        if not evaluate_privacy(
            observation.privacy_labels, _observation_scope(observation), request_scope, access
        ):
            return "privacy_blocked"
        return None

    def _rehydrate_state(
        self,
        tx: Transaction,
        request: StructuredRecallRequest,
        request_scope: Scope,
        candidate: RecallCandidate,
        now_us: int,
    ) -> str | None:
        del request
        try:
            record = tx.states.get(candidate.resource_id)
        except Exception:
            return "state_missing"
        if record.current_revision != candidate.resource_revision:
            return "stale_revision"
        if tx.is_tombstoned(record.tenant_id, "state_record", record.id):
            return "tombstoned"
        revision = tx.states.current_revision(record.current_revision_id)
        if revision.expires_us is not None and revision.expires_us <= now_us:
            return "expired"
        if not scope_allows(
            Scope(
                tenant_id=record.tenant_id,
                agent_id=record.agent_id,
                space_group_id=record.space_group_id,
                space_id=record.space_id,
                session_id=record.session_id,
            ),
            request_scope,
        ):
            return "scope_mismatch"
        return None

    def _rehydrate_focus(
        self,
        tx: Transaction,
        access: AccessContext,
        request_scope: Scope,
        candidate: RecallCandidate,
        now_us: int,
    ) -> str | None:
        try:
            item = tx.focus.get(candidate.resource_id)
        except Exception:
            return "focus_missing"
        if item.current_revision != candidate.resource_revision:
            return "stale_revision"
        if tx.is_tombstoned(item.tenant_id, "focus_item", item.id):
            return "tombstoned"
        focus_revision = tx.focus.current_revision_row(item.id)
        if focus_revision.status != FocusStatus.ACTIVE.value:
            return "status_not_active"
        if focus_revision.expires_us is not None and focus_revision.expires_us <= now_us:
            return "expired"
        data_scope = Scope(
            tenant_id=item.tenant_id,
            agent_id=item.agent_id,
            space_group_id=item.space_group_id,
            space_id=item.space_id,
            session_id=item.session_id,
        )
        if not scope_allows(data_scope, request_scope):
            return "scope_mismatch"
        if not evaluate_privacy(focus_revision.privacy_labels, data_scope, request_scope, access):
            return "privacy_blocked"
        return None

    def _rehydrate_claim(
        self,
        tx: Transaction,
        access: AccessContext,
        request: StructuredRecallRequest,
        request_scope: Scope,
        candidate: RecallCandidate,
        now_us: int,
    ) -> str | None:
        try:
            claim = tx.claims.get(candidate.resource_id)
        except Exception:
            return "claim_missing"
        revision = None
        if request.as_of_us is not None:
            if claim.history_available_from_us > request.as_of_us:
                return "history_unavailable"
            revision = tx.claims.revision_current_at(claim.id, request.as_of_us)
            if revision is None:
                return "as_of_not_visible"
            if revision.revision != candidate.resource_revision:
                return "stale_revision"
        else:
            if claim.current_revision != candidate.resource_revision:
                return "stale_revision"
            revision = tx.claims.current_revision_row(claim.id)
        if tx.is_tombstoned(access.tenant_id, "claim", claim.id):
            return "tombstoned"
        if not request.as_of_us and claim.status not in CLAIM_CURRENT_VISIBLE_STATUSES:
            return "status_not_visible"
        if revision.content_hash != candidate.content_hash and candidate.content_hash:
            return "content_hash_mismatch"
        data_scope = _claim_scope(claim)
        if not scope_allows(data_scope, request_scope):
            return "scope_mismatch"
        if not evaluate_privacy(revision.privacy_labels, data_scope, request_scope, access):
            return "privacy_blocked"
        # Valid time is evaluated at the REQUEST's evaluation instant: as_of
        # for historical reads (the claim was valid AT as_of), now for
        # current reads. A now-based check on an as_of request drops claims
        # that were valid then but expired now, and admits claims that were
        # not yet effective then.
        valid_at_us = request.as_of_us if request.as_of_us is not None else now_us
        if claim.valid_from_us is not None and claim.valid_from_us > valid_at_us:
            return "not_yet_valid"
        if claim.valid_until_us is not None and claim.valid_until_us <= valid_at_us:
            return "expired"
        return None

    def _rehydrate_episode(
        self,
        tx: Transaction,
        access: AccessContext,
        request_scope: Scope,
        candidate: RecallCandidate,
        now_us: int,
    ) -> str | None:
        del now_us
        try:
            episode = tx.episodes.get(candidate.resource_id)
        except Exception:
            return "episode_missing"
        if episode.current_revision != candidate.resource_revision:
            return "stale_revision"
        if tx.is_tombstoned(access.tenant_id, "episode", episode.id):
            return "tombstoned"
        if episode.status not in ("open", "sealed"):
            return "status_not_visible"
        revision = tx.episodes.current_revision_row(episode.id)
        if candidate.content_hash and revision.content_hash != candidate.content_hash:
            return "content_hash_mismatch"
        data_scope = Scope(
            tenant_id=episode.tenant_id,
            agent_id=episode.agent_id,
            space_group_id=episode.space_group_id,
            space_id=episode.space_id,
            session_id=episode.session_id,
        )
        if not scope_allows(data_scope, request_scope):
            return "scope_mismatch"
        if not evaluate_privacy(revision.privacy_labels, data_scope, request_scope, access):
            return "privacy_blocked"
        return None

    def _rehydrate_note(
        self,
        tx: Transaction,
        access: AccessContext,
        request_scope: Scope,
        candidate: RecallCandidate,
        now_us: int,
    ) -> str | None:
        del now_us
        try:
            note = tx.notes.get(candidate.resource_id)
        except Exception:
            return "note_missing"
        if note.current_revision != candidate.resource_revision:
            return "stale_revision"
        if tx.is_tombstoned(access.tenant_id, "note", note.id):
            return "tombstoned"
        if note.status not in (
            NoteStatus.INBOX.value,
            NoteStatus.PINNED.value,
            NoteStatus.SNOOZED.value,
        ):
            return "status_not_visible"
        revision = tx.notes.current_revision_row(note.id)
        if candidate.content_hash and revision.content_hash != candidate.content_hash:
            return "content_hash_mismatch"
        data_scope = Scope(
            tenant_id=note.tenant_id,
            agent_id=note.agent_id,
            space_group_id=note.space_group_id,
            space_id=note.space_id,
            session_id=note.session_id,
        )
        if not scope_allows(data_scope, request_scope):
            return "scope_mismatch"
        if not evaluate_privacy(revision.privacy_labels, data_scope, request_scope, access):
            return "privacy_blocked"
        return None

    def _rehydrate_relation(
        self,
        tx: Transaction,
        access: AccessContext,
        request: StructuredRecallRequest,
        request_scope: Scope,
        candidate: RecallCandidate,
        now_us: int,
    ) -> str | None:
        try:
            relation = tx.relations.get(candidate.resource_id)
        except Exception:
            return "relation_missing"
        if relation.current_revision != candidate.resource_revision:
            return "stale_revision"
        if tx.is_tombstoned(access.tenant_id, "relation", relation.id):
            return "tombstoned"
        if relation.status not in ("active", "disputed"):
            return "status_not_visible"
        revision = tx.relations.current_revision_row(relation.id)
        if candidate.content_hash and revision.content_hash != candidate.content_hash:
            return "content_hash_mismatch"
        valid_at_us = request.as_of_us if request.as_of_us is not None else now_us
        if relation.valid_from_us is not None and relation.valid_from_us > valid_at_us:
            return "not_yet_valid"
        if relation.valid_until_us is not None and relation.valid_until_us <= valid_at_us:
            return "expired"
        data_scope = Scope(
            tenant_id=relation.tenant_id,
            agent_id=relation.agent_id,
            space_group_id=relation.space_group_id,
            space_id=relation.space_id,
            session_id=relation.session_id,
        )
        if not scope_allows(data_scope, request_scope):
            return "scope_mismatch"
        if not evaluate_privacy(revision.privacy_labels, data_scope, request_scope, access):
            return "privacy_blocked"
        return None

    def _apply_budgets(
        self, candidates: list[RecallCandidate], request: StructuredRecallRequest
    ) -> list[RecallCandidate]:
        """Legacy entry kept for Phase 3/4 call sites; delegates to v2."""
        return self._rank_and_trim(candidates, request)


def _persona_metadata(tx: Transaction, agent_id: str) -> tuple[int, str]:
    """Top-level persona revision/hash — persona is never a candidate."""
    try:
        agent = tx.get_agent(agent_id)
    except Exception:
        return 0, ""
    if agent.persona_current_revision_id is None:
        return 0, ""
    try:
        persona = tx.get_persona_revision(agent.persona_current_revision_id)
    except Exception:
        return 0, ""
    return persona.revision, persona.content_hash


def _resolve_actor_entity(
    tx: Transaction, tenant_id: str, provider: str, realm: str, external_id: str
) -> str:
    """Resolve one ExternalActor through the identity registry.

    The caller never supplies internal entity ids (§18.1); the current
    speaker MUST resolve through a verified binding.
    """
    key = ExternalIdentityKey(
        tenant_id=tenant_id, provider=provider, realm=realm, external_id=external_id
    )
    identity = tx.find_external_identity(key)
    if identity is None:
        raise IdentityNotFoundError(
            "external actor identity is not registered",
            details={"provider": provider, "realm": realm},
        )
    binding = tx.verified_binding_for(identity.id)
    if binding is None:
        raise IdentityNotFoundError(
            "external actor identity has no verified binding",
            details={"provider": provider, "realm": realm},
        )
    return binding.entity_id


# ---------------------------------------------------------------------------
# Public /v1/recall service (Phase 6, ADR-0014 §3-5)


@dataclass(frozen=True, slots=True)
class ExternalActorRef:
    provider: str
    external_id: str
    realm: str = "default"
    weight: float = 1.0


@dataclass(frozen=True, slots=True)
class RecallUsageReportInput:
    request_id: str
    host_cycle_id: str
    returned_candidate_ids: tuple[str, ...]
    host_selected_candidate_ids: tuple[str, ...]
    model_visible_candidate_ids: tuple[str, ...]
    persona_revision: int
    reported_at_us: int


@dataclass(frozen=True, slots=True)
class RecallUsageReportResult:
    report_id: str
    created: bool
    model_visible_count: int
    host_selected_count: int
    returned_count: int


def _request_fingerprint(request: StructuredRecallRequest) -> str:
    """Stable digest of the request's logical content for replay identity.

    The deadline is EXCLUDED: ``build_request`` re-derives it from the wall
    clock on every call, and a transport retry of the same logical request
    carries a different one. Everything that shapes the response — scope
    dims, filters, budgets, purpose, as_of — is included.
    """
    payload: dict[str, object] = {
        "version": 1,
        "request_id": request.request_id,
        "agent_id": request.agent_id,
        "space_group_id": request.space_group_id,
        "space_id": request.space_id,
        "session_id": request.session_id,
        "token_budget": request.token_budget,
        "layer_budgets": request.layer_budgets or {},
        "candidate_limits": request.candidate_limits,
        "minimum_watermark": request.minimum_watermark,
        "include_trace": request.include_trace,
        "allow_partial": request.allow_partial,
        "state_namespaces": list(request.state_namespaces),
        "topic": request.topic,
        "purpose": request.purpose,
        "categories": sorted(request.categories) if request.categories is not None else None,
        "resource_types": sorted(request.resource_types)
        if request.resource_types is not None
        else None,
        "requested_privacy_labels": sorted(request.requested_privacy_labels)
        if request.requested_privacy_labels is not None
        else None,
        "as_of_us": request.as_of_us,
        "max_route_concurrency": request.max_route_concurrency,
    }
    return content_hash(payload)


def _scope_to_json(scope: Scope) -> dict[str, object]:
    return {
        "tenant_id": scope.tenant_id,
        "agent_id": scope.agent_id,
        "space_group_id": scope.space_group_id,
        "space_id": scope.space_id,
        "session_id": scope.session_id,
    }


def _scope_from_json(raw: dict[str, Any]) -> Scope:
    return Scope(
        tenant_id=str(raw["tenant_id"]),
        agent_id=str(raw["agent_id"]),
        space_group_id=raw.get("space_group_id"),
        space_id=raw.get("space_id"),
        session_id=raw.get("session_id"),
    )


def _candidate_to_json(candidate: RecallCandidate) -> dict[str, object]:
    return {
        "candidate_id": candidate.candidate_id,
        "route": candidate.route,
        "resource_type": candidate.resource_type,
        "resource_id": candidate.resource_id,
        "resource_revision": candidate.resource_revision,
        "text": candidate.text,
        "scores": candidate.scores,
        "final_score": candidate.final_score,
        "token_estimate": candidate.token_estimate,
        "occurred_us": candidate.occurred_us,
        "scope": _scope_to_json(candidate.scope),
        "privacy_labels": list(candidate.privacy_labels),
        "expires_us": candidate.expires_us,
        "content_hash": candidate.content_hash,
        "subject_entity_id": candidate.subject_entity_id,
        "category": candidate.category,
        "conflict_group": candidate.conflict_group,
        "conflict_state": candidate.conflict_state,
        "missing_components": list(candidate.missing_components),
        "placement": candidate.placement,
    }


def _candidate_from_json(raw: dict[str, Any]) -> RecallCandidate:
    return RecallCandidate(
        candidate_id=str(raw["candidate_id"]),
        route=str(raw["route"]),
        resource_type=str(raw["resource_type"]),
        resource_id=str(raw["resource_id"]),
        resource_revision=int(raw["resource_revision"]),
        text=str(raw["text"]),
        scores={str(k): v for k, v in dict(raw["scores"]).items()},
        final_score=float(raw["final_score"]),
        token_estimate=int(raw["token_estimate"]),
        occurred_us=int(raw["occurred_us"]),
        scope=_scope_from_json(dict(raw["scope"])),
        privacy_labels=tuple(str(v) for v in raw["privacy_labels"]),
        expires_us=raw["expires_us"],
        content_hash=str(raw["content_hash"]),
        subject_entity_id=raw["subject_entity_id"],
        category=str(raw["category"]),
        conflict_group=raw["conflict_group"],
        conflict_state=raw["conflict_state"],
        missing_components=tuple(str(v) for v in raw["missing_components"]),
        placement=str(raw["placement"]),
    )


def _result_to_json(result: StructuredRecallResult) -> str:
    """Serialize the served envelope for request-id replay (ADR-0014 §7).

    The replay must be byte-stable: the same request id returns the FIRST
    response verbatim — candidate order, scores, conflict marks and all —
    even after the canonical state moved on.
    """
    payload: dict[str, object] = {
        "version": 1,
        "request_id": result.request_id,
        "source_watermark": result.source_watermark,
        "completed_routes": list(result.completed_routes),
        "degraded_routes": [
            {
                "route": route.route,
                "reason_code": route.reason_code,
                "retryable": route.retryable,
                "fallback": route.fallback,
            }
            for route in result.degraded_routes
        ],
        "partial": result.partial,
        "candidates": [_candidate_to_json(candidate) for candidate in result.candidates],
        "dropped_by_rehydrate": result.dropped_by_rehydrate,
        "pending_event_ids": list(result.pending_event_ids),
        "persona_revision": result.persona_revision,
        "persona_content_hash": result.persona_content_hash,
        "retrieved_count": result.retrieved_count,
        "tombstone_watermark": result.tombstone_watermark,
        "trace": None
        if result.trace is None
        else {
            "request_hash": result.trace.request_hash,
            "ranker_version": result.trace.ranker_version,
            "total_duration_us": result.trace.total_duration_us,
            "routes": [
                {
                    "route": trace.route,
                    "outcome": trace.outcome,
                    "candidate_count": trace.candidate_count,
                    "duration_us": trace.duration_us,
                    "fallback": trace.fallback,
                }
                for trace in result.trace.routes
            ],
            "rehydrated_out": result.trace.rehydrated_out,
            "missing_score_components": result.trace.missing_score_components,
        },
    }
    return canonical_json(payload)


def _result_from_json(payload: str) -> StructuredRecallResult:
    raw: dict[str, Any] = dict(json.loads(payload))
    if int(raw.get("version", 0)) != 1:
        raise ConflictError("stored recall response has an unsupported version")
    trace_raw = raw.get("trace")
    trace = (
        None
        if trace_raw is None
        else RecallTrace(
            request_hash=str(trace_raw["request_hash"]),
            ranker_version=int(trace_raw["ranker_version"]),
            total_duration_us=int(trace_raw["total_duration_us"]),
            routes=tuple(
                RouteTrace(
                    str(item["route"]),
                    str(item["outcome"]),
                    int(item["candidate_count"]),
                    int(item["duration_us"]),
                    item["fallback"],
                )
                for item in trace_raw["routes"]
            ),
            rehydrated_out=int(trace_raw["rehydrated_out"]),
            missing_score_components=int(trace_raw["missing_score_components"]),
        )
    )
    return StructuredRecallResult(
        request_id=str(raw["request_id"]),
        source_watermark=int(raw["source_watermark"]),
        completed_routes=tuple(str(v) for v in raw["completed_routes"]),
        degraded_routes=tuple(
            DegradedRoute(
                str(item["route"]),
                str(item["reason_code"]),
                bool(item["retryable"]),
                str(item["fallback"]),
            )
            for item in raw["degraded_routes"]
        ),
        partial=bool(raw["partial"]),
        candidates=tuple(_candidate_from_json(item) for item in raw["candidates"]),
        dropped_by_rehydrate=int(raw["dropped_by_rehydrate"]),
        trace=trace,
        pending_event_ids=tuple(str(v) for v in raw["pending_event_ids"]),
        persona_revision=int(raw["persona_revision"]),
        persona_content_hash=str(raw["persona_content_hash"]),
        retrieved_count=int(raw["retrieved_count"]),
        tombstone_watermark=int(raw["tombstone_watermark"]),
    )


class RecallService:
    """The /v1/recall application service (ADR-0014 §3-4).

    Converts the public request onto the orchestrator's internal shape,
    converts the wall-clock deadline to the monotonic clock exactly ONCE,
    resolves ExternalActors server-side, persists the retrieved/returned
    usage stages and renders the public envelope.
    """

    def __init__(
        self,
        orchestrator: StructuredRecallOrchestrator,
        uow: UnitOfWork,
        clock: Clock,
        *,
        monotonic: MonotonicClock | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._uow = uow
        self._clock = clock
        self._monotonic = monotonic or SystemMonotonicClock()

    def build_request(
        self,
        *,
        request_id: str,
        agent_id: str,
        space_id: str,
        deadline_at_us: int,
        session_id: str | None = None,
        space_group_id: str | None = None,
        topic: str = "",
        purpose: str = "reply",
        token_budget: int = 2_000,
        layer_budgets: dict[str, int] | None = None,
        candidate_limits: dict[str, int] | None = None,
        categories: frozenset[str] | None = None,
        resource_types: frozenset[str] | None = None,
        requested_privacy_labels: frozenset[str] | None = None,
        as_of_us: int | None = None,
        minimum_watermark: int | None = None,
        include_trace: bool = False,
        allow_partial: bool = True,
        max_route_concurrency: int = DEFAULT_ROUTE_CONCURRENCY,
    ) -> StructuredRecallRequest:
        """Validate the public surface and convert the deadline ONCE."""
        if purpose not in RECALL_PURPOSES:
            raise InvalidRequestError(f"unknown recall purpose: {purpose!r}")
        if not topic or not topic.strip():
            raise InvalidRequestError("topic must be a non-empty string")
        remaining_us = deadline_at_us - self._clock.now_us()
        deadline_monotonic = self._monotonic.monotonic_us() + max(0, remaining_us)
        return StructuredRecallRequest(
            request_id=request_id,
            agent_id=agent_id,
            space_id=space_id,
            session_id=session_id,
            space_group_id=space_group_id,
            deadline_monotonic_us=deadline_monotonic,
            token_budget=token_budget,
            layer_budgets=layer_budgets,
            candidate_limits=candidate_limits or {},
            minimum_watermark=minimum_watermark,
            include_trace=include_trace,
            allow_partial=allow_partial,
            topic=topic,
            purpose=purpose,
            categories=categories,
            resource_types=resource_types,
            requested_privacy_labels=requested_privacy_labels,
            as_of_us=as_of_us,
            max_route_concurrency=max_route_concurrency,
        )

    def resolve_speaker(self, access: AccessContext, actors: tuple[ExternalActorRef, ...]) -> str:
        """Resolve the current speaker (first actor) server-side."""
        if not actors:
            raise InvalidRequestError("actors must contain the current speaker")
        speaker = actors[0]
        with self._uow.read() as tx:
            return _resolve_actor_entity(
                tx,
                access.tenant_id,
                speaker.provider,
                speaker.realm,
                speaker.external_id,
            )

    def recall(
        self,
        access: AccessContext,
        request: StructuredRecallRequest,
        *,
        actors: tuple[ExternalActorRef, ...] | None = None,
    ) -> StructuredRecallResult:
        """Run the orchestrator, persist the served envelope, replay on retry.

        The request model is caller-constructible, so ``speaker_entity_id``
        is untrusted input: it is neutralized FIRST and only the server-side
        actor resolution below may set it (§18.1, ADR-0014 §3) — an
        actorless request stays actorless no matter what the caller wrote.

        Request-level idempotency goes beyond first-write-wins: a retry of
        the same request id REPLAYS the stored first response verbatim (the
        canonical state may have moved; the host's usage report of either
        execution validates against the same returned set), a different
        request body under the same id is rejected, and an erasure that
        invalidated a referenced resource scrubs the stored body so the
        replay fails closed instead of resurrecting it.
        """
        if request.speaker_entity_id is not None:
            request = replace(request, speaker_entity_id=None)
        fingerprint = _request_fingerprint(request)
        with self._uow.read() as tx:
            # Authenticate BEFORE the replay short-circuit: an unauthorized
            # probe must never learn whether the request id already exists.
            StructuredRecallOrchestrator._authorize_request(tx, access, request)
            existing = tx.usage.get_request(access.tenant_id, request.request_id)
            if existing is not None:
                if str(existing["request_fingerprint"]) != fingerprint:  # type: ignore[index]
                    raise InvalidRequestError(
                        "recall request id was already used by a different request",
                        details={"request_id": request.request_id},
                    )
                stored = existing["response_json"]  # type: ignore[index]
                if stored is None:
                    raise ConflictError(
                        "recall response for this request id was scrubbed by an erasure",
                        details={"request_id": request.request_id},
                    )
                return _result_from_json(str(stored))
        effective = request
        if actors is not None:
            speaker = actors[0] if actors else None
            if speaker is None:
                raise InvalidRequestError("actors must contain the current speaker")
            with self._uow.read() as tx:
                speaker_entity_id = _resolve_actor_entity(
                    tx,
                    access.tenant_id,
                    speaker.provider,
                    speaker.realm,
                    speaker.external_id,
                )
            effective = replace(request, speaker_entity_id=speaker_entity_id)
        result = self._orchestrator.recall(access, effective)
        returned_ids = [candidate.candidate_id for candidate in result.candidates]
        resource_ids = sorted({candidate.resource_id for candidate in result.candidates})
        with self._uow.write() as tx:
            tx.usage.insert_request(
                request_id=effective.request_id,
                tenant_id=access.tenant_id,
                agent_id=effective.agent_id,
                persona_revision=result.persona_revision,
                source_watermark=result.source_watermark,
                tombstone_watermark=result.tombstone_watermark,
                schema_version=1,
                ranker_version=RECALL_RANKER_VERSION,
                token_estimator_version=TOKEN_ESTIMATOR_VERSION,
                retrieved_count=result.retrieved_count,
                returned_candidate_ids=returned_ids,
                request_fingerprint=fingerprint,
                resource_ids=resource_ids,
                response_json=_result_to_json(result),
            )
        return result


class RecallUsageService:
    """Host usage report validation and idempotent merge (ADR-0014 §7)."""

    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    def report(
        self, access: AccessContext, report: RecallUsageReportInput
    ) -> RecallUsageReportResult:
        if not report.host_cycle_id:
            raise InvalidRequestError("host_cycle_id must not be empty")
        with self._uow.write() as tx:
            row = tx.usage.get_request(access.tenant_id, report.request_id)
            if row is None:
                raise NotReadyError(
                    "usage report references an unknown recall request",
                    details={"request_id": report.request_id},
                )
            agent_id = str(row["agent_id"])  # type: ignore[index]
            if agent_id not in access.agent_ids and not access.admin:
                raise AccessDeniedError("recall request belongs to another agent")
            stored_persona = int(row["persona_revision"])  # type: ignore[index]
            if report.persona_revision != stored_persona:
                raise RevisionMismatchError(
                    "persona revision",
                    report.request_id,
                    report.persona_revision,
                    stored_persona,
                )
            stored: list[str] = json_loads_ids(str(row["returned_candidate_ids"]))  # type: ignore[index]
            returned = tuple(stored)
            # Completeness echo: the host must echo the exact returned set.
            if set(report.returned_candidate_ids) != set(returned):
                raise InvalidRequestError(
                    "returned_candidate_ids must echo the request's returned set",
                    details={"returned_count": len(returned)},
                )
            host_selected = report.host_selected_candidate_ids
            model_visible = report.model_visible_candidate_ids
            # Forgery gates: every id must belong to THIS request, and the
            # stage chain must be a subset chain (ADR-0014 §7).
            if not set(host_selected) <= set(returned):
                raise InvalidRequestError("host_selected must be a subset of returned")
            if not set(model_visible) <= set(host_selected):
                raise InvalidRequestError("model_visible must be a subset of host_selected")
            existing = tx.usage.get_report(
                access.tenant_id, report.request_id, report.host_cycle_id
            )
            if existing is not None:
                # An idempotent replay must replay the FIRST report, not
                # silently accept a different payload under the same
                # identity: a diverging set is a conflicting report, and the
                # returned counts always describe the STORED report.
                stored_selected = set(
                    json_loads_ids(str(existing["host_selected_ids"]))  # type: ignore[index]
                )
                stored_visible = set(
                    json_loads_ids(str(existing["model_visible_ids"]))  # type: ignore[index]
                )
                if stored_selected != set(host_selected) or stored_visible != set(model_visible):
                    raise InvalidRequestError(
                        "usage report replay conflicts with the stored report for this host cycle",
                        details={"report_id": str(existing["id"])},  # type: ignore[index]
                    )
                return RecallUsageReportResult(
                    report_id=str(existing["id"]),  # type: ignore[index]
                    created=False,
                    model_visible_count=len(stored_visible),
                    host_selected_count=len(stored_selected),
                    returned_count=len(returned),
                )
            report_id, created = tx.usage.insert_report(
                tenant_id=access.tenant_id,
                request_id=report.request_id,
                agent_id=agent_id,
                app_instance_id=access.app_instance_id,
                host_cycle_id=report.host_cycle_id,
                persona_revision=report.persona_revision,
                host_selected_ids=host_selected,
                model_visible_ids=model_visible,
                reported_at_us=report.reported_at_us,
            )
            return RecallUsageReportResult(
                report_id=report_id,
                created=created,
                model_visible_count=len(model_visible),
                host_selected_count=len(host_selected),
                returned_count=len(returned),
            )


def json_loads_ids(raw: str) -> list[str]:
    import json

    decoded = json.loads(raw)
    return [str(item) for item in decoded] if isinstance(decoded, list) else []


class SearchService:
    """The /v1/search application service: FTS-backed cross-resource search
    over the trusted current generation with the same rehydrate discipline
    (ADR-0014 §8)."""

    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        projection: FtsProjectionService,
        estimator: TokenEstimator | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._projection = projection
        self._estimator = estimator or DefaultTokenEstimator()

    def search(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        space_id: str | None = None,
        session_id: str | None = None,
        space_group_id: str | None = None,
        query: str,
        limit: int = 50,
    ) -> list[RecallCandidate]:
        if limit < 1 or limit > 200:
            raise InvalidRequestError("limit must be within 1..200")
        match = build_fts_query(query)
        now_us = self._clock.now_us()
        with self._uow.read() as tx:
            from iris_memory_core.application.write_support import (
                authorize_scope,
            )

            request_scope = authorize_scope(
                tx,
                access,
                agent_id=agent_id,
                space_id=space_id,
                session_id=session_id,
                space_group_id=space_group_id,
            )
            hits = self._projection.search_in_tx(
                tx,
                tenant_id=access.tenant_id,
                agent_id=agent_id,
                match_expression=match,
                space_group_id=space_group_id,
                space_id=space_id,
                session_id=session_id,
                valid_at_us=now_us,
                limit=limit * 2,
            )
            candidates: list[RecallCandidate] = []
            seen: set[str] = set()
            for hit in hits:
                document = hit.document
                if document.resource_id in seen:
                    continue
                seen.add(document.resource_id)
                canonical = _canonical_projection_row(
                    tx, document.resource_type, document.resource_id
                )
                if canonical is None:
                    continue
                current, revision, text, category, occurred_us = canonical
                if current.current_revision != document.resource_revision:
                    continue
                data_scope = Scope(
                    tenant_id=document.tenant_id,
                    agent_id=document.agent_id,
                    space_group_id=document.space_group_id,
                    space_id=document.space_id,
                    session_id=document.session_id,
                )
                if not scope_allows(data_scope, request_scope):
                    continue
                if not evaluate_privacy(revision.privacy_labels, data_scope, request_scope, access):
                    continue
                scores: dict[str, float | None] = dict(
                    _fts_base_scores(document.resource_type, current)
                )
                scores["relevance"] = hit.relevance
                scores["recency"] = round(_recency(now_us, occurred_us), 6)
                candidates.append(
                    RecallCandidate(
                        candidate_id=_candidate_id(
                            ROUTE_FTS, document.resource_id, document.resource_revision
                        ),
                        route=ROUTE_FTS,
                        resource_type=document.resource_type,
                        resource_id=document.resource_id,
                        resource_revision=document.resource_revision,
                        text=text,
                        scores=scores,
                        final_score=hit.relevance,
                        token_estimate=self._estimator.estimate(text),
                        occurred_us=occurred_us,
                        scope=data_scope,
                        privacy_labels=revision.privacy_labels,
                        content_hash=revision.content_hash,
                        subject_entity_id=document.subject_entity_id,
                        category=category,
                    )
                )
                if len(candidates) >= limit:
                    break
            return candidates


def new_request_id() -> str:
    return str(uuid.uuid4())


__all__ = [
    "CATEGORY_PRIORITY",
    "DEFAULT_ROUTES",
    "DEFAULT_ROUTE_CONCURRENCY",
    "DEFAULT_VECTOR_CANDIDATES",
    "FTS_AS_OF_REASON",
    "RECALL_PURPOSES",
    "RECALL_RANKER_VERSION",
    "ROUTE_CLAIMS",
    "ROUTE_FOCUS",
    "ROUTE_FTS",
    "ROUTE_RECENT",
    "ROUTE_RELATIONS",
    "ROUTE_STATE",
    "ROUTE_TASKS",
    "ROUTE_VECTOR",
    "ClaimsRoute",
    "DeadlineExceededError",
    "DegradedRoute",
    "DueTaskRoute",
    "ExternalActorRef",
    "FocusRoute",
    "FtsRoute",
    "RecallCandidate",
    "RecallRoute",
    "RecallService",
    "RecallTrace",
    "RecallUsageReportInput",
    "RecallUsageReportResult",
    "RecallUsageService",
    "RecentContextRoute",
    "RelationsRoute",
    "RouteDeadlineExceeded",
    "RouteTrace",
    "SearchService",
    "StateRoute",
    "StructuredRecallOrchestrator",
    "StructuredRecallRequest",
    "StructuredRecallResult",
    "VectorRoute",
    "check_deadline",
    "new_request_id",
    "stable_sort_key",
]
