"""Actual occurrence-work owner and immutable Provider result application.

Original request descriptors and PROCESSING references commit before dispatch.
Only a native audited terminal can produce internal results or a content guard.
Ordinary oversized success becomes a separately bounded fixed failure; sensitive
refusal ignores variable response text and can never become an ordinary failure.
"""
from __future__ import annotations
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from companion_memory.persistence import UnitOfWork, Value
from companion_memory.persistence.schema import InvalidValue, ValueTooLarge
from companion_memory.persistence.content_codec import encode_content, decode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.memory.formats import record
from companion_memory.provider.terminal_evidence import VerifiedTerminal, issued_terminal
from .interpretations import isolate_interpretation, decode_interpretation, REFUSAL_TEXT
if TYPE_CHECKING:
    from .service import MediaService
    from companion_memory.ingress.content_storage import ContentIngressTransactions


class MediaWorkTransactions:
    """Media-only writes with explicit typed ingress participation for raw recovery."""
    def __init__(self, media: MediaService):
        self.media = media
        self.evidence: dict[str, VerifiedTerminal] = {}
        self.unsent_evidence = {}

    def guard_key(self, uow: UnitOfWork, occurrence: MappingProxyType[str, Value], domain: str) -> str:
        """Derive one authorization-domain protection key from actual blob facts."""
        from .service import identity
        blob = self.media._blob(uow, cast(str, occurrence['blob_id']))
        return identity('media_guard', self.media.instance_id, domain, blob['sha256'], blob['byte_count'],
            occurrence['modality'], 'TRANSCRIBE' if occurrence['modality'] == 'AUDIO' else 'DESCRIBE')

    def reusable(self, uow: UnitOfWork, occurrence: MappingProxyType[str, Value], domain: str, fingerprint: str, prompt: str) -> tuple[MappingProxyType[str, Value], str] | None:
        """External choice wins; internal guard precedes completed content cache."""
        from .service import identity
        owner = self.media
        if occurrence['interpretation_id'] is not None:
            value = decode_interpretation(cast(str, owner._get('interpretations', uow, 'interpretation_id', occurrence['interpretation_id'])['body']).encode())
            if value['origin'] == 'EXTERNAL': return value, 'ORIGINAL'
        guards = owner.rows.stage('guards_get', uow, {'guard_key': self.guard_key(uow, occurrence, domain)})
        if guards:
            value = decode_interpretation(cast(str, owner._get('interpretations', uow, 'interpretation_id', guards[0]['interpretation_id'])['body']).encode())
            if value['origin'] != 'INTERNAL' or value['status'] != 'REFUSED' or value['provider_request_id'] != guards[0]['provider_request_id']:
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            return value, 'PROTECTED_REFUSAL'
        if occurrence['interpretation_id'] is not None:
            return decode_interpretation(cast(str, owner._get('interpretations', uow, 'interpretation_id', occurrence['interpretation_id'])['body']).encode()), 'ORIGINAL'
        key = identity('interpretation_cache', self.guard_key(uow, occurrence, domain), fingerprint, prompt, 'CONTENT')
        cached = owner.rows.stage('content_cache_get', uow, {'cache_key': key})
        if cached:
            value = decode_interpretation(cast(str, owner._get('interpretations', uow, 'interpretation_id', cached[0]['interpretation_id'])['body']).encode())
            if (value['origin'], value['scope_kind'], value['scope_id'], value['interpretation_fingerprint'], value['prompt_revision']) != ('INTERNAL', 'CONTENT', domain, fingerprint, prompt) or value['status'] not in ('COMPLETE', 'EMPTY'):
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            return value, 'CONTENT_REUSE'
        return None

    def select(self, uow: UnitOfWork, occurrence: MappingProxyType[str, Value], interpretation_id: str, kind: str) -> int:
        """Create an immutable selection version, leaving frozen old selections intact."""
        revision = cast(int, occurrence['selection_revision']) + 1
        if revision >= 2**63: raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        self.media.rows.stage('selections_insert', uow, {'occurrence_id': occurrence['occurrence_id'], 'selection_revision': revision,
            'interpretation_id': interpretation_id, 'generation': occurrence['generation'], 'selection_kind': kind})
        self.media.rows.stage('occurrences_update', uow, {**occurrence, 'interpretation_id': interpretation_id, 'selection_revision': revision})
        return revision

    def register(self, uow: UnitOfWork, ingress: ContentIngressTransactions, occurrence_id: str, run_id: str,
                 domain: str, profile: str, prompt: str, fingerprint: str, generation: int, now_us: int, preparation_id: str) -> MappingProxyType[str, Value]:
        """Create one durable byte-free original request and both recovery protections."""
        from .service import identity
        owner = self.media; occurrence = owner._get('occurrences', uow, 'occurrence_id', occurrence_id)
        if occurrence['state'] != 'ATTACHED': raise OwnerFailure('PRECONDITION_FAILED', 'media', 'BLOB_NOT_READY')
        if self.reusable(uow, occurrence, domain, fingerprint, prompt) is not None:
            raise OwnerFailure('PRECONDITION_FAILED', 'interpretation', 'SELECTION_CHANGED')
        count = owner.rows.stage('processing_count', uow, {})[0]['count']
        if cast(int, count) >= owner.settings.integer('media.processing_concurrency'):
            raise OwnerFailure('RESOURCE_BUSY', 'state', 'ADMISSION_FULL')
        task = 'TRANSCRIBE' if occurrence['modality'] == 'AUDIO' else 'DESCRIBE'
        wid = identity('occurrence_work', owner.configuration.database_id, occurrence_id, task)
        blob = owner._blob(uow, cast(str, occurrence['blob_id']))
        key = identity('media_request', wid, 1)
        artifact = identity('media_artifact', owner.configuration.database_id, wid, blob['blob_id'], blob['generation'])
        descriptor = MappingProxyType({'operation_key': key, 'run_id': run_id, 'profile_id': profile,
            'entry_ids': (occurrence['entry_id'],), 'parent_request_id': None, 'trace_id': None, 'batch_id': None,
            'dream_run_id': None, 'prompt_revision': prompt,
            'payload': MappingProxyType({'media': MappingProxyType({'artifact_id': artifact, 'owner_id': 'media', 'scope': owner.instance_id,
                'byte_count': blob['byte_count'], 'sha256': blob['sha256']}), 'modality': occurrence['modality'], 'task': task})})
        previous = owner.rows.stage('previous_failure', uow, {'blob_id': blob['blob_id'], 'authorization_domain_id': domain, 'task': task})
        row = MappingProxyType({'work_id': wid, 'occurrence_id': occurrence_id, 'entry_id': occurrence['entry_id'], 'message_id': occurrence['message_id'],
            'task': task, 'modality': occurrence['modality'], 'authorization_domain_id': domain, 'blob_id': blob['blob_id'], 'generation': blob['generation'],
            'profile_id': profile, 'prompt_revision': prompt, 'interpretation_fingerprint': fingerprint, 'config_snapshot_id': owner.configuration.snapshot_id,
            'revision': 1, 'owner_generation': generation, 'phase': 'READY_TO_REQUEST', 'admission_generation': 1, 'admission_preparation_id': preparation_id,
            'original_request_descriptor': encode_content(descriptor, 4096).decode(), 'original_operation_key': key,
            'provider_request_id': None, 'original_result_owner': 'media', 'request_association_state': 'LOOKUP_REQUIRED',
            'started_at_us': now_us, 'deadline_at_us': now_us + owner.settings.integer('media.occurrence_total_timeout_ms') * 1000,
            'spent_ms': 0, 'last_observed_at_us': now_us, 'selection_revision': occurrence['selection_revision'], 'interpretation_id': None,
            'previous_failure': previous[0]['work_id'] if previous else None, 'terminal_status': None})
        # All interpretation envelope attribution must fit before any dispatch.
        probe = {'interpretation_version': 1, 'interpretation_id': identity('interpretation', wid), 'blob_id': row['blob_id'], 'generation': row['generation'],
            'task': task, 'modality': row['modality'], 'origin': 'INTERNAL', 'status': 'REFUSED', 'text': REFUSAL_TEXT, 'coverage': 'UNSPECIFIED',
            'source_ref': 'R' * 128, 'interpretation_fingerprint': fingerprint, 'prompt_revision': prompt, 'scope_kind': 'CONTENT', 'scope_id': domain,
            'event_id': None, 'provider_request_id': 'R' * 128, 'created_at_us': 2**63 - 1, 'failure_reason': None}
        isolate_interpretation(probe, text_limit=owner.settings.integer('media.interpretation_text_max_bytes'), record_limit=owner.settings.integer('media.interpretation_record_max_bytes'))
        owner.rows.stage('work_insert', uow, dict(row))
        owner._reference(uow, cast(str, blob['blob_id']), cast(int, blob['generation']), 'PROCESSING', wid, occurrence_id, True, now_us)
        ingress.retain_payload(uow, cast(str, occurrence['message_id']), 'PROCESSING', wid)
        return row

    def retain_terminal(self, evidence: object) -> None:
        """Retain at most one native same-database media terminal per active slot."""
        if not issued_terminal(evidence): raise ValueError('Native Provider terminal evidence required.')
        assert type(evidence) is VerifiedTerminal
        request = evidence.request
        if (evidence._provider._ledger.storage is not self.media.storage or evidence.database_id != self.media.configuration.database_id
                or request['caller_module'] != 'media' or request['extension_id'] is not None or request['caller_scope'] != self.media.instance_id
                or request['result_owner'] != 'media' or request['capability'] != 'MEDIA_UNDERSTANDING' or request['task_role'] != 'MEDIA'
                or evidence.original_request is None): raise ValueError('The original media result binding must match.')
        if cast(str, request['object_id']) in self.evidence: return
        if len(self.evidence) >= self.media.settings.integer('media.processing_concurrency'): raise ValueError('Media terminal capacity is occupied.')
        self.evidence[cast(str, request['object_id'])] = evidence

    def store(self, uow: UnitOfWork, work_id: str, request_id: str, expected_revision: int, now_us: int) -> MappingProxyType[str, Value]:
        """Save one immutable interpretation and sensitive protection atomically."""
        from .service import identity
        owner = self.media; work = owner._get('work', uow, 'work_id', work_id)
        if work['revision'] != expected_revision or work['phase'] not in ('READY_TO_REQUEST', 'REQUEST_ASSOCIATED', 'REMOTE_UNKNOWN'):
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'WORK_FENCED')
        proof = self.evidence.get(request_id)
        if not issued_terminal(proof): raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        assert type(proof) is VerifiedTerminal
        from companion_memory.provider.values import freeze, as_record
        descriptor = as_record(freeze(decode_content(cast(str, work['original_request_descriptor']).encode(), 8192), 8192))
        if proof.original_request != descriptor or proof.request['operation_key'] != work['original_operation_key'] or proof.request['phase'] != 'TERMINAL':
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        if not proof.confirmed_sent or proof.request['outcome'] == 'MODE_BLOCKED':
            raise OwnerFailure('RESOURCE_BUSY', 'state', 'WAITING_ADMISSION')
        request = proof.request; sensitive = request['outcome'] == 'SENSITIVE_REFUSAL' and proof.terminal_reason == 'SENSITIVE_INFORMATION'
        interpretation: dict[str, Value] = {'interpretation_version': 1, 'interpretation_id': identity('interpretation', work_id),
            'blob_id': work['blob_id'], 'generation': work['generation'], 'task': work['task'], 'modality': work['modality'], 'origin': 'INTERNAL',
            'status': 'REFUSED' if sensitive else 'FAILED', 'text': REFUSAL_TEXT if sensitive else None, 'coverage': 'UNSPECIFIED',
            'source_ref': request_id, 'interpretation_fingerprint': work['interpretation_fingerprint'], 'prompt_revision': work['prompt_revision'],
            'scope_kind': 'CONTENT', 'scope_id': work['authorization_domain_id'], 'event_id': None, 'provider_request_id': request_id,
            'created_at_us': now_us, 'failure_reason': None if sensitive else 'OTHER_REFUSAL' if request['outcome'] == 'OTHER_REFUSAL' else 'PROVIDER_FAILURE'}
        if request['outcome'] == 'SUCCEEDED':
            response = as_record(proof.result); text = response['text']
            if type(text) is not str: raise InvalidValue()
            interpretation.update(status='EMPTY' if text == '' else 'COMPLETE', text=text, coverage='COMPLETE', source_ref=cast(str, request['handoff_id']), failure_reason=None)
        try:
            value = isolate_interpretation(interpretation, text_limit=owner.settings.integer('media.interpretation_text_max_bytes'), record_limit=owner.settings.integer('media.interpretation_record_max_bytes'))
        except InvalidValue as failure:
            if sensitive: raise
            interpretation.update(status='FAILED', text=None, coverage='UNSPECIFIED', source_ref=request_id,
                failure_reason='RESULT_LIMIT_EXCEEDED' if type(failure) is ValueTooLarge else 'RESULT_INVALID')
            value = isolate_interpretation(interpretation, text_limit=owner.settings.integer('media.interpretation_text_max_bytes'), record_limit=owner.settings.integer('media.interpretation_record_max_bytes'))
        owner.rows.stage('interpretations_insert', uow, {'interpretation_id': value['interpretation_id'], 'blob_id': value['blob_id'], 'body': encode_content(value, 2048).decode()})
        occurrence = owner._get('occurrences', uow, 'occurrence_id', work['occurrence_id'])
        guard_key = self.guard_key(uow, occurrence, cast(str, work['authorization_domain_id']))
        if sensitive:
            blob = owner._blob(uow, cast(str, work['blob_id']))
            existing = owner.rows.stage('guards_get', uow, {'guard_key': guard_key})
            if not existing:
                owner.rows.stage('guards_insert', uow, {'guard_key': guard_key, 'authorization_domain_id': work['authorization_domain_id'],
                    'sha256': blob['sha256'], 'byte_count': blob['byte_count'], 'modality': work['modality'], 'task': work['task'],
                    'interpretation_id': value['interpretation_id'], 'provider_request_id': request_id, 'evidence_ref': request_id,
                    'operation_ref': identity('media_terminal', work_id, request_id)})
        elif value['status'] in ('COMPLETE', 'EMPTY'):
            cache_key = identity('interpretation_cache', guard_key, work['interpretation_fingerprint'], work['prompt_revision'], 'CONTENT')
            if not owner.rows.stage('content_cache_get', uow, {'cache_key': cache_key}):
                owner.rows.stage('content_cache_insert', uow, {'cache_key': cache_key, 'interpretation_id': value['interpretation_id']})
        chosen = self.reusable(uow, occurrence, cast(str, work['authorization_domain_id']), cast(str, work['interpretation_fingerprint']), cast(str, work['prompt_revision']))
        selected = chosen[0] if chosen is not None else value
        revision = self.select(uow, occurrence, cast(str, selected['interpretation_id']), chosen[1] if chosen is not None else 'ORIGINAL')
        updated = {**work, 'terminal_status': value['status'], 'provider_request_id': request_id, 'request_association_state': 'RESULT_CONFIRMED', 'phase': 'RESULT_STORED',
            'revision': cast(int, work['revision']) + 1, 'interpretation_id': value['interpretation_id'], 'selection_revision': revision,
            'last_observed_at_us': now_us, 'spent_ms': max(cast(int, work['spent_ms']), (now_us - cast(int, work['started_at_us'])) // 1000)}
        owner.rows.stage('work_update', uow, updated)
        return value

    def release_processing(self, uow: UnitOfWork, ingress: ContentIngressTransactions, work_id: str, now_us: int) -> None:
        """Release recovery refs only after Provider consumers and file workers end."""
        owner = self.media; work = owner._get('work', uow, 'work_id', work_id)
        if work['phase'] != 'RESULT_STORED': raise OwnerFailure('PRECONDITION_FAILED', 'state', 'WORK_FENCED')
        owner._reference(uow, cast(str, work['blob_id']), cast(int, work['generation']), 'PROCESSING', work_id, cast(str, work['occurrence_id']), False, now_us)
        payload = ingress.event(uow, cast(str, work['message_id']))
        ingress.release_payload(uow, cast(str, work['message_id']), 'PROCESSING', work_id, cast(int, payload['references_revision']))
