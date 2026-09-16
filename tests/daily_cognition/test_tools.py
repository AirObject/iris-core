"""Native current-read tool owners share one real daily SQLite assembly."""
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import time
from types import MappingProxyType
from typing import cast
import unittest
from companion_memory.persistence import Committed
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.memory.service import MemoryService
from companion_memory.goals.service import GoalsService
from companion_memory.state.service import StateOwner
from companion_memory.retrieval.index import LocalIndex
from companion_memory.retrieval.semantic_work import SemanticWork
from companion_memory.retrieval.semantic_generation import SemanticGenerations
from companion_memory.retrieval.semantic_cache import SemanticQueryCache
from companion_memory.retrieval.semantic_query import SemanticQuery
from companion_memory.retrieval.semantic_files import VectorFiles
from companion_memory.cognition.daily_tools import DailyReadTools,DailyReadGrant
from .test_provider import ProviderFixture

class ToolFixture(ProviderFixture):
    async def open(self):
        await super().open();c=self.c
        self.goals=GoalsService(c.information_catalogs[2],self.storage,self.stored,'instance')
        from companion_memory.runtime.candidate_goals import CandidateGoalEffects
        c.content.goal_effects=CandidateGoalEffects(self.goals,c.content.memory)
        self.state=StateOwner(c.information_catalogs[1],self.storage,self.stored,'instance')
        self.index=LocalIndex(c.retrieval_catalog,self.storage,self.stored,'instance')
        information=c.content.memory.bind_information(self.stored);self.index.bind_memory(information)
        c.initializer.bind(self.storage,self.stored,'instance',{'goals':self.goals,'state':self.state,'retrieval':self.index,'memory':information},lambda uow:self.config.participate_snapshot(uow,self.stored))
        original=await c.initializer.initialize('information',time.time_ns()//1000)
        if type(original) is not Committed:raise AssertionError(original)
        self.index.bind_publication_operation(self.storage.bind_operation(next(d for d in c.management.commands if d.operation_kind=='index_publish'),'instance'))
        await c.initializer.recover('information')
        semantic=c.content.memory.semantic
        if semantic is None:raise AssertionError('Native semantic memory missing')
        c.work=SemanticWork(c.retrieval_catalog,self.storage,self.stored,'instance',semantic,self.provider,c.commands,lambda:None,lambda:None,lambda digest:False)
        self.files=VectorFiles(Path(cast(str,self.value.text.record('retrieval.semantic_storage')['index_root'])),file_limit_bytes=41943040,index_total_bytes=83886080,reader_limit=2)
        c.generations=SemanticGenerations(c.retrieval_catalog,self.storage,self.stored,'instance',semantic,self.files,lambda:None)
        c.cache=SemanticQueryCache(c.retrieval_catalog,self.storage,self.stored,'instance',next(d for d in c.semantic.commands if d.operation_kind=='record_result'))
        from companion_memory.runtime.content_service import ContentRuntimeService
        self.runtime=ContentRuntimeService(c.content,self.provider,None,'daily_learning',None,embedding=self.provider)
        initialized=await self.runtime.execute('initialize_content_runtime','initialize',{})
        if type(initialized) is not Committed:raise AssertionError(initialized)
        recovered=await self.runtime.initialize()
        from companion_memory.persistence import Found
        if type(recovered) is not Found:raise AssertionError(recovered)
        self.memory=self.runtime.memory
        self.port=self.memory.bind_query_scope(include_forgotten=False)
        self.tools=DailyReadTools(self.memory,self.index,SemanticQuery(c.work,c.cache,c.generations),self.goals,lambda:None)
        return self
    async def close(self):
        if not self.tools.close():raise AssertionError('Actual read is still occupied')
        self.memory.release_query_scope(self.port)
        if not await self.provider.close(time.monotonic()+5):raise AssertionError('Provider still occupied')
        if self.c.cache is not None:self.c.cache.close()
        if not await self.runtime.close():raise AssertionError('Runtime still occupied')
        self.files.close();self.index.close();self.goals.close();self.state.close()
        await super().close()

class DailyToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_native_read_tools_report_missing_and_degradation_without_writes_or_sends(self):
        with TemporaryDirectory() as directory:
            fixture=await ToolFixture(Path(directory),9).open()
            world=MappingProxyType({'kind':'REAL','context_id':None})
            grant=fixture.tools.bind(fixture.port,'entry','partition',('unknown-subject',),(world,))
            with sqlite3.connect(fixture.path) as db:before=db.execute('SELECT count(*) FROM audit_records').fetchone()[0]
            try:
                with self.assertRaises(TypeError):DailyReadGrant()
                requests=(
                    {'name':'search_memories','arguments':{'query':'杯子','world_scope':dict(world),'limit':4}},
                    {'name':'read_memories','arguments':{'refs':[{'object_id':'unknown-memory','expected_revision':1}]}},
                    {'name':'read_subjects','arguments':{'subject_ids':['unknown-subject']}},
                    {'name':'list_goals','arguments':{'world_scope':'REAL','limit':4}},
                )
                results=[]
                for request in requests:results.append(await fixture.tools.read(grant,request,time.monotonic()+5))
                self.assertIn('QUERY_VECTOR_MISSING',results[0]['reasons'])
                self.assertTrue(results[0]['truncated']);self.assertFalse(results[0]['items'])
                self.assertEqual(results[1]['missing'],(MappingProxyType({'object_id':'unknown-memory','reason':'NOT_FOUND'}),))
                self.assertEqual(results[2]['missing'],(MappingProxyType({'object_id':'unknown-subject','reason':'NOT_FOUND'}),))
                self.assertFalse(results[3]['items']);self.assertFalse(results[3]['truncated'])
                with self.assertRaises(OwnerFailure):
                    await fixture.tools.read(grant,{'name':'read_subjects','arguments':{'subject_ids':['not-granted']}},time.monotonic()+5)
                with sqlite3.connect(fixture.path) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM audit_records').fetchone()[0],before)
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],0)
                self.assertFalse(fixture.credentials)
            finally:
                fixture.tools.release(grant);await fixture.close()
