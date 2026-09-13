"""Runtime-owned first-persona mode effects inside existing owner transactions.

Only prepare, retry and approved publication invoke these fixed write effects.
The ordinary mode command retains its original audit and cannot finish the
initial run. No effect dispatches Provider work or opens ordinary admission.
"""
from __future__ import annotations
from dataclasses import replace
from types import MappingProxyType
from typing import TYPE_CHECKING,cast
from companion_memory.persistence import UnitOfWork,Value
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.schema import InvalidValue
from companion_memory.self_model.storage import PersonaStorage
from .content_gate import ContentGate
if TYPE_CHECKING:
    from .content_assembly import ContentAssembly


class InitialPersonaMode:
    """An explicit text-only replacement of the one original mode definition."""
    def __init__(self,assembly: ContentAssembly):
        if not assembly.text_format:raise InvalidValue()
        self.assembly=assembly;self._closed=False
        previous=assembly.command_definition('change_content_mode')
        self.definition=replace(previous,command_version=2,participants=assembly.repositories,handler=self.handle)
        self.persona:PersonaStorage|None=None
        self.gate:ContentGate|None=None

    def bind(self,persona: PersonaStorage) -> None:
        if type(persona) is not PersonaStorage or persona is not self.assembly.text_commands.persona or self.persona is not None:raise InvalidValue()
        self.persona=persona

    def current(self,uow: UnitOfWork,run_id: str,expected_epoch: int,*,enter: bool = False) -> MappingProxyType[str,Value]:
        """Compare the actual mode and revision; no external epoch becomes a fact."""
        if self._closed or set(self.assembly.storage.transaction_audit_owners(uow))!={'self_model','runtime'}:
            raise OwnerFailure('ACCESS_DENIED','identity','BOUNDARY_DENIED')
        mode=self.assembly._get('mode',uow,'mode_id','instance_mode')
        expected='NORMAL' if enter else 'DREAM_FOCUSED'
        if (mode['epoch']!=expected_epoch or mode['state']!=expected or mode['publication_id'] is not None
                or not enter and mode['run_id']!=run_id):raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        uow.require_commit_permission(lambda:not self._closed)
        return mode

    def enter(self,uow: UnitOfWork,run_id: str,expected_epoch: int,now_us: int) -> MappingProxyType[str,Value]:
        """Stage the original ENTER effect with the actual newly prepared run."""
        mode=self.current(uow,run_id,expected_epoch,enter=True)
        deadline=now_us+self.assembly.configuration.candidate.runtime.integer('runtime.focus_drain_timeout_ms')*1000
        return self._stage(uow,mode,state='DREAM_PREPARING',run_id=run_id,deadline_at_us=deadline)

    def retry(self,uow: UnitOfWork,run_id: str,expected_epoch: int) -> MappingProxyType[str,Value]:
        """Invalidate prior generation permits with one real persisted epoch CAS."""
        mode=self.current(uow,run_id,expected_epoch)
        return self._stage(uow,mode)

    def publish(self,uow: UnitOfWork,run_id: str,expected_epoch: int,publication_id: str) -> MappingProxyType[str,Value]:
        """Stage FINISH only after the same owner transaction validates publication."""
        mode=self.current(uow,run_id,expected_epoch)
        publication=self.persona.current_publication(uow) if self.persona is not None else None
        if publication is None or publication.value['object_id']!=publication_id or publication.value['run_id']!=run_id:
            raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
        return self._stage(uow,mode,state='DRAINING',publication_id=publication_id)

    def _stage(self,uow: UnitOfWork,mode: MappingProxyType[str,Value],**changes: Value) -> MappingProxyType[str,Value]:
        value=MappingProxyType({**mode,**changes,'epoch':cast(int,mode['epoch'])+1})
        self.assembly.rows.stage('mode_update',uow,dict(value))
        return value

    def handle(self,uow: UnitOfWork,values: MappingProxyType[str,Value]):
        gate=self.gate
        if self._closed or self.persona is None or gate is None or gate.state in ('CLOSED','FAULTED'):
            raise OwnerFailure('ACCESS_DENIED','identity','BOUNDARY_DENIED')
        uow.require_commit_permission(lambda:not self._closed and gate.state not in ('CLOSED','FAULTED'))
        action=values['action']
        if action in ('ENTER','FINISH'):
            raise OwnerFailure('ACCESS_DENIED','identity','BOUNDARY_DENIED')
        run=self.persona.read(uow,'run',cast(str,values['run_id']))
        if run is None:raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
        if action=='READY' and (run.value['state']!='PREPARED' or cast(int,run.value['mode_epoch'])>cast(int,values['expected_epoch'])):
            raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
        if action=='DRAINED' and (run.value['state']!='PUBLISHED' or run.value['publication_id'] is None):
            raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
        if action=='DRAINED':
            mode=self.assembly._get('mode',uow,'mode_id','instance_mode')
            publication=self.persona.current_publication(uow)
            if publication is None or mode['publication_id']!=run.value['publication_id'] or publication.value['object_id']!=mode['publication_id']:
                raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
        uow.require_commit_permission(lambda:not self._closed)
        return self.assembly.modes.handle('change_content_mode',uow,values)

    def close(self) -> None:
        """Stop new effects; the active storage owner retains any started write."""
        self._closed=True
