"""Structured Recall Orchestrator skeleton (§18, Phase 3.4 — internal only).

Phase 3 composes EXACTLY three routes — ``recent_context``, ``state`` and
``focus`` — with per-route sub-deadlines measured on the MONOTONIC clock,
per-route candidate caps, a unified candidate conversion, deterministic
versioned ranking, layer/token budget trimming and a FINAL canonical
rehydrate that re-verifies Scope/Privacy/Status/Expiry/Revision/Tombstone for
every candidate. Persona is never a candidate here.

Phase 6 owns the full `/v1/recall` protocol: this module deliberately stops
short of FTS/vector/graph routes, cache and usage reporting, and its request/
envelope types stay internal and extensible so Phase 6 can add routes without
redefining these three.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Protocol

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
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    InvalidRequestError,
    NotReadyError,
    ScopeViolationError,
)
from iris_memory_core.domain.focus import FocusStatus
from iris_memory_core.domain.observation import StoredObservation
from iris_memory_core.domain.privacy import evaluate_privacy
from iris_memory_core.domain.recent import DefaultTokenEstimator, TokenEstimator
from iris_memory_core.domain.scope import Scope, scope_allows

#: Bump when scoring weights, category priorities or budget logic change.
RECALL_RANKER_VERSION = 1

ROUTE_RECENT = "recent_context"
ROUTE_STATE = "state"
ROUTE_FOCUS = "focus"
DEFAULT_ROUTES = (ROUTE_RECENT, ROUTE_STATE, ROUTE_FOCUS)

#: Fixed category priorities for the stable sort tie-breaker (§18.6).
CATEGORY_PRIORITY = {ROUTE_RECENT: 0, ROUTE_STATE: 1, ROUTE_FOCUS: 2}

#: Equal share of the remaining budget per route; the last route runs to the
#: total deadline with whatever budget the earlier routes returned.
_ROUTE_SHARES = {ROUTE_RECENT: 1.0 / 3.0, ROUTE_STATE: 1.0 / 3.0, ROUTE_FOCUS: 1.0 / 3.0}

DEFAULT_RECENT_CANDIDATES = 12
DEFAULT_STATE_CANDIDATES = 8
DEFAULT_FOCUS_CANDIDATES = 10

#: Recency reference window for score normalization — deterministic given the
#: injected clock; replay tests pin the clock.
RECENCY_WINDOW_US = 3_600_000_000


def _hash_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class RouteDeadlineExceeded(Exception):
    """Internal cooperative-cancel signal raised inside a route."""


@dataclass(frozen=True, slots=True)
class StructuredRecallRequest:
    """Internal request shape (NOT the frozen Phase 6 /v1/recall contract)."""

    request_id: str
    agent_id: str
    space_id: str
    deadline_monotonic_us: int
    session_id: str | None = None
    token_budget: int = 2_000
    layer_budgets: dict[str, int] | None = None
    candidate_limits: dict[str, int] = field(default_factory=dict)
    minimum_watermark: int | None = None
    include_trace: bool = False
    allow_partial: bool = True
    state_namespaces: tuple[str, ...] = ("runtime", "environment", "topic")

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


@dataclass(frozen=True, slots=True)
class RecallCandidate:
    """Unified internal candidate (a Phase 6-extensible projection of §18.2)."""

    candidate_id: str
    route: str
    resource_type: str
    resource_id: str
    resource_revision: int
    text: str
    scores: dict[str, float]
    final_score: float
    token_estimate: int
    occurred_us: int
    scope: Scope
    privacy_labels: tuple[str, ...]
    expires_us: int | None = None

    @property
    def category(self) -> str:
        return self.route


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
            age = max(0, now_us - observation.occurred_us)
            recency = max(0.0, 1.0 - age / RECENCY_WINDOW_US)
            candidates.append(
                RecallCandidate(
                    candidate_id=_candidate_id(ROUTE_RECENT, observation.id, ref.revision),
                    route=ROUTE_RECENT,
                    resource_type="observation",
                    resource_id=observation.id,
                    resource_revision=ref.revision,
                    text=observation.content or "",
                    scores={"recency": round(recency, 6)},
                    final_score=round(0.5 + 0.5 * recency, 6),
                    token_estimate=self._estimator.estimate(observation.content or ""),
                    occurred_us=observation.occurred_us,
                    scope=_observation_scope(observation),
                    privacy_labels=observation.privacy_labels,
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
                age = max(0, now_us - entry.observed_us)
                recency = max(0.0, 1.0 - age / RECENCY_WINDOW_US)
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
            scores = {
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
                )
            )
        check_deadline(self._monotonic, deadline_us)
        return tuple(candidates)


def _candidate_id(route: str, resource_id: str, revision: int) -> str:
    """Deterministic candidate id — identical across replays of one snapshot."""
    digest = hashlib.sha256(f"{route}:{resource_id}:{revision}".encode()).hexdigest()[:16]
    return f"cand:{digest}"


def stable_sort_key(candidate: RecallCandidate) -> tuple[float, int, int, str]:
    """§18.6: (-final_score, category_priority, occurred_at DESC, id ASC)."""
    return (
        -candidate.final_score,
        CATEGORY_PRIORITY.get(candidate.route, 99),
        -candidate.occurred_us,
        candidate.resource_id,
    )


class StructuredRecallOrchestrator:
    """Runs the three structured routes under one deadline and budget."""

    def __init__(
        self,
        uow: UnitOfWork,
        recent: RecentContextService,
        states: StateService,
        focus: FocusService,
        *,
        clock: Clock,
        monotonic: MonotonicClock | None = None,
        estimator: TokenEstimator | None = None,
        routes: tuple[RecallRoute, ...] | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._monotonic = monotonic or SystemMonotonicClock()
        self._estimator = estimator or DefaultTokenEstimator()
        self._routes: tuple[RecallRoute, ...] = routes or (
            RecentContextRoute(recent, self._monotonic, self._estimator),
            StateRoute(states, self._estimator, monotonic=self._monotonic),
            FocusRoute(focus, self._estimator, monotonic=self._monotonic),
        )

    def recall(
        self, access: AccessContext, request: StructuredRecallRequest
    ) -> StructuredRecallResult:
        started = self._monotonic.monotonic_us()
        request_hash = _hash_id(
            f"{request.request_id}:{request.agent_id}:{request.space_id}:{request.session_id}"
        )
        completed: list[str] = []
        degraded: list[DegradedRoute] = []
        collected: list[RecallCandidate] = []
        route_traces: list[RouteTrace] = []
        dropped = 0
        with self._uow.read() as tx:
            # Authenticate the whole request before any early watermark or
            # deadline outcome. Otherwise an unreachable watermark could
            # short-circuit before the first route's authorization and turn
            # an unauthorized probe into `not_ready` instead of access_denied.
            self._authorize_request(tx, access, request)
            source_watermark = 0
            watermark_state = tx.watermark(access.tenant_id, request.agent_id)
            source_watermark = watermark_state.current_seq if watermark_state is not None else 0
            if (
                request.minimum_watermark is not None
                and source_watermark < request.minimum_watermark
            ):
                # Read-your-writes cannot be satisfied: the store has not seen
                # the claimed watermark. No key Route can succeed, so §18.3
                # requires a stable error rather than an all-degraded envelope.
                raise NotReadyError(
                    "minimum watermark not reachable at this time",
                    details={
                        "minimum_watermark": request.minimum_watermark,
                        "degraded_routes": [route.name for route in self._routes],
                    },
                )
            now_us = self._clock.now_us()
            total_budget = max(0, request.deadline_monotonic_us - started)
            for index, route in enumerate(self._routes):
                route_start = self._monotonic.monotonic_us()
                share = _ROUTE_SHARES.get(route.name, 1.0 / len(self._routes))
                if index == len(self._routes) - 1:
                    route_deadline = request.deadline_monotonic_us
                else:
                    route_deadline = min(
                        request.deadline_monotonic_us,
                        route_start + max(1, int(total_budget * share)),
                    )
                try:
                    candidates = route.collect(tx, request, access, route_deadline, now_us)
                    # The port boundary is authoritative even for a custom Route:
                    # a provider that returns only after its sub-deadline must
                    # not be recorded as completed merely because it omitted its
                    # own cooperative post-I/O check.
                    check_deadline(self._monotonic, route_deadline)
                    collected.extend(candidates)
                    completed.append(route.name)
                    route_traces.append(
                        RouteTrace(
                            route.name,
                            "completed",
                            len(candidates),
                            self._monotonic.monotonic_us() - route_start,
                        )
                    )
                except RouteDeadlineExceeded:
                    degraded.append(
                        DegradedRoute(
                            route.name,
                            "route_deadline_exceeded",
                            True,
                            "route_skipped_partial_results_kept",
                        )
                    )
                    route_traces.append(
                        RouteTrace(
                            route.name,
                            "degraded",
                            0,
                            self._monotonic.monotonic_us() - route_start,
                            "route_deadline_exceeded",
                        )
                    )
                except (AccessDeniedError, ScopeViolationError):
                    # Authorization failures are REQUEST-level errors: a route
                    # must never swallow a scope violation into degradation.
                    raise
                except Exception:
                    # Provider/projection failure degrades this route only;
                    # observation writes and the other routes are unaffected.
                    degraded.append(
                        DegradedRoute(
                            route.name, "route_failed", True, "route_skipped_canonical_intact"
                        )
                    )
                    route_traces.append(
                        RouteTrace(
                            route.name,
                            "degraded",
                            0,
                            self._monotonic.monotonic_us() - route_start,
                            "route_failed",
                        )
                    )
                if self._monotonic.monotonic_us() >= request.deadline_monotonic_us:
                    for remaining in self._routes[index + 1 :]:
                        degraded.append(
                            DegradedRoute(
                                remaining.name,
                                "route_deadline_exceeded",
                                True,
                                "route_skipped_total_deadline",
                            )
                        )
                        route_traces.append(
                            RouteTrace(remaining.name, "degraded", 0, 0, "route_deadline_exceeded")
                        )
                    break
            # Final canonical rehydrate (§18.5): every candidate is re-read
            # from the canonical tables and re-checked before it survives.
            rehydrated: list[RecallCandidate] = []
            for candidate in collected:
                if self._rehydrate(tx, access, request, candidate) is None:
                    rehydrated.append(candidate)
                else:
                    dropped += 1
            trimmed = self._apply_budgets(rehydrated, request)
        # A partial response requires at least one successful key Route (§18.3).
        # All-degraded is a stable error even when partial results were allowed:
        # there is no result whose partiality could be useful or trustworthy.
        partial = bool(degraded)
        if degraded and (not request.allow_partial or not completed):
            # Fail closed: the caller asked for complete-or-error, so a
            # degraded route is a stable request-level error even when other
            # routes completed.
            raise NotReadyError(
                "structured recall has no acceptable complete route result",
                details={"degraded_routes": [d.route for d in degraded]},
            )
        return StructuredRecallResult(
            request_id=request.request_id,
            source_watermark=source_watermark,
            completed_routes=tuple(completed),
            degraded_routes=tuple(degraded),
            partial=partial,
            candidates=tuple(trimmed),
            dropped_by_rehydrate=dropped,
            trace=self._trace(request_hash, started, tuple(route_traces), dropped)
            if request.include_trace
            else None,
        )

    @staticmethod
    def _authorize_request(
        tx: Transaction, access: AccessContext, request: StructuredRecallRequest
    ) -> None:
        agent = tx.get_agent(request.agent_id)
        if agent.tenant_id != access.tenant_id:
            raise AccessDeniedError("agent belongs to another tenant")
        if request.agent_id not in access.agent_ids:
            raise AccessDeniedError("agent is outside the access context")
        space = tx.get_space(request.space_id)
        if space.tenant_id != access.tenant_id:
            raise AccessDeniedError("space belongs to another tenant")
        if request.space_id not in access.allowed_space_ids:
            raise AccessDeniedError("space is outside the access context")
        if space.agent_id is not None and space.agent_id != request.agent_id:
            raise AccessDeniedError("space belongs to a different agent")
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
    ) -> RecallTrace:
        return RecallTrace(
            request_hash=request_hash,
            ranker_version=RECALL_RANKER_VERSION,
            total_duration_us=self._monotonic.monotonic_us() - started_us,
            routes=routes,
            rehydrated_out=dropped,
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
            space_group_id=None,
            space_id=request.space_id,
            session_id=request.session_id,
        )
        now_us = self._clock.now_us()
        if candidate.resource_type == "observation":
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
        if candidate.resource_type == "state_record":
            try:
                record = tx.states.get(candidate.resource_id)
            except Exception:
                return "state_missing"
            if record.current_revision != candidate.resource_revision:
                return "stale_revision"
            if tx.is_tombstoned(access.tenant_id, "state_record", record.id):
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
        if candidate.resource_type == "focus_item":
            try:
                item = tx.focus.get(candidate.resource_id)
            except Exception:
                return "focus_missing"
            if item.current_revision != candidate.resource_revision:
                return "stale_revision"
            if tx.is_tombstoned(access.tenant_id, "focus_item", item.id):
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
            if not evaluate_privacy(
                focus_revision.privacy_labels, data_scope, request_scope, access
            ):
                return "privacy_blocked"
            return None
        return "unknown_resource_type"

    def _apply_budgets(
        self, candidates: list[RecallCandidate], request: StructuredRecallRequest
    ) -> list[RecallCandidate]:
        ordered = sorted(candidates, key=stable_sort_key)
        layer_budgets = request.layer_budgets or {}
        layer_used: dict[str, int] = {}
        total_used = 0
        kept: list[RecallCandidate] = []
        for candidate in ordered:
            layer = layer_budgets.get(candidate.route)
            if (
                layer is not None
                and layer_used.get(candidate.route, 0) + candidate.token_estimate > layer
            ):
                continue
            if total_used + candidate.token_estimate > request.token_budget:
                continue
            layer_used[candidate.route] = (
                layer_used.get(candidate.route, 0) + candidate.token_estimate
            )
            total_used += candidate.token_estimate
            kept.append(candidate)
        return kept


def _observation_scope(observation: StoredObservation) -> Scope:
    return Scope(
        tenant_id=observation.tenant_id,
        agent_id=observation.agent_id,
        space_group_id=observation.space_group_id,
        space_id=observation.space_id,
        session_id=observation.session_id,
    )


def new_request_id() -> str:
    return str(uuid.uuid4())


__all__ = [
    "CATEGORY_PRIORITY",
    "DEFAULT_ROUTES",
    "RECALL_RANKER_VERSION",
    "ROUTE_FOCUS",
    "ROUTE_RECENT",
    "ROUTE_STATE",
    "DegradedRoute",
    "FocusRoute",
    "RecallCandidate",
    "RecallRoute",
    "RecallTrace",
    "RecentContextRoute",
    "RouteDeadlineExceeded",
    "RouteTrace",
    "StateRoute",
    "StructuredRecallOrchestrator",
    "StructuredRecallRequest",
    "StructuredRecallResult",
    "check_deadline",
    "new_request_id",
    "stable_sort_key",
]
