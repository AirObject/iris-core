"""Provider execution versions retain original profiles and credential references.

Only configuration-owner-issued snapshots enter the bounded verification cache.
Accounts, embedding space and compiled protocol resources keep their original
identities; changing them requires a separately supported migration. Transport
construction does not resolve secrets or perform network requests.
"""
from __future__ import annotations
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast
from companion_memory.configuration import PresentValue
from companion_memory.configuration.execution_versions import ExecutionVersions, ExecutionVersion
from companion_memory.configuration.managed_resolution import ManagedConfigurationCandidate
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.semantic_records import identity
from .chat_transport import ChatTransport
from .values import Record, as_record, InvalidData


def foundation(candidate, key: str):
    return next(e.state.value for e in candidate.foundation.list_entries()
        if e.definition.key == key and type(e.state) is PresentValue)


def profiles(candidate) -> tuple[Record, ...]:
    return tuple(as_record(value) for value in cast(tuple, foundation(candidate, 'provider.profiles')))


@dataclass(frozen=True, slots=True)
class PreparedTransports:
    owner: ManagedProviderVersions
    version: ExecutionVersion
    transports: dict[str, ChatTransport]


class ManagedProviderVersions:
    def __init__(self, versions: ExecutionVersions, factory: Callable[[ManagedConfigurationCandidate], dict[str, ChatTransport]]):
        self.versions, self.factory = versions, factory
        self.cache: OrderedDict[str, ExecutionVersion] = OrderedDict()
        self.active: PreparedTransports | None = None

    def compatible(self, candidate: ManagedConfigurationCandidate) -> None:
        birth = self.versions.birth.candidate
        if foundation(birth, 'provider.accounts') != foundation(candidate, 'provider.accounts'):
            raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'configuration', 'ACCOUNT_MIGRATION_UNAVAILABLE')
        old = {p['material_role']: p for p in profiles(birth)}
        for profile in profiles(candidate):
            previous = old[profile['material_role']]
            editable = {'max_input_units'} if profile['capability'] == 'GENERATION' else set()
            if any(profile[key] != previous[key] for key in previous if key not in editable):
                raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'configuration', 'PROFILE_MIGRATION_UNAVAILABLE')
        old_wire = birth.text.record('provider.transport')
        new_wire = candidate.text.record('provider.transport')
        old_roles = {as_record(role)['role']: as_record(role) for role in cast(tuple, old_wire['roles'])}
        for role in cast(tuple, new_wire['roles']):
            role = as_record(role)
            previous = old_roles[role['role']]
            editable = ('secret_ref', 'secret_revision') if role['role'] != 'MEDIA' else ()
            if any(role[key] != previous[key] for key in previous if key not in editable):
                raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'configuration', 'PROTOCOL_RESOURCE_MIGRATION_UNAVAILABLE')

    def remember(self, version: ExecutionVersion) -> None:
        if type(version) is not ExecutionVersion or version.issuer is not self.versions:
            raise InvalidData()
        self.compatible(version.candidate)
        self.cache[version.version_id] = version
        self.cache.move_to_end(version.version_id)
        # There are at most 32 registered attempts under the complete account
        # policy. Extra entries cover bounded failed preparations; point reads
        # reload an evicted version from its immutable owner before decoding.
        while len(self.cache) > 64:
            self.cache.popitem(last=False)

    def birth_revision(self, row: Record) -> str:
        return identity('daily-profile', self.versions.birth.version_id, cast(str, row['profile_id']))

    async def preload(self, row: Record) -> None:
        if row.get('capability') == 'EMBEDDING' or row.get('profile_revision') == self.birth_revision(row):
            return
        version = await self.versions.load(cast(str, row['profile_revision']))
        self.remember(version)

    def candidate(self, row: Record):
        if row['capability'] == 'EMBEDDING' or row['profile_revision'] == self.birth_revision(row):
            return self.versions.birth.candidate
        version = self.cache.get(cast(str, row['profile_revision']))
        if version is None:
            raise InvalidData()
        return version.candidate

    def transports(self, version: ExecutionVersion) -> dict[str, ChatTransport]:
        self.remember(version)
        if self.active is not None and self.active.version.version_id == version.version_id:
            return self.active.transports
        values = self.factory(version.candidate)
        wire = cast(tuple[Record, ...], version.candidate.text.record('provider.transport')['roles'])
        if set(values) != {cast(str, setting['role']) for setting in wire}:
            raise InvalidData()
        for setting in wire:
            role = cast(str, setting['role'])
            transport = values[role]
            if type(transport) is not ChatTransport or not (transport.matches_dream(setting)
                    if role in ('DREAM_REVIEW', 'PERSONA_DREAM', 'PERSONA_REVIEW') else transport.matches_daily(setting)):
                raise InvalidData()
        return values

    def prepare(self, version: ExecutionVersion) -> PreparedTransports:
        transports = self.transports(version)
        previous = self.active.version.candidate if self.active is not None else self.versions.birth.candidate
        old_roles = {as_record(role)['role']: as_record(role) for role in cast(tuple, previous.text.record('provider.transport')['roles'])}
        for raw in cast(tuple, version.candidate.text.record('provider.transport')['roles']):
            role = as_record(raw)
            old = old_roles[role['role']]
            if any(role[key] != old[key] for key in ('secret_ref', 'secret_revision', 'account_ref')):
                if not transports[cast(str, role['role'])].validate_credential_reference():
                    raise OwnerFailure('RESOURCE_UNAVAILABLE', 'credential', 'CREDENTIAL_UNAVAILABLE')
        return PreparedTransports(self, version, transports)

    def publish(self, resource: object) -> None:
        if type(resource) is not PreparedTransports or resource.owner is not self:
            raise InvalidData()
        self.active = resource
