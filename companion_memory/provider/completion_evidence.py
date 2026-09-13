"""Confirm an actual text terminal's finite original local completion keys.

The exact result owner receives only the matching operation identity bound to
the existing native terminal. Receipt reads validate required audits within
storage; neither history enumeration nor model dispatch is available here.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from companion_memory.persistence import Found as StorageFound, NotFound as StorageNotFound
from companion_memory.persistence.completion import finish_owned
from companion_memory.persistence.deadlines import DeadlineScope
from .ledger import LedgerFailure
from .ports import ResultGrant, ResultOwnerPort
from .terminal_evidence import VerifiedTerminal, issued_terminal
from .values import Data, Failed, InvalidData, Record, as_record
if TYPE_CHECKING:
    from .service import ProviderService


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class ConfirmedCompletion:
    """Native original operation tied to an unchanged Provider terminal.

    This confirms local commit identity only. It makes no assertion about cost
    completeness or ended consumers and cannot authorize a replacement request.
    """
    terminal: VerifiedTerminal
    operation: MappingProxyType[str, Data]

    def __init__(self):
        raise TypeError('Only the original Provider confirms its completion.')


def issued_completion(value: object) -> bool:
    """Reject copied, fabricated and cross-issuer completion carriers."""
    if type(value) is not ConfirmedCompletion:
        return False
    try:
        terminal = object.__getattribute__(value, 'terminal')
    except AttributeError:
        return False
    return (issued_terminal(terminal)
            and terminal._provider._completion_evidence.get(id(value)) is value)


def _allowed(service: ProviderService, port: ResultOwnerPort, terminal: object) -> bool:
    if not service._text or not issued_terminal(terminal):
        return False
    assert type(terminal) is VerifiedTerminal
    grant = service._ports.get(port)
    return (terminal._provider is service and type(grant) is ResultGrant
            and terminal.database_id == service._ledger.database_id
            and terminal.request['object_id'] in grant.request_ids
            and terminal.request['result_owner'] == grant.owner_id
            and terminal.request['format_version'] == 2
            and terminal.request['phase'] == 'TERMINAL')


async def confirm_completion(service: ProviderService, port: ResultOwnerPort, terminal: object):
    """Consume one shared reader slot until all actual child reads have ended."""
    from .service import error
    operation = 'recover_result'
    denied = service._access(port, operation, (ResultOwnerPort,))
    if denied is not None:
        return Failed(denied)
    if not _allowed(service, port, terminal):
        return Failed(error('ACCESS_DENIED', operation, 'capability', 'CAPABILITY_MISMATCH'))
    if (len(service._lookup_tasks) >= service._number('max_in_flight')
            or len(service._completion_evidence) >= service._number('max_in_flight')):
        return Failed(error('RESOURCE_BUSY', operation, 'state', 'ADMISSION_BUSY'))
    assert type(terminal) is VerifiedTerminal
    timeout = service._number('request_timeout_ms') / 1000
    task = asyncio.create_task(finish_owned(_confirm_owned(service, terminal, time.monotonic() + timeout)))
    service._lookup_tasks.add(task)
    task.add_done_callback(service._lookup_finished)
    done, _ = await asyncio.wait((task,), timeout=timeout)
    if not done:
        return Failed(error('TIMEOUT', operation, 'request', 'DEADLINE_EXCEEDED', True))
    denied = service._access(port, operation, (ResultOwnerPort,))
    if denied is not None:
        return Failed(denied)
    if not _allowed(service, port, terminal):
        return Failed(error('ACCESS_DENIED', operation, 'capability', 'CAPABILITY_MISMATCH'))
    key = task.result()
    if type(key) is Failed:
        return key
    value = object.__new__(ConfirmedCompletion)
    object.__setattr__(value, 'terminal', terminal)
    object.__setattr__(value, 'operation', key)
    service._completion_evidence[id(value)] = value
    return value


async def _confirm_owned(service: ProviderService, terminal: VerifiedTerminal, deadline: float) -> Record | Failed:
    from .service import derived_id
    try:
        with DeadlineScope(deadline):
            request = await service._ledger.get('requests', cast(str, terminal.request['object_id']))
            if request != terminal.request:
                raise InvalidData()
            assert request is not None
            request_id = cast(str, request['object_id'])
            attempts = await service._ledger.read('attempts_for_request', {'request_id': request_id})
            if len(attempts) != request['attempt_count'] or len(attempts) > 1:
                raise InvalidData()
            from .text_stored_schema import terminal_reason
            from .terminal_evidence import matches_text_attempt
            if (terminal_reason(request, attempts)!=terminal.terminal_reason
                    or not matches_text_attempt(terminal, attempts[0] if attempts else None)):raise InvalidData()
            # These are the complete fixed terminal-producing paths, never a
            # guessed key chosen from the request revision or a history scan.
            candidates = [('register', 'register-' + request_id),
                          ('terminate', 'terminate-' + request_id),
                          ('terminate', 'recover-terminal-' + request_id)]
            if attempts:
                attempt = attempts[0]
                if attempt['request_id'] != request_id or attempt['ordinal'] != 1:
                    raise InvalidData()
                for kind in ('settle', 'evidence'):
                    candidates.append((kind, kind + '-' + cast(str, attempt['object_id']) + '-' + str(attempt['evidence_revision'])))
            matches: list[Record] = []
            for kind, original in candidates:
                key = derived_id(kind, original)
                result = await service._ledger.operations[kind].read_receipt(key)
                if type(result) is StorageNotFound:
                    continue
                if type(result) is not StorageFound:
                    raise LedgerFailure(result)
                receipt = result.value
                root = as_record(receipt.result)
                if root != {'object_id': request_id, 'revision': request['revision']}:
                    continue
                identity = receipt.identity
                if (identity.database_id != terminal.database_id or identity.owner_namespace != 'provider'
                        or identity.scope_id != 'provider' or identity.operation_kind != kind
                        or identity.operation_key != key or receipt.command_version != 2):
                    raise InvalidData()
                matches.append(MappingProxyType({'owner_namespace': identity.owner_namespace,
                    'operation_kind': identity.operation_kind, 'scope_id': identity.scope_id,
                    'operation_key': identity.operation_key}))
            if len(matches) != 1 or await service._ledger.get('requests', request_id) != request:
                raise InvalidData()
            return matches[0]
    except (LedgerFailure, InvalidData) as failure:
        return service._read_failure('recover_result', failure)
