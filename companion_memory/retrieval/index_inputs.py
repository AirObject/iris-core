"""Finite index step payloads; no SQL, expression or unbounded object list."""
from companion_memory.persistence import Field, RecordSchema, SequenceSchema
from companion_memory.information.records import ID, REVISION, BOOL, TEXT, choice

BEGIN = RecordSchema((Field('expected_generation', ID, nullable=True),))
CLAIM = RecordSchema((Field('generation_id', ID), Field('expected_revision', REVISION), Field('owner_id', ID)))
APPLY = RecordSchema((Field('generation_id', ID), Field('page_id', ID), Field('expected_revision', REVISION),
    Field('object_id', ID), Field('object_revision', REVISION), Field('action', choice('UPSERT', 'REMOVE')),
    Field('body_digest', TEXT(64)), Field('normalized_text', TEXT(8192))))
CONFIRM = RecordSchema((Field('generation_id', ID), Field('page_id', ID), Field('expected_revision', REVISION), Field('scan_complete', BOOL)))
PUBLISH = RecordSchema((Field('generation_id', ID), Field('expected_revision', REVISION)))
FAIL = RecordSchema(PUBLISH.fields)
RETIRE = RecordSchema((Field('generation_id', ID), Field('page_id', ID, nullable=True), Field('expected_revision', REVISION, nullable=True),
    Field('objects', SequenceSchema(ID, 1, 16))))
SCHEMAS = {'index_begin': BEGIN, 'index_claim': CLAIM, 'index_apply_object': APPLY, 'index_confirm_page': CONFIRM,
    'index_publish': PUBLISH, 'index_fail': FAIL, 'index_retire_page': RETIRE, 'index_trim_page': RETIRE}
