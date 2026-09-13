"""Bounded local verification of every retained initial-persona generation.

The trusted host calls before ordinary readiness. A native original-query grant
can inspect only this instance's one run; it has unconditional send denial.
Historical proposals are reconstructed from original Provider evidence, never
from current prompts, fabricated outputs or a replacement network request.
"""
from __future__ import annotations
import asyncio
import time
from typing import cast
from companion_memory.persistence import Found,Value
from companion_memory.persistence.completion import CompletionScope
from companion_memory.persistence.deadlines import DeadlineScope
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.text_records import stable_identity
from companion_memory.provider import WorkGrant,ResultGrant,Found as ProviderFound,NotFound as ProviderMissing,CancellationSource
from companion_memory.provider.values import as_record,freeze
from companion_memory.provider.service import OPTIONALS
from companion_memory.provider.terminal_evidence import TerminalVerified
from companion_memory.provider.completion_evidence import ConfirmedCompletion
from companion_memory.provider.unsent_evidence import UnsentVerified
from companion_memory.ingress.events import plain
from companion_memory.memory.formats import record,sequence
from .transactions import PersonaTransactions
from .preparation import retained_material,generation_key
from .request_material import request_material
from .resolution import verify_resolved,verify_unsent_resolution
from .confirmation import confirm_absent


async def verify_original_persona(transactions: PersonaTransactions,deadline: float) -> None:
    """Verify all finite roots under the host's native closed recovery gate."""
    if type(transactions) is not PersonaTransactions or transactions.persona is None or transactions.initial is None or transactions.gate is None:raise InvalidValue()
    t=transactions;owner=t.persona;initial=t.initial;gate=t.gate;a=t.assembly
    assert owner is not None and initial is not None and gate is not None
    if t._closed or gate.state!='RECOVERING':raise OwnerFailure('ACCESS_DENIED','identity','BOUNDARY_DENIED')
    with DeadlineScope(deadline),CompletionScope() as cleanup:
        inputs=await initial._owner.rows.read('initial_self_inputs_recovery_page',{'after':'','limit':2})
        if len(inputs)>1:raise InvalidValue()
        if inputs:
            source_record=initial._decode(inputs[0])
            if source_record['object_id']!=initial.input_id:raise InvalidValue()
            await _receipt_target(t,record(source_record['operation']),cast(str,source_record['object_id']),1)
        else:
            subjects=await initial._owner.rows.read('subject_identity',{'kind':'SELF','platform_id':None,'external_subject_id':None})
            if subjects:raise InvalidValue()
        maps={}
        for kind,table,limit in (('run','initial_persona_runs',1),('candidate','initial_persona_candidates',3),('publication','persona_publications',1)):
            rows=await owner._rows.read(table+'_recovery_page',{'after':'','limit':4})
            if len(rows)>limit:raise InvalidValue()
            maps[kind]=tuple(owner._decode(kind,row) for row in rows)
        if not maps['run']:
            if maps['candidate'] or maps['publication']:raise InvalidValue()
            if inputs and await initial.read_original(initial.input_id,deadline) is None:raise InvalidValue()
            return
        if not inputs:raise InvalidValue()
        run=maps['run'][0];mode=(await a.rows.read('mode_get',{'mode_id':'instance_mode'}))[0]
        if mode['run_id']!=run['object_id'] or mode['state'] not in ('DREAM_PREPARING','DREAM_FOCUSED','DRAINING','NORMAL','FAULTED'):raise InvalidValue()
        if (run['state']=='PUBLISHED')!=(bool(maps['publication'])):raise InvalidValue()
        if run['state']!='PUBLISHED' and mode['state'] in ('NORMAL','DRAINING'):raise InvalidValue()
        if run['state']=='PUBLISHED' and (mode['publication_id']!=run['publication_id'] or mode['state'] not in ('DRAINING','NORMAL')):raise InvalidValue()
        source=(await initial.read_published_source(maps['publication'][0],deadline) if maps['publication']
            else await initial.read_original(cast(str,run['input_id']),deadline))
        if source is None:raise InvalidValue()
        retained_material(a.text_transactions.configuration,source.input,run)
        proposal_by_generation={cast(int,value['generation']):value for value in maps['candidate']}
        if len(proposal_by_generation)!=len(maps['candidate']) or any(g>cast(int,run['generation']) for g in proposal_by_generation):raise InvalidValue()
        for generation in range(1,cast(int,run['generation'])+1):
            proposal=proposal_by_generation.get(generation)
            if generation<run['generation'] and (proposal is None or proposal['resolution']=='SUCCEEDED' and proposal['review']!='REJECTED'):raise InvalidValue()
            if generation==run['generation'] and (run['resolution_id'] is not None)!=(proposal is not None):raise InvalidValue()
            key=generation_key(a.configuration.database_id,a.instance_id,cast(str,run['object_id']),generation)
            material=request_material(a.text_transactions.configuration,source.input,cast(str,run['object_id']),generation,key)
            if proposal is None and run['state']=='PREPARED':continue
            grant=WorkGrant('self_model',a.instance_id,None,'PERSONA',(cast(str,material.request['profile_id']),),('GENERATION',),'self_model',t.actor,
                (cast(str,run['object_id']),),internal_dream=True,prompt_revisions=(cast(str,run['prompt_ref']),))
            if not gate.admit_recovery(grant):raise OwnerFailure('RESOURCE_BUSY','resource','CLEANUP_PENDING')
            work=None;result_owner=None;proof=None;absent_confirmation=None
            try:
                work=t.provider.bind_work(grant)
                request={**cast(dict,plain(material.request)),'deadline':deadline,'cancellation':CancellationSource().token}
                found=await work.lookup_request('generate',request)
                if type(found) is ProviderMissing and (proposal is None and run['provider_request_id'] is None
                        or proposal is not None and proposal['provider_request_id'] is None):
                    proof=await work.verify_unsent('generate',request)
                    if type(proof) is not UnsentVerified or proof.value.conclusion!='REGISTRATION_ABSENT':raise InvalidValue()
                    if proposal is not None:
                        absent_confirmation=await confirm_absent(t,proposal)
                        original_run={**run,'generation':generation,'resolution_id':proposal['object_id'],'provider_operation_key':key,
                            'provider_request_id':None,'binding_digest':proposal['binding_digest'],'state':'KNOWN_FAILED','publication_id':None}
                        verify_unsent_resolution(a.text_transactions.configuration,source.input,original_run,proposal,proof.value,time.time_ns()//1000)
                    continue
                if type(found) is not ProviderFound:raise InvalidValue()
                actual=as_record(as_record(found.value)['request']);rid=cast(str,actual['object_id'])
                if generation==run['generation'] and run['provider_request_id'] is not None and rid!=run['provider_request_id']:raise InvalidValue()
                if proposal is None:continue
                if proposal['provider_request_id']!=rid or proposal['provider_operation_key']!=key:raise InvalidValue()
                result_owner=t.provider.bind_result_owner(ResultGrant('self_model',(rid,)))
                normalized=as_record(freeze({**{name:material.request.get(name) for name in OPTIONALS},**material.request},131072,owned=True))
                terminal=await result_owner.verify_terminal(rid,normalized)
                if type(terminal) is not TerminalVerified:raise InvalidValue()
                completion=await result_owner.confirm_completion(terminal.value)
                if type(completion) is not ConfirmedCompletion:raise InvalidValue()
                original_run={**run,'generation':generation,'resolution_id':proposal['object_id'],'provider_operation_key':key,
                    'provider_request_id':rid,'binding_digest':proposal['binding_digest'],
                    'state':'WAITING_REVIEW' if proposal['resolution']=='SUCCEEDED' else 'KNOWN_FAILED','publication_id':None}
                verify_resolved(a.text_transactions.configuration,source.input,original_run,proposal,completion,time.time_ns()//1000)
                if proposal['review_operation'] is not None:
                    await _receipt_target(t,record(proposal['review_operation']),cast(str,proposal['object_id']),cast(int,proposal['revision']))
                del completion,terminal
            finally:
                await cleanup.wait()
                if work is not None:
                    while not work.consumers_ended():await asyncio.sleep(.01)
                    t.provider.revoke(work)
                if result_owner is not None:t.provider.revoke(result_owner)
                gate.revoke(grant)
                proof=None;absent_confirmation=None
        await _receipt_target(t,record(run['last_operation']),cast(str,run['object_id']),cast(int,run['revision']))
        if maps['publication']:
            publication=await owner.current_original(deadline)
            if publication is None:raise InvalidValue()
            await _receipt_target(t,record(publication.value['publication_operation']),cast(str,publication.value['object_id']),1)
        if t._closed or gate.state!='RECOVERING' or time.monotonic()>=deadline:raise InvalidValue()


async def _receipt_target(t: PersonaTransactions,key,identity: str,revision: int):
    kind=key['operation_kind']
    initial=kind=='register_initial_self'
    if (key['owner_namespace']!=('memory' if initial else 'self_model') or key['scope_id']!=t.assembly.instance_id
            or not initial and kind not in t.definitions):raise InvalidValue()
    definition=t.initial_commands.commands[0] if initial else t.definitions[kind]
    operation=t.assembly.storage.bind_operation(definition,t.assembly.instance_id)
    saved=await operation.read_receipt(key['operation_key'])
    if type(saved) is not Found:raise InvalidValue()
    targets=sequence(record(saved.value.result)['targets'])
    if not any(record(target)['object_id']==identity and record(target)['revision']==revision for target in targets):raise InvalidValue()
