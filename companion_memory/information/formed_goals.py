"""One trusted formed-goal intention backed by independently verified owners.

Binding freezes the complete payload and original key. The final transaction
reads cognition's committed manifest and memory's current authorized basis.
Ordinary management handles cannot manufacture this authority; no model is
called and no proposal, memory, or source record is written by this adapter.
"""
from __future__ import annotations
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING
from companion_memory.cognition.candidates import CandidateBinding
from companion_memory.goals.inputs import INTERNAL
from companion_memory.goals.service import GoalAuthority
from companion_memory.memory.service import MemoryReadPort, MemoryService
from companion_memory.persistence import Committed, Found, NotFound, UnitOfWork
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.schema import valid_identifier
from .errors import InformationRejected, InformationNotCommitted, InformationUnconfirmed, rejected
from .records import Record, checked, text
if TYPE_CHECKING:
    from .management import ManagementPort

type GoalWorkResult = Committed | Found | NotFound | InformationRejected | InformationNotCommitted | InformationUnconfirmed


@dataclass(frozen=True, slots=True)
class FormedGoalAuthority:
    """Exact local intention and the two original native read owners."""
    payload: Record
    key: str
    snapshot_id: str
    candidates: CandidateBinding
    memory: MemoryService
    memory_port: MemoryReadPort

    @classmethod
    def bind(cls, payload: object, key: str, snapshot_id: str, candidates: CandidateBinding,
             memory: MemoryService, memory_port: MemoryReadPort) -> FormedGoalAuthority:
        if (not valid_identifier(key) or type(candidates) is not CandidateBinding or type(memory) is not MemoryService
                or type(memory_port) is not MemoryReadPort):
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        return cls(checked(INTERNAL, payload, 4096), key, snapshot_id, candidates, memory, memory_port)

    def verified(self, value: Record, key: str, route_ids: tuple[str, ...]) -> GoalAuthority:
        """Create only an exact-payload callback; both facts are read in its UoW."""
        if value != self.payload or key != self.key:
            raise OwnerFailure('ACCESS_DENIED', 'goal', 'BINDING_MISMATCH')
        def verify(uow: UnitOfWork, object_id: str) -> bool:
            if object_id != self.payload['basis_id']: return False
            manifest = self.candidates.published_goal_basis(uow, text(self.payload['source_id']), object_id, self.snapshot_id)
            subjects = self.payload['subject_ids']
            if type(subjects) is not tuple: return False
            return self.memory.verify_goal_basis(self.memory_port, uow, object_id, text(manifest['source_id']), tuple(text(s) for s in subjects))
        return GoalAuthority(route_ids, text(self.payload['source_id']), verify)


@dataclass(frozen=True, slots=True, init=False)
class FormedGoalWorkPort:
    """A native single-intention handle; replays confirm its original receipt."""
    _port: ManagementPort
    _work: FormedGoalAuthority

    def __init__(self): raise TypeError('A formed goal requires trusted native binding.')

    @classmethod
    def bind(cls, port: ManagementPort, work: FormedGoalAuthority) -> FormedGoalWorkPort:
        result = object.__new__(cls)
        object.__setattr__(result, '_port', port); object.__setattr__(result, '_work', work)
        return result

    @property
    def origin(self) -> Record:
        return MappingProxyType({'storage_execution': 'ACTUAL', 'model_adapter': 'SIMULATED', 'candidate_origin': 'SYNTHETIC'})

    async def execute(self) -> GoalWorkResult:
        return self._result(await self._port.execute('goal_inject_internal', self._work.key, self._work.payload), 'create_internal_goal')

    async def resolve(self) -> GoalWorkResult:
        return self._result(await self._port.resolve('goal_inject_internal', self._work.key, self._work.payload), 'resolve_management')

    def revoke(self) -> None:
        """Release this intention's admission binding; actual I/O stays retained."""
        self._port.revoke()

    @staticmethod
    def _result(value: object, operation: str) -> GoalWorkResult:
        if type(value) is InformationRejected or type(value) is InformationNotCommitted or type(value) is InformationUnconfirmed:
            return replace(value, error=replace(value.error, operation=operation))
        if type(value) is Committed or type(value) is Found or type(value) is NotFound: return value
        return rejected(operation, OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE'))
