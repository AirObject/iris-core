"""Dedicated approved-origin record and local publication pointer.

The local publication refers to the import. It carries no new Provider request
or candidate review and leaves the original generation publication untouched.
"""
from companion_memory.persistence.daily_records import BASE,DailyTable,Field,RecordSchema,ID,REVISION,DIGEST,OPERATION,enum,IndexSpec,daily_catalog
from companion_memory.persistence.schema import BoundedTextSchema
from companion_memory.persistence.text_records import extend_catalog
from .repository import persona_catalog

IMPORT=RecordSchema(BASE+(Field('original_database_id',ID),Field('original_publication_id',ID),Field('original_candidate_id',ID),
    Field('original_candidate_revision',REVISION),Field('original_candidate_digest',DIGEST),Field('original_text_digest',DIGEST),
    Field('original_text_utf8_digest',DIGEST),Field('review_evidence_digest',DIGEST),Field('import_grant_id',ID),
    Field('self_subject_id',ID),Field('text',BoundedTextSchema(1024)),Field('import_operation',OPERATION)))
PUBLICATION=RecordSchema(BASE+(Field('import_id',ID),Field('self_subject_id',ID),Field('self_revision',REVISION),
    Field('publication_origin',enum('IMPORTED_APPROVED')),Field('model_origin',enum('REMOTE_PROVIDER')),
    Field('approval_ref',BoundedTextSchema(512)),Field('publication_operation',OPERATION)))
IMPORT_TABLE=DailyTable('persona_imports',(IMPORT,),4096,False,(IndexSpec('by_instance',('scope_id',)),))
PUBLICATION_TABLE=DailyTable('persona_publications',(PUBLICATION,),4096,False)

def import_catalog():
    """Extend the original owner; its unique publication is the current pointer."""
    from dataclasses import replace
    from companion_memory.persistence.owned_statements import StatementCatalog
    from .formats import PUBLICATION as GENERATED_PUBLICATION,RUN,CANDIDATE
    catalog=extend_catalog(persona_catalog(),daily_catalog('self_model',2,(IMPORT_TABLE,)),2)
    shapes={'self_model_persona_publications':(GENERATED_PUBLICATION,PUBLICATION),
        'self_model_initial_persona_runs':(RUN,),'self_model_initial_persona_candidates':(CANDIDATE,)}
    definition=replace(catalog.definition,tables=tuple(replace(t,record_schemas=shapes[t.name]) if t.name in shapes else t for t in catalog.definition.tables))
    from companion_memory.persistence import StatementDefinition
    shared=[]
    for table in ('initial_persona_runs','initial_persona_candidates'):
        shared.append(('provider_read_'+table,StatementDefinition('SELECT object_id,revision,body FROM self_model_'+table+
            " WHERE :scope_id='provider' AND scope_id=:caller_scope AND object_id=:object_id",
            RecordSchema((Field('caller_scope',ID),Field('object_id',ID))),
            RecordSchema((Field('object_id',ID),Field('revision',REVISION),Field('body',BoundedTextSchema(4096)))),False)))
    statements=catalog.statements+tuple(shared)
    return StatementCatalog(replace(definition,statements=tuple(s for _,s in statements)),statements)
