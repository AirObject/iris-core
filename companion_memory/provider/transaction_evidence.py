"""Provider-owned short transaction checks for retained local coordination.

Only existing exact native work/result capabilities can read their request.
These methods join an already authorized local transaction without waiting,
issuing requests, expanding a result allowlist or releasing any liability.
"""
from __future__ import annotations
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from companion_memory.persistence import UnitOfWork, Staged, Value
from .completion_evidence import ConfirmedCompletion, issued_completion, _allowed
from .ledger import LedgerFailure
from .ports import ResultOwnerPort, WorkPort
from .resources import WorkGrant
from .values import Failed, Found, InvalidData, Record, as_record, freeze
if TYPE_CHECKING:
    from .service import ProviderService


def _read(service: ProviderService, uow: UnitOfWork, table: str, request_id: str, scope: str) -> Record | None:
    declaration = dict(service._ledger.assembly.repository.statements)['transaction_'+table]
    port = service._ledger.storage.bind_statement(service._ledger.assembly.repository.definition,declaration,scope)
    observed = port.participate(uow,{'request_id':request_id})
    if type(observed) is not Staged or type(observed.value) is not tuple or len(observed.value) > 1:
        raise LedgerFailure(observed)
    return service._ledger._decode(table, cast(MappingProxyType[str,Value],observed.value[0])) if observed.value else None


def _live(service: ProviderService, port: WorkPort | ResultOwnerPort) -> bool:
    return (service._text and not service._ledger_faulted
            and service._access(port, 'recover_result' if type(port) is ResultOwnerPort else 'lookup_request', (type(port),)) is None)


def request_in_transaction(service: ProviderService, port: WorkPort, uow: UnitOfWork,
                           request_id: str, original_request: object) -> Found | Failed:
    """Match complete original material and live caller scope to one current row."""
    from .service import error
    try:
        original = as_record(freeze(original_request,131072,owned=True))
    except InvalidData:
        return Failed(error('INVALID_INPUT','lookup_request','request','INVALID_SHAPE'))
    try:
        if not _live(service, port):
            return Failed(error('ACCESS_DENIED','lookup_request','capability','CAPABILITY_MISMATCH'))
        grant = service._ports.get(port)
        if type(grant) is not WorkGrant:
            return Failed(error('ACCESS_DENIED','lookup_request','capability','CAPABILITY_MISMATCH'))
        request = _read(service, uow, 'requests', request_id,grant.caller_scope)
        if request is None:
            return Failed(error('ACCESS_DENIED','lookup_request','capability','CAPABILITY_MISMATCH'))
        if any(request[name] != getattr(grant,name) for name in
               ('caller_module','caller_scope','extension_id','task_role','result_owner')):
            return Failed(error('ACCESS_DENIED','lookup_request','capability','CAPABILITY_MISMATCH'))
        attribution = as_record(request['attribution'])
        if (request['capability'] != 'GENERATION' or request['profile_id'] not in grant.profiles
                or attribution['run_id'] not in grant.run_ids
                or any(entry not in grant.entry_ids for entry in cast(tuple[str,...],attribution['entry_ids']))
                or service._original_fingerprint(original,request,request['execution_evidence']) != request['fingerprint']):
            return Failed(error('ACCESS_DENIED','lookup_request','capability','CAPABILITY_MISMATCH'))
        uow.require_commit_permission(lambda:_live(service,port))
        return Found(request)
    except (InvalidData,LedgerFailure) as failure:
        return service._read_failure('lookup_request',failure)


def completion_in_transaction(service: ProviderService, port: ResultOwnerPort, uow: UnitOfWork,
                              completion: object, retry_work: WorkPort | None = None) -> Found | Failed:
    """Recheck actual completion; retry additionally requires complete ended cost.

    The registered receipt was checked by the original completion issuer. The
    actual current request is compared inside this transaction; a later ledger
    revision requires fresh original confirmation. A retry reads the current
    cumulative budget in this same snapshot and never reserves or resets it.
    """
    from .service import error
    try:
        if (not _live(service,port) or not issued_completion(completion)
                or type(completion) is not ConfirmedCompletion or not _allowed(service,port,completion.terminal)):
            return Failed(error('ACCESS_DENIED','recover_result','capability','CAPABILITY_MISMATCH'))
        terminal = completion.terminal
        request_id=cast(str,terminal.request['object_id']);scope=cast(str,terminal.request['caller_scope'])
        request = _read(service,uow,'requests',request_id,scope)
        if request != terminal.request:
            return Failed(error('ACCESS_DENIED','recover_result','capability','CAPABILITY_MISMATCH'))
        assert request is not None
        attempt = _read(service,uow,'attempts',request_id,scope)
        from .text_stored_schema import terminal_reason
        from .terminal_evidence import matches_text_attempt
        if (terminal_reason(request, (attempt,) if attempt is not None else ())!=terminal.terminal_reason
                or not matches_text_attempt(terminal,attempt)):raise InvalidData()
        if retry_work is not None:
            if (type(retry_work) is not WorkPort or retry_work._native() is not service or terminal.original_request is None
                    or request['task_role'] != 'PERSONA' or request['caller_module'] != 'self_model' or request['result_owner'] != 'self_model'):
                return Failed(error('ACCESS_DENIED','recover_result','capability','CAPABILITY_MISMATCH'))
            matched = request_in_transaction(service,retry_work,uow,cast(str,request['object_id']),terminal.original_request)
            if type(matched) is Failed:
                return matched
            if not retry_work.consumers_ended() or service._lookup_tasks:
                return Failed(error('RESOURCE_BUSY','recover_result','state','ADMISSION_BUSY',True))
            if (1 if attempt is not None else 0) != request['attempt_count']:
                raise InvalidData()
            if attempt is not None:
                reservation = _read(service,uow,'reservations',request_id,scope)
                usage = as_record(attempt['usage'])
                if (attempt['state'] not in ('COMPLETED','NOT_SENT') or reservation is None
                        or reservation['cost_complete'] is not True or usage['cost_complete'] is not True
                        or any(reservation[name] != 0 for name in ('held_atoms','quota_reserved','quota_held'))
                        or usage['quota_held'] != 0 or usage['quota_known'] is None):
                    return Failed(error('RESOURCE_BUSY','recover_result','state','ADMISSION_BUSY'))
            profile = service._profiles[cast(str,request['profile_id'])]
            account = service._accounts[cast(str,profile['account_id'])]
            budget = _read(service,uow,'budget_windows',request_id,scope)
            from .text_accounting import liability, check_budget
            if budget is None or budget['policy'] != account or budget['account_id']!=request['account_id']:
                raise InvalidData()
            reason = check_budget(budget,liability(account,profile)[0])
            if reason is not None:
                return Failed(error('PAUSED_BUDGET','recover_result','budget',reason))
            uow.require_commit_permission(lambda:_live(service,retry_work) and retry_work.consumers_ended() and not service._lookup_tasks)
        uow.require_commit_permission(lambda:_live(service,port) and issued_completion(completion))
        return Found(MappingProxyType({'request_id':request['object_id'],'revision':request['revision']}))
    except (InvalidData,LedgerFailure) as failure:
        return service._read_failure('recover_result',failure)


def unsent_in_transaction(service: ProviderService, port: WorkPort, uow: UnitOfWork,
                          evidence: object, original_request: object, *, retry: bool = False) -> Found | Failed:
    """Verify actual registration absence under the retained original admission seal.

    Only an issued self-model persona capability can join these two fixed reads.
    The seal and ended consumers are checked again immediately before commit.
    Neither a missing row nor a caller-supplied zero revision issues this proof.
    """
    from .service import error
    from .unsent_evidence import VerifiedUnsent, issued_unsent, request_key
    try:
        original=as_record(freeze(original_request,131072,owned=True))
        grant=service._ports.get(port)
        if (type(retry) is not bool or not _live(service,port) or type(grant) is not WorkGrant
                or type(evidence) is not VerifiedUnsent or not issued_unsent(evidence)
                or evidence._provider is not service or evidence.database_id!=service._ledger.database_id
                or evidence.conclusion!='REGISTRATION_ABSENT' or evidence.request_id is not None
                or evidence.original_request!=original
                or (grant.caller_module,grant.task_role,grant.result_owner)!=('self_model','PERSONA','self_model')
                or (evidence.caller_module,evidence.caller_scope,evidence.result_owner,evidence.capability)
                    !=(grant.caller_module,grant.caller_scope,grant.result_owner,'GENERATION')
                or original['run_id'] not in grant.run_ids or original['profile_id'] not in grant.profiles
                or original['entry_ids']!=() or original['batch_id'] is not None):
            return Failed(error('ACCESS_DENIED','lookup_request','capability','CAPABILITY_MISMATCH'))
        key=request_key(grant,original)
        def sealed():
            return (_live(service,port) and issued_unsent(evidence) and service._unsent_evidence.get(key) is evidence
                and port.consumers_ended() and not service._lookup_tasks
                and not any(request_key(job.grant,job.request)==key for job in service._jobs))
        if not sealed():return Failed(error('RESOURCE_BUSY','lookup_request','state','ADMISSION_BUSY',True))
        repository=service._ledger.assembly.repository
        def read(name,values):
            statement=dict(repository.statements)[name]
            bound=service._ledger.storage.bind_statement(repository.definition,statement,grant.caller_scope)
            result=bound.participate(uow,values)
            if type(result) is not Staged or type(result.value) is not tuple or len(result.value)>1:raise LedgerFailure(result)
            return result.value
        rows=read('transaction_original_absence',{'caller_module':grant.caller_module,'extension_id':grant.extension_id or '',
            'operation_key':original['operation_key']})
        if rows:raise InvalidData()
        if retry:
            profile=service._profiles[cast(str,original['profile_id'])];account=service._accounts[cast(str,profile['account_id'])]
            rows=read('transaction_unsent_budget',{'caller_scope':grant.caller_scope,'account_id':profile['account_id'],'window_id':account['window_id']})
            if len(rows)!=1:raise InvalidData()
            budget=service._ledger._decode('budget_windows',cast(MappingProxyType[str,Value],rows[0]))
            if budget['policy']!=account:raise InvalidData()
            from .text_accounting import liability,check_budget
            reason=check_budget(budget,liability(account,profile)[0])
            if reason is not None:return Failed(error('PAUSED_BUDGET','lookup_request','budget',reason))
        uow.require_commit_permission(sealed)
        return Found(MappingProxyType({'request_id':None,'revision':0}))
    except (InvalidData,LedgerFailure) as failure:
        return service._read_failure('lookup_request',failure)
