"""Independent processes create and confirm actual dream effects without resend."""
import asyncio
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import cast
from companion_memory.persistence import Committed,Found
from companion_memory.information.management import HostIdentity
from companion_memory.dream.port import OPERATIONS
from companion_memory.memory.formats import record
from companion_memory.self_model.current import Available
from tests.daily_cognition.test_reasoning import responses
from .host_support import make_dream_host
from .seed_support import establish_memory
from .test_review_host import score_output


async def run(root:Path,mode:str,port:int,wire_requests:list[bytes]):
    credentials=[];host=make_dream_host(root,port,credentials,with_self=True)
    receipts=[]
    try:
        value=await host.initialize(mode)
        if type(value) is not Found:raise AssertionError(value)
        control=host.combination.dream
        if control is None:raise AssertionError('Missing dream control')
        if mode=='CREATE_NEW':
            registered=await host.register_entry('register','entry','host','sample_platform','external')
            if type(registered) is not Committed:raise AssertionError(registered)
            await establish_memory(host,with_self=True)
            admin=await host.bind_dream(HostIdentity('admin','supervisor','host','entry',OPERATIONS,(),time.monotonic()+180))
            started=await admin.start_dream('start','run',1,1,mode='BACKGROUND')
            resumed=await admin.resume_dream('resume','run',1,1)
            if type(started) is not Committed or type(resumed) is not Committed:raise AssertionError((started,resumed))
            for index in range(10):
                if index in (6,7):await asyncio.sleep(30)
                result=await host.advance_dream()
                if type(result) is not Committed:raise AssertionError((index,result))
                receipts.append({'kind':result.receipt.identity.operation_kind,'key':result.receipt.identity.operation_key,
                    'commit_id':result.receipt.commit_id,'fingerprint':result.receipt.fingerprint})
            with (root/'original-receipts.json').open('x') as stream:json.dump(receipts,stream)
        else:
            if control.dispatch_enabled:raise AssertionError('Reopen enabled sending')
            originals=json.loads((root/'original-receipts.json').read_text())
            for original in originals:
                receipt=await control.confirm(original['kind'],original['key'])
                if type(receipt) is not Committed or receipt.receipt.commit_id!=original['commit_id'] or receipt.receipt.fingerprint!=original['fingerprint']:raise AssertionError('Original proof changed')
            receipts=originals
        if host.current_persona is None or host.assembly.memory.information is None:raise AssertionError('Missing public owners')
        current=await host.current_persona.port.read_current(time.monotonic()+5)
        if type(current) is not Available:raise AssertionError(current)
        memory=(await host.assembly.memory.information.current_page('',1))[0]
        run=await control.inspect('run')
        if run is None or run['state']!='COMPLETED':raise AssertionError(run)
        with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
            request_count=db.execute('SELECT count(*) FROM provider_requests').fetchone()[0]
            static=db.execute('SELECT assembly FROM application_metadata').fetchone()[0]
            tables=[row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            indexes=[row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index' ORDER BY name")]
        from companion_memory.persistence.content_codec import encode_content
        from companion_memory.configuration.cognition_identity import cognition_candidate_values
        configuration=cognition_candidate_values(host.configuration)
        materials=[]
        for raw in wire_requests:
            decoded=json.loads(raw);body=decoded['messages'][1]['content'];material=json.loads(body)
            materials.append({'wire_bytes':len(raw),'wire_digest':sha256(raw).hexdigest(),'system_bytes':len(decoded['messages'][0]['content'].encode()),
                'complete_material_bytes':len(body.encode()),'complete_material_digest':sha256(body.encode()).hexdigest(),
                'evidence_objects':len(material.get('evidence',[])),'evidence_source_count':sum(len(e['sources']) for e in material.get('evidence',[]))})
        print(json.dumps({'mode':mode,'persona_revision':current.value['revision'],'publication_id':current.value['publication_id'],
            'persona_digest':sha256(cast(str,current.value['text']).encode()).hexdigest(),'memory_revision':memory['revision'],
            'belief':record(memory['scores'])['belief'],'request_count':request_count,'new_credentials':len(credentials),
            'confirmed_receipts':len(receipts),'receipt_digest':sha256(json.dumps(receipts,sort_keys=True).encode()).hexdigest(),
            'static_bytes':len(static),'static_digest':sha256(static).hexdigest(),'tables':tables,'indexes':indexes,
            'capacity':{'qualified_materials':materials,'persona_public_projection_bytes':len(encode_content(current.value,8192)),
                'persona_text_bytes':len(cast(str,current.value['text']).encode()),'configuration_domains':{d['domain_id']:len(d['entries']) for d in configuration['domains']}},
            'commands':[{'owner':d.owner_namespace,'kind':d.operation_kind,'version':d.command_version} for d in host.combination.commands]},ensure_ascii=False))
    finally:
        if not await host.close():raise AssertionError('Actual resource cleanup incomplete')


if __name__=='__main__':
    root=Path(sys.argv[1]);mode=sys.argv[2]
    if mode=='CREATE_NEW':
        outputs=(score_output,{'schema_version':1,'text':'我是 Iris，明确区分观察、转述与推测。','basis_refs':[],'change_reason':'整理已有合成设定的表达。'},
            {'schema_version':1,'decision':'APPROVE','reason':'保持原有身份与来源边界。'})
        with responses(outputs) as (port,requests,failures):
            asyncio.run(run(root,mode,port,requests))
            if len(requests)!=3 or failures:raise AssertionError((len(requests),failures))
    else:asyncio.run(run(root,mode,9,[]))
