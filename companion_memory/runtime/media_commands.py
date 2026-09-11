"""Runtime preparation checks and fixed media/ingress transaction participation.

The runtime proves the original claimed occurrence membership. Media owns the
request/selection/guard records, and ingress owns independent recovery protection.
Commands contain no file paths, raw bytes, model calls or arbitrary statements.
"""
from __future__ import annotations
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from companion_memory.persistence import AuditFieldBinding, AuditResultBinding, BoundedTextSchema, Field, RecordSchema, ResultBoundCommandDefinition, UnitOfWork, Value
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.logging_service import AuditRequirement
from companion_memory.memory.formats import ID, INT, REVISION, record, sequence, enum
from .preparation_format import decode_preparation
if TYPE_CHECKING:
    from .content_assembly import ContentAssembly


class MediaCommands:
    """Closed local operations for one persistent occurrence and original result."""
    def __init__(self, content: ContentAssembly):
        from .content_assembly import owner_fact, FACT, RESULT, result_schema
        self.content = content
        layouts = (
            ('change_occurrence_admission', ('media', 'ingress'), (Field('work_id', ID), Field('expected_revision', REVISION), Field('action', enum('CLOSE', 'REOPEN')), Field('preparation_id', ID))),
            ('register_occurrence_work', ('media', 'ingress'), (Field('preparation_id', ID), Field('owner_generation', REVISION), Field('occurrence_id', ID),
                Field('authorization_domain_id', ID), Field('profile_id', ID), Field('prompt_revision', BoundedTextSchema(64)), Field('interpretation_fingerprint', BoundedTextSchema(64)))),
            ('reuse_occurrence_interpretation', ('media',), (Field('preparation_id', ID), Field('owner_generation', REVISION), Field('occurrence_id', ID),
                Field('authorization_domain_id', ID), Field('prompt_revision', BoundedTextSchema(64)), Field('interpretation_fingerprint', BoundedTextSchema(64)))),
            ('associate_occurrence_request', ('media',), (Field('work_id', ID), Field('request_id', ID), Field('expected_revision', REVISION))),
            ('store_occurrence_result', ('media',), (Field('work_id', ID), Field('request_id', ID), Field('expected_revision', REVISION))),
            ('release_occurrence_processing', ('media', 'ingress'), (Field('work_id', ID),)),
        )
        definitions = []
        for name, owners, fields in layouts:
            audits = tuple(AuditRequirement(owner, owner + '_content', name.upper(), 1, ('APPLY',), owner_fact(owner)) for owner in owners)
            bindings = tuple(AuditResultBinding(a.event_slot, 1, (
                AuditFieldBinding('actor_kind', 'CONSTANT', constant='SYSTEM'), AuditFieldBinding('actor_ref', 'INTENT', ('actor',)),
                AuditFieldBinding('reason_code', 'CONSTANT', constant='APPLY'), AuditFieldBinding('target_refs', 'RESULT', ('targets',)),
                AuditFieldBinding('change', 'RESULT', ('facts', a.owner_module)),
            )) for a in audits)
            def handler(uow, values, kind=name): return self.content.run_handler(kind, uow, values, self.handle)
            definitions.append(ResultBoundCommandDefinition('runtime', name, 1, RecordSchema((Field('operation_id', ID),) + fields),
                1, result_schema(owners), content.repositories, audits, handler, RecordSchema((Field('actor', ID),)), bindings))
        self.commands = tuple(definitions)

    def handle(self, name: str, uow: UnitOfWork, value: MappingProxyType[str, Value]):
        from companion_memory.media.service import MediaService
        content = self.content; media = content.media
        value = content.with_transaction_time(value)
        if type(media) is not MediaService: raise InvalidValue()
        media = cast(MediaService, media)
        if name == 'change_occurrence_admission':
            from companion_memory.media.admissions import close_unsent, readmit
            work = media._get('work', uow, 'work_id', value['work_id'])
            if work['revision'] != value['expected_revision']:
                raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'WORK_FENCED')
            if value['action'] == 'CLOSE':
                proof = media.work.unsent_evidence.pop(work['work_id'], None)
                close_unsent(media, content.ingress, uow, work, proof, value['now_us'])
            else:
                prep = content._get('preparations', uow, 'preparation_id', value['preparation_id'])
                mode = content._get('mode', uow, 'mode_id', 'instance_mode')
                if (mode['state'] not in ('NORMAL', 'DRAINING') or prep['phase'] != 'CLAIMED' or prep['mode_epoch'] != mode['epoch']
                    or cast(int, value['now_us']) >= cast(int, prep['deadline_at_us']) or cast(int, value['now_us']) < cast(int, prep['last_observed_at_us'])):
                    raise OwnerFailure('MODE_BLOCKED', 'state', 'DREAMING')
                members = sequence(decode_preparation(cast(str, prep['manifest']))['ordered_members'])
                if work['occurrence_id'] not in {cast(str, record(s)['occurrence_id']) for m in members for s in sequence(record(m)['media'])}:
                    raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
                readmit(media, content.ingress, uow, work, prep, value['now_us'])
            return content._result(uow, value, 'WAITING_ADMISSION' if value['action'] == 'CLOSE' else 'READY_TO_REQUEST', cast(str, work['entry_id']),
                audit={'media': {'provenance': 'PROVIDER_CONFIRMED', 'reused': False}})
        if name in ('register_occurrence_work', 'reuse_occurrence_interpretation'):
            preparation = content._get('preparations', uow, 'preparation_id', value['preparation_id'])
            mode = content._get('mode', uow, 'mode_id', 'instance_mode')
            if (preparation['phase'] not in (('CLAIMED', 'MEDIA_READY') if name == 'reuse_occurrence_interpretation' else ('CLAIMED',)) or preparation['owner_generation'] != value['owner_generation']
                    or preparation['mode_epoch'] != mode['epoch'] or mode['state'] not in ('NORMAL', 'DRAINING')
                    or cast(int, value['now_us']) >= cast(int, preparation['deadline_at_us'])
                    or cast(int, value['now_us']) < cast(int, preparation['last_observed_at_us'])):
                raise OwnerFailure('PRECONDITION_FAILED', 'state', 'WORK_FENCED')
            manifest = decode_preparation(cast(str, preparation['manifest']))
            if value['occurrence_id'] not in {cast(str, record(s)['occurrence_id']) for m in sequence(manifest['ordered_members']) for s in sequence(record(m)['media'])}:
                raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
            if name == 'register_occurrence_work':
                work = media.work.register(uow, content.ingress, cast(str, value['occurrence_id']), cast(str, preparation['run_id']),
                    cast(str, value['authorization_domain_id']), cast(str, value['profile_id']), cast(str, value['prompt_revision']),
                    cast(str, value['interpretation_fingerprint']), cast(int, value['owner_generation']), cast(int, value['now_us']), cast(str, preparation['preparation_id']))
                return content._result(uow, value, 'READY_TO_REQUEST', cast(str, preparation['entry_id']), operation_id=work['work_id'])
            occurrence = media._get('occurrences', uow, 'occurrence_id', value['occurrence_id'])
            reusable = media.work.reusable(uow, occurrence, cast(str, value['authorization_domain_id']), cast(str, value['interpretation_fingerprint']), cast(str, value['prompt_revision']))
            if reusable is None: raise OwnerFailure('PRECONDITION_FAILED', 'interpretation', 'SELECTION_CHANGED')
            interpreted, kind = reusable
            media.work.select(uow, occurrence, cast(str, interpreted['interpretation_id']), kind)
            return content._result(uow, value, 'RESULT_STORED', cast(str, occurrence['entry_id']), operation_id=interpreted['interpretation_id'],
                audit={'media': {'provenance': 'EXTERNAL_REPORT' if interpreted['origin'] == 'EXTERNAL' else 'PROVIDER_CONFIRMED',
                    'guard_created': False, 'reused': True, 'references': tuple(MappingProxyType({'name': k, 'object_id': item, 'revision': None})
                        for k, item in (('occurrence', occurrence['occurrence_id']), ('interpretation', interpreted['interpretation_id']), ('selection_kind', kind), ('interpretation_status', interpreted['status'])))}})
        work = media._get('work', uow, 'work_id', value['work_id'])
        if name == 'associate_occurrence_request':
            if work['revision'] != value['expected_revision'] or work['provider_request_id'] is not None or work['phase'] != 'READY_TO_REQUEST':
                raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'WORK_FENCED')
            media.rows.stage('work_update', uow, {**work, 'revision': cast(int, work['revision']) + 1,
                'provider_request_id': value['request_id'], 'phase': 'REQUEST_ASSOCIATED', 'request_association_state': 'ID_CONFIRMED'})
            return content._result(uow, value, 'REQUEST_ASSOCIATED', cast(str, work['entry_id']))
        if name == 'store_occurrence_result':
            occurrence = media._get('occurrences', uow, 'occurrence_id', work['occurrence_id'])
            protected_before = bool(media.rows.stage('guards_get', uow, {'guard_key': media.work.guard_key(uow, occurrence, cast(str, work['authorization_domain_id']))}))
            result = media.work.store(uow, cast(str, value['work_id']), cast(str, value['request_id']), cast(int, value['expected_revision']), cast(int, value['now_us']))
            return content._result(uow, value, 'RESULT_STORED', cast(str, work['entry_id']), operation_id=result['interpretation_id'], audit={'media': {
                'references': tuple(MappingProxyType({'name': key, 'object_id': value, 'revision': None}) for key, value in (('work', work['work_id']), ('occurrence', work['occurrence_id']), ('interpretation', result['interpretation_id']), ('request', result['provider_request_id']), ('authorization_domain', work['authorization_domain_id']), ('interpretation_status', result['status']), ('failure_reason', result['failure_reason'])) if value is not None),
                'provenance': 'PROVIDER_CONFIRMED', 'guard_created': result['status'] == 'REFUSED' and not protected_before, 'reused': False}})
        if name == 'release_occurrence_processing':
            media.work.release_processing(uow, content.ingress, cast(str, value['work_id']), cast(int, value['now_us']))
            return content._result(uow, value, 'PROCESSING_RELEASED', cast(str, work['entry_id']))
        raise InvalidValue()
