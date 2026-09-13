"""Original no-send conclusions park learning without consuming frozen targets."""
from __future__ import annotations
import hashlib
from typing import cast
from companion_memory.persistence import Committed, Found, Value
from companion_memory.persistence.content_codec import encode_content, decode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.provider.unsent_evidence import VerifiedUnsent, UnsentVerified, issued_unsent
from companion_memory.provider.service import OPTIONALS
from companion_memory.provider.values import freeze, as_record
from companion_memory.memory.formats import record


def descriptor_digest(request):
    """The complete normalized original request, excluding volatile wait controls."""
    body = {key: request.get(key) for key in OPTIONALS}
    body.update({key: request[key] for key in ('operation_key', 'run_id', 'profile_id', 'payload', 'entry_ids')})
    return hashlib.sha256(encode_content(cast(Value, freeze(body, 65536, owned=True)), 65536)).hexdigest()


def close_admission(assembly, uow, work, batch, value):
    """Native Provider isolation proves the complete retained original descriptor."""
    from .content_assembly import stable
    proof = assembly._unsent_learning.pop(work['batch_id'], None)
    binding = as_record(freeze(decode_content(work['model_binding'].encode(), 8192), 8192))
    from companion_memory.cognition.text_context import normalized_request_digest
    descriptor=normalized_request_digest if assembly.text_format else descriptor_digest
    if (type(proof) is not VerifiedUnsent or not issued_unsent(proof)
            or work['phase'] != 'REQUEST_ASSOCIATED'
            or proof._provider._ledger.storage is not assembly.storage or proof.database_id != assembly.configuration.database_id or proof.caller_scope != assembly.instance_id
            or proof.caller_module != 'cognition' or proof.result_owner != 'cognition' or proof.capability != 'GENERATION'
            or proof.request_id != work['provider_request_id']
            or descriptor(proof.original_request) != binding.get('request_digest')):
        raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
    mode = assembly._get('mode', uow, 'mode_id', 'instance_mode')
    assembly.rows.stage('learning_admissions_insert', uow, {
        'admission_id': stable('learning_admission', work['batch_id'], work['admission_generation']),
        'batch_id': work['batch_id'], 'admission_generation': work['admission_generation'],
        'operation_key': work['provider_operation_key'], 'request_id': work['provider_request_id'],
        'request_digest': binding['request_digest'], 'conclusion': proof.conclusion,
        'closed_at_us': value['now_us'], 'mode_epoch': mode['epoch']})
    assembly.rows.stage('work_update', uow, {**work, 'phase': 'WAITING_ADMISSION', 'revision': work['revision'] + 1})
    return assembly._result(uow, value, 'WAITING_ADMISSION', batch['entry_id'], batch_id=work['batch_id'])


def reopen_admission(assembly, uow, work, batch, value):
    """A distinct explicit event or resumed mode admits only the closed generation."""
    from .content_assembly import stable
    mode = assembly._get('mode', uow, 'mode_id', 'instance_mode')
    prior = assembly._get('learning_admissions', uow, 'admission_id', stable('learning_admission', work['batch_id'], work['admission_generation']))
    if (mode['state'] not in ('NORMAL', 'DRAINING') or work['phase'] != 'WAITING_ADMISSION'
            or value['expected_epoch'] != mode['epoch'] or work['provider_operation_key'] != prior['operation_key']
            or (value['trigger_key'] == work['admission_trigger'] and mode['epoch'] <= prior['mode_epoch'])):
        raise OwnerFailure('PRECONDITION_FAILED', 'state', 'WORK_FENCED')
    trigger = assembly.rows.stage('learning_triggers_get', uow, {'trigger_key': value['trigger_key']})
    if trigger and trigger[0]['batch_id'] != work['batch_id']:
        raise OwnerFailure('IDEMPOTENCY_CONFLICT', 'state', 'CONTENT_MISMATCH')
    if not trigger:
        assembly.rows.stage('learning_triggers_insert', uow, {'trigger_key': value['trigger_key'], 'batch_id': work['batch_id']})
    generation = work['admission_generation'] + 1
    binding=None
    if assembly.text_format:
        from companion_memory.memory.sources import decode_source
        # The first context and all of its hashes remain immutable. Only the
        # work's current admission descriptor changes in this same transaction.
        original_binding=decode_content(work['model_binding'].encode(),8192)
        if (prior['request_digest']!=as_record(freeze(original_binding,8192))['request_digest'] or prior['request_id']!=work['provider_request_id']):
            raise OwnerFailure('PRECONDITION_FAILED','state','WORK_FENCED')
        context=assembly.text_transactions.original(uow,decode_source(batch['manifest']),work,original_binding)
        binding=encode_content(assembly.text_transactions.work_binding(context,generation),8192).decode()
    assembly.rows.stage('work_update', uow, {**work, 'phase': 'FROZEN', 'revision': work['revision'] + 1,
        'admission_generation': generation, 'admission_trigger': value['trigger_key'],
        'provider_operation_key': None, 'provider_request_id': None, 'model_binding': binding})
    return assembly._result(uow, value, 'FROZEN', batch['entry_id'], batch_id=work['batch_id'])


async def conclude_unsent(runtime, work, port, request):
    """Confirm local no-send storage before treating this owner as safely parked."""
    from .content_assembly import stable
    from types import MappingProxyType
    proof = await port.verify_unsent('generate', request)
    if type(proof) is not UnsentVerified: return None
    runtime.assembly._unsent_learning[work['batch_id']] = proof.value
    try:
        result = await runtime.execute('close_learning_admission', stable('close_learning_admission', work['batch_id'], work['admission_generation']),
            {'batch_id': work['batch_id'], 'generation': work['generation'], 'expected_revision': work['revision']})
    finally: runtime.assembly._unsent_learning.pop(work['batch_id'], None)
    if type(result) is not Committed: return result
    return Found(MappingProxyType({'state': 'WAITING_ADMISSION', 'remote_result': 'NOT_SENT', 'cleanup_pending': False}))
