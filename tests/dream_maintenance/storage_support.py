"""Actual local storage fixture for dream-owner tests, without a model adapter."""
from pathlib import Path
import sqlite3
import resource

from companion_memory.configuration.dream_resolution import DreamConfigurationOk, resolve_dream_configuration
from companion_memory.configuration.dream_persistence import DreamConfigurationAssembly
from companion_memory.configuration.dream_persistent_results import ConfigurationCommitted
from companion_memory.dream.control import DreamControl
from companion_memory.persistence import DatabaseResources, PersistenceService, Ready, Committed
from companion_memory.persistence.schema import InvalidValue
from .configuration_support import inputs


class FaultConnections:
    def __init__(self):
        self.fault: str | None = None
        self.errors: list[int] = []

    def connect(self, database: str, *, uri: bool, timeout: float,
                isolation_level: None, check_same_thread: bool) -> sqlite3.Connection:
        owner = self

        class Connection(sqlite3.Connection):
            changed_memory=False
            def execute(self, sql, parameters=(), /):
                if sql == 'BEGIN IMMEDIATE':
                    self.changed_memory=False
                    if owner.fault == 'READONLY':
                        super().execute('PRAGMA query_only=ON')
                    elif owner.fault == 'FULL':
                        count = super().execute('PRAGMA page_count').fetchone()[0]
                        super().execute('PRAGMA max_page_count=' + str(count))
                if sql.startswith('UPDATE ') and any(name in sql for name in ('memory_objects','self_model_current_persona')):self.changed_memory=True
                limited=sql=='COMMIT' and self.changed_memory and owner.fault=='KERNEL_WRITE_LIMIT'
                limits=resource.getrlimit(resource.RLIMIT_FSIZE)
                if limited:
                    # Target the actual SQLite commit write, after log admission.
                    resource.setrlimit(resource.RLIMIT_FSIZE,(0,resource.getrlimit(resource.RLIMIT_FSIZE)[1]))
                try:
                    return super().execute(sql, parameters)
                except sqlite3.Error as error:
                    owner.errors.append(error.sqlite_errorcode)
                    raise
                finally:
                    if limited:resource.setrlimit(resource.RLIMIT_FSIZE,limits)

        return sqlite3.connect(database, uri=uri, timeout=timeout, isolation_level=isolation_level,
            check_same_thread=check_same_thread, factory=Connection)


class ControlStorage(FaultConnections):
    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.now = 1700000000000000
        self.configuration = DreamConfigurationAssembly()
        self.control = DreamControl()
        self.storage = PersistenceService(self.configuration.repositories + (self.control.catalog.definition,),
            self.configuration.commands + self.control.commands, assembly_format='DREAM_MAINTENANCE_V1')
        self.publisher = None

    async def open(self, mode: str = 'CREATE_NEW'):
        supplied = inputs(self.root)
        resolved = resolve_dream_configuration(*supplied)
        if type(resolved) is not DreamConfigurationOk:
            raise AssertionError(resolved)
        path = str(self.root / 'database' / 'runtime.sqlite3')
        resources = DatabaseResources('dream-control-tests', lambda identity, requested:
            (identity, requested) == ('dream-control-tests', path), connect=self.connect)
        if mode not in ('CREATE_NEW', 'OPEN_EXISTING'):
            raise InvalidValue()
        initialized = await self.storage.initialize(resolved.value.foundation, resources, mode)
        if type(initialized) is not Ready:
            raise AssertionError(initialized)
        self.publisher = self.configuration.bind(self.storage, 'instance', resolved.value)
        stored = await self.publisher.persist_dream_configuration('configuration', resolved.value,
            actor='bootstrap', protected_directories=supplied[6])
        if type(stored) is not ConfigurationCommitted or stored.configuration is None:
            raise AssertionError(stored)
        self.control.bind(self.storage, stored.configuration, checkpoint=lambda: None, now=lambda: self.now)
        if mode == 'CREATE_NEW':
            initialized = await self.control.execute('initialize_dream_control', 'initialize', {}, actor='admin')
            if type(initialized) is not Committed:
                raise AssertionError(initialized)
        return self

    async def close(self):
        if not self.control.close() or self.publisher is None or not self.publisher.close():
            raise AssertionError('Actual owner work remains')
        await self.storage.close()
        if self.storage.get_health().lifecycle != 'CLOSED':
            raise AssertionError(self.storage.get_health())
