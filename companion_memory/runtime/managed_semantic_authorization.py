"""Managed semantic authority tied to explicit current product model consent.

This is a new production binding, separate from frozen experimental packages.
Retrieval persists its original intent before Provider registration; the Provider
ledger owns finite attempt budgets and UNKNOWN isolation. No trial slot journal
is created, reopened or replenished by this capability.
"""
from __future__ import annotations
from dataclasses import dataclass
from hashlib import sha256
import time
from types import MappingProxyType
from companion_memory.configuration.managed_persistence import StoredManagedConfiguration
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.semantic_records import Record, INTENT, identity, isolate, string, number
from companion_memory.persistence.owned_statements import OwnerFailure


@dataclass(frozen=True, slots=True)
class ManagedSemanticGrant:
    binding: Record
    digest: str


class ManagedSemanticAuthorization:
    """One native host binding; activation is volatile and reopening never sends."""
    def __init__(self, host):
        stored = host.stored
        if type(stored) is not StoredManagedConfiguration or not host.combination.managed_format:
            raise ValueError('A native managed host is required.')
        self.host = host
        package = identity('managed-semantic-consent', stored.database_id, host.resources.instance_id, stored.snapshot_id)
        binding = MappingProxyType({'format': 'MANAGED_SEMANTIC_CONSENT_V1', 'package_id': package,
            'database_id': stored.database_id, 'instance_id': host.resources.instance_id,
            'config_snapshot_id': stored.snapshot_id, 'expires_at': 2**63 - 1})
        self.grant = ManagedSemanticGrant(binding, sha256(encode_content(binding, 4096)).hexdigest())
        self._activated = False

    @property
    def activated(self) -> bool:
        return self._activated and self.host.resources.send_authorized('managed-semantic')

    def activate(self) -> None:
        if not self.host.resources.send_authorized('managed-semantic'):
            raise OwnerFailure('ACCESS_DENIED', 'provider', 'MODEL_DISPATCH_PAUSED')
        self._activated = True

    def original(self, work_id: str) -> None:
        """The native retrieval work row is the sole durable intent authority."""
        return None

    def slot(self, work_id: str) -> str:
        return identity('managed-semantic-slot', self.grant.binding['package_id'], work_id)

    def reserve(self, work: Record, request_digest: str, deadline_at: int, slot_id: str) -> Record:
        config = work['config']
        if (not self.activated or work['kind'] != 'EMBED' or type(config) is not MappingProxyType
                or config != {'database_id': self.grant.binding['database_id'], 'instance_id': self.grant.binding['instance_id'],
                    'snapshot_id': self.grant.binding['config_snapshot_id']}
                or slot_id != self.slot(string(work['work_id'])) or deadline_at <= time.time_ns() // 1000):
            raise OwnerFailure('ACCESS_DENIED', 'semantic', 'OPERATION_NOT_GRANTED')
        return isolate(INTENT, {'package_id': self.grant.binding['package_id'], 'slot_id': slot_id,
            'authorization_digest': self.grant.digest, 'request_digest': request_digest, 'expires_at': deadline_at})

    def permits(self, description: Record, intent: Record) -> bool:
        return (self.activated and intent['package_id'] == self.grant.binding['package_id']
            and intent['authorization_digest'] == self.grant.digest
            and intent['slot_id'] == self.slot(string(description['work_id']))
            and intent['request_digest'] == sha256(encode_content(description, 65536)).hexdigest()
            and intent['expires_at'] == description['deadline_at'] and number(intent['expires_at']) > time.time_ns() // 1000)

    def complete(self, work_id: str) -> None:
        """Retrieval's audited terminal/cleanup rows already own this completion."""

    def close(self) -> None:
        self._activated = False
