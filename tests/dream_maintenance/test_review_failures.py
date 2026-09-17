"""Actual owner competition and complete invalid results cannot overwrite memory."""
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from typing import cast
from companion_memory.persistence import Committed,Found
from companion_memory.information.management import HostIdentity
from companion_memory.dream.port import OPERATIONS
from companion_memory.memory.formats import record
from tests.daily_cognition.test_reasoning import responses
from .host_support import make_dream_host
from .seed_support import establish_memory
from .test_review_host import score_output


class ReviewFailures(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_formal_edit_defers_consumed_candidate_without_resend(self):
        with TemporaryDirectory() as directory,responses((score_output,)) as (port,requests,failures):
            host=make_dream_host(Path(directory),port,[],with_self=True)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                await establish_memory(host,with_self=True)
                admin=await host.bind_dream(HostIdentity('admin','supervisor','host','entry',OPERATIONS,(),time.monotonic()+120))
                self.assertIs(type(await admin.start_dream('start','run',1,1,mode='BACKGROUND')),Committed)
                self.assertIs(type(await admin.resume_dream('resume','run',1,1)),Committed)
                for _ in range(3):self.assertIs(type(await host.advance_dream()),Committed)
                information=host.assembly.memory.information
                if information is None or host.runtime is None:raise AssertionError('Missing native owners')
                current=(await information.current_page('',1))[0]
                from companion_memory.persistence.content_codec import decode_content
                from companion_memory.memory.formats import isolate_links
                oid=cast(str,current['object_id']);revision=cast(int,current['revision'])
                stored=(await host.assembly.memory.rows.read('links_get',{'object_id':oid}))[0]
                links=isolate_links(decode_content(cast(str,stored['body']).encode(),2048),oid,revision)
                new_links={name:tuple(dict(record(link))|{('object_revision' if name=='sources' else 'dependent_revision'):revision+1} for link in cast(tuple,items)) for name,items in links.items()}
                proposed=dict(current)|{'revision':revision+1,'modified_at_us':time.time_ns()//1000,
                    'scores':dict(record(current['scores']))|{'belief':81,'belief_reason':'并发正式维护的新判断。'}}
                result=await host.runtime.maintenance.bind((oid,)).set_scores('concurrent',{'change_version':1,'action':'SET_SCORES','target_id':oid,
                    'expected_revision':revision,'proposed_value':proposed,'links':new_links})
                self.assertIs(type(result),Committed,result)
                deferred=await host.advance_dream();self.assertIs(type(deferred),Committed,deferred)
                if type(deferred) is not Committed:raise AssertionError(deferred)
                self.assertEqual(record(deferred.receipt.result)['state'],'DEFERRED_CONFLICT')
                after=(await information.current_page('',1))[0]
                self.assertEqual(record(after['scores'])['belief'],81);self.assertEqual(after['revision'],revision+1)
                self.assertEqual(len(requests),1);self.assertFalse(failures)
                run=await admin.inspect_dream('run')
                if run is None:raise AssertionError('Missing run')
                self.assertEqual(run['steps_deferred'],1);self.assertIsNone(run['active_step_id'])
                self.assertIsNotNone(await admin.confirm_dream_step(deferred.receipt.identity.operation_kind,deferred.receipt.identity.operation_key))
                self.assertEqual(len(requests),1)
            finally:self.assertTrue(await host.close())

    async def test_undeclared_action_is_retained_failed_then_disposed_without_business_write(self):
        invalid={'schema_version':1,'decision':'CHANGE','reason':'非法状态请求。','actions':[{'action':'DELETE_OBJECT','object_id':'invented'}]}
        with TemporaryDirectory() as directory,responses((invalid,)) as (port,requests,failures):
            host=make_dream_host(Path(directory),port,[],with_self=True)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                await establish_memory(host,with_self=True)
                admin=await host.bind_dream(HostIdentity('admin','supervisor','host','entry',OPERATIONS,(),time.monotonic()+120))
                self.assertIs(type(await admin.start_dream('start','run',1,1,mode='BACKGROUND')),Committed)
                self.assertIs(type(await admin.resume_dream('resume','run',1,1)),Committed)
                for _ in range(2):self.assertIs(type(await host.advance_dream()),Committed)
                information=host.assembly.memory.information
                if information is None:raise AssertionError('Missing memory')
                before=await information.current_page('',16)
                consumed=await host.advance_dream();self.assertIs(type(consumed),Committed,consumed)
                if type(consumed) is not Committed:raise AssertionError(consumed)
                self.assertEqual(record(consumed.receipt.result)['state'],'FAILED')
                disposed=await host.advance_dream();self.assertIs(type(disposed),Committed,disposed)
                if type(disposed) is not Committed:raise AssertionError(disposed)
                self.assertEqual(set(record(record(disposed.receipt.result)['facts'])),{'dream'})
                self.assertEqual(await information.current_page('',16),before)
                owner=host.combination.dream_review
                if owner is None:raise AssertionError('Missing review')
                work=(await owner.rows.page('dream_work'))[0]
                self.assertIsNotNone(work['result_material_id']);self.assertIsNone(work['candidate_id'])
                self.assertEqual(work['reason'],'INVALID_RESPONSE');self.assertEqual(len(requests),1);self.assertFalse(failures)
            finally:self.assertTrue(await host.close())
