"""Native first-persona work bound to the actual retained run and mode epoch.

Trusted local management owns this capability. Serialized role names, arbitrary
WorkGrants and ordinary entry ports cannot install a focused dispatch permit.
One permit shares the original gate capacity and stays until its own consumers
end. Recovery permits can inspect original work but can never send.
"""
from __future__ import annotations
from dataclasses import dataclass
import time
from typing import cast
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.deadlines import DeadlineScope
from companion_memory.provider import WorkGrant,WorkPort
from .transactions import PersonaTransactions
from .request_material import PersonaRequestMaterial
from .preparation import retained_material
from companion_memory.memory.formats import record


@dataclass(frozen=True,slots=True,init=False)
class PersonaWorkPermit:
    """One retained generation, exact original request and issued Provider port."""
    grant: WorkGrant
    work: WorkPort
    material: PersonaRequestMaterial
    generation: int
    epoch: int
    recovery: bool
    def __init__(self):raise TypeError('Persona work requires native initialization management.')


class InitialPersonaWork:
    """One trusted gate participant; no command, table or public role is added."""
    def __init__(self,transactions: PersonaTransactions):
        if (type(transactions) is not PersonaTransactions or transactions.persona is None or transactions.initial is None
                or transactions.gate is None or transactions.gate._initial_persona is not None):raise InvalidValue()
        self.transactions=transactions;self.gate=transactions.gate
        self._permit:PersonaWorkPermit|None=None;self._closed=False
        self.gate._initial_persona=self

    def owns(self,grant: WorkGrant) -> bool:
        return self._permit is not None and self._permit.grant is grant

    def allowed(self,grant: WorkGrant) -> bool:
        """Called under the gate's short dispatch lock, with no I/O or waiting."""
        permit=self._permit;t=self.transactions
        return (not self._closed and not t._closed and permit is not None and permit.grant is grant and not permit.recovery
            and permit.epoch==self.gate.epoch and self.gate.state=='DREAM_FOCUSED'
            and t.provider.get_health().lifecycle=='READY' and t.assembly.storage.get_health().lifecycle=='READY')

    async def issue(self,run_id: str,generation: int,original_key: str,deadline: float,*,recovery: bool = False) -> PersonaWorkPermit:
        """Re-read actual owner records before granting exactly this generation.

        This is a trusted coordinator method, never an entry or HTTP operation.
        The coordinator has already completed association (or is restoring it).
        It retains the returned permit through every actual descendant consumer.
        """
        t=self.transactions;owner=t.persona;initial=t.initial
        if self._closed or t._closed or self._permit is not None or owner is None or initial is None:
            raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING')
        if type(generation) is not int or not 1<=generation<=3 or type(recovery) is not bool:raise InvalidValue()
        with DeadlineScope(deadline):
            run=await owner.read_original('run',run_id,deadline)
            if run is None or run.value['generation']!=generation or run.value['provider_operation_key']!=original_key:
                raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
            current=run.value
            if current['state']=='PREPARED' or not recovery and (current['state']!='REQUEST_ASSOCIATED' or current['provider_request_id'] is not None):
                raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
            from companion_memory.persistence import Found
            from companion_memory.memory.service import MemoryError
            input_port=t.initial_commands._port
            if input_port is None:raise InvalidValue()
            found=await input_port.read_initial(cast(str,current['input_id']),deadline)
            if type(found) is MemoryError:raise OwnerFailure(found.code,found.field,found.reason,found.cleanup_pending)
            if type(found) is not Found:raise InvalidValue()
            source=record(found.value)
            material=retained_material(t.assembly.text_transactions.configuration,record(source['input']),current)
            modes=await t.assembly.rows.read('mode_get',{'mode_id':'instance_mode'})
            if len(modes)!=1:raise InvalidValue()
            mode=modes[0]
            if (mode['run_id']!=run_id or not recovery and (mode['state']!='DREAM_FOCUSED'
                    or mode['publication_id'] is not None or mode['epoch']!=current['mode_epoch'])):
                raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
            # The same native management coordinator serializes run changes;
            # confirm the complete revision after the other owner reads as well.
            again=await owner.read_original('run',run_id,deadline)
            if again!=run:raise OwnerFailure('PRECONDITION_FAILED','revision','REVISION_CONFLICT')
        grant=WorkGrant('self_model',t.assembly.instance_id,None,'PERSONA',(cast(str,material.request['profile_id']),),
            ('GENERATION',),'self_model',t.actor,(run_id,),internal_dream=True,prompt_revisions=(cast(str,current['prompt_ref']),))
        with self.gate.lock:
            if (self._closed or t._closed or time.monotonic()>=deadline or self._permit is not None or t._scope is not None
                    or len(self.gate._grants)>=self.gate.capacity or self.gate.integrity_pending()
                    or not (recovery and self.gate.state=='RECOVERING') and (self.gate.epoch!=mode['epoch'] or self.gate.state!=mode['state'])
                    or not recovery and self.gate._mode_cutoff):
                raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING')
            try:port=t.provider.bind_work(grant)
            except ValueError:raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING') from None
            permit=object.__new__(PersonaWorkPermit)
            for name,value in (('grant',grant),('work',port),('material',material),('generation',generation),('epoch',mode['epoch']),('recovery',recovery)):
                object.__setattr__(permit,name,value)
            self._permit=permit
            self.gate._grants[id(grant)]=grant,cast(int,mode['epoch']),None,recovery
            return permit

    def release(self,permit: PersonaWorkPermit) -> bool:
        """Release only the actual original work's ended consumers, never a timeout."""
        with self.gate.lock:
            if self._permit is not permit:return False
            if not permit.work.consumers_ended():return False
            self.transactions.provider.revoke(permit.work);self.gate.revoke(permit.grant);self._permit=None
            return True

    def close(self) -> None:
        """Close dispatch immediately while the original permit keeps cleanup ownership."""
        with self.gate.lock:self._closed=True
