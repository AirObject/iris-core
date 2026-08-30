"""Identity registry: bindings, redirects, field authority and identity views."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from iris_memory_core.application.identity import IdentityService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    BindingConflictError,
    ConflictError,
    IdempotencyKeyReusedError,
    IdempotencyUnavailableError,
    InvalidTransitionError,
    NotFoundError,
    ReasonRequiredError,
    RedirectCycleError,
    RevisionMismatchError,
)
from iris_memory_core.domain.identity import BindingState, EntityKind, EntityState, FieldAuthority
from iris_memory_core.domain.model import Entity, ExternalIdentity
from iris_memory_core.domain.scope import Scope
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.uow import Store
from tests.conftest import access_for


@dataclass(frozen=True, slots=True)
class World:
    """Two entities, one external identity linked to the first entity."""

    tenant: str
    entity_a: Entity
    entity_b: Entity
    identity: ExternalIdentity


@pytest.fixture
def world(store: Store, identities: IdentityService, admin_access: AccessContext) -> World:
    entity_a = identities.create_entity(admin_access, EntityKind.PERSON, display_name="A")
    entity_b = identities.create_entity(admin_access, EntityKind.PERSON, display_name="B")
    identity = identities.register_external_identity(
        admin_access, "qq-onebot11", "bot-1", "10001", entity_id=entity_a.id
    )
    return World(
        tenant=admin_access.tenant_id,
        entity_a=entity_a,
        entity_b=entity_b,
        identity=identity,
    )


def test_external_identity_registration_is_idempotent_by_unique_key(
    store: Store, identities: IdentityService, admin_access: AccessContext, world: World
) -> None:
    again = identities.register_external_identity(
        admin_access, "qq-onebot11", "bot-1", "10001", entity_id=world.entity_b.id
    )
    assert again.id == world.identity.id
    assert again.entity_id == world.entity_a.id  # re-registration never rewrites the link
    other_realm = identities.register_external_identity(
        admin_access, "qq-onebot11", "bot-2", "10001"
    )
    assert other_realm.id != world.identity.id  # realm participates in uniqueness


def test_binding_lifecycle_propose_confirm_revoke(
    store: Store, identities: IdentityService, admin_access: AccessContext, world: World
) -> None:
    binding = identities.propose_binding(
        admin_access,
        world.identity.id,
        world.entity_a.id,
        proof_digest="sha256:abc",
        reason="admin reviewed evidence",
    )
    assert binding.state is BindingState.PROPOSED
    with pytest.raises(AccessDeniedError):
        identities.confirm_binding(
            access_for(world.tenant), binding.id, expected_revision=1, reason="not admin"
        )
    verified = identities.confirm_binding(
        admin_access, binding.id, expected_revision=binding.revision, reason="admin confirmed"
    )
    assert verified.state is BindingState.VERIFIED
    assert verified.confirmed_by == "admin"
    with pytest.raises(InvalidTransitionError):
        identities.confirm_binding(
            admin_access, binding.id, expected_revision=verified.revision, reason="again"
        )
    with pytest.raises(RevisionMismatchError):
        identities.revoke_binding(
            admin_access, binding.id, expected_revision=1, reason="stale revision"
        )
    with store.read() as tx:
        entity = tx.get_entity(world.entity_a.id)
    assert entity.state is EntityState.CANONICAL  # promoted on first verified binding
    revoked = identities.revoke_binding(
        admin_access, binding.id, expected_revision=verified.revision, reason="gdpr request"
    )
    assert revoked.state is BindingState.REVOKED and revoked.valid_until_us is not None


def test_reason_codes_are_required_for_high_risk_operations(
    identities: IdentityService, admin_access: AccessContext, world: World
) -> None:
    with pytest.raises(ReasonRequiredError):
        identities.propose_binding(
            admin_access, world.identity.id, world.entity_a.id, proof_digest="x", reason=""
        )
    binding = identities.propose_binding(
        admin_access, world.identity.id, world.entity_a.id, proof_digest="x", reason="evidence"
    )
    with pytest.raises(ReasonRequiredError):
        identities.confirm_binding(admin_access, binding.id, expected_revision=1, reason=" ")
    with pytest.raises(ReasonRequiredError):
        identities.revoke_binding(admin_access, binding.id, expected_revision=1, reason="")


def test_second_verified_binding_conflicts_and_parks_for_adjudication(
    store: Store, identities: IdentityService, admin_access: AccessContext, world: World
) -> None:
    first = identities.propose_binding(
        admin_access, world.identity.id, world.entity_a.id, proof_digest="d1", reason="evidence"
    )
    identities.confirm_binding(
        admin_access, first.id, expected_revision=first.revision, reason="confirmed"
    )
    second = identities.propose_binding(
        admin_access, world.identity.id, world.entity_b.id, proof_digest="d2", reason="evidence"
    )
    with pytest.raises(BindingConflictError):
        identities.confirm_binding(
            admin_access, second.id, expected_revision=second.revision, reason="confirmed"
        )
    with store.read() as tx:
        parked = tx.get_binding(second.id)
    assert parked.state is BindingState.CONFLICTED
    # After revoking the winner, the conflicted binding can be verified.
    identities.revoke_binding(
        admin_access, first.id, expected_revision=first.revision + 1, reason="wrong entity"
    )
    resolved = identities.confirm_binding(
        admin_access, second.id, expected_revision=2, reason="adjudicated"
    )
    assert resolved.state is BindingState.VERIFIED


def test_nicknames_never_create_verified_bindings(
    store: Store, identities: IdentityService, admin_access: AccessContext
) -> None:
    """Two identities sharing a nickname stay unmerged (ADR-0003)."""
    first = identities.register_external_identity(
        admin_access, "bilibili", "main", "u-1", entity_id=None
    )
    second = identities.register_external_identity(
        admin_access, "bilibili", "main", "u-2", entity_id=None
    )
    assert first.id != second.id
    # No binding is auto-created; nothing is merged by attribute similarity.
    with store.read() as tx:
        assert tx.verified_binding_for(first.id) is None
        assert tx.verified_binding_for(second.id) is None


def test_redirect_chain_resolution_and_guards(
    store: Store, identities: IdentityService, admin_access: AccessContext, world: World
) -> None:
    binding = identities.propose_binding(
        admin_access, world.identity.id, world.entity_a.id, proof_digest="d", reason="evidence"
    )
    identities.confirm_binding(
        admin_access, binding.id, expected_revision=binding.revision, reason="confirmed"
    )
    entity_c = identities.create_entity(admin_access, EntityKind.PERSON, display_name="C")
    # entity_a was promoted to canonical (revision 2) when its binding verified.
    identities.redirect_entity(
        admin_access,
        world.entity_a.id,
        world.entity_b.id,
        expected_revision=world.entity_a.revision + 1,
        reason="duplicate person",
    )
    identities.redirect_entity(
        admin_access,
        world.entity_b.id,
        entity_c.id,
        expected_revision=world.entity_b.revision,
        reason="merged into C",
    )
    view = identities.identity_view(admin_access, world.identity.id)
    # The identity is bound to A; redirects lead to C as the current entity.
    assert view.entity_id_at_ingest == world.entity_a.id
    assert view.current_entity_id == entity_c.id
    with store.read() as tx:
        a = tx.get_entity(world.entity_a.id)
    assert a.state is EntityState.REDIRECTED
    # Cycles and duplicate outgoing redirects are rejected.
    with pytest.raises(RedirectCycleError):
        identities.redirect_entity(
            admin_access, entity_c.id, world.entity_a.id, expected_revision=1, reason="cycle"
        )
    with pytest.raises(ConflictError):
        identities.redirect_entity(
            admin_access, world.entity_a.id, entity_c.id, expected_revision=1, reason="dup"
        )


def test_identity_views_keep_at_ingest_and_current_separate(
    store: Store, identities: IdentityService, admin_access: AccessContext, world: World
) -> None:
    import time

    binding = identities.propose_binding(
        admin_access, world.identity.id, world.entity_a.id, proof_digest="d", reason="evidence"
    )
    identities.confirm_binding(
        admin_access, binding.id, expected_revision=binding.revision, reason="confirmed"
    )
    verified_at = identities.identity_view(admin_access, world.identity.id).entity_id_at_ingest
    assert verified_at == world.entity_a.id

    time.sleep(0.01)
    verified_us = store.clock.now_us()  # safely after confirm, before revoke
    identities.revoke_binding(
        admin_access, binding.id, expected_revision=binding.revision + 1, reason="revoked later"
    )
    after_revoke_us = store.clock.now_us()

    # Current view: no verified binding anymore.
    current = identities.identity_view(admin_access, world.identity.id)
    assert current.current_entity_id is None
    # Historical view at ingest time still resolves the then-verified entity.
    historical = identities.identity_view(admin_access, world.identity.id, at_us=verified_us)
    assert historical.entity_id_at_ingest == world.entity_a.id
    historical_after = identities.identity_view(
        admin_access, world.identity.id, at_us=after_revoke_us
    )
    assert historical_after.entity_id_at_ingest is None


def test_field_authority_merge_semantics(
    store: Store, identities: IdentityService, admin_access: AccessContext, world: World
) -> None:
    entity = world.entity_a
    first = identities.record_attribute(
        admin_access, entity.id, "display_name", "平台名", FieldAuthority.PLATFORM_VERIFIED, "src:1"
    )
    assert first.outcome == "supersede" and first.changed is True
    # Higher authority correction supersedes.
    corrected = identities.record_attribute(
        admin_access,
        entity.id,
        "display_name",
        "用户纠正",
        FieldAuthority.EXPLICIT_CORRECTION,
        "src:2",
    )
    assert corrected.outcome == "supersede"
    assert corrected.current.value == "用户纠正"
    # Lower authority refresh cannot overwrite the explicit correction.
    ignored = identities.record_attribute(
        admin_access, entity.id, "display_name", "推断名", FieldAuthority.INFERRED, "src:3"
    )
    assert ignored.outcome == "ignored" and ignored.changed is False
    assert ignored.current.value == "用户纠正"
    # Same authority, different value: conflict coexists for adjudication.
    coexist = identities.record_attribute(
        admin_access,
        entity.id,
        "display_name",
        "另一权威值",
        FieldAuthority.EXPLICIT_CORRECTION,
        "src:4",
    )
    assert coexist.outcome == "coexist"
    with store.read() as tx:
        conflicts = tx.conflicted_attributes(entity.id)
    assert len(conflicts) == 1 and conflicts[0].value == "另一权威值"
    # The current value is untouched while the conflict awaits adjudication.
    current = identities.record_attribute(
        admin_access, entity.id, "probe", "v", FieldAuthority.INFERRED, "src:5"
    )
    del current


def test_tombstoned_entity_is_excluded_from_reads(
    store: Store, identities: IdentityService, admin_access: AccessContext, world: World
) -> None:
    identities.tombstone_entity(
        admin_access,
        world.entity_a.id,
        expected_revision=world.entity_a.revision,
        reason="gdpr erasure",
    )
    scope = Scope(tenant_id=world.tenant)
    with pytest.raises(NotFoundError):
        identities.get_entity(admin_access, scope, world.entity_a.id)
    with store.read() as tx:
        assert tx.is_tombstoned(world.tenant, "entity", world.entity_a.id)
        seqs = tx.raw().execute("SELECT MAX(tombstone_seq) FROM resource_tombstones").fetchone()
    assert int(seqs[0]) >= 1


def test_lateral_access_matrix_blocks_cross_dimension_reads(
    store: Store, identities: IdentityService, admin_access: AccessContext, world: World
) -> None:
    """Tenant/Agent/Group/Space/Entity cross read directions (quantified baseline)."""
    scope = Scope(tenant_id=world.tenant)
    # Same tenant, plain entity: visible at tenant scope.
    assert identities.get_entity(admin_access, scope, world.entity_a.id).id == world.entity_a.id

    # Cross-tenant read: denied.
    other_tenant = access_for("tenant-b")
    with pytest.raises(AccessDeniedError):
        identities.get_entity(other_tenant, Scope(tenant_id="tenant-b"), world.entity_a.id)

    # entity_private label without consent: not found; with consent: visible.
    private = identities.create_entity(
        admin_access,
        EntityKind.PERSON,
        display_name="private-person",
        privacy_labels=(f"entity:{world.entity_a.id}:private",),
    )
    with pytest.raises(NotFoundError):
        identities.get_entity(admin_access, scope, private.id)
    consenting = access_for(world.tenant, consent_entities=frozenset({world.entity_a.id}))
    assert identities.get_entity(consenting, scope, private.id).id == private.id

    # restricted label: admin only.
    restricted = identities.create_entity(
        admin_access, EntityKind.PERSON, display_name="ops", privacy_labels=("restricted",)
    )
    with pytest.raises(NotFoundError):
        identities.get_entity(access_for(world.tenant), scope, restricted.id)
    assert identities.get_entity(admin_access, scope, restricted.id).id == restricted.id

    # Custom label: granted only when carried by the access context.
    custom = identities.create_entity(
        admin_access, EntityKind.PERSON, display_name="vip", privacy_labels=("custom:tier-vip",)
    )
    with pytest.raises(NotFoundError):
        identities.get_entity(access_for(world.tenant), scope, custom.id)
    # Admin alone does not grant tenant-defined custom labels; they must be carried.
    from iris_memory_core.domain.access import AccessContext

    vip_access = AccessContext(
        tenant_id=world.tenant,
        app_instance_id="app",
        granted_custom_labels=frozenset({"custom:tier-vip"}),
    )
    assert identities.get_entity(vip_access, scope, custom.id).id == custom.id


def test_write_directions_are_blocked_by_tenant_and_admin(
    identities: IdentityService, admin_access: AccessContext, world: World
) -> None:
    user_access = access_for(world.tenant)
    with pytest.raises(AccessDeniedError):
        identities.redirect_entity(
            user_access, world.entity_a.id, world.entity_b.id, expected_revision=1, reason="x"
        )
    with pytest.raises(AccessDeniedError):
        identities.tombstone_entity(user_access, world.entity_a.id, expected_revision=1, reason="x")
    # Attribute ingestion stays on the app path (no admin needed) but tenant-bound.
    write = identities.record_attribute(
        user_access, world.entity_a.id, "nickname", "n", FieldAuthority.CLAIM_SUPPORTED, "src"
    )
    assert write.changed is True
    other_tenant = access_for("tenant-b")
    with pytest.raises(AccessDeniedError):
        identities.record_attribute(
            other_tenant, world.entity_a.id, "nickname", "n", FieldAuthority.INFERRED, "src"
        )


def test_tombstoned_entity_does_not_resurrect_through_identity_views(
    store: Store, identities: IdentityService, admin_access: AccessContext, world: World
) -> None:
    binding = identities.propose_binding(
        admin_access, world.identity.id, world.entity_a.id, proof_digest="d", reason="evidence"
    )
    identities.confirm_binding(
        admin_access, binding.id, expected_revision=binding.revision, reason="confirmed"
    )
    verified_us = store.clock.now_us()
    before = identities.identity_view(admin_access, world.identity.id)
    assert before.entity_id_at_ingest == world.entity_a.id
    assert before.current_entity_id == world.entity_a.id

    identities.tombstone_entity(
        admin_access,
        world.entity_a.id,
        expected_revision=world.entity_a.revision + 1,  # canonical promotion bumped it
        reason="gdpr erasure",
    )
    current = identities.identity_view(admin_access, world.identity.id)
    assert current.entity_id_at_ingest is None
    assert current.current_entity_id is None
    historical = identities.identity_view(admin_access, world.identity.id, at_us=verified_us)
    assert historical.entity_id_at_ingest is None  # tombstone excludes history too

    # A redirect target being tombstoned also blocks current resolution.
    other = identities.create_entity(admin_access, EntityKind.PERSON, display_name="other")
    other_binding_identity = identities.register_external_identity(
        admin_access, "qq-onebot11", "bot-2", "20002", entity_id=other.id
    )
    other_binding = identities.propose_binding(
        admin_access, other_binding_identity.id, other.id, proof_digest="d", reason="evidence"
    )
    identities.confirm_binding(
        admin_access,
        other_binding.id,
        expected_revision=other_binding.revision,
        reason="confirmed",
    )
    terminal = identities.create_entity(admin_access, EntityKind.PERSON, display_name="terminal")
    identities.redirect_entity(
        admin_access,
        other.id,
        terminal.id,
        expected_revision=other.revision + 1,  # canonical promotion bumped it
        reason="merge",
    )
    identities.tombstone_entity(
        admin_access, terminal.id, expected_revision=terminal.revision, reason="erasure"
    )
    view = identities.identity_view(admin_access, other_binding_identity.id)
    assert view.entity_id_at_ingest == other.id  # source alive
    assert view.current_entity_id is None  # terminal tombstoned


def test_field_authority_cannot_be_spoofed_and_same_value_promotes(
    store: Store, identities: IdentityService, admin_access: AccessContext, world: World
) -> None:
    user = access_for(world.tenant)
    with pytest.raises(AccessDeniedError):
        identities.record_attribute(
            user, world.entity_a.id, "display_name", "x", FieldAuthority.ADMIN_CONFIRMED, "src"
        )
    with pytest.raises(AccessDeniedError):
        identities.record_attribute(
            user, world.entity_a.id, "display_name", "x", FieldAuthority.EXPLICIT_CORRECTION, "src"
        )
    platform_without_capability = access_for(world.tenant)
    with pytest.raises(AccessDeniedError):
        identities.record_attribute(
            platform_without_capability,
            world.entity_a.id,
            "level",
            "5",
            FieldAuthority.PLATFORM_VERIFIED,
            "src",
        )
    from iris_memory_core.domain.access import AccessContext

    platform = AccessContext(
        tenant_id=world.tenant,
        app_instance_id="app",
        capabilities=frozenset({"platform_ingest"}),
    )
    assert (
        identities.record_attribute(
            platform, world.entity_a.id, "level", "5", FieldAuthority.PLATFORM_VERIFIED, "src"
        ).outcome
        == "supersede"
    )

    # Same value at higher authority promotes instead of being ignored.
    first = identities.record_attribute(
        user, world.entity_a.id, "display_name", "Alice", FieldAuthority.INFERRED, "src:1"
    )
    assert first.outcome == "supersede"
    promoted = identities.record_attribute(
        admin_access,
        world.entity_a.id,
        "display_name",
        "Alice",
        FieldAuthority.ADMIN_CONFIRMED,
        "src:2",
    )
    assert promoted.outcome == "supersede"
    assert promoted.current.authority is FieldAuthority.ADMIN_CONFIRMED
    # After promotion, a platform value can no longer overwrite the name.
    attacker = identities.record_attribute(
        platform,
        world.entity_a.id,
        "display_name",
        "Mallory",
        FieldAuthority.PLATFORM_VERIFIED,
        "src:3",
    )
    assert attacker.outcome == "ignored"
    assert attacker.current.value == "Alice"


def test_concurrent_confirmation_parks_exactly_one_binding_in_conflicted(
    store: Store, identities: IdentityService, admin_access: AccessContext, world: World
) -> None:
    """Two concurrent confirms of rival bindings: one verifies, one parks."""
    import threading

    first = identities.propose_binding(
        admin_access, world.identity.id, world.entity_a.id, proof_digest="d1", reason="evidence"
    )
    second = identities.propose_binding(
        admin_access, world.identity.id, world.entity_b.id, proof_digest="d2", reason="evidence"
    )
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def confirm(binding_id: str, revision: int) -> None:
        barrier.wait()
        try:
            identities.confirm_binding(
                admin_access, binding_id, expected_revision=revision, reason="confirmed"
            )
        except BaseException as error:
            with lock:
                errors.append(error)

    threads = [
        threading.Thread(target=confirm, args=(first.id, first.revision)),
        threading.Thread(target=confirm, args=(second.id, second.revision)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    conflicts = [e for e in errors if isinstance(e, BindingConflictError)]
    assert len(conflicts) == 1
    with store.read() as tx:
        states = {tx.get_binding(first.id).state, tx.get_binding(second.id).state}
    assert states == {BindingState.VERIFIED, BindingState.CONFLICTED}


def test_identity_operations_are_idempotent_by_key(
    store: Store,
    identities: IdentityService,
    idempotency: IdempotencyManager,
    admin_access: AccessContext,
    world: World,
) -> None:
    """Identity admin writes replay their first outcome under the same key."""
    del identities
    wired = IdentityService(store, idempotency)
    binding = wired.propose_binding(
        admin_access, world.identity.id, world.entity_a.id, proof_digest="d", reason="evidence"
    )
    verified = wired.confirm_binding(
        admin_access,
        binding.id,
        expected_revision=binding.revision,
        reason="confirmed",
        idempotency_key="confirm-1",
    )
    # Replay with the same key: binding stays at the same revision.
    replayed = wired.confirm_binding(
        admin_access,
        binding.id,
        expected_revision=binding.revision,
        reason="confirmed",
        idempotency_key="confirm-1",
    )
    assert replayed.id == verified.id
    assert replayed.revision == verified.revision
    with store.read() as tx:
        revisions = (
            tx.raw()
            .execute("SELECT COUNT(*) FROM binding_revisions WHERE binding_id = ?", (binding.id,))
            .fetchone()[0]
        )
    assert int(revisions) == 2  # proposed + verified; the replay added nothing


def test_idempotent_replay_returns_the_first_outcome_not_current_state(
    store: Store,
    identities: IdentityService,
    idempotency: IdempotencyManager,
    admin_access: AccessContext,
    world: World,
) -> None:
    """Replay of a confirm key returns the verified snapshot even after a later
    revoke moved the aggregate on (the reviewer's revoked/revision=3 repro)."""
    del identities
    wired = IdentityService(store, idempotency)
    binding = wired.propose_binding(
        admin_access, world.identity.id, world.entity_a.id, proof_digest="d", reason="evidence"
    )
    verified = wired.confirm_binding(
        admin_access,
        binding.id,
        expected_revision=binding.revision,
        reason="confirmed",
        idempotency_key="confirm-first",
    )
    assert verified.state is BindingState.VERIFIED and verified.revision == 2
    wired.revoke_binding(
        admin_access, binding.id, expected_revision=verified.revision, reason="gdpr request"
    )
    replay = wired.confirm_binding(
        admin_access,
        binding.id,
        expected_revision=binding.revision,
        reason="confirmed",
        idempotency_key="confirm-first",
    )
    assert replay.state is BindingState.VERIFIED  # FIRST outcome, not revoked
    assert replay.revision == 2
    with store.read() as tx:
        current = tx.get_binding(binding.id)
    assert current.state is BindingState.REVOKED and current.revision == 3  # replay added nothing


def test_idempotency_fingerprint_covers_expected_revision(
    store: Store,
    idempotency: IdempotencyManager,
    admin_access: AccessContext,
    world: World,
) -> None:
    """Same key + different expected_revision is a reused key, not a replay."""
    wired = IdentityService(store, idempotency)
    binding = wired.propose_binding(
        admin_access, world.identity.id, world.entity_a.id, proof_digest="d", reason="evidence"
    )
    wired.confirm_binding(
        admin_access,
        binding.id,
        expected_revision=binding.revision,
        reason="confirmed",
        idempotency_key="confirm-rev",
    )
    with pytest.raises(IdempotencyKeyReusedError):
        wired.confirm_binding(
            admin_access,
            binding.id,
            expected_revision=999,
            reason="confirmed",
            idempotency_key="confirm-rev",
        )


def test_idempotency_key_without_runner_fails_loudly(
    store: Store, admin_access: AccessContext, world: World
) -> None:
    unwired = IdentityService(store)
    with pytest.raises(IdempotencyUnavailableError):
        unwired.create_entity(admin_access, EntityKind.PERSON, idempotency_key="k")
    with pytest.raises(IdempotencyUnavailableError):
        unwired.confirm_binding(
            admin_access, world.identity.id, expected_revision=1, reason="r", idempotency_key="k"
        )


def test_tombstoned_binding_is_excluded_from_collections_and_views(
    store: Store,
    identities: IdentityService,
    admin_access: AccessContext,
    world: World,
) -> None:
    """The reviewer's resurrection repro: a tombstoned verified binding must
    vanish from verified_binding_for/bindings_for AND from identity_view."""
    binding = identities.propose_binding(
        admin_access, world.identity.id, world.entity_a.id, proof_digest="d", reason="evidence"
    )
    identities.confirm_binding(
        admin_access, binding.id, expected_revision=binding.revision, reason="confirmed"
    )
    assert identities.identity_view(admin_access, world.identity.id).current_entity_id is not None

    with store.write() as tx:
        tx.record_tombstone(
            tenant_id=world.tenant,
            resource_type="binding",
            resource_id=binding.id,
            reason_code="gdpr_erasure",
            deleted_by="admin",
        )
    with pytest.raises(NotFoundError), store.read() as tx:
        tx.get_binding(binding.id)
    with store.read() as tx:
        assert tx.verified_binding_for(world.identity.id) is None
        assert tx.bindings_for(world.identity.id) == ()
    view = identities.identity_view(admin_access, world.identity.id)
    assert view.entity_id_at_ingest is None
    assert view.current_entity_id is None


def test_redirect_edges_from_tombstoned_sources_drop_out_of_resolution(
    store: Store,
    identities: IdentityService,
    admin_access: AccessContext,
    world: World,
) -> None:
    """A→C→D with the middle entity C tombstoned: C's outgoing edge drops, so
    resolution stops at the deleted C and the view nulls it (a tombstoned
    terminal resolves to nothing — never back to the pre-merge entity)."""
    entity_c = identities.create_entity(admin_access, EntityKind.PERSON, display_name="C")
    entity_d = identities.create_entity(admin_access, EntityKind.PERSON, display_name="D")
    identities.redirect_entity(
        admin_access,
        world.entity_a.id,
        entity_c.id,
        expected_revision=world.entity_a.revision,
        reason="merge",
    )
    identities.redirect_entity(
        admin_access, entity_c.id, entity_d.id, expected_revision=1, reason="merge"
    )
    binding = identities.propose_binding(
        admin_access, world.identity.id, world.entity_a.id, proof_digest="d", reason="evidence"
    )
    identities.confirm_binding(
        admin_access, binding.id, expected_revision=binding.revision, reason="confirmed"
    )
    view = identities.identity_view(admin_access, world.identity.id)
    assert view.current_entity_id == entity_d.id

    identities.tombstone_entity(
        admin_access, entity_c.id, expected_revision=2, reason="gdpr erasure"
    )
    with store.read() as tx:
        assert entity_d.id not in tx.redirect_map(world.tenant).values()
        assert tx.get_entity_redirect(entity_c.id) is None
    view = identities.identity_view(admin_access, world.identity.id)
    assert view.entity_id_at_ingest == world.entity_a.id
    assert view.current_entity_id is None  # chain ends at the deleted entity


def test_field_authority_requires_capability_or_management_plane(
    identities: IdentityService, admin_access: AccessContext, world: World
) -> None:
    """explicit_correction needs the identity.correct capability; the admin
    management plane is the documented alternative grant (capability OR admin)."""
    plain = access_for(world.tenant)
    with pytest.raises(AccessDeniedError):
        identities.record_attribute(
            plain, world.entity_a.id, "display_name", "v", FieldAuthority.EXPLICIT_CORRECTION, "s"
        )
    corrector = AccessContext(
        tenant_id=world.tenant,
        app_instance_id="app-1",
        capabilities=frozenset({"identity.correct"}),
    )
    corrected = identities.record_attribute(
        corrector, world.entity_a.id, "display_name", "v", FieldAuthority.EXPLICIT_CORRECTION, "s"
    )
    assert corrected.changed is True
    # The management plane (admin) may assert high authorities without each
    # individual capability — an explicit design decision, documented here.
    admin_write = identities.record_attribute(
        admin_access, world.entity_a.id, "nickname", "n", FieldAuthority.ADMIN_CONFIRMED, "s"
    )
    assert admin_write.changed is True
