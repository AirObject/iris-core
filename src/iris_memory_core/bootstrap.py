"""Trusted offline initialization; callers receive only scoped credentials and IDs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
from pathlib import Path

from iris_memory_core.application.security import CredentialService
from iris_memory_core.domain.identity import (
    BindingMethod,
    BindingState,
    EntityKind,
    ExternalIdentityKey,
)
from iris_memory_core.domain.surface import SurfaceMode
from iris_memory_core.indexing.fts import FtsDegradedError, FtsProjectionService
from iris_memory_core.runtime import ServiceConfig, open_store

APPLICATION_CAPABILITIES = (
    "active-surface.v1",
    "contract.negotiation",
    "error-envelope.v1",
    "events.sse.v1",
    "events.checkpoint.v1",
    "focus-items.v1",
    "health.readiness.v2",
    "health.v1",
    "notes.v1",
    "observe.batch.v1",
    "persona.read.v1",
    "recall.v1",
    "recall.revalidate.v1",
    "recall.usage.v1",
    "source-cursor.v1",
)


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--agent-name", required=True)
    parser.add_argument("--app-instance", required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--expires-days", type=int, default=30)
    parser.add_argument("--actor-provider")
    parser.add_argument("--actor-subject")
    parser.add_argument("--surface-mode", choices=("off", "advisory", "required"), default="off")
    parser.add_argument("--allow-local-sqlite", action="store_true")
    parser.add_argument(
        "--initialize-search",
        action="store_true",
        help="Build and verify the new tenant's FTS generation inside the bootstrap transaction",
    )


def run(args: argparse.Namespace) -> int:
    if bool(args.actor_provider) != bool(args.actor_subject):
        raise ValueError("actor-provider and actor-subject must be supplied together")
    if not 1 <= args.expires_days <= 365:
        raise ValueError("expires-days must be between 1 and 365")
    if any(
        not value.strip() or len(value) > 128
        for value in (
            args.tenant,
            args.agent_name,
            args.app_instance,
        )
    ):
        raise ValueError("tenant, agent-name and app-instance must contain 1-128 characters")
    store = open_store(
        ServiceConfig(
            database=args.database,
            allow_local_sqlite=args.allow_local_sqlite,
        )
    )
    # O_EXCL refuses existing files and symlinks. Never overwrite a credential.
    descriptor = os.open(args.credential_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output, store.write() as tx:
            tx.insert_tenant(args.tenant, status="active")
            agent = tx.insert_agent(args.tenant, args.agent_name, actor="offline-bootstrap")
            if args.surface_mode != "off":
                _, _, revision = tx.surfaces.state(args.tenant, agent.id)
                tx.surfaces.set_mode(
                    args.tenant,
                    agent.id,
                    SurfaceMode(args.surface_mode),
                    expected_revision=revision,
                )
            space = tx.insert_space(
                args.tenant, "local", agent_id=agent.id, actor="offline-bootstrap"
            )
            actor_identity_id: str | None = None
            if args.actor_provider and args.actor_subject:
                # The trusted operator explicitly establishes this new identity.
                # Existing identity keys cannot be rebound through bootstrap.
                identity_key = ExternalIdentityKey(
                    args.tenant,
                    args.actor_provider,
                    "default",
                    args.actor_subject,
                )
                if tx.find_external_identity(identity_key) is not None:
                    raise ValueError("bootstrap actor identity already exists")
                entity = tx.insert_entity(args.tenant, EntityKind.PERSON, actor="offline-bootstrap")
                identity = tx.insert_external_identity(identity_key, entity_id=entity.id)
                actor_identity_id = identity.id
                binding = tx.insert_binding(
                    args.tenant,
                    identity.id,
                    entity.id,
                    method=BindingMethod.ADMIN_CONFIRMATION,
                    confidence=1.0,
                    proof_digest=hashlib.sha256(
                        f"offline-bootstrap:{identity.id}:{entity.id}".encode()
                    ).hexdigest(),
                    actor="offline-bootstrap",
                    reason_code="bootstrap",
                )
                tx.transition_binding(
                    binding.id,
                    BindingState.VERIFIED,
                    expected_revision=binding.revision,
                    actor="offline-bootstrap",
                    reason_code="bootstrap",
                )
            tx.advance_watermark(
                args.tenant,
                agent.id,
                (
                    ("agent", agent.id, 1),
                    ("space", space.id, 1),
                    ("persona_revision", agent.persona_current_revision_id or "", 1),
                ),
            )
            search_generation_id: str | None = None
            if args.initialize_search:
                try:
                    report = FtsProjectionService(store, store.clock).rebuild_in_tx(tx, args.tenant)
                except FtsDegradedError as error:
                    raise ValueError(
                        f"search initialization failed: {error.reason_code}"
                    ) from error
                search_generation_id = report.generation_id
                tx.audit(
                    tenant_id=args.tenant,
                    actor="offline-bootstrap",
                    action="fts.initialized",
                    resource_type="fts_generation",
                    resource_id=report.generation_id,
                    reason_code="bootstrap",
                )
            secret = secrets.token_urlsafe(48)
            credential = CredentialService(store, store.clock).issue_in_transaction(
                tx,
                secret,
                tenant_id=args.tenant,
                app_instance_id=args.app_instance,
                plane="application",
                agent_ids=[agent.id],
                space_ids=[space.id],
                capabilities=APPLICATION_CAPABILITIES,
                data_purposes=["reply"],
                expires_us=store.clock.now_us() + args.expires_days * 86_400_000_000,
                actor="offline-bootstrap",
            )
            tx.audit(
                tenant_id=args.tenant,
                actor="offline-bootstrap",
                action="tenant.bootstrapped",
                resource_type="tenant",
                resource_id=args.tenant,
                reason_code="bootstrap",
            )
            identifiers = {
                "tenant_id": args.tenant,
                "agent_id": agent.id,
                "space_id": space.id,
                "credential_id": credential.id,
            }
            if actor_identity_id is not None:
                identifiers["actor_external_identity_id"] = actor_identity_id
            if search_generation_id is not None:
                identifiers["search_generation_id"] = search_generation_id
            output.write(json.dumps({**identifiers, "token": secret}) + "\n")
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        args.credential_file.unlink(missing_ok=True)
        raise
    print(json.dumps(identifiers))
    return 0
