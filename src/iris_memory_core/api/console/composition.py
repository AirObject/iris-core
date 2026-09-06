"""Console-only composition, including lazy crypto and the shared storage UoW."""

from __future__ import annotations

from pathlib import Path

from iris_memory_core.api.console.contracts import load_contract
from iris_memory_core.api.console.crypto import ConsoleCrypto, load_authentication_key
from iris_memory_core.application.console.security import OperatorSecurity
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.uow import Store


def assemble(
    store: Store, *, key_path: Path | None = None
) -> tuple[OperatorSecurity, ConsoleCrypto]:
    path = key_path or store.runtime.database.parent / "console-auth.key"
    with store.read() as tx:
        # Missing schema 12 fails startup before any listener opens.
        initialized = tx.console.authentication_initialized()
    if initialized and not path.exists():
        raise RuntimeError("Console authentication key missing; offline recovery is required")
    material = ConsoleCrypto(load_authentication_key(path))
    permissions = load_contract()["components"]["schemas"]["Grant"]["properties"]["permissions"][
        "items"
    ]["enum"]
    return OperatorSecurity(
        store,
        store.clock,
        store.ids,
        permissions=frozenset(permissions),
        fingerprint=material.fingerprint,
        seal=material.seal,
        open_sealed=material.open_sealed,
        idempotency=IdempotencyManager(store),
    ), material
