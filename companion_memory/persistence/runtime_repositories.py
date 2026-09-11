"""Fixed module-owned record repositories for runtime transactions and indexed reads.

Each table has explicit indexed identity, revision and domain attributes. Trusted
assembly gets one finite statement map per owner. Application callers get bound
ports, never SQL, connection objects or a dynamically selectable table.
"""
from dataclasses import dataclass
from .definitions import RepositoryDefinition,StatementDefinition,TableDefinition
from .schema import Field,RecordSchema,ScalarSchema,BoundedTextSchema

ID=ScalarSchema('identifier')
INT=ScalarSchema('integer')
TEXT=BoundedTextSchema(8192)


@dataclass(frozen=True,slots=True)
class RuntimeRepository:
    """Finite declarations for a single domain owner, assembled before CREATE_NEW."""
    definition: RepositoryDefinition
    statements: tuple[tuple[str,StatementDefinition],...]


def create_runtime_repositories() -> tuple[RuntimeRepository,...]:
    """Declare complete ingress/buffer/runtime tables; no synthetic business tables."""
    layouts={
        'ingress':('entries','events','payloads'),
        'buffers':('positions','references','batches','members','entry_state'),
        'runtime':('mode','work','triggers','transfers','recovery','dream_calls','scheduler'),
    }
    repositories=[]
    for owner,names in layouts.items():
        tables=[];statements=[]
        for name in names:
            table=owner+'_'+name
            body_schema=BoundedTextSchema(65536) if name in ('dream_calls','recovery') else TEXT
            row=RecordSchema((Field('object_id',ID),Field('entry_id',ID),Field('sequence',INT),Field('state',ID),Field('revision',INT),Field('body',body_schema)))
            params=row
            tables.append(TableDefinition(table,'CREATE TABLE '+table+' (scope_id TEXT NOT NULL, object_id TEXT NOT NULL, entry_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK(sequence>=0), state TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0), body TEXT NOT NULL, PRIMARY KEY(scope_id,object_id))'))
            tables.append(TableDefinition(table+'_entry','CREATE INDEX '+table+'_entry ON '+table+'(scope_id,entry_id,state,sequence,object_id)'))
            if name=='positions':
                tables.append(TableDefinition(table+'_sequence','CREATE UNIQUE INDEX '+table+'_sequence ON '+table+'(scope_id,entry_id,sequence)'))
            if name=='batches':
                tables.append(TableDefinition(table+'_active',"CREATE UNIQUE INDEX "+table+"_active ON "+table+"(scope_id,entry_id) WHERE state!='TERMINAL'"))
            def add(suffix,sql,p,r,w):
                statements.append((name+'_'+suffix,StatementDefinition(sql,p,r,w)))
            add('get','SELECT object_id,entry_id,sequence,state,revision,body FROM '+table+' WHERE scope_id=:scope_id AND object_id=:object_id',RecordSchema((Field('object_id',ID),)),row,False)
            add('insert','INSERT INTO '+table+' VALUES(:scope_id,:object_id,:entry_id,:sequence,:state,:revision,:body) RETURNING object_id,entry_id,sequence,state,revision,body',params,row,True)
            add('update','UPDATE '+table+' SET state=:state, revision=:revision, body=:body WHERE scope_id=:scope_id AND object_id=:object_id AND revision=:expected_revision RETURNING object_id,entry_id,sequence,state,revision,body',RecordSchema((Field('object_id',ID),Field('state',ID),Field('revision',INT),Field('body',body_schema),Field('expected_revision',INT))),row,True)
            add('delete','DELETE FROM '+table+' WHERE scope_id=:scope_id AND object_id=:object_id AND revision=:expected_revision RETURNING object_id',RecordSchema((Field('object_id',ID),Field('expected_revision',INT))),RecordSchema((Field('object_id',ID),)),True)
            # Metadata pages never include bodies. Full bodies require bounded point reads.
            meta=RecordSchema(row.fields[:-1])
            add('page','SELECT object_id,entry_id,sequence,state,revision FROM '+table+' WHERE scope_id=:scope_id AND object_id>:after ORDER BY object_id LIMIT :limit',RecordSchema((Field('after',BoundedTextSchema(128)),Field('limit',ScalarSchema('integer',1,128)))),meta,False)
            if name=='recovery':
                add('history_page','SELECT object_id,entry_id,sequence,state,revision FROM '+table+' WHERE scope_id=:scope_id AND sequence<>:generation AND object_id>:after ORDER BY object_id LIMIT :limit',RecordSchema((Field('generation',INT),Field('after',BoundedTextSchema(128)),Field('limit',ScalarSchema('integer',1,128)))),meta,False)
            add('entry','SELECT object_id,entry_id,sequence,state,revision FROM '+table+' WHERE scope_id=:scope_id AND entry_id=:entry_id AND state=:state AND (sequence>:after_sequence OR (sequence=:after_sequence AND object_id>:after_id)) ORDER BY sequence,object_id LIMIT :limit',RecordSchema((Field('entry_id',ID),Field('state',ID),Field('after_sequence',INT),Field('after_id',BoundedTextSchema(128)),Field('limit',ScalarSchema('integer',1,128)))),meta,False)
            add('count','SELECT count(*) AS count FROM '+table+' WHERE scope_id=:scope_id AND entry_id=:entry_id AND state=:state',RecordSchema((Field('entry_id',ID),Field('state',ID))),RecordSchema((Field('count',INT),)),False)
        if owner=='runtime':
            statement=StatementDefinition('SELECT count(*) AS count FROM runtime_work WHERE scope_id=:scope_id AND state=:state',RecordSchema((Field('state',ID),)),RecordSchema((Field('count',INT),)),False)
            statements.append(('work_state_count',statement))
        if owner=='buffers':
            statement=StatementDefinition('SELECT count(*) AS count FROM buffers_entry_state WHERE scope_id=:scope_id AND state=:state',RecordSchema((Field('state',ID),)),RecordSchema((Field('count',INT),)),False)
            statements.append(('entry_pending_count',statement))
            parameters=RecordSchema((Field('entries',BoundedTextSchema(32768)),Field('after',BoundedTextSchema(128)),Field('waterline',BoundedTextSchema(128)),Field('limit',ScalarSchema('integer',1,128))))
            columns='e.object_id,e.entry_id,e.sequence,e.state,e.revision,e.body'
            counts="(SELECT count(*) FROM buffers_positions p WHERE p.scope_id=e.scope_id AND p.entry_id=e.entry_id AND p.state='NORMAL') AS normal_count,(SELECT count(*) FROM buffers_positions p WHERE p.scope_id=e.scope_id AND p.entry_id=e.entry_id AND p.state='STAGED') AS staged_count,(SELECT count(*) FROM buffers_batches b WHERE b.scope_id=e.scope_id AND b.entry_id=e.entry_id AND b.state!='TERMINAL') AS active_batches,(SELECT count(*) FROM buffers_members m JOIN buffers_batches b ON b.scope_id=m.scope_id AND b.object_id=m.state WHERE m.scope_id=e.scope_id AND m.entry_id=e.entry_id AND b.state!='TERMINAL' AND json_extract(m.body,'$.data.role')='T') AS active_targets"
            observed=RecordSchema((Field('object_id',ID),Field('entry_id',ID),Field('sequence',INT),Field('state',ID),Field('revision',INT),Field('body',TEXT),Field('normal_count',INT),Field('staged_count',INT),Field('active_batches',INT),Field('active_targets',INT)))
            statement=StatementDefinition('SELECT '+columns+','+counts+' FROM buffers_entry_state e WHERE e.scope_id=:scope_id AND instr(:entries, \'"\'||e.entry_id||\'"\')>0 AND e.object_id>:after AND e.object_id<=:waterline ORDER BY e.object_id LIMIT :limit',parameters,observed,False)
            statements.append(('entry_observation',statement))
            batch_parameters=RecordSchema(parameters.fields+(Field('batch_id',ID,nullable=True),Field('filter_state',ScalarSchema('enum',choices=('FROZEN','TERMINAL')),nullable=True)))
            statement=StatementDefinition('SELECT object_id,entry_id,sequence,state,revision,body FROM buffers_batches WHERE scope_id=:scope_id AND instr(:entries, \'"\'||entry_id||\'"\')>0 AND object_id>:after AND object_id<=:waterline AND (:batch_id IS NULL OR object_id=:batch_id) AND (:filter_state IS NULL OR state=:filter_state) ORDER BY object_id LIMIT :limit',batch_parameters,RecordSchema((Field('object_id',ID),Field('entry_id',ID),Field('sequence',INT),Field('state',ID),Field('revision',INT),Field('body',TEXT))),False)
            statements.append(('batch_observation',statement))
            for kind,table in (('entries','buffers_entry_state'),('batches','buffers_batches')):
                boundary=StatementDefinition('SELECT max(object_id) AS waterline FROM '+table+' WHERE scope_id=:scope_id AND instr(:entries, char(34)||entry_id||char(34))>0',RecordSchema((Field('entries',BoundedTextSchema(32768)),)),RecordSchema((Field('waterline',ID,nullable=True),)),False)
                statements.append((kind+'_waterline',boundary))
        repositories.append(RuntimeRepository(RepositoryDefinition(owner,1,tuple(tables),tuple(s for _,s in statements)),tuple(statements)))
    return tuple(repositories)
