"""Exact native configuration branches for shared cognition domain services.

The old and dream publishers retain separate sealed identities and validators.
Sharing a domain service never converts a dream snapshot into an old snapshot.
"""
from __future__ import annotations
from .daily_persistence import StoredDailyConfiguration, stored_daily_configuration_issue
from .dream_persistence import StoredDreamConfiguration, stored_dream_configuration_issue

type StoredCognitionConfiguration = StoredDailyConfiguration | StoredDreamConfiguration


def stored_cognition_configuration_issue(value: object, *, storage: PersistenceService | None = None) -> str | None:
    """Dispatch only to the original validator for the exact issuing format."""
    if storage is not None and not storage.accepts_cognition_configuration(value):return 'DEFINITION_MISMATCH'
    if type(value) is StoredDailyConfiguration:
        return stored_daily_configuration_issue(value)
    if type(value) is StoredDreamConfiguration:
        return stored_dream_configuration_issue(value)
    return 'DEFINITION_MISMATCH'

from .daily_resolution import DailyConfigurationCandidate, daily_snapshot_issue
from .dream_resolution import DreamConfigurationCandidate, dream_snapshot_issue
from .daily_persistence import DailyConfigurationAssembly, DailyConfigurationBinding
from .dream_persistence import DreamConfigurationAssembly, DreamConfigurationBinding
from companion_memory.persistence import PersistenceService
from companion_memory.persistence.schema import InvalidValue

type CognitionCandidate = DailyConfigurationCandidate | DreamConfigurationCandidate
type CognitionPublisher = DailyConfigurationBinding | DreamConfigurationBinding


def cognition_candidate_values(candidate: CognitionCandidate):
    """Select the original complete codec by its exact native candidate identity."""
    if type(candidate) is DreamConfigurationCandidate:
        from .dream_codec import candidate_values
        return candidate_values(candidate)
    if type(candidate) is DailyConfigurationCandidate:
        from .daily_codec import candidate_values
        return candidate_values(candidate)
    raise InvalidValue()


def cognition_candidate_issue(candidate: object):
    if type(candidate) is DreamConfigurationCandidate:
        return dream_snapshot_issue(candidate)
    return daily_snapshot_issue(candidate)


def bind_cognition_configuration(assembly: DailyConfigurationAssembly | DreamConfigurationAssembly,
                                 storage: PersistenceService, instance: str, candidate: CognitionCandidate):
    if type(assembly) is DreamConfigurationAssembly and type(candidate) is DreamConfigurationCandidate:
        return assembly.bind(storage, instance, candidate)
    if type(assembly) is DailyConfigurationAssembly and type(candidate) is DailyConfigurationCandidate:
        return assembly.bind(storage, instance, candidate)
    raise InvalidValue()


async def persist_cognition_configuration(publisher: CognitionPublisher, key: str, candidate: CognitionCandidate,
                                          *, actor: str, protected_directories: dict[str,tuple[str,...]|list[str]]):
    if type(publisher) is DreamConfigurationBinding and type(candidate) is DreamConfigurationCandidate:
        return await publisher.persist_dream_configuration(key,candidate,actor=actor,protected_directories=protected_directories)
    if type(publisher) is DailyConfigurationBinding and type(candidate) is DailyConfigurationCandidate:
        return await publisher.persist_daily_configuration(key,candidate,actor=actor,protected_directories=protected_directories)
    raise InvalidValue()


async def initialize_cognition_roots(publisher: CognitionPublisher,key: str,stored: StoredCognitionConfiguration):
    if type(publisher) is DreamConfigurationBinding and type(stored) is StoredDreamConfiguration:
        return await publisher.initialize_business_roots(key,stored)
    if type(publisher) is DailyConfigurationBinding and type(stored) is StoredDailyConfiguration:
        return await publisher.initialize_business_roots(key,stored)
    raise InvalidValue()


def participate_cognition_snapshot(publisher: CognitionPublisher,uow,stored: StoredCognitionConfiguration):
    if type(publisher) is DreamConfigurationBinding and type(stored) is StoredDreamConfiguration:
        return publisher.participate_snapshot(uow,stored)
    if type(publisher) is DailyConfigurationBinding and type(stored) is StoredDailyConfiguration:
        return publisher.participate_snapshot(uow,stored)
    raise InvalidValue()
