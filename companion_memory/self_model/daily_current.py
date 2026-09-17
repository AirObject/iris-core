"""Native current-persona capability for the daily self-model owner.

Reads expose the actual reviewed publication and its unchanged imported source.
Mode and publication checks fence both read delivery and candidate commitment.
The capability provides no import, review, generation or editing operation.
"""
from __future__ import annotations
from companion_memory.configuration.cognition_identity import StoredCognitionConfiguration, StoredDreamConfiguration, StoredManagedConfiguration, stored_cognition_configuration_issue
from dataclasses import dataclass
from types import MappingProxyType
import time
from companion_memory.persistence import Found,NotFound,UnitOfWork
from companion_memory.persistence.schema import InvalidValue,valid_identifier
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.runtime.content_gate import ContentGate
from companion_memory.configuration.daily_persistence import StoredDailyConfiguration
from .approved_import import ApprovedPersonaImport
from .daily_persona import DailyPersona
from .unified_persona import UnifiedPersona
from .current import Available,Unavailable,Matched,Conflict
from .results import owner_failure,rejected

@dataclass(frozen=True,slots=True,init=False)
class DailyCurrentPersonaPort:
    """An owner-issued current-only capability bound to the complete daily instance."""
    _owner:DailyCurrentPersona
    def __init__(self):raise TypeError('Trusted daily setup issues this capability.')
    def matches(self,configuration:StoredCognitionConfiguration,storage:object) -> bool:
        owner=getattr(self,'_owner',None)
        return (type(owner) is DailyCurrentPersona and owner.port is self and owner.source.configuration is configuration
            and owner.source.storage is storage and not owner.closed)
    async def read_current(self,deadline:float):
        owner=getattr(self,'_owner',None)
        if type(owner) is not DailyCurrentPersona or owner.port is not self:return rejected('read_current','boundary')
        return await owner.read(deadline)
    async def has_publication(self, deadline: float) -> bool:
        """Observe publication availability during focus without releasing text."""
        owner = getattr(self, '_owner', None)
        if type(owner) is not DailyCurrentPersona or owner.port is not self or owner.closed:
            raise OwnerFailure('INVALID_STATE', 'persona', 'NOT_READY')
        current = await owner.source.read_current(deadline)
        if owner.closed or time.monotonic() >= deadline:
            raise OwnerFailure('TIMEOUT', 'persona', 'DEADLINE_EXCEEDED')
        if type(current) is Found:
            return True
        if type(current) is NotFound:
            return False
        raise OwnerFailure('STORAGE_FAILED', 'persona', 'READ_FAILED')
    def verify_current(self,uow:UnitOfWork,publication_id:str,expected_revision:int):
        owner=getattr(self,'_owner',None)
        if type(owner) is not DailyCurrentPersona or owner.port is not self:return rejected('verify_current','boundary')
        return owner.verify(uow,publication_id,expected_revision)
    def verify_recovery(self,uow:UnitOfWork,publication_id:str,expected_revision:int):
        """Confirm an original publication only while the native gate is recovering."""
        owner=getattr(self,'_owner',None)
        if type(owner) is not DailyCurrentPersona or owner.port is not self or owner.closed or owner.gate.state!='RECOVERING':return rejected('verify_current','boundary')
        current=owner.source.participate_current(uow,publication_id,expected_revision)
        uow.require_commit_permission(lambda:not owner.closed and owner.gate.state=='RECOVERING')
        return Conflict() if current is None else Matched(owner.project(current))

class DailyCurrentPersona:
    """Share the actual self-model owner and runtime gate, with no extra writer."""
    def __init__(self,source:ApprovedPersonaImport|DailyPersona|UnifiedPersona,gate:ContentGate):
        if type(source) not in (ApprovedPersonaImport,DailyPersona,UnifiedPersona) or not source.bound or type(gate) is not ContentGate:raise InvalidValue()
        self.source=source;self.gate=gate;self.closed=False
        port=object.__new__(DailyCurrentPersonaPort);object.__setattr__(port,'_owner',self);self.port=port
    def project(self,current):
        if (type(self.source.configuration) is StoredDreamConfiguration or type(self.source.configuration) is StoredManagedConfiguration):
            from .periodic_projection import project_current
            return project_current(current)
        return current
    def checkpoint(self):
        if self.closed:raise OwnerFailure('INVALID_STATE','state','SERVICE_CLOSED')
        value=self.gate.information_checkpoint()
        if value is None:raise OwnerFailure('MODE_BLOCKED','state','DREAMING')
        return value
    async def read(self,deadline:float):
        try:
            checkpoint=self.checkpoint()
            observed=await self.source.read_current(deadline)
            if type(observed) is Found:value=Available(self.project(observed.value))
            elif type(observed) is NotFound:value=Unavailable()
            else:raise InvalidValue()
            delivered=[]
            if (time.monotonic()>=deadline or not self.gate.start_information_delivery(checkpoint,lambda:not self.closed,lambda:delivered.append(value))):
                raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            return delivered[0]
        except OwnerFailure as failure:return owner_failure('read_current',failure)
        except InvalidValue:return rejected('read_current','record')
    def verify(self,uow:UnitOfWork,publication_id:str,revision:int):
        if not valid_identifier(publication_id) or type(revision) is not int or revision<1:return rejected('verify_current','shape')
        try:
            with self.gate.lock:
                if self.closed or self.gate.information_operation_reason() is not None:raise OwnerFailure('MODE_BLOCKED','state','DREAMING')
                epoch=self.gate.epoch
            value=self.source.participate_current(uow,publication_id,revision)
            if value is None:return Conflict()
            uow.require_commit_permission(lambda:not self.closed and self.gate.epoch==epoch and self.gate.information_operation_reason() is None)
            return Matched(self.project(value))
        except OwnerFailure as failure:return owner_failure('verify_current',failure)
        except InvalidValue:return rejected('verify_current','record')
    def close(self) -> None:
        """Revoke current delivery; actual reads remain owned by self-model cleanup."""
        self.closed=True
        if type(self.source) is UnifiedPersona:self.source.close()

def query_projection(current):
    """Preserve imported origin in the public reply without claiming local generation."""
    from companion_memory.persistence.content_codec import encode_content
    if current.get('projection_version')=='PERIODIC_PERSONA_V1':
        from .periodic_projection import encode_projection
        encode_projection(current)
        return current
    if current.get('publication_origin')!='IMPORTED_APPROVED':
        from .formats import query_projection as generated_projection
        return generated_projection(current)
    result=MappingProxyType({'availability':'AVAILABLE','text':current['text'],'revision':current['revision'],
        'publication_id':current['publication_id'],'generated_at':current['generated_at_us'],'review_status':'APPROVED',
        'origin':'REMOTE_PROVIDER','publication_origin':'IMPORTED_APPROVED','stale':current['stale'],
        'original_database_id':current['original_database_id'],'original_publication_id':current['original_publication_id'],
        'original_candidate_id':current['original_candidate_id'],'review_evidence_digest':current['review_evidence_digest']})
    encode_content(result,2048)
    return result
