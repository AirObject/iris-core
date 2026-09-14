"""Original-key persona preparation and generation through native management.

Generation is called once. Local setup confirmations retain the original keys;
the returned candidate is never reviewed or published by this module.
"""
import asyncio
import time
from companion_memory.persistence import Committed,Found
from companion_memory.persistence.text_records import stable_identity
from companion_memory.memory.formats import record
from companion_memory.runtime.text_host import TextHost
from companion_memory.ingress.events import plain
from companion_memory.self_model.formats import candidate_digest
from tests.text_learning.host_driver import confirm_local
from .materials import INITIAL_SELF,SUBJECTS


async def join(host: TextHost) -> None:
    """Await the retained native management owner, including actual cleanup."""
    if host.persona is not None and host.persona._task is not None:
        await asyncio.shield(host.persona._task)


async def prepare(host: TextHost) -> tuple[str,str]:
    """Register the approved self and six subjects, then freeze the first run."""
    assert host.stored is not None
    port=host.initialization_port();instance=host.resources.instance_id
    result=await confirm_local(lambda:port.register_initial_self('initial-self','PRESET',INITIAL_SELF,'SYNTHETIC_FIXTURE',time.monotonic()+10))
    await join(host)
    if type(result) is not Committed:raise ValueError('Initial self was not committed: '+repr(result))
    subjects=tuple({'subject_version':1,'subject_id':sid,'instance_id':instance,'kind':kind,
        'platform_id':'sample_platform' if kind=='PLATFORM_PERSON' else None,
        'external_subject_id':sid if kind=='PLATFORM_PERSON' else None,'label':label,'revision':1} for sid,kind,label in SUBJECTS)
    result=await confirm_local(lambda:port.register_initial_subjects('initial-subjects',subjects,'SYNTHETIC_FIXTURE',time.monotonic()+10))
    await join(host)
    if type(result) is not Committed:raise ValueError('Initial subjects were not committed: '+repr(result))
    input_id=stable_identity('self-input',host.stored.database_id,instance)
    run_id=stable_identity('persona-run',host.stored.database_id,instance)
    result=await confirm_local(lambda:port.prepare_initial_persona('prepare-persona',input_id,1,1,time.monotonic()+10))
    await join(host)
    if type(result) is not Committed:raise ValueError('Persona preparation was not committed: '+repr(result))
    owner=host.combination.persona.persona;assert owner is not None
    run=await owner.read_original('run',run_id,time.monotonic()+10)
    if run is None:raise ValueError('Original persona run missing.')
    return run_id,str(run.value['provider_operation_key'])


async def pending(host: TextHost,run_id: str) -> dict:
    """Read the exact candidate and digest through the restricted public port."""
    value=await host.initialization_port().read_pending(run_id,time.monotonic()+10)
    await join(host)
    if type(value) is not Found:return {'read_result':repr(value)}
    candidate=record(value.value)
    return {'candidate':plain(candidate),'candidate_digest':candidate_digest(candidate)}
