"""Real disposable SQLite resources, retained identity and explicit gate authority.

The gate belongs solely to this test fixture. Its lock coordinates epoch changes
with dispatch; it is not a production mode service or role-string authorizer.
"""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import threading
import time
from typing import cast
from companion_memory.configuration import MetadataValue
from companion_memory.persistence import DatabaseResources, PersistenceService, Ready as StorageReady
from companion_memory.provider import (CancellationSource, LedgerAssembly, ObserverGrant, ProviderResources, ProviderService, Ready,
                                      Scenario, SimulationAdapter, WorkGrant, bind_gate)
from tests.configuration.provider_support import provider_snapshot
from tests.persistence.support import Hooks


class Gate:
    def __init__(self):
        self.lock = threading.RLock()
        self.mode = "NORMAL"
        self.revoked = False
        self.before_dispatch = lambda: None
        self.binding = bind_gate(self.check, self.dispatch, self.authorized)
    def authorized(self, grant: WorkGrant) -> bool:
        with self.lock:
            return not self.revoked
    def check(self, grant: WorkGrant) -> bool:
        with self.lock:
            return not self.revoked and (self.mode == "NORMAL" or self.mode == "FOCUSED" and grant.internal_dream and grant.task_role == "DREAM")
    def dispatch(self, grant, start):
        self.before_dispatch()
        with self.lock:
            if not self.check(grant):
                return False
            start()
            return True


class Fixture:
    def __init__(self, directory: Path, scenarios: tuple[Scenario, ...], changes: dict[str, MetadataValue] | None = None):
        self.directory = directory.resolve()
        self.path = self.directory / "provider.sqlite3"
        self.retained = self.directory / "provider-identity.json"
        if not self.retained.exists():
            self.retained.write_text(json.dumps({"identity": "synthetic-provider-database", "path": str(self.path)}))
        self.snapshot = provider_snapshot(str(self.path), changes)
        self.assembly = LedgerAssembly()
        self.storage = PersistenceService(self.assembly.repositories, self.assembly.commands)
        self.hooks = Hooks()
        self.storage_resources = DatabaseResources("synthetic-provider-database", self.retention_check, connect=self.hooks.connect)
        self.gate = Gate()
        self.adapter = SimulationAdapter(scenarios)
        self.resources = ProviderResources(self.gate.binding, self.adapter)
        self.service = ProviderService(self.assembly)
        self.grant = WorkGrant("cognition", "sample_scope", None, "LEARNING", ("generation", "embedding", "rerank", "media"),
                               ("GENERATION", "EMBEDDING", "RERANK", "MEDIA_UNDERSTANDING"), "sample_owner", "sample_scheduler", ("sample_run",))
        self.work = self.service.bind_work(self.grant)
        self.observer = self.service.bind_observer(ObserverGrant(("sample_scope",), ("sample_account",)))
        self.binding = None
        self.cancellation = CancellationSource()
    def retention_check(self, identity: str, path: str) -> bool:
        return json.loads(self.retained.read_text()) == {"identity": identity, "path": path}
    async def initialize(self, mode: str = "CREATE_NEW"):
        result = await self.storage.initialize(self.snapshot, self.storage_resources, mode)
        assert type(result) is StorageReady, result
        self.binding = self.assembly.bind(self.storage, self.snapshot)
        return await self.service.initialize(self.snapshot, self.binding, self.resources)
    async def close(self):
        await self.service.close()
        await self.storage.close()
    def request(self, key: str = "sample-operation", profile: str = "generation", payload: object = None):
        return {"operation_key": key, "run_id": "sample_run", "profile_id": profile, "deadline": time.monotonic()+2,
                "cancellation": self.cancellation.token, "payload": payload if payload is not None else {"messages": [{"role": "USER", "text": "0123456789"}], "input_units_limit": 10, "output_units_limit": 20}}
    @staticmethod
    def query(**values):
        return {"start": "2000-01-01T00:00:00+00:00", "end": "2100-01-01T00:00:00+00:00", "caller_scope": None,
                "capability": None, "task_role": None, "profile_id": None, "account_id": None, "group_by": "NONE", **values}


def success(text: str = "hello") -> Scenario:
    return Scenario("SUCCEEDED", {"text": text, "stop_reason": "STOP"}, {"coverage": "COMPLETE", "billing_input_units": 10, "billing_output_units": 5})


async def until(predicate, timeout: float = 2):
    """Yield to controlled event completion with a finite failure deadline."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


def completed(value: object):
    """Assert and narrow a committed work branch, retaining useful failure output."""
    from companion_memory.provider import Completed
    assert type(value) is Completed, value
    return value


def record(value: object):
    """Assert a native immutable record before inspecting a result's fields."""
    from types import MappingProxyType
    from companion_memory.provider.values import Record
    assert type(value) is MappingProxyType, value
    return cast(Record, value)


def records(value: object):
    """Assert a complete immutable row sequence without a lossy coercion."""
    from companion_memory.provider.values import Record
    assert type(value) is tuple, value
    return tuple(record(item) for item in value)


def found(value: object):
    """Assert and narrow a successful read branch, distinct from an empty miss."""
    from companion_memory.provider import Found
    assert type(value) is Found, value
    return value.value
