"""Native bounded evidence of isolated original work that never reached dispatch.

An ordinary lookup miss is not this capability. The issuer serializes with
registration, rejects live consumers and uncertain storage, and seals the old key
while evidence remains held by the local owner transaction.
"""
from __future__ import annotations
from companion_memory.persistence.completion import finish_owned
import asyncio
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from .values import Data, InvalidData, as_record, freeze
from .values import Failed
from .ports import WorkPort
from .resources import WorkGrant
if TYPE_CHECKING:
    from .service import ProviderService


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class VerifiedUnsent:
    """Issuer-bound original descriptor and reliable no-dispatch conclusion."""
    _provider: ProviderService
    database_id: str
    caller_module: str
    caller_scope: str
    result_owner: str
    capability: str
    original_request: MappingProxyType[str, Data]
    request_id: str | None
    conclusion: str
    deadline: float

    def __init__(self): raise TypeError('Unsent evidence requires the original Provider owner.')


@dataclass(frozen=True, slots=True)
class UnsentVerified:
    value: VerifiedUnsent


def request_key(grant, request):
    return grant.caller_module, grant.caller_scope, grant.extension_id, request['operation_key']


def issued_unsent(evidence: object) -> bool:
    from .service import ProviderService
    if type(evidence) is not VerifiedUnsent: return False
    try: provider = object.__getattribute__(evidence, '_provider')
    except AttributeError: return False
    return type(provider) is ProviderService and any(value is evidence for value in provider._unsent_evidence.values())


async def verify_unsent(provider, port, action, raw):
    """Serialize exact original lookup with admission and retain all lower readers."""
    from .service import CAPABILITIES, _Job, _AdmissionStopped, error
    from .ledger import LedgerFailure
    operation = 'lookup_request'
    denied = provider._access(port, operation, (WorkPort,))
    if denied is not None: return Failed(denied)
    grant = cast(WorkGrant, provider._ports[port])
    if type(action) is not str or action not in CAPABILITIES or CAPABILITIES[action] not in grant.capabilities:
        return Failed(error('ACCESS_DENIED', operation, 'capability', 'CAPABILITY_MISMATCH'))
    try: request, token, deadline = provider._normalize(raw, grant, action, provider._now())
    except _AdmissionStopped as stopped:
        return Failed(stopped.cause)
    except (InvalidData, PermissionError):
        return Failed(error('INVALID_INPUT', operation, 'request', 'INVALID_SHAPE'))
    if request['profile_id'] not in grant.profiles:
        return Failed(error('ACCESS_DENIED', operation, 'capability', 'CAPABILITY_MISMATCH'))
    if len(provider._lookup_tasks) >= provider._number('max_in_flight') or len(provider._unsent_evidence) >= provider._number('max_in_flight'):
        return Failed(error('RESOURCE_BUSY', operation, 'state', 'ADMISSION_BUSY'))
    key = request_key(grant, request)
    def active():
        health = provider._ledger.storage.get_health()
        return (provider._ledger_faulted or health.lifecycle != 'READY' or health.writes_in_flight
            or any(request_key(job.grant, job.request) == key for job in provider._jobs))
    async def inspect():
        async with provider._serial:
            if active(): return Failed(error('RESOURCE_BUSY', operation, 'state', 'ADMISSION_BUSY', True))
            try:
                rows = await provider._ledger.read('requests_find', {'caller_scope': grant.caller_scope, 'caller_module': grant.caller_module,
                    'extension_id': grant.extension_id or '', 'operation_key': request['operation_key']})
                request_id = None; conclusion = 'REGISTRATION_ABSENT'
                if rows:
                    stored = rows[0]
                    job = _Job(action, grant, request, token, deadline, stored['object_id'])
                    if stored['fingerprint'] != provider._semantic(job, stored['execution_evidence']):
                        return Failed(error('IDEMPOTENCY_CONFLICT', operation, 'request', 'CONTENT_MISMATCH'))
                    attempts = await provider._ledger.read('attempts_for_request', {'request_id': stored['object_id']})
                    if stored['phase'] != 'TERMINAL' or stored['outcome'] not in ('MODE_BLOCKED', 'PAUSED_BUDGET') or attempts:
                        return Failed(error('RESOURCE_BUSY', operation, 'state', 'ADMISSION_BUSY'))
                    request_id = stored['object_id']; conclusion = 'ZERO_ATTEMPT_ADMISSION_TERMINAL'
                if active():
                    return Failed(error('RESOURCE_BUSY', operation, 'state', 'ADMISSION_BUSY', True))
                denied = provider._lookup_wait_issue(port, token, deadline)
                if denied is not None: return Failed(denied)
                evidence = object.__new__(VerifiedUnsent)
                for name, value in {'_provider': provider, 'database_id': provider._ledger.database_id, 'caller_module': grant.caller_module,
                    'caller_scope': grant.caller_scope, 'result_owner': grant.result_owner, 'capability': CAPABILITIES[action],
                    'original_request': as_record(freeze(request, provider._number('request_max_bytes'), owned=True)),
                    'request_id': request_id, 'conclusion': conclusion,'deadline':deadline}.items(): object.__setattr__(evidence, name, value)
                provider._unsent_evidence[key] = evidence
                return UnsentVerified(evidence)
            except (LedgerFailure, InvalidData) as failure: return provider._read_failure(operation, failure)
    task = asyncio.create_task(finish_owned(inspect()))
    provider._lookup_owners[task] = port
    if action == "understand_media": provider._lookup_consumers[task] = raw["payload"]["media"]
    provider._lookup_tasks.add(task); task.add_done_callback(provider._lookup_finished)
    done, _ = await asyncio.wait((task,), timeout=max(0, deadline - provider._now()))
    if not done: return Failed(error('TIMEOUT', operation, 'request', 'DEADLINE_EXCEEDED', True))
    denied = provider._lookup_wait_issue(port, token, deadline)
    if denied is not None: return Failed(denied)
    return task.result()
