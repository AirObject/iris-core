"""Memory's semantic coverage tables and indexed floor/ack observations.

The catalog extends only a new semantic memory assembly. Lexical sequence and
ack tables remain distinct and are never deleted by semantic application.
"""
from dataclasses import replace
from companion_memory.persistence.semantic_catalog import SemanticTable,catalog,projection,BODY_ROW
from companion_memory.persistence.semantic_records import ID,N,P,H,record
from companion_memory.persistence import StatementDefinition,TableDefinition,BoundedTextSchema
from companion_memory.persistence.owned_statements import StatementCatalog
from .semantic_schema import GAP,ACK,PUBLICATION,validate

TABLES=(
    SemanticTable('semantic_gap',(GAP,),lambda v:validate('semantic_gap',v),
        (projection('space_id'),projection('object_id'),projection('first_uncovered_seq',numeric=True)),
        ('UNIQUE(scope_id,space_id,object_id)',),removable=True),
    SemanticTable('semantic_ack',(ACK,),lambda v:validate('semantic_ack',v),
        (projection('space_id'),projection('object_id'),projection('artifact_id')),
        ('UNIQUE(scope_id,space_id,object_id)',)),
    SemanticTable('semantic_publication',(PUBLICATION,),lambda v:validate('semantic_publication',v),
        (projection('space_id'),),('UNIQUE(scope_id,space_id)',)),
)


def semantic_memory_catalog() -> StatementCatalog:
    result=catalog('memory',TABLES)
    extra=(
        ('semantic_floor',StatementDefinition('SELECT min(first_uncovered_seq) AS minimum,count(*) AS count FROM memory_semantic_gap WHERE scope_id=:scope_id AND space_id=:space_id',record(space_id=ID),record(minimum=(P,),count=N),False)),
        ('semantic_ack_objects',StatementDefinition('SELECT row_id,revision,body FROM memory_semantic_ack WHERE scope_id=:scope_id AND space_id=:space_id AND object_id>:after ORDER BY object_id LIMIT 8',record(space_id=ID,after=BoundedTextSchema(128)),BODY_ROW,False)),
    )
    index=TableDefinition('memory_semantic_floor_index','CREATE INDEX memory_semantic_floor_index ON memory_semantic_gap(scope_id,space_id,first_uncovered_seq)')
    statements=result.statements+extra
    return StatementCatalog(replace(result.definition,tables=result.definition.tables+(index,),statements=tuple(s for _,s in statements)),statements)
