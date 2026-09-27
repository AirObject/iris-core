"""Publish the operator's initial role locally without manufacturing model evidence.

The immutable memory-owned input is the sole source. Publication, its receipt
and the current pointer commit together. No model run may be abandoned by this
path, and an existing publication can never be replaced.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from companion_memory.persistence import Found, NotFound
from companion_memory.persistence.daily_records import BASE, DailyRows, DailyTable, identity
from companion_memory.persistence.daily_results import result, target
from companion_memory.persistence.schema import BoundedTextSchema, InvalidValue, RecordSchema
from companion_memory.persistence.semantic_records import ID, P, H, fields, enum, Record
from companion_memory.persistence.text_records import OPERATION, digest, stable_identity
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.deadlines import DeadlineScope

if TYPE_CHECKING:
    from .daily_persona import DailyPersona

PUBLICATION = RecordSchema(BASE + fields(input_id=ID, input_digest=H,
    self_subject_id=ID, self_revision=P, role_name=BoundedTextSchema(256),
    text=BoundedTextSchema(4096), text_digest=H, publication_origin=enum('LOCAL_DEFAULT'),
    model_origin=enum('LOCAL_CONFIGURATION'), review_status=enum('USER_CONFIRMED'),
    publication_operation=OPERATION))
LOCAL_TABLE = DailyTable('local_persona_publications', (PUBLICATION,), 8192, False)


def role_text(label: str, body: str) -> str:
    """Keep the confirmed name and complete initial material as explicit settings."""
    return '名称：' + label + '\n\n初始设定：' + body


class LocalPersona:
    """Share the generated-persona owner, its lease and original receipt verifier."""

    def __init__(self, owner: DailyPersona):
        self.owner = owner

    def rows(self) -> DailyRows:
        owner = self.owner
        config = owner.configuration
        return DailyRows(owner.catalog, (LOCAL_TABLE,), owner.storage,
            config.database_id, config.scope_id, config.snapshot_id)

    def publication_id(self) -> str:
        config = self.owner.configuration
        return identity('local-persona-publication', config.database_id, config.scope_id)

    def handle(self, uow, values, now: int, operation: Record):
        """Publish only from normal mode with no existing generated-persona run."""
        owner = self.owner
        mode = owner.admit(uow)
        if mode['state'] != 'NORMAL':
            raise OwnerFailure('MODE_BLOCKED', 'persona', 'PERSONA_WORK_PENDING')
        run_id = stable_identity('persona-run', owner.configuration.database_id, owner.configuration.scope_id)
        if owner.owner.read(uow, 'run', run_id) is not None:
            raise OwnerFailure('PRECONDITION_FAILED', 'persona', 'PERSONA_WORK_PENDING')
        if owner.owner.current_publication(uow) is not None:
            raise OwnerFailure('PRECONDITION_FAILED', 'persona', 'ALREADY_PUBLISHED')
        if self.rows().get('local_persona_publications', uow, self.publication_id()) is not None:
            raise OwnerFailure('PRECONDITION_FAILED', 'persona', 'ALREADY_PUBLISHED')
        source = owner.initial.participate_initial(uow, values['input_id'])
        if source.subject['revision'] != values['expected_self_revision']:
            raise OwnerFailure('PRECONDITION_FAILED', 'self', 'REVISION_CONFLICT')
        # Synthetic test imports cannot acquire a real operator-confirmed origin.
        if source.input['input_origin'] != 'ACTUAL_INPUT':
            raise OwnerFailure('ACCESS_DENIED', 'input', 'BINDING_MISMATCH')
        label, body = cast(str, source.subject['label']), cast(str, source.input['body'])
        if not label.strip() or not body.strip():
            raise OwnerFailure('INVALID_INPUT', 'input', 'INVALID_SHAPE')
        config = owner.configuration
        text = role_text(label, body)
        publication = self.rows().write('local_persona_publications', uow, {
            'format_version': 1, 'object_id': self.publication_id(), 'revision': 1,
            'database_id': config.database_id, 'instance_id': config.scope_id,
            'config_snapshot_id': config.snapshot_id, 'created_at_us': now, 'updated_at_us': now,
            **{name: source.input[name] for name in ('input_digest', 'self_subject_id', 'self_revision')},
            'input_id': source.input['object_id'], 'role_name': label, 'text': text, 'text_digest': digest(text),
            'publication_origin': 'LOCAL_DEFAULT', 'model_origin': 'LOCAL_CONFIGURATION',
            'review_status': 'USER_CONFIRMED', 'publication_operation': operation,
        })
        from .unified_persona import initialize_pointer
        pointer = initialize_pointer(owner, uow, self.publication_id(), 'LOCAL_DEFAULT', operation, now)
        facts: dict[str, object] = {'self_model': {'rows_changed': 2, 'targets': (target(self.publication_id(), 1), target(pointer, 1))}}
        return MappingProxyType(dict(result(values['operation_id'], 'PUBLISHED_LOCAL', facts)) | {
            'records': (MappingProxyType({'object_id': publication['object_id'], 'revision': 1,
                'digest': digest(publication)}),)})

    @staticmethod
    def projection(publication: Record, stale: bool) -> Record:
        if publication['text_digest'] != digest(publication['text']):
            raise InvalidValue()
        return MappingProxyType({'publication_id': publication['object_id'], 'revision': publication['revision'],
            'text': publication['text'], 'generated_at_us': publication['created_at_us'],
            'review': 'USER_CONFIRMED', 'model_origin': 'LOCAL_CONFIGURATION',
            'publication_origin': 'LOCAL_DEFAULT', 'stale': stale})

    async def read_current(self, deadline: float):
        """Verify the retained command and actual SELF before returning local text."""
        if self.owner.closed or not self.owner.bound:
            raise OwnerFailure('INVALID_STATE', 'persona', 'NOT_READY')
        with DeadlineScope(deadline):
            return await self._read_current(deadline)

    async def _read_current(self, deadline: float):
        publication = await self.rows().read('local_persona_publications', self.publication_id())
        if publication is None:
            return NotFound()
        await self.owner.verify_record_original(publication['publication_operation'], publication)
        stale = await self.owner.initial.publication_stale_original(publication, deadline)
        if self.owner.closed:
            raise OwnerFailure('INVALID_STATE', 'persona', 'NOT_READY')
        return Found(self.projection(publication, stale))

    def participate_current(self, uow, publication_id: str, revision: int):
        if publication_id != self.publication_id() or revision != 1:
            return None
        publication = self.rows().get('local_persona_publications', uow, publication_id)
        if publication is None:
            return None
        receipt = self.owner.confirm_local(uow, publication['publication_operation'])
        self.owner.verify_record_receipt(receipt, publication)
        return self.projection(publication, self.owner.initial.publication_stale(uow, publication))
