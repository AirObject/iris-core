"""Open the complete managed storage graph with only protected bootstrap enabled.

The authority file precedes SQLite creation. An initialized management database
does not imply a business-ready instance or permission to dispatch any model.
"""
from __future__ import annotations
from hashlib import sha256
from pathlib import Path

from companion_memory.configuration import EffectiveSnapshot
from companion_memory.configuration.deployment import DeploymentSettings
from companion_memory.configuration.managed_bootstrap import bootstrap_snapshot
from companion_memory.management.identity import digest
from companion_memory.persistence import DatabaseResources, Ready, Committed
from companion_memory.persistence._codec import assembly_value
from .daily_assembly import DailyAssembly
from .managed_resources import ManagedResources


class ManagedBootstrap:
    """One owner of bootstrap storage; business assembly is attached exactly once."""
    def __init__(self, settings: DeploymentSettings):
        self.settings = settings
        self.assembly = DailyAssembly(dream_format=True, managed_format=True)
        encoded = assembly_value(self.assembly.repositories, self.assembly.commands, assembly_format='MANAGED_RUNTIME_V1')
        self.assembly_digest = sha256(encoded).hexdigest()
        self.resources: ManagedResources | None = None
        self.state = 'NEW'
        self.snapshot: EffectiveSnapshot | None = None

    async def open(self, *, retained_resources: ManagedResources | None = None):
        """Restore local authority and expose only authenticated bootstrap controls."""
        if self.state != 'NEW':
            raise ValueError('Bootstrap already opened.')
        self.state = 'RECOVERING'
        if retained_resources is not None and (retained_resources.fd < 0 or retained_resources.settings != self.settings
                or retained_resources.identity['assembly_digest'] != self.assembly_digest):
            raise ValueError('Retained resource ownership differs.')
        resources = self.resources = retained_resources or ManagedResources(self.settings, self.assembly_digest)
        snapshot = bootstrap_snapshot(self.settings, resources.protected_directories())
        self.snapshot = snapshot
        database = resources.root / 'db/memory.sqlite3'
        mode = 'OPEN_EXISTING' if database.exists() else 'CREATE_NEW'
        result = await self.assembly.storage.initialize(snapshot,
            DatabaseResources(resources.database_id, resources.retained_identity_check), mode)
        if type(result) is not Ready:
            self.state = 'FAULTED'
            return result
        if self.assembly.identity is None:
            raise ValueError('Missing management authority.')
        secret = resources.read_secret('bootstrap')
        self.assembly.identity.bind(self.assembly.storage, resources.database_id, resources.instance_id,
                                   self.settings, digest(secret))
        if self.assembly.backup is None:
            raise ValueError('Missing backup owner.')
        self.assembly.backup.bind(self.assembly.storage, resources.database_id, resources.instance_id, str(resources.identity['authority_id']))
        restore_path = resources.root / 'bootstrap/restore-switch.json'
        if restore_path.exists():
            from .managed_resources import read_record
            from .managed_restore import restore_manifest
            import json
            intent = read_record(restore_path)
            restore_manifest(resources.root, intent)
            binding = {key: intent[key] for key in ('switch_id', 'runtime_root', 'target_device', 'target_inode', 'database_id', 'instance_id')}
            evidence = await self.assembly.backup.execute('record_restore_activation', str(intent['switch_id']),
                {'restore_id': intent['switch_id'], 'target_binding': digest(json.dumps(binding, sort_keys=True, separators=(',', ':'))),
                    **{key: intent[key] for key in ('backup_id', 'manifest_digest', 'source_authority', 'target_authority')}}, 'deployment_restore')
            if type(evidence) is not Committed:
                self.state = 'FAULTED'
                return evidence
        self.state = 'BOOTSTRAP'
        return result

    async def close(self) -> bool:
        """Retain the volume lease while any underlying owner still holds resources."""
        self.state = 'CLOSING'
        if self.assembly.identity is not None and not self.assembly.identity.close():
            return False
        if self.assembly.backup is not None and not self.assembly.backup.close():
            return False
        await self.assembly.storage.close()
        if self.assembly.storage.get_health().lifecycle != 'CLOSED':
            return False
        if self.resources is not None:
            self.resources.close()
        self.state = 'CLOSED'
        return True
