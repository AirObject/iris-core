"""Native local recovery observations without readiness or sending authority.

The trusted host retains one exact recovery grant and one actual operation.
Publication still belongs to the host's final lifecycle fence. A deadline only
stops waiting; the original task and all descendant cleanup remain owned.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
import time
from companion_memory.persistence.completion import finish_owned,retain_completion
from companion_memory.persistence.deadlines import DeadlineScope
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.owned_statements import OwnerFailure
from .transactions import PersonaTransactions
from .recovery import verify_original_persona
from .results import rejected,owner_failure


@dataclass(frozen=True,slots=True,init=False)
class PersonaRecoveryGrant:
    """An exact host-owned local recovery capability, never a Provider work grant."""
    _owner: LocalPersonaRecovery
    def __init__(self):raise TypeError('Trusted host setup issues local recovery authority.')


@dataclass(frozen=True,slots=True)
class LocallyRecovered:
    mode: str
    learning_ready: bool
    cleanup_pending: bool=False


@dataclass(frozen=True,slots=True)
class Pending:
    mode: str='RECOVERING'
    learning_ready: bool=False
    cleanup_pending: bool=True


class LocalPersonaRecovery:
    """One finite local verification before ordinary runtime admission opens."""
    def __init__(self,transactions: PersonaTransactions):
        if (type(transactions) is not PersonaTransactions or transactions.persona is None or transactions.gate is None
                or transactions.gate.state!='RECOVERING' or transactions._closed):raise InvalidValue()
        self.transactions=transactions;self._task:asyncio.Task|None=None
        grant=object.__new__(PersonaRecoveryGrant);object.__setattr__(grant,'_owner',self);self.grant=grant

    async def recover_local(self,grant: PersonaRecoveryGrant,deadline: float):
        t=self.transactions;gate=t.gate
        if type(grant) is not PersonaRecoveryGrant or grant is not self.grant or getattr(grant,'_owner',None) is not self:
            return rejected('recover_local','boundary')
        if type(deadline) not in (float,int) or not time.monotonic()<deadline<float('inf'):return rejected('recover_local','shape')
        if t._closed or gate is None or gate.state!='RECOVERING':return rejected('recover_local','state')
        if self._task is not None:return Pending()
        async def inspect():
            try:
                with DeadlineScope(deadline):
                    await verify_original_persona(t,deadline)
                    assert t.persona is not None
                    publication=await t.persona.current_original(deadline)
                    modes=await t.assembly.rows.read('mode_get',{'mode_id':'instance_mode'})
                    if len(modes)!=1 or type(modes[0]['state']) is not str:raise InvalidValue()
                    with gate.lock:
                        if t._closed or gate.state!='RECOVERING':return rejected('recover_local','state')
                        if time.monotonic()>=deadline:return Pending()
                        return LocallyRecovered(modes[0]['state'],publication is not None)
            except OwnerFailure as failure:return owner_failure('recover_local',failure)
            except (InvalidValue,KeyError,TypeError):return rejected('recover_local','record')
        task=asyncio.create_task(finish_owned(inspect()));self._task=task;retain_completion(task)
        def ended(job):
            if not job.cancelled():job.exception()
            if self._task is job:self._task=None
        task.add_done_callback(ended)
        done,_=await asyncio.wait((task,),timeout=max(0,deadline-time.monotonic()))
        return task.result() if done else Pending()
