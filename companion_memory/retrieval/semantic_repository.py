"""Retrieval's semantic work and artifact tables with real relational constraints.

Network work has a separate unique original request key. Input leaves physically
require EMBED parents; deletion rows cannot acquire a request or vector link.
"""
from companion_memory.persistence.semantic_catalog import SemanticTable,catalog,projection,BODY_ROW
from companion_memory.persistence.semantic_records import ID,H,N,record
from companion_memory.persistence import StatementDefinition,BoundedTextSchema
from dataclasses import replace
from companion_memory.persistence.owned_statements import StatementCatalog
from .semantic_schema import (EMBED_WORK,DELETE_WORK,SCHEMAS,validate)


def _table(name: str, projections, constraints, *, mutable=True, removable=False):
    return SemanticTable(name,(EMBED_WORK,DELETE_WORK) if name=='embedding_work' else (SCHEMAS[name],),
        lambda value:validate(name,value),tuple(projections),tuple(constraints),mutable,removable)


TABLES=(
    _table('embedding_work',(projection('work_id'),projection('kind'),projection('config_instance','config.instance_id'),projection('original_request_key')),
        ('UNIQUE(scope_id,work_id)','UNIQUE(scope_id,work_id,kind)','UNIQUE(scope_id,config_instance,original_request_key)',
         "CHECK(kind IN ('EMBED','DELETE_LOCAL'))",
         "CHECK(kind!='DELETE_LOCAL' OR (original_request_key IS NULL AND json_type(body,'$.intent') IS NULL AND json_type(body,'$.artifact_id') IS NULL AND json_type(body,'$.request_ref') IS NULL))")),
    _table('embedding_input_leaf',(projection('work_id'),projection('ordinal',numeric=True),('parent_kind','TEXT',"'EMBED'")),
        ('UNIQUE(scope_id,work_id,ordinal)',"CHECK(ordinal BETWEEN 0 AND 2)","CHECK(revision=1)",
         'FOREIGN KEY(scope_id,work_id,parent_kind) REFERENCES retrieval_embedding_work(scope_id,work_id,kind)'),mutable=False),
    _table('embedding_artifact',(projection('artifact_id'),projection('request_id','request_ref.request_id')),
        ('UNIQUE(scope_id,artifact_id)','UNIQUE(scope_id,request_id)','CHECK(revision=1)'),mutable=False),
    _table('embedding_vector_leaf',(projection('artifact_id'),projection('ordinal',numeric=True)),
        ('UNIQUE(scope_id,artifact_id,ordinal)','CHECK(ordinal BETWEEN 0 AND 1)','CHECK(revision=1)',
         'FOREIGN KEY(scope_id,artifact_id) REFERENCES retrieval_embedding_artifact(scope_id,artifact_id)'),mutable=False),
    _table('semantic_generation',(projection('generation_id'),projection('space_id'),projection('file_name')),
        ('UNIQUE(scope_id,generation_id)','UNIQUE(scope_id,space_id,file_name)')),
    _table('semantic_member',(projection('generation_id'),projection('ordinal',numeric=True),projection('object_id'),projection('artifact_id')),
        ('UNIQUE(scope_id,generation_id,ordinal)','UNIQUE(scope_id,generation_id,object_id)','CHECK(ordinal BETWEEN 0 AND 4095)',
         'FOREIGN KEY(scope_id,generation_id) REFERENCES retrieval_semantic_generation(scope_id,generation_id)',
         'FOREIGN KEY(scope_id,artifact_id) REFERENCES retrieval_embedding_artifact(scope_id,artifact_id)'),mutable=False,removable=True),
    _table('semantic_page',(projection('generation_id'),projection('page_no',numeric=True)),
        ('UNIQUE(scope_id,generation_id,page_no)','CHECK(page_no BETWEEN 0 AND 511)',
         'FOREIGN KEY(scope_id,generation_id) REFERENCES retrieval_semantic_generation(scope_id,generation_id)'),removable=True),
    _table('semantic_control',(projection('space_id'),),('UNIQUE(scope_id,space_id)',)),
    _table('query_embedding_cache',(projection('space_id'),projection('partition_id'),projection('cache_key'),projection('artifact_id')),
        ('UNIQUE(scope_id,space_id,partition_id,cache_key)',
         'FOREIGN KEY(scope_id,artifact_id) REFERENCES retrieval_embedding_artifact(scope_id,artifact_id)'),removable=True),
)


def semantic_retrieval_catalog() -> StatementCatalog:
    base=catalog('retrieval',TABLES)
    extra=(
        ('semantic_work_counts',StatementDefinition("SELECT json_extract(body,'$.state') AS state,count(*) AS count,max(json_extract(body,'$.cleanup_pending')) AS cleanup_pending FROM retrieval_embedding_work WHERE scope_id=:scope_id GROUP BY json_extract(body,'$.state')",record(),record(state=ID,count=N,cleanup_pending=N),False)),
        ('semantic_cache_counts',StatementDefinition("SELECT json_extract(body,'$.state') AS state,count(*) AS count FROM retrieval_query_embedding_cache WHERE scope_id=:scope_id GROUP BY json_extract(body,'$.state')",record(),record(state=ID,count=N),False)),
        ('semantic_artifact_matching',StatementDefinition("SELECT row_id,revision,body FROM retrieval_embedding_artifact WHERE scope_id=:scope_id AND json_extract(body,'$.space_id')=:space_id AND json_extract(body,'$.purpose')=:purpose AND json_extract(body,'$.partition_id')=:partition_id AND json_extract(body,'$.material_digest')=:material_digest ORDER BY row_id LIMIT 1",record(space_id=ID,purpose=ID,partition_id=ID,material_digest=H),BODY_ROW,False)),
        ('provider_received_artifact',StatementDefinition("SELECT row_id,revision,body FROM retrieval_embedding_artifact WHERE :scope_id='provider' AND scope_id=:caller_scope AND artifact_id=:artifact_id AND request_id=:request_id LIMIT 2",record(caller_scope=ID,artifact_id=ID,request_id=ID),BODY_ROW,False)),
        ('semantic_work_active_count',StatementDefinition("SELECT count(*) AS count FROM retrieval_embedding_work WHERE scope_id=:scope_id AND json_extract(body,'$.state') IN ('PREPARED','BOUND','REMOTE_UNKNOWN','RESULT_STORED','LOCAL_PREPARED')",record(),record(count=N),False)),
        ('semantic_artifact_count',StatementDefinition('SELECT count(*) AS count FROM retrieval_embedding_artifact WHERE scope_id=:scope_id',record(),record(count=N),False)),
        ('semantic_cache_key',StatementDefinition('SELECT row_id,revision,body FROM retrieval_query_embedding_cache WHERE scope_id=:scope_id AND space_id=:space_id AND cache_key=:cache_key ORDER BY row_id LIMIT 2',record(space_id=ID,cache_key=H),BODY_ROW,False)),
        ('semantic_cache_gc',StatementDefinition('SELECT row_id,revision,body FROM retrieval_query_embedding_cache WHERE scope_id=:scope_id AND space_id=:space_id AND cache_key>:after ORDER BY cache_key LIMIT 8',record(space_id=ID,after=BoundedTextSchema(64)),BODY_ROW,False)),
        ('semantic_cache_consumers',StatementDefinition("SELECT count(*) AS count FROM retrieval_embedding_work WHERE scope_id=:scope_id AND json_extract(body,'$.artifact_id')=:artifact_id AND (json_extract(body,'$.cleanup_pending')=1 OR json_extract(body,'$.state') NOT IN ('APPLIED','SUPERSEDED','KNOWN_FAILED','NOT_SENT'))",record(artifact_id=ID),record(count=N),False)),
        ('semantic_generation_members',StatementDefinition('SELECT row_id,revision,body FROM retrieval_semantic_member WHERE scope_id=:scope_id AND generation_id=:generation_id AND ordinal>=:ordinal ORDER BY ordinal LIMIT 8',record(generation_id=ID,ordinal=N),BODY_ROW,False)),
        ('semantic_generation_pages',StatementDefinition('SELECT row_id,revision,body FROM retrieval_semantic_page WHERE scope_id=:scope_id AND generation_id=:generation_id AND page_no>=:page_no ORDER BY page_no LIMIT 8',record(generation_id=ID,page_no=N),BODY_ROW,False)),
        ('semantic_retirement_rows',StatementDefinition("SELECT 'semantic_member' AS table_name,row_id,revision,body FROM retrieval_semantic_member WHERE scope_id=:scope_id AND generation_id=:generation_id AND row_id>:after UNION ALL SELECT 'semantic_page' AS table_name,row_id,revision,body FROM retrieval_semantic_page WHERE scope_id=:scope_id AND generation_id=:generation_id AND row_id>:after ORDER BY row_id LIMIT 8",record(generation_id=ID,after=BoundedTextSchema(128)),record(table_name=ID,row_id=ID,revision=N,body=BoundedTextSchema(8192)),False)),
    )
    statements=base.statements+extra
    return StatementCatalog(replace(base.definition,statements=tuple(statement for _,statement in statements)),statements)
