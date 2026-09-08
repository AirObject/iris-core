"""Startup validation and atomic offline rotation of every sealed revision."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from iris_memory_core.application.ports.transaction import UnitOfWork
from iris_memory_core.providers.secrets import ProviderSecrets, read_private_file, unavailable


def deployment_secrets(
    database: Path,
    master_key_file: Path | None,
    *,
    allowed_references: Mapping[str, frozenset[str]] | None = None,
    reserved_files: frozenset[Path] = frozenset(),
) -> ProviderSecrets:
    console_key = database.absolute().parent / "console-auth.key"
    if master_key_file is not None and master_key_file.absolute() == console_key:
        raise unavailable()
    # Different filenames must not permit the same Console master material.
    if master_key_file is not None and console_key.exists():
        provider_key = read_private_file(master_key_file, maximum=32)
        console_material = read_private_file(console_key, maximum=4096)
        if provider_key == console_material:
            raise unavailable()
    return ProviderSecrets(
        master_key_file=master_key_file,
        allowed_references=allowed_references,
        reserved_files=reserved_files | {console_key},
    )


def validate_sealed_startup(uow: UnitOfWork, secrets: ProviderSecrets) -> int:
    """Validate retained history too; an inactive row can be selected by rollback."""
    count = 0
    with uow.read() as tx:
        for revision in tx.providers.sealed_revisions():
            if revision.secret is None:
                raise unavailable()
            secrets.resolve(
                revision.tenant_id, revision.config_id, revision.content_revision, revision.secret
            )
            count += 1
    return count


def rotate_sealed(uow: UnitOfWork, source: ProviderSecrets, target: ProviderSecrets) -> int:
    """One transaction across all tenants/history; any failure restores every old envelope.

    The offline entry point supplies downtime acknowledgement and a verified
    backup before calling this adapter. Master files are never overwritten.
    """
    count = 0
    with uow.write() as tx:
        for revision in tx.providers.sealed_revisions():
            if revision.secret is None:
                raise unavailable()
            replacement = source.reseal(
                target,
                revision.tenant_id,
                revision.config_id,
                revision.content_revision,
                revision.secret,
            )
            # Independently authenticate the new envelope before persisting it.
            if target.fingerprint(
                revision.tenant_id, revision.config_id, revision.content_revision, replacement
            ) != source.fingerprint(
                revision.tenant_id,
                revision.config_id,
                revision.content_revision,
                revision.secret,
            ):
                raise unavailable()
            tx.providers.replace_ciphertext(revision, replacement)
            count += 1
            tx.audit(
                tenant_id=revision.tenant_id,
                actor="offline:provider-master-rotation",
                action="provider.secrets.rotated",
                resource_type="provider_config",
                resource_id=revision.config_id,
                reason_code="operator_request",
                details={"content_revision": revision.content_revision},
            )
    return count
