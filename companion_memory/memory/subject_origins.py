"""Memory-owned provenance for actual internal subject creation.

A creation retains its real frozen source through a SUBJECT holder. Reuse of an
existing platform identity is not a creation and must not insert an origin.
"""
from companion_memory.persistence.daily_records import BASE,DailyTable,Field,RecordSchema,ID,OPERATION,IndexSpec,daily_catalog
from companion_memory.persistence.schema import SequenceSchema
from .formats import ANCHOR_SCHEMA

ORIGIN=RecordSchema(BASE+(Field('subject_id',ID),Field('candidate_id',ID),Field('batch_id',ID),Field('source_id',ID),
    Field('target_anchors',SequenceSchema(ANCHOR_SCHEMA,1,2)),Field('created_operation',OPERATION)))
TABLE=DailyTable('subject_origins',(ORIGIN,),4096,False,(IndexSpec('by_subject',('subject_id',)),))

def subject_origin_catalog():
    """Include the complete body in the daily immutable memory declaration."""
    return daily_catalog('memory',5,(TABLE,))
