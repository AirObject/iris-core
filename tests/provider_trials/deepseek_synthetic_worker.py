"""One synthetic native host step and its original-key no-send recovery.

Only literal loopback and fabricated credentials are available. This worker is
an offline qualification tool, not a live trial entry or human review authority.
"""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import time
from typing import cast
from companion_memory.persistence import Found, Committed
from companion_memory.provider import ObserverGrant, Found as ProviderFound
from companion_memory.provider.credentials import CredentialResolver, CredentialUnavailable, Available, CredentialLease
from companion_memory.provider.values import dump
from companion_memory.ingress.events import plain
from companion_memory.memory.formats import sequence
from companion_memory.self_model.current import Available as PersonaAvailable
from companion_memory.information.management import HostIdentity
from tests.runtime.configuration_support import event
from tests.information.test_queries import query
from tests.text_learning.host_driver import confirm_local
from .host import assemble
from .deepseek_configuration import configuration
from .materials import BODIES, SUBJECTS, WORLDS
from .persona_execution import prepare, pending, join
from .files import write_new, canonical


async def execute(root: Path, platform: str, action: str, port: int | None, ordinal: int):
    """Run a finite offline step; recover never has a usable credential."""
    send=action in ('persona','learn')
    if send and (type(port) is not int or not 1<=port<=65535):raise ValueError('Synthetic sends require literal loopback.')
    resolver=CredentialResolver(lambda *args:Available(CredentialLease(b'synthetic-deepseek-only')) if send else CredentialUnavailable('UNAVAILABLE'))
    config,inputs=configuration(root)
    host=assemble(config,root,platform,inputs[6],resolver,loopback_port=port if send else None)
    result:dict={'platform':platform,'action':action,'ordinal':ordinal,'supplier_requests':0}
    try:
        opened=await confirm_local(lambda:host.initialize('CREATE_NEW' if action=='persona' else 'OPEN_EXISTING'))
        assert type(opened) is Found,opened
        assert host.stored is not None
        result['configuration_id']=host.stored.snapshot_id
        if action=='persona':
            run,key=await prepare(host)
            generated=await host.initialization_port().generate(run,1,key,time.monotonic()+60)
            await join(host);candidate=await pending(host,run)
            result.update(generation=repr(generated),candidate=candidate,run_id=run,request_key=key)
            assert candidate['candidate']['resolution']=='SUCCEEDED',result
            owner=host.combination.persona.persona;assert owner is not None
            original=await owner.read_original('run',run,time.monotonic()+10);assert original is not None
            api=host.initialization_port()
            # This decision exists only in the synthetic test database.
            reviewed=await api.review_initial_persona('synthetic-review',run,cast(int,original.value['revision']),
                candidate['candidate']['object_id'],1,candidate['candidate_digest'],'APPROVE',time.monotonic()+10)
            await join(host);assert type(reviewed) is Committed,reviewed
            original=await owner.read_original('run',run,time.monotonic()+10);assert original is not None
            published=await api.publish_initial_persona('synthetic-publish',run,cast(int,original.value['revision']),
                candidate['candidate']['object_id'],2,candidate['candidate_digest'],host.gate.epoch,time.monotonic()+10)
            await join(host);assert type(published) is Committed,published
            result['synthetic_publication']=plain(published.receipt.result)
        elif action in ('learn','recover'):
            registered=await confirm_local(lambda:host.register_entry('register-entry','entry','host','sample_platform','synthetic-conversation'))
            assert type(registered) is Committed,registered
            runtime=host.runtime;assert runtime is not None and runtime.text_contexts is not None
            entry=runtime.bind_entry('entry');subjects=tuple(s[0] for s in SUBJECTS)
            read=runtime.memory.bind_read(subjects,('read_subject','get_current'))
            runtime.text_contexts.bind_entry(entry,read,subjects,(),WORLDS)
            if send:
                for index in range(0 if ordinal==0 else ordinal*2+1,ordinal*2+3):
                    value=event('synthetic-message-'+str(index),BODIES[index]);value['event_version']=2
                    accepted=await confirm_local(lambda:entry.accept_event('accept-'+str(index),value))
                    assert type(accepted) is Committed,accepted
            started=time.monotonic()
            learned=await entry.run_learning('learn-'+str(ordinal))
            result['learning_elapsed_seconds']=time.monotonic()-started
            assert type(learned) is Committed,learned
            receipt=plain(learned.receipt.result);assert type(receipt) is dict
            result['receipt']=receipt
            work=(await runtime.assembly.rows.read('work_get',{'batch_id':receipt['batch_id']}))[0]
            result['work']=plain(work)
            observer=host.provider.bind_observer(ObserverGrant((host.resources.instance_id,),('deepseek-trial-account',)))
            try:
                request=await observer.get_request(work['provider_request_id']);assert type(request) is ProviderFound
                result['provider']=json.loads(dump(request.value))
            finally:host.provider.revoke(observer)
            ids=tuple(item['object_id'] for item in receipt['object_refs']);result['objects']=[];result['sources']=[]
            if ids:
                memory=runtime.memory.bind_read(ids,('get_current',));source=runtime.sources.bind_inspection(ids)
                for identity in ids:
                    current=await memory.get_current(identity);assert type(current) is Found;result['objects'].append(plain(current.value))
                    manifest=await source.read_source_manifest(identity,receipt['source_id']);assert type(manifest) is Found
                    members=[]
                    for index in range(len(sequence(manifest.value['ordered_members']))):
                        member=await source.read_source_member(identity,receipt['source_id'],index);assert type(member) is Found
                        members.append(plain(member.value))
                    result['sources'].append({'object_id':identity,'manifest':plain(manifest.value),'members':members})
            native=await host.bind_query(HostIdentity('synthetic-query-'+action+str(ordinal),'local-trial-operator','host','entry',frozenset(('search_memory','prepare_reply')),(),time.monotonic()+60))
            result['queries']=[]
            for index,identity in enumerate(ids):
                asked=query('query-'+str(index),query_text='',object_ids=(identity,))
                found=await confirm_local(lambda:native.search_memory(asked));assert type(found) is Found,found
                result['queries'].append(plain(found.value))
        observer=host.provider.bind_observer(ObserverGrant((host.resources.instance_id,),('deepseek-trial-account',)))
        try:
            budget=await observer.get_budget_state();assert type(budget) is ProviderFound
            result['budget']=json.loads(dump(budget.value))
        finally:host.provider.revoke(observer)
        assert host.current_persona is not None
        current=await host.current_persona.port.read_current(time.monotonic()+10);assert type(current) is PersonaAvailable
        result['persona']=plain(current.value)
    finally:
        result['close_report']=await host.close()
        result['provider_health']=asdict(host.provider.get_health());result['storage_health']=asdict(host.storage.get_health())
    assert result['close_report'] and result['provider_health']['in_flight']==0 and not result['provider_health']['cleanup_pending']
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('persona','learn','recover','recover-persona'))
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--platform',choices=('macos','linux'),required=True);parser.add_argument('--port',type=int);parser.add_argument('--ordinal',type=int,default=0)
    args=parser.parse_args()
    result=asyncio.run(execute(args.root,args.platform,args.action,args.port,args.ordinal))
    write_new(args.output,canonical(result))
    print(json.dumps({'action':args.action,'ordinal':args.ordinal,'close_report':result['close_report']}))

if __name__=='__main__':main()
