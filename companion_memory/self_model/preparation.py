"""First-persona identity and pre-dispatch transitions from retained originals.

Runtime supplies its actual mode epoch; the self-model owner derives the only
run and per-generation request keys. These pure transitions grant no dispatch
or management authority and must join runtime's existing mode transaction.
"""
from types import MappingProxyType
from typing import cast
from companion_memory.configuration.text_persistence import StoredTextConfiguration,stored_text_configuration_issue
from companion_memory.configuration import PresentValue
from companion_memory.configuration.resolution_results import ResolutionOk
from companion_memory.memory.initial_self import isolate_initial_self
from companion_memory.memory.formats import record,sequence
from companion_memory.persistence.schema import InvalidValue,Value,freeze_value
from companion_memory.persistence.text_records import ID,REVISION,UINT,digest,stable_identity
from companion_memory.provider.service import derived_id
from .formats import isolate_run
from .request_material import PersonaRequestMaterial,request_material
from .transitions import _operation


def generation_key(database_id: str,instance_id: str,run_id: str,generation: int) -> str:
    """The sole key for a retained generation, independent of management retries."""
    for value in (database_id,instance_id,run_id):freeze_value(ID,value)
    if type(generation) is not int or not 1<=generation<=3:raise InvalidValue()
    return derived_id('persona-request',database_id,instance_id,run_id,generation,'GENERATION')


def prepare_run(configuration: StoredTextConfiguration,initial: object,expected_self_revision: int,
                mode_epoch: int,operation: object,now_us: int) -> MappingProxyType[str,Value]:
    """Freeze the actual initial source, resources and first request identity."""
    if stored_text_configuration_issue(configuration) is not None:raise InvalidValue()
    source=isolate_initial_self(initial)
    freeze_value(REVISION,mode_epoch);freeze_value(REVISION,expected_self_revision);freeze_value(UINT,now_us)
    database=configuration.database_id;instance=cast(str,source['instance_id'])
    key=_operation(operation,'prepare_initial_persona',instance)
    if (source['database_id']!=database or source['config_snapshot_id']!=configuration.snapshot_id
            or source['object_id']!=stable_identity('self-input',database,instance)
            or source['self_revision']!=expected_self_revision or now_us<cast(int,source['created_at_us'])):raise InvalidValue()
    run_id=stable_identity('persona-run',database,instance)
    material=request_material(configuration,source,run_id,1,generation_key(database,instance,run_id,1))
    selected=configuration.candidate.foundation.get_entry('provider.accounts')
    if type(selected) is not ResolutionOk or type(selected.value.state) is not PresentValue:raise InvalidValue()
    accounts=sequence(cast(Value,selected.value.state.value))
    if len(accounts)!=1:raise InvalidValue()
    account=record(accounts[0]);settings=configuration.candidate.text.record('self_model.initial_persona')
    return isolate_run({'format_version':1,'object_id':run_id,'revision':1,'database_id':database,'instance_id':instance,
        'config_snapshot_id':configuration.snapshot_id,'created_at_us':now_us,'input_id':source['object_id'],'input_digest':source['input_digest'],
        'self_subject_id':source['self_subject_id'],'self_revision':expected_self_revision,'generation':1,'state':'PREPARED',
        'provider_operation_key':material.request['operation_key'],'provider_request_id':None,'resolution_id':None,'publication_id':None,
        'mode_epoch':mode_epoch,'prompt_ref':settings['prompt_ref'],'schema_ref':settings['schema_ref'],'transform_ref':settings['transform_ref'],
        'account_id':account['account_id'],'window_id':account['window_id'],'binding_digest':digest(material.binding),
        'original_operation':key,'last_operation':key,'updated_at_us':now_us})


def retained_material(configuration: StoredTextConfiguration,initial: object,run: object) -> PersonaRequestMaterial:
    """Rebuild a generation only when every retained source/resource identity agrees."""
    current=isolate_run(run);source=isolate_initial_self(initial)
    if any(source[name]!=current[name] for name in ('database_id','instance_id','config_snapshot_id','input_digest','self_subject_id','self_revision')):
        raise InvalidValue()
    if (source['object_id']!=current['input_id'] or current['config_snapshot_id']!=configuration.snapshot_id
            or current['database_id']!=configuration.database_id or current['object_id']!=stable_identity('persona-run',configuration.database_id,cast(str,current['instance_id']))):
        raise InvalidValue()
    expected=generation_key(configuration.database_id,cast(str,current['instance_id']),cast(str,current['object_id']),cast(int,current['generation']))
    if current['provider_operation_key']!=expected:raise InvalidValue()
    material=request_material(configuration,source,cast(str,current['object_id']),cast(int,current['generation']),expected)
    settings=configuration.candidate.text.record('self_model.initial_persona')
    if digest(material.binding)!=current['binding_digest'] or any(current[name]!=settings[name] for name in ('prompt_ref','schema_ref','transform_ref')):raise InvalidValue()
    selected=configuration.candidate.foundation.get_entry('provider.accounts')
    if type(selected) is not ResolutionOk or type(selected.value.state) is not PresentValue:raise InvalidValue()
    accounts=sequence(cast(Value,selected.value.state.value))
    if len(accounts)!=1:raise InvalidValue()
    account=record(accounts[0])
    if any(current[name]!=account[name] for name in ('account_id','window_id')):raise InvalidValue()
    return material


def associate_run(configuration: StoredTextConfiguration,initial: object,run: object,expected_revision: int,
                  generation: int,mode_epoch: int,operation: object,now_us: int) -> MappingProxyType[str,Value]:
    """Associate one generation before Provider work under the current focused epoch."""
    current=isolate_run(run);retained_material(configuration,initial,current)
    key=_operation(operation,'associate_initial_persona_request',current['instance_id'])
    freeze_value(REVISION,mode_epoch);freeze_value(UINT,now_us)
    if (current['state']!='PREPARED' or current['revision']!=expected_revision or current['generation']!=generation
            or mode_epoch<cast(int,current['mode_epoch']) or now_us<cast(int,current['updated_at_us'])):raise InvalidValue()
    return isolate_run({**current,'revision':expected_revision+1,'state':'REQUEST_ASSOCIATED','mode_epoch':mode_epoch,
        'last_operation':key,'updated_at_us':now_us})


def confirm_run(run: object,expected_revision: int,generation: int,request_id: str,operation: object,now_us: int) -> MappingProxyType[str,Value]:
    """Bind the real Provider request after its owner has verified the original key."""
    current=isolate_run(run);key=_operation(operation,'confirm_initial_persona_request',current['instance_id'])
    freeze_value(ID,request_id);freeze_value(UINT,now_us)
    if (current['state']!='REQUEST_ASSOCIATED' or current['revision']!=expected_revision or current['generation']!=generation
            or current['provider_request_id'] is not None or now_us<cast(int,current['updated_at_us'])):raise InvalidValue()
    return isolate_run({**current,'revision':expected_revision+1,'provider_request_id':request_id,'last_operation':key,'updated_at_us':now_us})
