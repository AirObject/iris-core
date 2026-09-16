"""Durable turns and read steps keep original material and result identity.

A prepared tool may be read again after process recovery only before its result
is stored. These schemas retain references to complete material leaves; no root
holds a second copy of a Provider output or a monetary ledger.
"""
from companion_memory.persistence.daily_records import BASE,DailyTable,Field,RecordSchema,ID,UINT,OPERATION,DIGEST,enum,IndexSpec,daily_catalog
from companion_memory.persistence.schema import ScalarSchema,BoundedTextSchema
from .daily_output import TOOL_ARGUMENTS

RUN=RecordSchema(BASE+(Field('batch_id',ID),Field('context_id',ID),Field('context_digest',DIGEST),Field('authority_digest',DIGEST),
    Field('phase',enum('FROZEN','TURN_PREPARED','TURN_CONFIRMED','TOOLS_READY','WAITING_ADMISSION','REMOTE_UNKNOWN','CANDIDATE_STORED','TERMINAL')),
    Field('turn_count',ScalarSchema('integer',0,3)),Field('tool_count',ScalarSchema('integer',0,4)),Field('active_turn_id',ID,nullable=True),
    Field('candidate_id',ID,nullable=True),Field('deadline_at_us',UINT),Field('terminal_operation',OPERATION,nullable=True),Field('transcript_digest',DIGEST)))
TURN=RecordSchema(BASE+(Field('run_id',ID),Field('ordinal',ScalarSchema('integer',0,2)),Field('phase',enum('PREPARED','ASSOCIATED','RESULT_STORED','RELEASED')),
    Field('material_id',ID),Field('material_digest',DIGEST),Field('wire_digest',DIGEST),Field('provider_operation_key',ID),
    Field('provider_request_id',ID,nullable=True),Field('handoff_id',ID,nullable=True),Field('result_kind',enum('TOOL','FINAL','FAILED','SENSITIVE'),nullable=True),
    Field('result_ref',ID,nullable=True),Field('result_digest',DIGEST,nullable=True),Field('previous_turn_digest',DIGEST,nullable=True),Field('original_operation',OPERATION)))
FAILURE=RecordSchema(tuple(Field(name,ID) for name in ('code','operation','field','reason')))
def tool_schema(name: str) -> RecordSchema:
    """A tool's name selects its complete argument schema, never an arbitrary map."""
    return RecordSchema(BASE+(Field('run_id',ID),Field('turn_id',ID),Field('ordinal',ScalarSchema('integer',0,3)),Field('name',enum(name)),
        Field('arguments',TOOL_ARGUMENTS[name]),Field('state',enum('PREPARED','RESULT_STORED','FAILED','RELEASED')),Field('grant_digest',DIGEST),
        Field('result_ref',ID,nullable=True),Field('result_digest',DIGEST,nullable=True),Field('started_at_us',UINT,nullable=True),Field('ended_at_us',UINT,nullable=True),
        Field('failure',FAILURE,nullable=True),Field('original_operation',OPERATION)))
TABLES=(DailyTable('reasoning_runs',(RUN,),8192,True,(IndexSpec('by_batch',('batch_id',)),)),
    DailyTable('reasoning_turns',(TURN,),8192,True,(IndexSpec('by_run_ordinal',('run_id','ordinal'),point_read=False),)),
    DailyTable('reasoning_tools',tuple(tool_schema(name) for name in TOOL_ARGUMENTS),4096,True,(IndexSpec('by_run_ordinal',('run_id','ordinal'),point_read=False),)))
def reasoning_catalog():
    """All three closed business records contribute their complete static schemas."""
    from dataclasses import replace
    from companion_memory.persistence import StatementDefinition
    from companion_memory.persistence.owned_statements import StatementCatalog
    catalog=daily_catalog('cognition',5,TABLES)
    fields=RecordSchema((Field('object_id',ID),Field('revision',UINT),Field('body',BoundedTextSchema(8192))))
    shared=[]
    for name in ('reasoning_runs','reasoning_turns'):
        shared.append(('provider_read_'+name,StatementDefinition('SELECT object_id,revision,body FROM cognition_'+name+
            " WHERE :scope_id='provider' AND scope_id=:caller_scope AND object_id=:object_id",
            RecordSchema((Field('caller_scope',ID),Field('object_id',ID))),fields,False)))
    shared.append(('provider_read_turn_request',StatementDefinition("SELECT object_id,revision,body FROM cognition_reasoning_turns WHERE :scope_id='provider' AND scope_id=:caller_scope AND json_extract(body,'$.provider_request_id')=:request_id LIMIT 2",
        RecordSchema((Field('caller_scope',ID),Field('request_id',ID))),fields,False)))
    statements=catalog.statements+tuple(shared)
    return StatementCatalog(replace(catalog.definition,statements=tuple(s for _,s in statements)),statements)
