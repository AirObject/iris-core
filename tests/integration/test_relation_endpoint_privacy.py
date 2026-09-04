"""The canonical relations route must apply the graph route's privacy verdict.

ADR-0016 §11.8 taught graph traversal to evaluate each ENDPOINT ENTITY's own
privacy labels, but the canonical ``relations`` route and the final relation
rehydrate never learned it. Since a relation candidate's text and refs NAME
both endpoints, a relation pointing at a ``restricted`` entity disclosed that
entity through the relations route exactly while graph traversal refused it —
one dataset, two privacy verdicts (ADR-0017 §8).

Both tests below fail against the pre-fix implementation.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.application.ports import SystemMonotonicClock
from iris_memory_core.application.recall import RelationsRoute
from iris_memory_core.domain.identity import EntityKind
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.phase8_helpers import TENANT, Phase8World


def _restricted_entity(world: Phase8World, name: str) -> str:
    with world.store.write() as tx:
        entity = tx.identities.insert_entity(
            TENANT,
            EntityKind.PERSON,
            display_name=name,
            privacy_labels=["restricted"],
        )
    world.entities[name] = entity.id
    return entity.id


def _relations_candidates(
    world: Phase8World, speaker_entity_id: str, *, admin: bool = False
) -> tuple[Any, ...]:
    """Drive the REAL RelationsRoute.collect — direct invocation, so no other
    route's verdict can mask this one through cross-route dedupe."""
    request = replace(
        world.service.build_request(
            request_id=f"rel-priv-{speaker_entity_id[:8]}-{admin}-{world.clock.now_us()}",
            agent_id=world.agent,
            space_id=world.space,
            deadline_at_us=world.clock.now_us() + 10_000_000,
            topic="friends",
            purpose="reply",
            token_budget=100_000,
        ),
        speaker_entity_id=speaker_entity_id,
    )
    monotonic = SystemMonotonicClock()
    with world.store.read() as tx:
        return RelationsRoute().collect(
            tx,
            request,
            world.admin_access if admin else world.access,
            monotonic.monotonic_us() + 10_000_000,
            world.clock.now_us(),
        )


class TestRelationEndpointPrivacy:
    def test_public_relation_to_restricted_endpoint_is_not_a_candidate(
        self, world: Phase8World
    ) -> None:
        bob = world.entity("RelPrivBob")
        carol = _restricted_entity(world, "RelRestrictedCarol")
        relation = world.relate("rel-priv-1", bob, carol, relation_type="knows")

        # The relation's OWN labels are public; the endpoint entity is not.
        candidates = _relations_candidates(world, bob)
        assert not any(c.resource_id == relation.relation_id for c in candidates)

        # Admin access evaluates the restricted label and does see it — the
        # check is a privacy verdict, not a blanket failure.
        admin_candidates = _relations_candidates(world, bob, admin=True)
        assert any(c.resource_id == relation.relation_id for c in admin_candidates)

    def test_public_relation_between_public_entities_still_served(self, world: Phase8World) -> None:
        bob = world.entity("RelPublicBob")
        dave = world.entity("RelPublicDave")
        relation = world.relate("rel-priv-2", bob, dave, relation_type="knows")

        candidates = _relations_candidates(world, bob)
        assert any(c.resource_id == relation.relation_id for c in candidates)

    def test_rehydrate_rejects_a_relation_whose_endpoint_became_restricted(
        self, world: Phase8World
    ) -> None:
        """The final rehydrate is the last line of defense (§18.5).

        A candidate collected while both endpoints were public — by ANY route,
        including graph — must still be dropped once an endpoint turns
        restricted between collection and the fresh rehydrate transaction.
        """
        from iris_memory_core.domain.scope import Scope

        bob = world.entity("RehyBob")
        carol = world.entity("RehyCarol")
        relation = world.relate("rel-priv-3", bob, carol, relation_type="knows")

        # Collect while both endpoints are public.
        collected = _relations_candidates(world, bob)
        candidate = next(c for c in collected if c.resource_id == relation.relation_id)

        request = replace(
            world.service.build_request(
                request_id=f"rel-rehy-{world.clock.now_us()}",
                agent_id=world.agent,
                space_id=world.space,
                deadline_at_us=world.clock.now_us() + 10_000_000,
                topic="friends",
                purpose="reply",
                token_budget=100_000,
            ),
            speaker_entity_id=bob,
        )
        scope = Scope(tenant_id=TENANT, agent_id=world.agent, space_id=world.space)
        orchestrator = world.service._orchestrator

        with world.store.read() as tx:
            assert (
                orchestrator._rehydrate_relation(
                    tx, world.access, request, scope, candidate, world.clock.now_us()
                )
                is None
            )

        # Restrict one endpoint AFTER collection.
        with world.store.write() as tx:
            tx.raw().execute(
                "UPDATE entities SET privacy_labels = ? WHERE id = ?",
                ('["restricted"]', carol),
            )

        with world.store.read() as tx:
            assert (
                orchestrator._rehydrate_relation(
                    tx, world.access, request, scope, candidate, world.clock.now_us()
                )
                == "endpoint_privacy_blocked"
            )
            # Admin still passes: this is a privacy verdict, not a hard drop.
            assert (
                orchestrator._rehydrate_relation(
                    tx, world.admin_access, request, scope, candidate, world.clock.now_us()
                )
                is None
            )


@pytest.fixture
def world(
    clocked_store: Store, mutable_clock: MutableClock, generous_gauge: BackpressureGauge
) -> Phase8World:
    return Phase8World(clocked_store, mutable_clock)
