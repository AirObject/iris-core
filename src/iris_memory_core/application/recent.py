"""RecentContextProjection application service (§9.1, Phase 3.1).

Reads serve the verified current generation when it is fresh and intact, and
fall back to a bounded canonical observation window when it is not — the
projection is never allowed to become a fact source (ADR-0001). Rebuilds
follow the shadow discipline: build deterministically, validate the built
object, then insert the generation and swap the pointer inside ONE write
transaction, so a failure anywhere leaves the previous verified generation
(or the canonical fallback) in charge.
"""

from __future__ import annotations

from iris_memory_core.application.ports import Clock, Transaction, UnitOfWork
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    InvalidRequestError,
    NotFoundError,
    require_reason,
)
from iris_memory_core.domain.model import Space
from iris_memory_core.domain.observation import StoredObservation
from iris_memory_core.domain.privacy import evaluate_privacy
from iris_memory_core.domain.recent import (
    RECENT_BUILDER_VERSION,
    BuiltProjection,
    DefaultTokenEstimator,
    RecentContextView,
    RecentWindowPolicy,
    StoredGeneration,
    TokenEstimator,
    build_projection,
    projection_invariants,
    recent_target_key,
)
from iris_memory_core.domain.scope import Scope, scope_allows

# The window fetch cap bounds both the fallback read and the rebuild source;
# the builder's policy then trims within it deterministically.
_WINDOW_FETCH_MULTIPLIER = 5


def _hash_id(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class RecentContextService:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        *,
        policy: RecentWindowPolicy | None = None,
        estimator: TokenEstimator | None = None,
        builder_version: int = RECENT_BUILDER_VERSION,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._policy = policy or RecentWindowPolicy()
        self._estimator = estimator or DefaultTokenEstimator()
        self._builder_version = builder_version

    @property
    def builder_version(self) -> int:
        return self._builder_version

    # -- authorization ------------------------------------------------------

    def _authorize(
        self,
        tx: Transaction,
        access: AccessContext,
        *,
        agent_id: str,
        space_id: str,
        session_id: str | None,
    ) -> tuple[str, Space]:
        agent = tx.get_agent(agent_id)
        if agent.tenant_id != access.tenant_id:
            raise AccessDeniedError("agent belongs to another tenant")
        if agent_id not in access.agent_ids:
            raise AccessDeniedError("agent is outside the access context")
        space = tx.get_space(space_id)
        if space.tenant_id != access.tenant_id:
            raise AccessDeniedError("space belongs to another tenant")
        if space_id not in access.allowed_space_ids:
            raise AccessDeniedError("space is outside the access context")
        if space.agent_id is not None and space.agent_id != agent_id:
            raise AccessDeniedError("space belongs to a different agent")
        if session_id is not None:
            session = tx.get_session(session_id)
            if session.tenant_id != access.tenant_id:
                raise AccessDeniedError("session belongs to another tenant")
            if session.space_id != space_id:
                raise InvalidRequestError("session does not belong to the given space")
        return agent.tenant_id, space

    # -- build helpers -------------------------------------------------------

    def _load_window(
        self,
        tx: Transaction,
        *,
        tenant_id: str,
        agent_id: str,
        space_id: str,
        session_id: str | None,
        access: AccessContext | None = None,
    ) -> list[StoredObservation]:
        """Committed observations of exactly this target.

        With ``access`` (the reader-facing fallback path) each row is also
        re-checked for tombstones and privacy — the canonical fallback must
        never surface content the generation checks would have rejected.
        """
        limit = self._policy.max_observations * _WINDOW_FETCH_MULTIPLIER
        observations = tx.recent.observation_window(
            tenant_id=tenant_id,
            agent_id=agent_id,
            space_id=space_id,
            session_id=session_id,
            limit=limit,
        )
        if access is None:
            return list(observations)
        request = Scope(
            tenant_id=tenant_id, agent_id=agent_id, space_id=space_id, session_id=session_id
        )
        visible: list[StoredObservation] = []
        for observation in observations:
            if tx.is_tombstoned(tenant_id, "observation", observation.id):
                continue
            data_scope = Scope(
                tenant_id=observation.tenant_id,
                agent_id=observation.agent_id,
                space_group_id=observation.space_group_id,
                space_id=observation.space_id,
                session_id=observation.session_id,
            )
            if not scope_allows(data_scope, request):
                continue
            if not evaluate_privacy(observation.privacy_labels, data_scope, request, access):
                continue
            visible.append(observation)
        return visible

    def _build(
        self,
        tx: Transaction,
        *,
        tenant_id: str,
        agent_id: str,
        space_id: str,
        session_id: str | None,
        access: AccessContext | None = None,
    ) -> tuple[BuiltProjection, int]:
        observations = self._load_window(
            tx,
            tenant_id=tenant_id,
            agent_id=agent_id,
            space_id=space_id,
            session_id=session_id,
            access=access,
        )
        watermark_state = tx.watermark(tenant_id, agent_id)
        watermark = watermark_state.current_seq if watermark_state is not None else 0
        projection = build_projection(
            observations,
            watermark=watermark,
            policy=self._policy,
            estimator=self._estimator,
            builder_version=self._builder_version,
        )
        return projection, watermark

    def _generation_is_usable(
        self,
        generation: StoredGeneration,
        tx: Transaction,
        access: AccessContext,
        request: Scope,
        *,
        now_us: int,
        minimum_watermark: int | None = None,
    ) -> tuple[bool, str]:
        """Final acceptance checks for a stored generation before serving it."""
        expected_target_key = recent_target_key(
            request.tenant_id,
            request.agent_id or "",
            request.space_id or "",
            request.session_id,
        )
        if (
            generation.target_key != expected_target_key
            or generation.tenant_id != request.tenant_id
            or generation.agent_id != request.agent_id
            or generation.space_id != request.space_id
            or generation.session_id != request.session_id
        ):
            return False, "generation_target_mismatch"
        if generation.status != "verified":
            return False, "generation_retired"
        if generation.projection.builder_version != self._builder_version:
            return False, "builder_version_mismatch"
        if projection_invariants(generation.projection):
            return False, "projection_invalid"
        if generation.expires_us is not None and generation.expires_us <= now_us:
            return False, "generation_expired"
        if (
            minimum_watermark is not None
            and generation.projection.source_watermark < minimum_watermark
        ):
            return False, "watermark_behind"
        # Every referenced observation must still be a committed, visible fact
        # of this target: tombstoned or scope-violating refs are never served.
        for ref in generation.projection.referenced_refs():
            try:
                observation = tx.observations.get(ref.observation_id)
            except NotFoundError:
                return False, "observation_missing"
            if observation.revision != ref.revision:
                return False, "observation_revision_mismatch"
            if observation.occurred_us != ref.occurred_us:
                return False, "observation_time_mismatch"
            if self._estimator.estimate(observation.content or "") != ref.token_estimate:
                return False, "observation_token_mismatch"
            if tx.is_tombstoned(generation.tenant_id, "observation", observation.id):
                return False, "observation_tombstoned"
            data_scope = Scope(
                tenant_id=observation.tenant_id,
                agent_id=observation.agent_id,
                space_group_id=observation.space_group_id,
                space_id=observation.space_id,
                session_id=observation.session_id,
            )
            if not scope_allows(data_scope, request):
                return False, "observation_scope_mismatch"
            if not evaluate_privacy(observation.privacy_labels, data_scope, request, access):
                return False, "privacy_blocked"
        return True, ""

    # -- reads -----------------------------------------------------------------

    def get(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        space_id: str,
        session_id: str | None = None,
        minimum_watermark: int | None = None,
    ) -> RecentContextView:
        """Serve the verified generation or the canonical fallback window."""
        with self._uow.read() as tx:
            tenant_id, _space = self._authorize(
                tx, access, agent_id=agent_id, space_id=space_id, session_id=session_id
            )
            now_us = self._clock.now_us()
            target_key = recent_target_key(tenant_id, agent_id, space_id, session_id)
            request = Scope(
                tenant_id=tenant_id,
                agent_id=agent_id,
                space_id=space_id,
                session_id=session_id,
            )
            generation = tx.recent.current(target_key)
            if generation is not None:
                usable, reason = self._generation_is_usable(
                    generation,
                    tx,
                    access,
                    request,
                    now_us=now_us,
                    minimum_watermark=minimum_watermark,
                )
                if usable:
                    return RecentContextView(
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        space_id=space_id,
                        session_id=session_id,
                        space_group_id=generation.space_group_id,
                        projection=generation.projection,
                        source="generation",
                        expires_us=generation.expires_us,
                    )
            # Canonical fallback: a bounded, deterministic window read that
            # can never serve another space's observations (structural WHERE),
            # tombstoned rows or privacy-blocked content for THIS reader.
            projection, _watermark = self._build(
                tx,
                tenant_id=tenant_id,
                agent_id=agent_id,
                space_id=space_id,
                session_id=session_id,
                access=access,
            )
            return RecentContextView(
                tenant_id=tenant_id,
                agent_id=agent_id,
                space_id=space_id,
                session_id=session_id,
                source="canonical",
                projection=projection,
                degraded_reason=reason if generation is not None else "no_generation",
            )

    def get_for_route(
        self,
        tx: Transaction,
        access: AccessContext,
        *,
        agent_id: str,
        space_id: str,
        session_id: str | None,
        minimum_watermark: int | None,
    ) -> RecentContextView:
        """In-transaction read used by the recall route (deadline-checked)."""
        tenant_id, _space = self._authorize(
            tx, access, agent_id=agent_id, space_id=space_id, session_id=session_id
        )
        now_us = self._clock.now_us()
        target_key = recent_target_key(tenant_id, agent_id, space_id, session_id)
        request = Scope(
            tenant_id=tenant_id, agent_id=agent_id, space_id=space_id, session_id=session_id
        )
        generation = tx.recent.current(target_key)
        reason = "no_generation"
        if generation is not None:
            usable, reason = self._generation_is_usable(
                generation,
                tx,
                access,
                request,
                now_us=now_us,
                minimum_watermark=minimum_watermark,
            )
            if usable:
                return RecentContextView(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    space_id=space_id,
                    session_id=session_id,
                    space_group_id=generation.space_group_id,
                    projection=generation.projection,
                    source="generation",
                    expires_us=generation.expires_us,
                )
        projection, _watermark = self._build(
            tx,
            tenant_id=tenant_id,
            agent_id=agent_id,
            space_id=space_id,
            session_id=session_id,
            access=access,
        )
        return RecentContextView(
            tenant_id=tenant_id,
            agent_id=agent_id,
            space_id=space_id,
            session_id=session_id,
            source="canonical",
            projection=projection,
            degraded_reason=reason if generation is not None else "no_generation",
        )

    # -- rebuild / invalidate ----------------------------------------------------

    def rebuild(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        space_id: str,
        session_id: str | None = None,
        reason: str | None = None,
    ) -> RecentContextView:
        """Admin/internally-triggered rebuild: build → validate → atomic swap."""
        reason_code = require_reason(reason)
        if not access.admin:
            raise AccessDeniedError("recent context rebuild requires admin access")
        with self._uow.write() as tx:
            tenant_id, space = self._authorize(
                tx, access, agent_id=agent_id, space_id=space_id, session_id=session_id
            )
            projection, _watermark = self._build(
                tx, tenant_id=tenant_id, agent_id=agent_id, space_id=space_id, session_id=session_id
            )
            self._commit_generation(
                tx,
                tenant_id=tenant_id,
                agent_id=agent_id,
                space_group_id=space.space_group_id,
                space_id=space_id,
                session_id=session_id,
                projection=projection,
                actor=f"access:{access.app_instance_id}",
                reason_code=reason_code,
            )
            return RecentContextView(
                tenant_id=tenant_id,
                agent_id=agent_id,
                space_id=space_id,
                session_id=session_id,
                space_group_id=space.space_group_id,
                projection=projection,
                source="generation",
                expires_us=self._clock.now_us() + self._policy.ttl_us,
            )

    def _commit_generation(
        self,
        tx: Transaction,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str,
        session_id: str | None,
        projection: BuiltProjection,
        actor: str,
        reason_code: str,
    ) -> str:
        """Validate then insert+swap atomically; validation failure writes nothing."""
        problems = projection_invariants(projection)
        if problems:
            raise InvalidRequestError(
                "rebuilt projection failed validation",
                details={"problems": list(problems)},
            )
        target_key = recent_target_key(tenant_id, agent_id, space_id, session_id)
        generation_id = tx.recent.insert_generation(
            tenant_id=tenant_id,
            agent_id=agent_id,
            space_group_id=space_group_id,
            space_id=space_id,
            session_id=session_id,
            target_key=target_key,
            projection=projection,
            expires_us=self._clock.now_us() + self._policy.ttl_us,
        )
        tx.recent.swap_pointer(
            target_key=target_key,
            tenant_id=tenant_id,
            agent_id=agent_id,
            space_group_id=space_group_id,
            space_id=space_id,
            session_id=session_id,
            current_generation_id=generation_id,
        )
        tx.audit(
            tenant_id=tenant_id,
            actor=actor,
            action="recent_context.rebuilt",
            resource_type="recent_context_target",
            resource_id=target_key,
            reason_code=reason_code,
            details={
                "builder_version": projection.builder_version,
                "source_watermark": projection.source_watermark,
                "hot_refs": len(projection.hot_observation_refs),
                "segments": len(projection.summary_segments),
                "token_estimate": projection.token_estimate,
                "generation_id_hash": _hash_id(generation_id),
            },
        )
        return generation_id

    def rebuild_internal(
        self,
        tx: Transaction,
        *,
        tenant_id: str,
        agent_id: str,
        space_id: str,
        session_id: str | None,
        actor: str,
        reason_code: str,
    ) -> BuiltProjection:
        """Handler path: the same build/validate/swap inside a caller's tx."""
        space = tx.get_space(space_id)
        projection, _watermark = self._build(
            tx, tenant_id=tenant_id, agent_id=agent_id, space_id=space_id, session_id=session_id
        )
        self._commit_generation(
            tx,
            tenant_id=tenant_id,
            agent_id=agent_id,
            space_group_id=space.space_group_id,
            space_id=space_id,
            session_id=session_id,
            projection=projection,
            actor=actor,
            reason_code=reason_code,
        )
        return projection

    def invalidate(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        space_id: str,
        session_id: str | None = None,
        reason: str | None = None,
    ) -> bool:
        """Drop the pointer; reads fall back to canonical until rebuilt."""
        reason_code = require_reason(reason)
        if not access.admin:
            raise AccessDeniedError("recent context invalidation requires admin access")
        with self._uow.write() as tx:
            tenant_id, _space = self._authorize(
                tx, access, agent_id=agent_id, space_id=space_id, session_id=session_id
            )
            target_key = recent_target_key(tenant_id, agent_id, space_id, session_id)
            retired = tx.recent.retire_pointer(target_key)
            tx.audit(
                tenant_id=tenant_id,
                actor=f"access:{access.app_instance_id}",
                action="recent_context.invalidated",
                resource_type="recent_context_target",
                resource_id=target_key,
                reason_code=reason_code,
                details={"retired": retired},
            )
            return retired

    def maintenance_sweep(self, tx: Transaction, *, now_us: int) -> int:
        """Retire expired generations (focus/recent maintenance handler path)."""
        return tx.recent.expire_stale(now_us)
