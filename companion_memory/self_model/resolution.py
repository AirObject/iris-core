"""Derive a first-persona candidate from its confirmed original Provider result.

The owner reconstitutes the retained input and generation resources before using
any generated text. The returned run and candidate require one caller-owned
transaction; no function here grants review, publication, retry or model work.
"""
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from companion_memory.configuration.text_persistence import StoredTextConfiguration, stored_text_configuration_issue
from companion_memory.cognition.text_context import normalized_request_digest
from companion_memory.cognition.text_output import isolate_initial_persona
from companion_memory.cognition.text_resources import output_schema
from companion_memory.memory.initial_self import isolate_initial_self
from companion_memory.persistence.schema import InvalidValue, Value, freeze_value
from companion_memory.persistence.text_records import UINT, digest, stable_identity
from companion_memory.provider.chat_protocol import ChatBinding, validate_structured_result
from companion_memory.provider.completion_evidence import ConfirmedCompletion, issued_completion
from companion_memory.provider.values import as_record
from .formats import isolate_candidate, isolate_run, candidate_digest
from .request_material import request_material
from .transitions import _operation, matching_generation


@dataclass(frozen=True, slots=True)
class ResolvedPersona:
    """Complete immutable proposal and its matching run CAS, never partial text."""
    run: MappingProxyType[str, Value]
    candidate: MappingProxyType[str, Value]


def resolve_known(configuration: StoredTextConfiguration, initial: object, run: object,
                  completion: ConfirmedCompletion, operation: object, now_us: int) -> ResolvedPersona:
    """Validate actual terminal, original material and known business resolution.

    Missing or mismatched native evidence is an integrity failure. Valid protocol
    output that fails the persona schema becomes a retained known failure. An
    unknown remote result or an unregistered no-send proof uses neither branch.
    """
    if stored_text_configuration_issue(configuration) is not None or not issued_completion(completion):
        raise InvalidValue()
    source = isolate_initial_self(initial); current = isolate_run(run)
    freeze_value(UINT, now_us)
    key = _operation(operation, 'record_initial_persona_resolution', current['instance_id'])
    terminal = completion.terminal; request = terminal.request; attribution = as_record(request['attribution'])
    instance = cast(str, current['instance_id']); database = configuration.database_id
    if (current['state'] not in ('REQUEST_ASSOCIATED', 'REMOTE_UNKNOWN') or now_us < cast(int, current['updated_at_us'])
            or current['database_id'] != database or terminal.database_id != database
            or current['config_snapshot_id'] != configuration.snapshot_id or source['object_id'] != current['input_id']
            or source['object_id'] != stable_identity('self-input', database, instance)
            or any(source[name] != current[name] for name in ('database_id','instance_id','config_snapshot_id','input_digest','self_subject_id','self_revision'))
            or current['object_id'] != stable_identity('persona-run', database, instance)):
        raise InvalidValue()
    if (request['caller_module'] != 'self_model' or request['caller_scope'] != instance
            or request['task_role'] != 'PERSONA' or request['capability'] != 'GENERATION' or request['result_owner'] != 'self_model'
            or request['source'] != 'REMOTE_PROVIDER' or request['config_snapshot_id'] != configuration.snapshot_id
            or request['account_id'] != current['account_id']
            or as_record(as_record(request['execution_evidence'])['account'])['window_id'] != current['window_id']
            or request['operation_key'] != current['provider_operation_key'] or request['object_id'] != current['provider_request_id']
            or attribution['run_id'] != current['object_id'] or attribution['entry_ids'] != () or attribution['batch_id'] is not None
            or terminal.original_request is None):
        raise InvalidValue()
    material = request_material(configuration, source, cast(str, current['object_id']), cast(int, current['generation']), cast(str, current['provider_operation_key']))
    if (digest(material.binding) != current['binding_digest']
            or normalized_request_digest(terminal.original_request) != material.binding['request_digest']):
        raise InvalidValue()
    settings = configuration.candidate.text.record('self_model.initial_persona')
    generation = configuration.candidate.text.record('provider.generation')
    if any(current[name] != settings[name] for name in ('prompt_ref','schema_ref','transform_ref')):
        raise InvalidValue()
    resolution = 'KNOWN_FAILED'; reason = 'INVALID_RESPONSE'; text: Value = None
    if request['outcome'] == 'SUCCEEDED':
        binding = ChatBinding(cast(str, generation['model_id']), cast(tuple[str,...], generation['expected_reported_models']),
            cast(str | None, generation['resolved_model_id']), cast(str, settings['schema_ref']), cast(str, settings['schema_digest']),
            'initial_persona', output_schema('PERSONA'))
        # Protocol damage invalidates the retained evidence; business-schema
        # rejection alone may create a known failed generation.
        structured = validate_structured_result(terminal.result, binding)
        try:
            output = isolate_initial_persona(structured['output'], cast(str, source['object_id']))
            text = output['text']; resolution = 'SUCCEEDED'; reason = 'NONE'
        except InvalidValue:
            pass
    elif request['outcome'] in ('OTHER_REFUSAL','CANCELLED','TIMED_OUT','PAUSED_BUDGET','MODE_BLOCKED','CONFIGURATION_REJECTED'):
        reason = cast(str, request['outcome'])
    elif request['outcome'] in ('FAILED','UNSUPPORTED_CAPABILITY'):
        if terminal.terminal_reason == 'AUTHENTICATION_FAILED':
            reason = 'AUTHENTICATION_FAILED'
        elif terminal.terminal_reason == 'OUTPUT_LIMIT':
            reason = 'OUTPUT_LIMIT'
    else:
        raise InvalidValue()
    candidate_id = stable_identity('persona-candidate', database, instance, current['object_id'], current['generation'])
    candidate = isolate_candidate({'format_version':1,'object_id':candidate_id,'revision':1,'database_id':database,
        'instance_id':instance,'config_snapshot_id':configuration.snapshot_id,'created_at_us':current['created_at_us'],
        'run_id':current['object_id'],'generation':current['generation'],'provider_operation_key':current['provider_operation_key'],
        'provider_request_id':request['object_id'],'handoff_id':request['handoff_id'],'terminal_receipt':completion.operation,
        'resolution':resolution,'failure_reason':reason,'text':text,'text_digest':digest(text) if text is not None else None,
        'input_id':source['object_id'],'input_digest':source['input_digest'],'binding_digest':current['binding_digest'],
        'review':'PENDING' if resolution == 'SUCCEEDED' else 'NOT_APPLICABLE','reviewed_by':None,'reviewed_at_us':None,'review_operation':None})
    updated = isolate_run({**current,'revision':cast(int,current['revision'])+1,'resolution_id':candidate_id,
        'state':'WAITING_REVIEW' if resolution == 'SUCCEEDED' else 'KNOWN_FAILED','last_operation':key,'updated_at_us':now_us})
    return ResolvedPersona(updated, candidate)


def verify_resolved(configuration: StoredTextConfiguration,initial: object,run: object,candidate: object,
                    completion: ConfirmedCompletion,now_us: int) -> None:
    """Compare retained immutable resolution to the same native original result.

    Review metadata stays outside candidate identity and is checked by the review
    owner. This comparison projection never writes a state or repeats a request.
    """
    current=isolate_run(run);retained=isolate_candidate(candidate)
    matching_generation(current,retained)
    reconstruction={**current,'state':'REQUEST_ASSOCIATED','resolution_id':None,'publication_id':None}
    key={'owner_namespace':'self_model','operation_kind':'record_initial_persona_resolution',
        'scope_id':current['instance_id'],'operation_key':'verify-retained-resolution'}
    rebuilt=resolve_known(configuration,initial,reconstruction,completion,key,now_us)
    if candidate_digest(rebuilt.candidate)!=candidate_digest(retained):raise InvalidValue()


def resolve_unsent(configuration: StoredTextConfiguration,initial: object,run: object,
                   evidence: object,operation: object,now_us: int) -> ResolvedPersona:
    """Retain no-send absence using this actual local resolution operation.

    The caller also checks the seal inside its transaction. This pure derivation
    verifies complete frozen material and cannot manufacture a Provider record.
    NONE means absence of a known failure reason, never fabricated cancellation.
    """
    from companion_memory.provider.unsent_evidence import VerifiedUnsent,issued_unsent
    from companion_memory.provider.service import OPTIONALS
    from .preparation import retained_material
    if (stored_text_configuration_issue(configuration) is not None or type(evidence) is not VerifiedUnsent
            or not issued_unsent(evidence) or evidence.conclusion!='REGISTRATION_ABSENT' or evidence.request_id is not None):raise InvalidValue()
    source=isolate_initial_self(initial);current=isolate_run(run);freeze_value(UINT,now_us)
    key=_operation(operation,'record_initial_persona_resolution',current['instance_id'])
    material=retained_material(configuration,source,current)
    original={**{name:material.request.get(name) for name in OPTIONALS},**material.request}
    if (current['state']!='REQUEST_ASSOCIATED' or current['provider_request_id'] is not None
            or current['database_id']!=evidence.database_id or evidence.caller_module!='self_model'
            or evidence.caller_scope!=current['instance_id'] or evidence.result_owner!='self_model' or evidence.capability!='GENERATION'
            or evidence.original_request!=original or now_us<cast(int,current['updated_at_us'])):raise InvalidValue()
    candidate_id=stable_identity('persona-candidate',configuration.database_id,cast(str,current['instance_id']),current['object_id'],current['generation'])
    candidate=isolate_candidate({'format_version':1,'object_id':candidate_id,'revision':1,'database_id':current['database_id'],
        'instance_id':current['instance_id'],'config_snapshot_id':configuration.snapshot_id,'created_at_us':current['created_at_us'],
        'run_id':current['object_id'],'generation':current['generation'],'provider_operation_key':current['provider_operation_key'],
        'provider_request_id':None,'handoff_id':None,'terminal_receipt':key,'resolution':'NOT_SENT','failure_reason':'NONE',
        'text':None,'text_digest':None,'input_id':source['object_id'],'input_digest':source['input_digest'],
        'binding_digest':current['binding_digest'],'review':'NOT_APPLICABLE','reviewed_by':None,'reviewed_at_us':None,'review_operation':None})
    updated=isolate_run({**current,'revision':cast(int,current['revision'])+1,'resolution_id':candidate_id,
        'state':'KNOWN_FAILED','last_operation':key,'updated_at_us':now_us})
    return ResolvedPersona(updated,candidate)


def verify_unsent_resolution(configuration: StoredTextConfiguration,initial: object,run: object,candidate: object,
                             evidence: object,now_us: int) -> None:
    """Rebuild the immutable absent resolution using its own actual local key."""
    current=isolate_run(run);retained=isolate_candidate(candidate);matching_generation(current,retained)
    reconstruction={**current,'state':'REQUEST_ASSOCIATED','resolution_id':None,'publication_id':None}
    rebuilt=resolve_unsent(configuration,initial,reconstruction,evidence,retained['terminal_receipt'],now_us)
    if candidate_digest(rebuilt.candidate)!=candidate_digest(retained):raise InvalidValue()
