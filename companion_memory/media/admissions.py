"""Verified unsent occurrence admissions and immutable prior-request conclusions."""
from __future__ import annotations
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import Value
from companion_memory.persistence.content_codec import decode_content, encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.provider.unsent_evidence import VerifiedUnsent, issued_unsent
from companion_memory.provider.values import freeze, as_record


def close_unsent(owner, ingress, uow, work, evidence, now):
    """Only native owner-isolated no-send proof can retire PROCESSING protection."""
    if not issued_unsent(evidence) or type(evidence) is not VerifiedUnsent:
        raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
    descriptor = as_record(freeze(decode_content(work['original_request_descriptor'].encode(), 8192), 8192))
    if (work['phase'] not in ('READY_TO_REQUEST', 'REQUEST_ASSOCIATED', 'REMOTE_UNKNOWN')
        or evidence._provider._ledger.storage is not owner.storage or evidence.database_id != owner.configuration.database_id or evidence.caller_scope != owner.instance_id
        or evidence.caller_module != 'media' or evidence.result_owner != 'media' or evidence.capability != 'MEDIA_UNDERSTANDING'
        or evidence.original_request != descriptor or evidence.request_id != work['provider_request_id']):
        raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
    owner.rows.stage('admissions_insert', uow, {'work_id': work['work_id'], 'admission_generation': work['admission_generation'],
        'preparation_id': work['admission_preparation_id'], 'operation_key': work['original_operation_key'],
        'request_id': work['provider_request_id'], 'descriptor': work['original_request_descriptor'], 'conclusion': evidence.conclusion, 'closed_at_us': now})
    owner.rows.stage('work_update', uow, {**work, 'phase': 'WAITING_ADMISSION', 'revision': work['revision'] + 1,
        'request_association_state': 'UNSENT_CONFIRMED', 'last_observed_at_us': max(now, work['last_observed_at_us']),
        'spent_ms': max(work['spent_ms'], max(0, now - work['started_at_us']) // 1000)})
    owner._reference(uow, work['blob_id'], work['generation'], 'PROCESSING', work['work_id'], work['occurrence_id'], False, now)
    payload = ingress.event(uow, work['message_id'])
    ingress.release_payload(uow, work['message_id'], 'PROCESSING', work['work_id'], payload['references_revision'])


def readmit(owner, ingress, uow, work, preparation, now):
    """A different authorized preparation may admit only a proven unsent generation."""
    from .service import identity
    if (work['phase'] != 'WAITING_ADMISSION' or work['request_association_state'] != 'UNSENT_CONFIRMED'
        or work['admission_preparation_id'] == preparation['preparation_id']):
        raise OwnerFailure('PRECONDITION_FAILED', 'state', 'WORK_FENCED')
    prior = owner.rows.stage('admissions_get', uow, {'work_id': work['work_id'], 'admission_generation': work['admission_generation']})
    if not prior or prior[0]['operation_key'] != work['original_operation_key'] or prior[0]['descriptor'] != work['original_request_descriptor']:
        raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
    if owner.rows.stage('processing_count', uow, {})[0]['count'] >= owner.settings.integer('media.processing_concurrency'):
        raise OwnerFailure('RESOURCE_BUSY', 'state', 'ADMISSION_FULL')
    occurrence = owner._get('occurrences', uow, 'occurrence_id', work['occurrence_id'])
    if (occurrence['state'] != 'ATTACHED' or occurrence['generation'] != work['generation']
        or owner.work.reusable(uow, occurrence, work['authorization_domain_id'], work['interpretation_fingerprint'], work['prompt_revision']) is not None):
        raise OwnerFailure('PRECONDITION_FAILED', 'interpretation', 'SELECTION_CHANGED')
    admission = work['admission_generation'] + 1
    key = identity('media_request', work['work_id'], admission)
    original = cast(dict, decode_content(work['original_request_descriptor'].encode(), 8192))
    descriptor = as_record(freeze({**original, 'operation_key': key, 'run_id': preparation['run_id']}, 8192))
    updated = {**work, 'phase': 'READY_TO_REQUEST', 'revision': work['revision'] + 1,
        'admission_generation': admission, 'admission_preparation_id': preparation['preparation_id'], 'owner_generation': preparation['owner_generation'],
        'original_operation_key': key, 'original_request_descriptor': encode_content(cast(Value, descriptor), 4096).decode(), 'provider_request_id': None,
        'request_association_state': 'LOOKUP_REQUIRED', 'started_at_us': now, 'last_observed_at_us': now, 'spent_ms': 0,
        'deadline_at_us': now + owner.settings.integer('media.occurrence_total_timeout_ms') * 1000}
    owner.rows.stage('work_update', uow, updated)
    owner._reference(uow, work['blob_id'], work['generation'], 'PROCESSING', work['work_id'], work['occurrence_id'], True, now)
    ingress.retain_payload(uow, work['message_id'], 'PROCESSING', work['work_id'])
