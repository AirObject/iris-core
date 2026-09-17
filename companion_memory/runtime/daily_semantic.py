"""Daily semantic coordination using the shared host's original admission.

Original resume confirmation cannot reactivate a reopened or paused process.
All work, artifact publication and file cleanup use native semantic ports.
"""
from types import MappingProxyType
from companion_memory.persistence import Found,NotFound,Receipt
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.owned_statements import OwnerFailure
from .semantic_management import SemanticManagementPort
from .semantic_results import outcome,LocalConfirmation

class DailySemanticManagement(SemanticManagementPort):
    async def run_work(self,work_id:str,*,slot_id:str|None=None,_request_deadline_at:int|None=None,_admission_deadline:float|None=None):
        from .managed_semantic_authorization import ManagedSemanticAuthorization
        authorization = self.host.authorization
        if type(authorization) is ManagedSemanticAuthorization and slot_id is None:
            slot_id = authorization.slot(work_id)
        return await super().run_work(work_id, slot_id=slot_id, _request_deadline_at=_request_deadline_at,
            _admission_deadline=_admission_deadline)

    @outcome
    async def resume(self,key:str) -> Receipt:
        self.host.normal()
        port=self.host.storage.bind_operation(self.definitions['resume'],self.owner.instance)
        original=await port.read_receipt(key)
        if type(original) is Found:return original.value
        if type(original) is not NotFound:raise LocalConfirmation(original)
        authorization=self.host.authorization
        if authorization is None:raise OwnerFailure('ACCESS_DENIED','binding','OPERATION_NOT_GRANTED')
        authorization.activate();control=await self.control()
        if control['pause_reason'] in ('UNKNOWN','INTEGRITY'):raise InvalidValue()
        receipt=await self._commit('resume',key,MappingProxyType({'space_id':self.owner.space,'expected_revision':control['revision'],
            'authorization_digest':authorization.grant.digest}))
        self.host.enable_semantic_dispatch();return receipt
