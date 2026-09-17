"""A changed indirect basis fences publication and marks a current summary stale."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import MappingProxyType
from typing import cast
import unittest
from companion_memory.persistence import Committed,Found
from companion_memory.memory.formats import record
from companion_memory.self_model.current import Available
from tests.daily_cognition.test_reasoning import responses
from .host_support import make_dream_host
from .seed_support import establish_memory
from .test_independent_roots import add_basis
from .test_influence_host import forget
from .test_stage_host import quiet


class PersonaBasisTests(unittest.IsolatedAsyncioTestCase):
    async def test_indirect_source_loss_before_and_after_publication(self):
        def generate(request):
            body=json.loads(request['messages'][1]['content'])
            return {'schema_version':1,'text':'我是 Iris，在合成表达中区分观察、转述与推测。',
                'basis_refs':[{'object_id':item['object']['object_id'],'revision':item['object']['revision']} for item in body['evidence']],
                'change_reason':'依据完整当前合成自我材料整理表达。'}
        for before_publish in (True,False):
            with self.subTest(before_publish=before_publish),TemporaryDirectory() as directory,responses((generate,{'schema_version':1,'decision':'APPROVE','reason':'独立检查当前来源后认可。'})) as (port,requests,failures):
                host=make_dream_host(Path(directory),port,[],with_self=True)
                try:
                    self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                    self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                    first=await establish_memory(host,with_self=True);fixed=host.fixed;c=host.combination.dream;persona=host.current_persona
                    info=host.assembly.memory.information
                    if fixed is None or c is None or info is None or persona is None:raise AssertionError('Missing native owners')
                    old=await persona.port.read_current(time.monotonic()+5);self.assertIs(type(old),Available)
                    self.assertIs(type(await fixed.establish_one(fixed.envelope('fixed_establish','second',MappingProxyType({
                        'set_id':'fixed-set','expected_revision':15,'ordinal':1,'expected_member_revision':1}),1),time.monotonic()+5)),Committed)
                    second=next(v for v in await info.current_page('',16) if v['object_id']!=first['object_id'])
                    added=await add_basis(host,first,second,'basis');self.assertIs(type(added),Committed,added)
                    self.assertIs(type(await c.execute('start_background_dream','start',{'run_id':'run','expected_revision':1,'mode_epoch':1,'trigger':'MANUAL','local_date':None},actor='admin')),Committed)
                    self.assertIs(type(await c.execute('resume_dream','resume',{'run_id':'run','expected_revision':1,'mode_epoch':1},actor='admin')),Committed)
                    self.assertIs(type(await c.execute('prepare_periodic_persona','prepare',{'run_id':'run','expected_revision':2,'mode_epoch':1},actor='dream_coordinator')),Committed)
                    for expected in ('GENERATED','REVIEWED'):
                        await quiet(host);result=await host.advance_dream();self.assertIs(type(result),Committed,result)
                        if type(result) is not Committed:raise AssertionError(result)
                        self.assertEqual(record(result.receipt.result)['state'],expected)
                    if before_publish:self.assertIs(type(await forget(host,second)),Committed)
                    result=await host.advance_dream();self.assertIs(type(result),Committed,result)
                    if type(result) is not Committed:raise AssertionError(result)
                    self.assertEqual(record(result.receipt.result)['state'],'KEPT_PREVIOUS' if before_publish else 'PUBLISHED')
                    if not before_publish:self.assertIs(type(await forget(host,second)),Committed)
                    observed=await persona.port.read_current(time.monotonic()+5);self.assertIs(type(observed),Available,observed)
                    if type(observed) is not Available:raise AssertionError(observed)
                    if before_publish:self.assertEqual(observed,old)
                    else:self.assertTrue(observed.value['stale']);self.assertEqual(observed.value['revision'],2)
                    # Public current-only capabilities never expose retained
                    # candidate leaves or a historical-body lookup operation.
                    self.assertFalse(hasattr(persona.port,'read_history'));self.assertFalse(hasattr(persona.port,'read_candidate'))
                    self.assertEqual(len(requests),2);self.assertFalse(failures)
                finally:self.assertTrue(await host.close())
