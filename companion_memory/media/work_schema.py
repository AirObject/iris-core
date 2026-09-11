"""Closed durable occurrence work, immutable selections and content protection.

An occurrence has one original analysis intent regardless of preparation window.
Byte-free request descriptors preserve original lookup and result attribution.
"""
from companion_memory.persistence import Field, BoundedTextSchema
from companion_memory.memory.formats import ID, INT, REVISION, enum

WORK_LAYOUTS = {
    'work_descriptors': (('work_id', 'admission_generation'), (Field('work_id', ID), Field('admission_generation', REVISION), Field('body', BoundedTextSchema(4096)))),
    'work': (('work_id',), (Field('work_id', ID), Field('occurrence_id', ID), Field('entry_id', ID), Field('message_id', ID),
        Field('task', enum('DESCRIBE', 'TRANSCRIBE')), Field('modality', enum('IMAGE', 'AUDIO', 'VIDEO')),
        Field('authorization_domain_id', ID), Field('blob_id', ID), Field('generation', REVISION),
        Field('profile_id', ID), Field('prompt_revision', BoundedTextSchema(64)), Field('interpretation_fingerprint', BoundedTextSchema(64)),
        Field('config_snapshot_id', ID), Field('revision', REVISION), Field('owner_generation', REVISION),
        Field('phase', enum('READY_TO_REQUEST', 'REQUEST_ASSOCIATED', 'RESULT_STORED', 'WAITING_ADMISSION', 'PARKED', 'REMOTE_UNKNOWN')),
        Field('admission_generation', REVISION), Field('admission_preparation_id', ID), Field('original_operation_key', ID),
        Field('provider_request_id', ID, nullable=True), Field('original_result_owner', ID), Field('request_association_state', ID),
        Field('started_at_us', INT), Field('deadline_at_us', INT), Field('spent_ms', INT), Field('last_observed_at_us', INT),
        Field('selection_revision', REVISION), Field('interpretation_id', ID, nullable=True),
        Field('previous_failure', ID, nullable=True), Field('terminal_status', enum('COMPLETE', 'EMPTY', 'FAILED', 'REFUSED'), nullable=True))),
    'admissions': (('work_id', 'admission_generation'), (Field('work_id', ID), Field('admission_generation', REVISION), Field('preparation_id', ID),
        Field('operation_key', ID), Field('request_id', ID, nullable=True),
        Field('conclusion', enum('REGISTRATION_ABSENT', 'ZERO_ATTEMPT_ADMISSION_TERMINAL')), Field('closed_at_us', INT))),
    'selections': (('occurrence_id', 'selection_revision'), (Field('occurrence_id', ID), Field('selection_revision', REVISION),
        Field('interpretation_id', ID), Field('generation', REVISION), Field('selection_kind', enum('ORIGINAL', 'CONTENT_REUSE', 'PROTECTED_REFUSAL')))),
    'guards': (('guard_key',), (Field('guard_key', ID), Field('authorization_domain_id', ID), Field('sha256', BoundedTextSchema(64)),
        Field('byte_count', REVISION), Field('modality', enum('IMAGE', 'AUDIO', 'VIDEO')), Field('task', enum('DESCRIBE', 'TRANSCRIBE')),
        Field('interpretation_id', ID), Field('provider_request_id', ID), Field('evidence_ref', ID), Field('operation_ref', ID))),
    'content_cache': (('cache_key',), (Field('cache_key', ID), Field('interpretation_id', ID))),
}
