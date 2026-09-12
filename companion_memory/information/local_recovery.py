"""Host initialization owns frozen local goal completion, never transport rights."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
import time
from typing import TYPE_CHECKING
from companion_memory.persistence import Committed, Found
from companion_memory.persistence.completion import CompletionScope
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from .records import Record, checked, identity, text
from .errors import InformationResult, rejected
if TYPE_CHECKING:
    from .management import ManagementAssembly, ManagementPort
    from companion_memory.goals.service import GoalsService


@dataclass(frozen=True, slots=True)
class LocalCompletion:
    kind: str
    key: str
    payload: Record


class LocalGoalRecovery:
    """One assembly-owned capability derived exclusively from retained owner facts.

    The exact command survives a logical timeout and is confirmed before the
    next owner scan. Actual completion and durable confirmation are independent.
    Ordinary issued ports cannot acquire this capability through names or IDs.
    """
    def __init__(self, management: ManagementAssembly, goals: GoalsService, recovering: Callable[[], bool]):
        self.management, self.goals, self.recovering = management, goals, recovering
        self._port: ManagementPort | None = None
        self._work: LocalCompletion | None = None
        self._completion: CompletionScope | None = None
        self._running = False

    def permits(self, port: ManagementPort, kind: str) -> bool:
        return self.recovering() and port is self._port and self._work is not None and self._work.kind == kind

    def verify(self, port: ManagementPort, kind: str, key: str, payload: Record) -> None:
        if port is not self._port: return
        work = self._work
        if (not self.permits(port, kind) or work is None
                or key != self.management.operation_key(port, kind, work.key)
                or encode_content(payload, 24576) != encode_content(work.payload, 24576)):
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')

    async def run(self) -> InformationResult:
        from .management import HostIdentity, SCHEMAS
        if not self.recovering():
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
        if self._running or self._completion is not None and self._completion.pending:
            return rejected('initialize', OwnerFailure('RESOURCE_BUSY', 'state', 'ADMISSION_FULL', True))
        self._running = True
        try:
            while True:
                if not self.recovering():
                    return rejected('initialize', OwnerFailure('MODE_BLOCKED', 'state', 'RUNTIME_FAULTED'))
                if self._work is None:
                    tasks = await self.goals.running_tasks()
                    if not self.recovering():
                        return rejected('initialize', OwnerFailure('INVALID_STATE', 'state', 'SERVICE_CLOSED'))
                    if tasks:
                        current, decision, canonical = await self.goals.task_decision(text(tasks[0]['task_id']))
                        payload: dict[str, object] = {'task_id': current['task_id'], 'expected_revision': current['revision'], 'owner_id': current['owner_id']}
                        kind = 'goal_exact_merge' if decision == 'MERGE' else 'goal_dedup_finish'
                        if canonical is not None: payload.update(canonical_id=canonical['goal_id'], canonical_revision=canonical['revision'])
                        else: payload['status'] = decision
                        self._work = LocalCompletion(kind, identity('recover_goal_task', current['task_id'], current['revision'], decision), checked(SCHEMAS[kind], payload, 24576))
                    else:
                        attempts = await self.goals.unresolved_attempts()
                        if not self.recovering():
                            return rejected('initialize', OwnerFailure('INVALID_STATE', 'state', 'SERVICE_CLOSED'))
                        if attempts:
                            attempt = attempts[0]
                            self._work = LocalCompletion('goal_attempt_finish', identity('recover_reminder', attempt['delivery_id']),
                                checked(SCHEMAS['goal_attempt_finish'], {'delivery_id': attempt['delivery_id'], 'expected_revision': attempt['revision'],
                                    'state': 'UNKNOWN', 'reason': 'COMMIT_UNCONFIRMED'}, 24576))
                if self._work is None:
                    # An empty page is only completion evidence while this host
                    # still owns recovery; a concurrent close keeps precedence.
                    if not self.recovering():
                        return rejected('initialize', OwnerFailure('INVALID_STATE', 'state', 'SERVICE_CLOSED'))
                    if self._port is not None: self.management.revoke(self._port)
                    self._port = None
                    from types import MappingProxyType
                    return Found(MappingProxyType({'state': 'READY'}))
                if self._port is None:
                    self._port = self.management.issue(HostIdentity('information_recovery', 'information_host', self.goals.binding.instance_id,
                        self.goals.binding.instance_id, frozenset(('goal_attempt_finish', 'goal_dedup_finish', 'goal_exact_merge')), (), time.monotonic() + 3600))
                work = self._work
                with CompletionScope() as completion:
                    result = await self._port.execute(work.kind, work.key, work.payload)
                self._completion = completion
                if type(result) is not Committed: return result
                if completion.pending:
                    # Keep the same exact command and capability until its own
                    # actual tail ends; the next run confirms its original key.
                    return rejected('initialize', OwnerFailure('RESOURCE_BUSY', 'state', 'ADMISSION_FULL', True))
                self._work = None
        finally: self._running = False
