"""Immutable execution versions separate from durable business birth identity.

Only complete versions reconstructed by the configuration owner are issued.
Context selection is task-local and inherited by retained worker tasks; changing
the active version cannot mutate an already issued version or its nested values.
"""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .managed_resolution import ManagedConfigurationCandidate
from companion_memory.persistence.owned_statements import OwnerFailure
if TYPE_CHECKING:
    from .managed_versions import ManagedVersions

_selected: ContextVar[ExecutionVersion | None] = ContextVar('managed_execution_configuration', default=None)


@dataclass(frozen=True, slots=True, init=False)
class ExecutionVersion:
    version_id: str
    candidate: ManagedConfigurationCandidate
    issuer: ExecutionVersions

    def __init__(self):
        raise TypeError('Load a complete execution version from configuration.')


def _issue(version_id: str, candidate: ManagedConfigurationCandidate, issuer: ExecutionVersions) -> ExecutionVersion:
    value = object.__new__(ExecutionVersion)
    for key, content in (('version_id', version_id), ('candidate', candidate), ('issuer', issuer)):
        object.__setattr__(value, key, content)
    return value


class ExecutionVersions:
    def __init__(self, versions: ManagedVersions, birth_id: str, birth: ManagedConfigurationCandidate):
        self.versions = versions
        self.birth = _issue(birth_id, birth, self)
        self.active = self.birth

    @property
    def current(self) -> ExecutionVersion:
        selected = _selected.get()
        return selected if selected is not None and selected.issuer is self else self.active

    async def load(self, version_id: str) -> ExecutionVersion:
        if version_id == self.birth.version_id:
            return self.birth
        if version_id == self.active.version_id:
            return self.active
        return _issue(version_id, await self.versions.load(version_id), self)

    @contextmanager
    def use(self, version: ExecutionVersion):
        if type(version) is not ExecutionVersion or version.issuer is not self:
            raise OwnerFailure('ACCESS_DENIED', 'configuration', 'BINDING_MISMATCH')
        token = _selected.set(version)
        try:
            yield version
        finally:
            _selected.reset(token)

    def publish(self, version: ExecutionVersion):
        if type(version) is not ExecutionVersion or version.issuer is not self:
            raise OwnerFailure('ACCESS_DENIED', 'configuration', 'BINDING_MISMATCH')
        self.active = version


def selected_execution() -> ExecutionVersion | None:
    """Carry only the immutable configuration reference into its SQLite worker."""
    return _selected.get()
