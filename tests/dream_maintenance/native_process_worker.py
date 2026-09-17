"""A standalone native interpreter for original atomic memory and control recovery."""
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import time
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import Found,Committed
from tests.semantic.test_semantic_host import material
from .host_support import make_dream_host


async def run(root:Path,mode:str):
    credentials=[];host=make_dream_host(root,9,credentials)
    try:
        opened=await host.initialize(mode)
        if type(opened) is not Found:raise AssertionError((host.phase,opened))
        control=host.combination.dream
        if control is None or host.stored is None:raise AssertionError('Missing native owners')
        if mode=='CREATE_NEW':
            if type(await host.register_entry('register','entry','host','sample_platform','external')) is not Committed:raise AssertionError('Entry')
            fixed=host.fixed
            if fixed is None:raise AssertionError('Missing fixed owner')
            claims=host.resources.review.claims
            def env(kind,key,payload):return fixed.envelope(kind,key,MappingProxyType(payload),1)
            config={'database_id':host.stored.database_id,'instance_id':'instance','snapshot_id':host.stored.snapshot_id}
            begun=await fixed.begin(env('fixed_begin','begin',{'set_id':'fixed-set','config':config,
                **{k:claims[k] for k in ('manifest_digest','review_ref','review_digest')}}),time.monotonic()+5)
            if type(begun) is not Committed:raise AssertionError(begun)
            for ordinal,member in enumerate(material()):
                added=await fixed.add_member(env('fixed_add_member','add-'+str(ordinal),{'set_id':'fixed-set','expected_revision':ordinal+1,**member}),time.monotonic()+5)
                if type(added) is not Committed:raise AssertionError(added)
            sealed=await fixed.seal(env('fixed_seal','seal',{'set_id':'fixed-set','expected_revision':13}),time.monotonic()+5)
            if type(sealed) is not Committed:raise AssertionError(sealed)
            established=await fixed.establish_one(env('fixed_establish','establish',{'set_id':'fixed-set','expected_revision':14,'ordinal':0,'expected_member_revision':1}),time.monotonic()+5)
            if type(established) is not Committed:raise AssertionError(established)
            with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:before=json.loads(db.execute('SELECT body FROM memory_objects').fetchone()[0])
            now=before['created_at_us']+10*86400000000;control.now=lambda:now
            for kind,key,payload in (
                ('start_background_dream','start',{'run_id':'run','expected_revision':1,'mode_epoch':1,'trigger':'MANUAL','local_date':None}),
                ('resume_dream','resume',{'run_id':'run','expected_revision':1,'mode_epoch':1}),
                ('decay_dream_memory','decay',{'run_id':'run','expected_revision':2,'mode_epoch':1,'memory_id':before['object_id'],'memory_revision':1,'observed_at_us':now}),
                ('abort_background_dream','abort',{'run_id':'run','expected_revision':3,'mode_epoch':1})):
                outcome=await control.execute(kind,key,payload,actor='admin')
                if type(outcome) is not Committed:raise AssertionError(outcome)
        else:
            if control.dispatch_enabled or host.scheduling():raise AssertionError('Startup reopened dispatch')
        original=await control.confirm('decay_dream_memory','decay')
        if type(original) is not Committed:raise AssertionError(original)
        with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
            current=json.loads(db.execute('SELECT body FROM memory_objects').fetchone()[0])
            anchors=db.execute('SELECT count(*) FROM memory_maintenance_anchors').fetchone()[0]
            requests=db.execute('SELECT count(*) FROM provider_requests').fetchone()[0]
            steps=db.execute('SELECT count(*) FROM dream_steps').fetchone()[0]
        if requests or credentials or anchors!=1 or steps!=1 or current['revision']!=2:raise AssertionError('Original material differs')
        print(json.dumps({'commit_id':original.receipt.commit_id,'fingerprint':original.receipt.fingerprint,
            'object_revision':current['revision'],'requests':requests,'credentials':len(credentials),'mode':mode}),flush=True)
    finally:
        if not await host.close():raise AssertionError('Actual resources remain')


if __name__=='__main__':
    asyncio.run(run(Path(sys.argv[1]),sys.argv[2]))
