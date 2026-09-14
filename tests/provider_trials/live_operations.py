"""Explicit user-bound publication and single frozen text-learning operations.

Management uses exact candidate revisions/digests without editing text. Learning
keeps one original key and captures formal objects and every permanent source
member through owner ports; recovery has no credential or sending capability.
"""
import time
import asyncio
import json
from typing import cast
from companion_memory.persistence import Found, Committed
from companion_memory.ingress.events import plain
from companion_memory.memory.formats import sequence
from companion_memory.runtime.text_host import TextHost
from companion_memory.runtime.content_assembly import stable
from companion_memory.self_model.current import Available as PersonaAvailable
from companion_memory.information.management import HostIdentity
from companion_memory.retrieval.query_service import QueryPort
from companion_memory.information.errors import InformationRejected
from companion_memory.provider import ObserverGrant,Found as ProviderFound
from companion_memory.provider.values import dump
from tests.runtime.configuration_support import event
from tests.information.test_queries import query
from tests.text_learning.host_driver import confirm_local
from .materials import BODIES, SUBJECTS, WORLDS
from .persona_execution import pending, join


async def query_original(port: QueryPort, request: object):
    """Retry only a bounded local busy query with the unchanged request object.

    This port cannot dispatch a model. Other errors retain their first result;
    no candidate, native deadline, request identity or budget is changed.
    """
    until=time.monotonic()+2
    for ordinal in range(16):
        value=await port.search_memory(request)
        if (type(value) is not InformationRejected or value.error.code!='RESOURCE_BUSY'
                or value.error.cleanup_pending or time.monotonic()>=until or ordinal==15):return value
        await asyncio.sleep(.05)
    raise AssertionError('Bounded local query loop exhausted.')


async def current_persona(host: TextHost, result: dict) -> None:
    """Record the public immutable current publication, without generating it."""
    assert host.current_persona is not None
    current=await host.current_persona.port.read_current(time.monotonic()+10)
    if type(current) is not PersonaAvailable:raise ValueError('Approved current persona unavailable.')
    result['current_persona']=plain(current.value)
    result['mode']=host.gate.state


async def publish(host: TextHost, run_id: str, approval: dict, platform: str, result: dict) -> None:
    """Review the approved original revision, then atomically publish that text.

    Only exact original-key local confirmation is used. A changed candidate,
    preexisting publication or unfinished receipt stops before the next action.
    """
    expected=approval['candidates'][platform]
    if (approval['approved_by'] not in ('用户','synthetic-human') or not approval['approval_ref']
            or expected['decision']!='APPROVE' or expected['revision']!=1 or expected['run_id']!=run_id):
        raise ValueError('Exact explicit user candidate approval required.')
    candidate=await pending(host,run_id);value=candidate['candidate']
    if (value['object_id']!=expected['candidate_id'] or value['revision']!=1 or value['review']!='PENDING'
            or candidate['candidate_digest']!=expected['candidate_digest'] or value['text_digest']!=expected['text_digest']):
        raise ValueError('Original approved candidate changed.')
    result['approved_original']=candidate;result['user_approval']=approval
    owner=host.combination.persona.persona;assert owner is not None
    run=await owner.read_original('run',run_id,time.monotonic()+10);assert run is not None
    if run.value['publication_id'] is not None:raise ValueError('Original run is already published.')
    port=host.initialization_port()
    review_revision=cast(int,run.value['revision'])
    reviewed=await confirm_local(lambda:port.review_initial_persona('user-approve-initial-persona',run_id,
        review_revision,expected['candidate_id'],1,expected['candidate_digest'],'APPROVE',time.monotonic()+10))
    await join(host);result['review_receipt']=repr(reviewed)
    if type(reviewed) is not Committed:raise ValueError('Original review commit unconfirmed.')
    run=await owner.read_original('run',run_id,time.monotonic()+10);assert run is not None
    revised=await pending(host,run_id)
    if (revised['candidate']['review']!='APPROVED' or revised['candidate']['revision']!=2
            or revised['candidate_digest']!=expected['candidate_digest'] or revised['candidate']['text']!=value['text']):
        raise ValueError('Reviewed candidate binding changed.')
    publish_revision=cast(int,run.value['revision'])
    published=await confirm_local(lambda:port.publish_initial_persona('user-publish-initial-persona',run_id,
        publish_revision,expected['candidate_id'],2,expected['candidate_digest'],host.gate.epoch,time.monotonic()+10))
    await join(host);result['publication_receipt']=repr(published)
    if type(published) is not Committed:raise ValueError('Original publication commit unconfirmed.')
    result['publication_facts']=plain(published.receipt.result)
    await current_persona(host,result)
    if result['current_persona']['text']!=value['text'] or host.gate.state!='NORMAL':
        raise ValueError('Current publication or normal-mode confirmation failed.')


async def learn(host: TextHost, ordinal: int, send: bool, result: dict) -> None:
    """Run or confirm one original batch; never manufacture readmission keys.

    UNKNOWN keeps its original work and request facts, with no terminal claim.
    Queries and source inspection are local public capabilities only.
    """
    if type(ordinal) is not int or not 0<=ordinal<6:raise ValueError('Original batch ordinal required.')
    if host.gate.state!='NORMAL':raise ValueError('Published normal mode required for learning.')
    await current_persona(host,result)
    registered=await confirm_local(lambda:host.register_entry('register-text-trial-entry','entry','host','sample_platform','synthetic-conversation'))
    if type(registered) is not Committed:raise ValueError('Original entry registration unconfirmed.')
    runtime=host.runtime;assert runtime is not None and runtime.text_contexts is not None and host.stored is not None
    entry=runtime.bind_entry('entry');subjects=tuple(s[0] for s in SUBJECTS)
    read=runtime.memory.bind_read(subjects,('read_subject','get_current'))
    runtime.text_contexts.bind_entry(entry,read,subjects,(),WORLDS)
    key='learn-'+str(ordinal)
    bid=stable('batch',stable('preparation',host.stored.database_id,'entry',key))
    if not send and not await runtime.assembly.rows.read('work_get',{'batch_id':bid}):
        raise ValueError('Recovery requires the already frozen original work.')
    if send and ordinal>0:
        prior=stable('batch',stable('preparation',host.stored.database_id,'entry','learn-'+str(ordinal-1)))
        previous=await runtime.assembly.rows.read('work_get',{'batch_id':prior})
        if not previous or previous[0]['phase']!='TERMINAL':raise ValueError('Previous original batch terminal required.')
    if send:
        result['accepted']=[]
        for index in range(0 if ordinal==0 else ordinal*2+1,ordinal*2+3):
            value=event('synthetic-message-'+str(index),BODIES[index]);value['event_version']=2
            accepted=await confirm_local(lambda:entry.accept_event('accept-synthetic-'+str(index),value))
            result['accepted'].append(repr(accepted))
            if type(accepted) is not Committed:raise ValueError('Original input receipt unconfirmed.')
    learned=await entry.run_learning(key);result['learning_result']=repr(learned)
    # This same original key is confirmation only, not a new scheduling trigger.
    if type(learned) is not Committed:
        learned=await entry.run_learning(key);result['learning_confirmation']=repr(learned)
    works=await runtime.assembly.rows.read('work_get',{'batch_id':bid})
    if works:
        result['work']=plain(works[0]);result['observed_request_id']=works[0]['provider_request_id']
        # Capture native terminal/budget before optional queries can fail. A
        # local query error must not erase known remote or settlement evidence.
        if works[0]['provider_request_id'] is not None:
            observer=host.provider.bind_observer(ObserverGrant((host.resources.instance_id,),('deepseek-trial-account',)))
            try:
                observed=await observer.get_request(works[0]['provider_request_id'])
                budget=await observer.get_budget_state()
                if type(observed) is not ProviderFound or type(budget) is not ProviderFound:
                    raise ValueError('Original native accounting evidence unavailable.')
                result['provider']=json.loads(dump(observed.value));result['budget']=json.loads(dump(budget.value))
            finally:host.provider.revoke(observer)
    result['batch_ordinal']=ordinal;result['local_terminal_confirmed']=type(learned) is Committed
    if type(learned) is not Committed:return
    receipt=plain(learned.receipt.result);assert type(receipt) is dict
    result['receipt']=receipt;result['commit_id']=learned.receipt.commit_id
    ids=tuple(item['object_id'] for item in receipt['object_refs']);result['objects']=[];result['sources']=[]
    if ids:
        memory=runtime.memory.bind_read(ids,('get_current',));source=runtime.sources.bind_inspection(ids)
        for identity in ids:
            current=await memory.get_current(identity)
            if type(current) is not Found:raise ValueError('Formal memory unavailable.')
            result['objects'].append(plain(current.value))
            manifest=await source.read_source_manifest(identity,receipt['source_id'])
            if type(manifest) is not Found:raise ValueError('Permanent source manifest unavailable.')
            members=[]
            for index in range(len(sequence(manifest.value['ordered_members']))):
                member=await source.read_source_member(identity,receipt['source_id'],index)
                if type(member) is not Found:raise ValueError('Permanent source member unavailable.')
                members.append(plain(member.value))
            result['sources'].append({'object_id':identity,'manifest':plain(manifest.value),'members':members})
    native=await host.bind_query(HostIdentity('trial-query-'+str(ordinal)+'-'+str(send),'local-trial-operator','host','entry',frozenset(('search_memory','prepare_reply')),(),time.monotonic()+60))
    result['queries']=[]
    for index,identity in enumerate(ids):
        asked=query('inspect-'+str(ordinal)+'-'+str(send)+'-'+str(index),query_text='',object_ids=(identity,))
        found=await query_original(native,asked)
        result['queries'].append({'object_id':identity,'result':plain(found.value) if type(found) is Found else repr(found)})
        if type(found) is not Found:raise ValueError('Public local memory query unconfirmed.')
