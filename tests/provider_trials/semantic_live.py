"""Run exactly the reviewed allocated embedding package through the public host.

Preparation opens no credential and sends nothing. Execution consumes original
purpose slots, stopping on any unresolved or unsuccessful operation. Recovery
cannot reserve or send; it reuses only completed work and paid query artifacts.
The external approval files and final code manifest are mandatory trust inputs.
"""
import asyncio
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
import time
from types import MappingProxyType
from typing import cast

from companion_memory.cognition.fixed_memory import FixedReviewAuthority
from companion_memory.configuration.semantic_codec import candidate_values
from companion_memory.configuration.semantic_resolution import resolve_semantic_configuration,SemanticConfigurationOk
from companion_memory.ingress.events import plain
from companion_memory.information.management import HostIdentity
from companion_memory.memory.initial_self_storage import InitialSelfBinding
from companion_memory.persistence import Committed,Found,Receipt
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.semantic_records import identity,number,string,isolate
from companion_memory.provider.chat_transport import ChatTransport
from companion_memory.provider.credentials import CredentialResolver,CredentialUnavailable
from companion_memory.provider.file_credentials import protected_file_resolver
from companion_memory.provider.values import Record as ModelRecord
from companion_memory.provider.wire_evidence import WireEvidence
from companion_memory.retrieval.semantic_material import space_identity
from companion_memory.runtime.semantic_authorization import SemanticActivationAuthority,USAGE_BINDING
from companion_memory.runtime.semantic_host import SemanticHost
from tests.information.publication_support import index_identity
from tests.semantic.configuration_support import inputs
from tests.semantic.evaluation import QueryGold,evaluate
from tests.semantic.fault_support import lexical
from tests.semantic.reviewed_local import verified,PROPOSAL_DIGEST,MATERIAL_DIGEST,DECISION_DIGEST
from tests.semantic.test_semantic_host import make_host,opened

ACCOUNT='semantic-allocated-embedding-20260915'
SECRET='semantic-allocated-embedding-secret'
REVISION='allocated-20260915'
PACKAGE='semantic-allocated-18-20260915'
DECISION='semantic-allocated-usage-approval-2026-09-15'


def save(base:Path,name:str,value:object) -> None:
    """Durable nonsecret evidence, separate from the native business ledger."""
    import os
    def compatible(raw:object) -> object:
        if type(raw) is MappingProxyType:return dict(raw)
        raise TypeError('Evidence contains a non-JSON carrier.')
    data=json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False,default=compatible).encode()
    with open(base/name,'xb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())


def load(base:Path,*,recovery:bool=False):
    proposal=verified(base/'proposal.canonical.json',PROPOSAL_DIGEST)
    package=verified(base/'material-package.json',MATERIAL_DIGEST)
    review=verified(base/'material-review-decision.json',DECISION_DIGEST)
    allocation=json.loads((base/'allocation-decision.json').read_text())
    trusted=json.loads((base/'trusted-inputs.json').read_text())
    assert sha256((base/'allocation-decision.json').read_bytes()).hexdigest()==trusted['allocation_decision_sha256']
    assert allocation['decision_ref']==DECISION and allocation['allocation_evidence']=='USER_CONFIRMED_ALLOCATION'
    assert allocation['scope']['attempt_limit']==18 and allocation['scope']['money_admission']=='WAIVED_FOR_THIS_PACKAGE'
    assert review['expected_amended_proposal_sha256']==PROPOSAL_DIGEST and review['reviewed_by']=='codex_supervisor'
    code=json.loads((base/'code-files.json').read_text())
    actual=code
    if recovery and (base/'recovery-code-files.json').exists():
        repair=json.loads((base/'recovery-code-files.json').read_text())
        assert repair['original_code_digest']==code['aggregate_sha256']
        actual=repair['code']
    for row in actual['files']:assert sha256(Path(row['path']).read_bytes()).hexdigest()==row['sha256'],row['path']
    assert sha256(b''.join((r['path']+'\0'+r['sha256']+'\n').encode() for r in actual['files'])).hexdigest()==actual['aggregate_sha256']
    assert sha256(b''.join((r['path']+'\0'+r['sha256']+'\n').encode() for r in code['files'])).hexdigest()==code['aggregate_sha256']
    return proposal,package,review,trusted,code


def assemble(base:Path,mode:str,proposal,package,review,trusted):
    root=base/'instance'
    if mode=='PREPARE':root.mkdir(mode=0o700)
    supplied=inputs(root,offline=False,usage_only=True)
    account=supplied[0]['explicit_values']['provider.accounts'][0]
    account.update(account_id=ACCOUNT,window_id='allocated-local-window-20260915',evidence_ref=DECISION)
    epoch='allocated-embedding-20260915'
    space=space_identity('ARK_CODING_DENSE_TEXT_V1','https://ark.cn-beijing.volces.com/api/coding/v3/embeddings','doubao-embedding-vision',epoch)
    profiles=supplied[0]['explicit_values']['provider.profiles']
    for profile in profiles:profile.update(account_id=ACCOUNT,space_id=space)
    supplied[5]['explicit_values']['retrieval.semantic'].update(space_id=space,deployment_epoch=epoch)
    transport=supplied[5]['explicit_values']['provider.embedding_transport']
    transport.update(secret_ref=SECRET,secret_revision=REVISION,server_evidence_ref='allocated-direct-document-verification',sdk_evidence_digest=trusted['sdk_digest'])
    resolved=resolve_semantic_configuration(*supplied);assert type(resolved) is SemanticConfigurationOk,resolved
    claims={'instance_id':proposal['instance_id'],'set_id':proposal['set_id'],'entry_id':proposal['entry_id'],
        'review_ref':review['review_ref'],'review_digest':PROPOSAL_DIGEST,'manifest_digest':package['manifest_digest'],'reviewed_by':review['reviewed_by']}
    grant=FixedReviewAuthority(lambda received:dict(received)==claims).grant(claims)
    template=make_host(root,1,usage_only=True)
    resolver=protected_file_resolver(Path('/dev/shm/iris-allocated-credential/embedding.api-key'),secret_ref=SECRET,secret_revision=REVISION,account_ref=ACCOUNT) if mode=='EXECUTE' else CredentialResolver(lambda *_:CredentialUnavailable('UNAVAILABLE'))
    evidence=None
    if mode=='EXECUTE':
        directory=base/'wire';directory.mkdir(mode=0o700);evidence=WireEvidence(directory,multiple=True)
    native=ChatTransport.for_embedding(cast(ModelRecord,resolved.value.text.record('provider.embedding_transport')),resolver,ACCOUNT,time.monotonic,evidence=evidence)
    host=SemanticHost(resolved.value,replace(template.resources,instance_id=proposal['instance_id'],review=grant,
        initial_self=InitialSelfBinding(**proposal['self_initialization']['binding']),transport=native))
    return host,claims


async def fixed_objects(host,proposal,claims):
    assert host.initial is not None and host.fixed is not None and host.stored is not None
    initial=proposal['self_initialization'];fixed=host.fixed
    def env(kind,key,payload):return fixed.envelope(kind,key,MappingProxyType(payload),1)
    result=await host.initial.register_initial_self(initial['original_key'],initial['input_kind'],initial['body'],initial['input_origin']);assert type(result) is Committed,result
    result=await host.register_entry('reviewed-entry',proposal['entry_id'],'host','sample_platform','reviewed-external-entry');assert type(result) is Committed,result
    result=await host.register_initial_subjects('reviewed-subjects',proposal['subjects'],'SYNTHETIC_FIXTURE');assert type(result) is Committed,result
    config={'database_id':host.stored.database_id,'instance_id':proposal['instance_id'],'snapshot_id':host.stored.snapshot_id}
    result=await fixed.begin(env('fixed_begin','begin',{'set_id':proposal['set_id'],'config':config,**{k:claims[k] for k in ('manifest_digest','review_ref','review_digest')}}),time.monotonic()+5);assert type(result) is Committed,result
    for n,member in enumerate(proposal['memories']):
        result=await fixed.add_member(env('fixed_add_member','add:'+str(n),{'set_id':proposal['set_id'],'expected_revision':n+1,
            **{k:member[k] for k in ('ordinal','member_id','event_json','memory_json','content_digest')}}),time.monotonic()+5);assert type(result) is Committed,result
    result=await fixed.seal(env('fixed_seal','seal',{'set_id':proposal['set_id'],'expected_revision':13}),time.monotonic()+5);assert type(result) is Committed,result
    for n in range(12):
        result=await fixed.establish_one(env('fixed_establish','establish:'+str(n),{'set_id':proposal['set_id'],'expected_revision':14+n,'ordinal':n,'expected_member_revision':1}),time.monotonic()+5);assert type(result) is Committed,result


async def queries(base,host,port,proposal,label,modes):
    responses={};metrics={}
    gold=tuple(QueryGold(q['query_id'],{v['object_id']:v['grade'] for v in q['labels']}) for q in proposal['queries'])
    for mode in modes:
        rows={}
        for item in proposal['queries']:
            request=dict(item['query']);qid=item['query_id'];request['request_key']=label+':'+mode+':'+qid
            if mode=='LEXICAL_BASELINE':request['retrieval_mode']='LOCAL_LEXICAL_V1'
            elif mode=='LEXICAL_ONLY':request.update(allow_partial=True,require_complete=False)
            result=await port.search_memory(request)
            if type(result) is Found:rows[qid]=plain(result.value)
            else:save(base,label+'-'+mode+'-'+qid.replace(':','_')+'-failure.json',{'request':request,'result':repr(result)})
        responses[mode]=rows;metrics[mode]=evaluate(gold,rows,mode)
    save(base,label+'-queries.json',responses);save(base,label+'-metrics.json',metrics)
    return responses,metrics


async def http_result(base,host,business,proposal,label,complete):
    """HTTP and direct queries share one actual principal and cached partition."""
    assert host.http is not None
    token=host.http.issue_test_session(business,time.monotonic()+300);address,port=await host.http.start()
    request=dict(proposal['queries'][4]['query']);request['request_key']=label+':http-empty'
    if not complete:request.update(allow_partial=True,require_complete=False)
    body=json.dumps(request,ensure_ascii=False).encode();reader,writer=await asyncio.open_connection(address,port)
    writer.write(('POST /api/host/memory/search HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer '+token+'\r\nContent-Length: '+str(len(body))+'\r\n\r\n').encode()+body)
    await writer.drain();raw=await reader.read();writer.close();await writer.wait_closed()
    (base/(label+'-http.bin')).write_bytes(raw)
    assert raw.startswith(b'HTTP/1.1 200'),raw
    response=json.loads(raw.split(b'\r\n\r\n',1)[1])['value'];assert response['response_version']==2
    return response


async def run(base:Path,mode:str):
    assert mode in ('PREPARE','EXECUTE','RECOVER')
    proposal,package,review,trusted,code=load(base,recovery=mode=='RECOVER')
    host,claims=assemble(base,mode,proposal,package,review,trusted)
    output={'mode':mode,'package_id':PACKAGE,'allocation_decision_ref':DECISION,'fee_atoms':None,'supplier_quota_known':None}
    try:
        ready=await opened(host,'CREATE_NEW' if mode=='PREPARE' else 'OPEN_EXISTING');assert type(ready) is Found,ready
        assert host.embedding is not None and host.semantic is not None and host.stored is not None and host.queries is not None and host.queries.semantic is not None
        provider=host.embedding;management=host.semantic
        assert provider.executions==0
        if mode=='PREPARE':await fixed_objects(host,proposal,claims)
        client=HostIdentity('allocated-query','principal','host',proposal['entry_id'],frozenset(('search_memory','resolve_recall')),(),time.monotonic()+3600)
        business=await host.bind_business(client);port=business._query
        partition=host.queries.semantic.partition(port)
        if mode=='PREPARE':
            manager=await host.bind_management(replace(index_identity(),entry_id=proposal['entry_id']))
            await lexical(host,'allocated-lexical',manager)
            await queries(base,host,port,proposal,mode,('LEXICAL_BASELINE','LEXICAL_ONLY'))
            await http_result(base,host,business,proposal,mode,False)
            binding={'format':'SEMANTIC_TRIAL_AUTH_V2','verification_mode':'USER_ALLOCATED_USAGE_TRIAL',
                'package_id':PACKAGE,'set_id':proposal['set_id'],'instance_id':proposal['instance_id'],'database_id':host.stored.database_id,
                'config_snapshot_id':host.stored.snapshot_id,'code_digest':code['aggregate_sha256'],'material_digest':MATERIAL_DIGEST,
                'review_digest':PROPOSAL_DIGEST,'protocol_digest':trusted['protocol_digest'],'sdk_digest':trusted['sdk_digest'],'decision_ref':DECISION,
                'execution':'REAL','account_evidence_ref':DECISION,'input_evidence_ref':'reviewed-18-byte-bounds',
                'server_evidence_ref':'allocated-direct-document-verification','document_ids':tuple(r['object_id'] for r in package['requests'] if r['purpose']=='DOCUMENT'),
                'queries':tuple({'query_id':q['query_id'],'text':q['query_text']} for q in proposal['queries']),'expires_at':time.time_ns()//1000+21600000000}
            expected=isolate(USAGE_BINDING,binding,1048576)
            grant=SemanticActivationAuthority(lambda received:received==expected).grant(binding)
            host.bind_activation(grant)
            assert type(await management.resume('allocated-resume')) is Receipt
            # Preparation is local; opening/activation never registers a Provider request.
            works=[]
            for slot in package['requests']:
                if slot['purpose']=='DOCUMENT':work_id=await management.prepare_document(slot['object_id'],'allocated:'+slot['slot_id'],partition)
                else:work_id=await management.prepare_query(slot['rendered_text'],'allocated:'+slot['slot_id'],partition)
                assert type(work_id) is str,work_id
                work=await management.work(work_id);text=await management.owner.text(work)
                assert text==slot['rendered_text'] and work['material_digest']==slot['material_digest']
                preview=provider.request(work,text,number(binding['expires_at']))
                assert preview.wire==slot['request_body_utf8'].encode()
                native_slot=identity('semantic-slot',PACKAGE,slot['purpose'],slot['object_id'] if slot['purpose']=='DOCUMENT' else slot['query_id'])
                works.append({'slot_id':slot['slot_id'],'native_slot_id':native_slot,'work_id':work_id,'request_id':preview.request_id,'attempt_id':preview.attempt_id,
                    'purpose':slot['purpose'],'body_sha256':sha256(preview.wire).hexdigest(),'profile_id':preview.description['profile_id'],
                    'space_id':preview.description['space_id'],'config':plain(preview.description['config']),'material_digest':work['material_digest'],
                    'deadline_binding':'Assigned once by public run_work at first admission; never refreshed after reserve.'})
            assert len(works)==18 and provider.executions==0
            save(base,'activation.json',binding);save(base,'native-bindings.json',works)
            save(base,'configuration.json',candidate_values(host.configuration))
        else:
            binding=json.loads((base/'activation.json').read_text());expected=isolate(USAGE_BINDING,binding,1048576)
            assert binding['decision_ref']==DECISION and binding['code_digest']==code['aggregate_sha256'] and binding['review_digest']==PROPOSAL_DIGEST
            grant=SemanticActivationAuthority(lambda received:received==expected).grant(binding);host.bind_activation(grant)
            works=json.loads((base/'native-bindings.json').read_text());complete=True
            if mode=='EXECUTE':assert type(await management.resume('allocated-resume')) is Receipt
            for slot in works:
                work=await management.work(slot['work_id'])
                if mode=='RECOVER' and work['intent'] is None:complete=False;continue
                if mode=='EXECUTE':
                    control=await management.control()
                    if control['last_cleanup_at'] is not None:
                        earliest=number(control['last_cleanup_at'])+30000000
                        while time.time_ns()//1000<earliest:await asyncio.sleep(min(.5,(earliest-time.time_ns()//1000)/1000000))
                start=time.time_ns()
                result=await management.run_work(slot['work_id'],slot_id=slot['native_slot_id'] if mode=='EXECUTE' else None)
                save(base,mode+'-'+slot['slot_id']+'.json',{'started_ns':start,'ended_ns':time.time_ns(),'result':plain(result) if type(result) is MappingProxyType else repr(result),
                    'work':await management.work(slot['work_id']),'native_request':await provider.ledger.get('requests',slot['request_id']),
                    'attempts':await provider.ledger.read('attempts_for_request',{'request_id':slot['request_id']})})
                print(json.dumps({'mode':mode,'slot':slot['slot_id'],'state':result.get('state') if type(result) is MappingProxyType else type(result).__name__,
                    'cleanup_pending':result.get('cleanup_pending') if type(result) is MappingProxyType else None,'provider_executions':provider.executions}),flush=True)
                if type(result) is not MappingProxyType or result['state']!='APPLIED' or result['cleanup_pending']:
                    complete=False;output['stop_slot']=slot['slot_id'];output['stop_result']=plain(result) if type(result) is MappingProxyType else repr(result);break
            if complete and mode=='EXECUTE':
                publication=await management.owner.memory.rows.read('semantic_publication',management.owner.memory.root_id);assert publication is not None
                result=await management.publish(identity('semantic-generation',PACKAGE,1),number(publication['material_seq']))
                save(base,'semantic-publication.json',plain(result) if type(result) is MappingProxyType else repr(result))
                assert type(result) is MappingProxyType and result['state']=='PUBLISHED',result
            before=provider.executions
            responses,metrics=await queries(base,host,port,proposal,mode,('LEXICAL_BASELINE','HYBRID') if complete else ('LEXICAL_BASELINE','LEXICAL_ONLY'))
            await http_result(base,host,business,proposal,mode,complete)
            if complete:
                repeated,_=await queries(base,host,port,proposal,mode+'-reuse',('HYBRID',))
                assert metrics['HYBRID']==evaluate(tuple(QueryGold(q['query_id'],{v['object_id']:v['grade'] for v in q['labels']}) for q in proposal['queries']),repeated['HYBRID'],'HYBRID')
            assert provider.executions==before
            if mode=='RECOVER':assert provider.executions==0
            output.update(all_slots_applied=complete,query_new_sends=0,provider_executions=provider.executions)
        # A read-only diagnostic snapshot never substitutes for a business write.
        with sqlite3.connect('file:'+str(base/'instance/database/runtime.sqlite3')+'?mode=ro',uri=True) as reader:
            ledger={name:[json.loads(row[0]) for row in reader.execute('SELECT body FROM provider_'+name)] for name in ('requests','attempts','budget_windows','reservations','cost_items','handoffs')}
        save(base,mode+'-ledger.json',ledger)
        output.update(native_requests=len(ledger['requests']),native_attempts=len(ledger['attempts']),provider_executions=provider.executions,
            configuration_id={'database_id':host.stored.database_id,'snapshot_id':host.stored.snapshot_id},pending=provider.cleanup_pending)
    finally:
        closed=await host.close();output['closed']=closed
        save(base,mode+'-summary.json',output)
        assert closed
    print(json.dumps(output,ensure_ascii=False),flush=True)


if __name__=='__main__':asyncio.run(run(Path(sys.argv[1]).resolve(),sys.argv[2]))
