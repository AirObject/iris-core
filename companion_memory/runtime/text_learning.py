"""Native text learning from complete retained context to one atomic terminal.

New work freezes every owner input before association. Re-entry reconstructs
that exact request and can only inspect Provider; a missed lookup never grants a
send. Final receipt inputs remain reconstructible after payload leaf release.
"""
from __future__ import annotations
import time
from types import MappingProxyType
from typing import TYPE_CHECKING,cast
from companion_memory.cognition.context_storage import StoredContext
from companion_memory.cognition.text_context import restore_context,admission_request
from companion_memory.ingress.events import plain
from companion_memory.memory.formats import record,sequence
from companion_memory.persistence import Committed,Found,Value
from companion_memory.persistence.completion import CompletionScope
from companion_memory.persistence.content_codec import decode_content,encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.schema import InvalidValue
from companion_memory.provider import WorkGrant,ResultGrant,CancellationSource,Completed,Pending,Found as ProviderFound
from companion_memory.provider.terminal_evidence import TerminalVerified
from companion_memory.provider.values import as_record,freeze
from companion_memory.provider.service import OPTIONALS
from .content_assembly import stable
from .results import Rejected,RuntimeError
if TYPE_CHECKING:
    from .content_service import ContentRuntimeService


async def learn_text(runtime: ContentRuntimeService,source: MappingProxyType[str,Value],*,fresh: bool,admission_event: str|None=None):
    r=runtime;a=r.assembly;text=a.text_transactions;bid=cast(str,source['batch_id'])
    deadline=r.request_deadline()
    work=(await a.rows.read('work_get',{'batch_id':bid}))[0]
    if work['phase'] in ('CANDIDATE_STORED','TERMINAL'):
        rows=await a.cognition._rows.read('get',{'candidate_id':work['candidate_id']})
        if len(rows)!=1:raise InvalidValue()
        manifest=a.cognition.manifest(decode_content(cast(str,rows[0]['manifest']).encode(),4096))
        kind='commit_content_published' if manifest['ordered_change_refs'] else 'commit_content_without_objects'
        finish=r.confirm_command if work['phase']=='TERMINAL' else r.execute
        return await finish(kind,stable('finish',bid),{'batch_id':bid,'candidate_id':work['candidate_id'],
            'expected_revision':cast(int,work['revision'])-(1 if work['phase']=='TERMINAL' else 0),'generation':work['generation'],
            'readable_objects':[],'readable_subjects':[]})
    if work['phase']=='WAITING_ADMISSION':
        if not fresh or admission_event is None or r.gate.state not in ('NORMAL','DRAINING'):
            return Found(MappingProxyType({'state':'WAITING_ADMISSION','remote_result':'NOT_SENT'}))
        prior=(await a.rows.read('learning_admissions_get',{'admission_id':stable('learning_admission',bid,work['admission_generation'])}))[0]
        if admission_event==work['admission_trigger'] and r.gate.epoch<=cast(int,prior['mode_epoch']):
            return Found(MappingProxyType({'state':'WAITING_ADMISSION','remote_result':'NOT_SENT'}))
        reopened=await r.execute('reopen_learning_admission',stable('readmit_learning',bid,admission_event,r.gate.epoch),
            {'batch_id':bid,'generation':work['generation'],'expected_revision':work['revision'],'trigger_key':admission_event,'expected_epoch':r.gate.epoch})
        if type(reopened) is not Committed:return reopened
        work=(await a.rows.read('work_get',{'batch_id':bid}))[0]
    if work['model_binding'] is None:
        if not fresh:return Found(MappingProxyType({'state':'PARKED'}))
        if r.text_contexts is None:raise OwnerFailure('PRECONDITION_FAILED','state','PERSONA_REQUIRED')
        key=stable('learning_context',bid)
        context,scope=await r.text_contexts.collect(source,work,key,deadline)
        a.text_commands.retain(context,scope)
        with CompletionScope() as cleanup:
            try:
                staged=await r.execute('stage_learning_context',key,{'batch_id':bid,'expected_revision':work['revision'],'generation':work['generation'],
                    'manifest':encode_content(context.manifest,8192).decode(),'leaves':[encode_content(leaf,8192).decode() for leaf in context.leaves]})
            finally:
                await cleanup.wait();a.text_commands.release_retained(context)
        if type(staged) is not Committed:return staged
        work=(await a.rows.read('work_get',{'batch_id':bid}))[0]
    binding=record(cast(Value,freeze(decode_content(cast(str,work['model_binding']).encode(),8192),8192)))
    stored=await text.contexts.load(cast(str,binding['context_id']),deadline)
    if type(stored) is not StoredContext:raise OwnerFailure('INTEGRITY_FAILURE','storage','CONTEXT_UNRECOVERABLE')
    context=restore_context(stored.manifest,stored.leaves,text.request_identity(source,work),text.chat)
    if text.work_binding(context,cast(int,work['admission_generation']))!=binding:raise InvalidValue()
    descriptor=admission_request(context,cast(int,work['admission_generation']))
    send=False
    if work['phase'] in ('FROZEN','PARKED'):
        if not fresh:return Found(MappingProxyType({'state':'PARKED'}))
        associated=await r.execute('associate_content_request',stable('associate_learning',bid,work['admission_generation']),
            {'batch_id':bid,'expected_revision':work['revision'],'generation':work['generation'],
                'provider_operation_key':descriptor['operation_key'],'model_binding':work['model_binding']})
        if type(associated) is not Committed:return associated
        send=associated.source=='NEW';work=(await a.rows.read('work_get',{'batch_id':bid}))[0]
    if work['phase']!='REQUEST_ASSOCIATED':raise InvalidValue()
    grant=WorkGrant('cognition',a.instance_id,None,'LEARNING',(r.learning_profile,),('GENERATION',),'cognition','content_scheduler',
        (cast(str,source['run_id']),),(cast(str,source['entry_id']),),(bid,),prompt_revisions=(cast(str,binding['prompt_revision']),))
    held=r._learning_held.get(bid)
    if held:grant,port=held;send=False
    else:
        admitted=r.gate.admit(grant,r.gate.epoch,expected_protection_revision=r.gate.protection_revision) if send else r.gate.admit_recovery(grant)
        if not admitted and send:send=False;admitted=r.gate.admit_recovery(grant)
        if not admitted:return Rejected(RuntimeError('RESOURCE_BUSY','run_learning','state','ADMISSION_FULL'))
        try:port=r.provider.bind_work(grant)
        except ValueError:
            r.gate.revoke(grant)
            return Rejected(RuntimeError('RESOURCE_BUSY','run_learning','state','ADMISSION_FULL'))
        r._learning_held[bid]=grant,port
    request={**cast(dict,plain(descriptor)),'deadline':deadline,'cancellation':CancellationSource().token}
    response=await port.generate(request) if send else await port.lookup_request('generate',request)
    rid=response.record['object_id'] if type(response) is Completed else response.reference['request_id'] if type(response) is Pending else as_record(as_record(response.value)['request'])['object_id'] if type(response) is ProviderFound else None
    from .learning_admissions import conclude_unsent
    from .content_learning import provider_observation
    if rid is None:
        concluded=await conclude_unsent(r,work,port,request)
        return concluded if concluded is not None else provider_observation(r,response,'ORIGINAL_REQUEST_UNCONFIRMED')
    if work['provider_request_id'] is None:
        confirmed=await r.execute('confirm_content_request',stable('confirm_learning',bid,cast(str,rid)),{'batch_id':bid,
            'expected_revision':work['revision'],'generation':work['generation'],'request_id':rid})
        if type(confirmed) is not Committed:return confirmed
        work=(await a.rows.read('work_get',{'batch_id':bid}))[0]
    elif work['provider_request_id']!=rid:raise InvalidValue()
    owner=r.provider.bind_result_owner(ResultGrant('cognition',(cast(str,rid),)))
    with CompletionScope() as cleanup:
        try:
            original=as_record(freeze({**{name:descriptor.get(name) for name in OPTIONALS},**descriptor},131072,owned=True))
            terminal=await owner.verify_terminal(rid,original)
            if type(terminal) is not TerminalVerified:return provider_observation(r,terminal,'REMOTE_UNKNOWN')
            if not terminal.value.confirmed_sent or terminal.value.request['outcome']=='MODE_BLOCKED':
                concluded=await conclude_unsent(r,work,port,request)
                return concluded if concluded is not None else Found(MappingProxyType({'state':'WAITING_ADMISSION'}))
            candidate=await text.rebuild_candidate(source,work,context,terminal.value,deadline)
            a.retain_learning_terminal(terminal.value)
            staged=await r.execute('store_content_candidate',stable('candidate_stage',bid),{'batch_id':bid,'expected_revision':work['revision'],
                'generation':work['generation'],'manifest':encode_content(candidate.manifest,4096).decode(),
                'leaves':[encode_content(leaf,8192).decode() for leaf in candidate.leaves]})
        finally:
            await cleanup.wait();r.provider.revoke(owner)
    if type(staged) is not Committed:return staged
    return await learn_text(r,source,fresh=False)
