"""Closed self-model errors, independent of storage and Provider outcomes."""
from dataclasses import dataclass
from companion_memory.persistence.owned_statements import OwnerFailure

OPERATIONS=frozenset(('register_initial_self','prepare_initial_persona','associate_initial_persona_request',
    'confirm_initial_persona_request','record_initial_persona_resolution','review_initial_persona','retry_initial_persona',
    'publish_initial_persona','read_current','verify_current','recover_local'))
COMBINATIONS={
    'shape':('INVALID_INPUT','input','INVALID_SHAPE'),
    'boundary':('ACCESS_DENIED','identity','BOUNDARY_DENIED'),
    'revision':('PRECONDITION_FAILED','revision','REVISION_CONFLICT'),
    'state':('PRECONDITION_FAILED','state','STATE_MISMATCH'),
    'binding':('INTEGRITY_FAILURE','identity','BINDING_MISMATCH'),
    'record':('INTEGRITY_FAILURE','storage','RECORD_INVALID'),
    'busy':('RESOURCE_BUSY','resource','CLEANUP_PENDING'),
    'unconfirmed':('PERSISTENCE_FAILED','storage','COMMIT_UNCONFIRMED'),
}


@dataclass(frozen=True,slots=True)
class SelfModelError:
    code: str
    operation: str
    field: str
    reason: str
    def __post_init__(self):
        if self.operation not in OPERATIONS or (self.code,self.field,self.reason) not in COMBINATIONS.values():
            raise ValueError('A closed self-model error combination is required.')


@dataclass(frozen=True,slots=True)
class Rejected:
    error: SelfModelError
    cleanup_pending: bool=False


@dataclass(frozen=True,slots=True)
class Failed:
    error: SelfModelError
    cleanup_pending: bool=False


def rejected(operation: str,category: str,cleanup_pending: bool=False):
    # Orchestration and pending-candidate reads retain their owning operation's
    # error identity; neither introduces another public error operation.
    selected={'generate':'record_initial_persona_resolution','read_pending':'read_current'}.get(operation,operation)
    code,field,reason=COMBINATIONS[category]
    error=SelfModelError(code,selected,field,reason)
    envelope=Failed if selected in ('read_current','verify_current','recover_local') else Rejected
    return envelope(error,cleanup_pending)


def owner_failure(operation: str,failure: OwnerFailure):
    category=('boundary' if failure.code=='ACCESS_DENIED' else 'shape' if failure.code=='INVALID_INPUT'
        else 'busy' if failure.code in ('RESOURCE_BUSY','TIMEOUT') else 'revision' if failure.field=='revision'
        else 'record' if failure.code in ('INTEGRITY_FAILURE','STORAGE_FAILED') else 'state')
    return rejected(operation,category,failure.cleanup_pending)
