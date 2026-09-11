"""Bounded per-command first-cause handoff from synchronous transaction handlers.

Only active invocations own slots. A late handler cannot create an orphan slot,
replace a first cause, or attribute a failure to an unrelated operation key.
"""
from dataclasses import dataclass
from threading import RLock
from .records import DomainFailure,digest


@dataclass(slots=True)
class CauseSlot:
    users:int=0
    issue:DomainFailure | None=None


class CommandCauses:
    def __init__(self):
        self.lock=RLock();self.slots:dict[str,CauseSlot]={}
    def watch(self,action:str,values:object) -> tuple[str,CauseSlot]:
        identity=digest((action,values))
        with self.lock:
            slot=self.slots.setdefault(identity,CauseSlot());slot.users+=1
            return identity,slot
    def record(self,action:str,values:object,issue:DomainFailure):
        with self.lock:
            slot=self.slots.get(digest((action,values)))
            if slot is not None and slot.issue is None:slot.issue=issue
    def release(self,identity:str):
        with self.lock:
            slot=self.slots[identity];slot.users-=1
            if not slot.users:del self.slots[identity]
