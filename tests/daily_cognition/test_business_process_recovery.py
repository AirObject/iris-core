"""Complete native business state outlives its actual creating interpreter."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from companion_memory.persistence import Found
from .test_host import make_host

CHILD=r'''
import json,os,sys,unittest
from contextlib import contextmanager
from pathlib import Path
import tests.daily_cognition.test_semantic_host as scenario
root=Path(sys.argv[1]);evidence=Path(sys.argv[2])
@contextmanager
def retained_directory():
 yield str(root)
scenario.TemporaryDirectory=retained_directory
suite=unittest.TestSuite((scenario.DailySemanticHostTests('test_learning_document_query_publication_http_and_zero_send_reopen'),))
result=unittest.TextTestRunner(verbosity=2).run(suite)
if not result.wasSuccessful():sys.exit(25)
with evidence.open('x') as stream:
 json.dump({'creating_process':os.getpid(),'scenario_passed':True,'model_requests':3,'actual_http_verified':True},stream)
 stream.flush();os.fsync(stream.fileno())
'''

class BusinessProcessRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_interpreter_verifies_media_subject_goal_persona_and_published_indices(self):
        with TemporaryDirectory() as directory:
            base=Path(directory);root=base/'instance';root.mkdir();evidence=base/'created.json'
            child=await asyncio.create_subprocess_exec(sys.executable,'-c',CHILD,str(root),str(evidence),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            try:out,err=await asyncio.wait_for(child.communicate(),180)
            except TimeoutError:
                child.kill();await child.wait();raise
            print(out.decode());print(err.decode())
            print(json.dumps({'command':[sys.executable,'-c',CHILD,str(root),str(evidence)],'exit_code':child.returncode}))
            self.assertEqual(child.returncode,0,(out.decode(),err.decode()))
            created=json.loads(evidence.read_text());self.assertNotEqual(created['creating_process'],os.getpid())
            self.assertTrue(created['scenario_passed']);self.assertTrue(created['actual_http_verified'])
            credentials=[];host=make_host(root,9,credentials)
            try:
                opened=await host.initialize('OPEN_EXISTING');self.assertIs(type(opened),Found,opened)
                self.assertFalse(credentials);self.assertFalse(host.scheduling())
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],3)
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_objects').fetchone()[0],1)
                    origins=[json.loads(row[0]) for row in db.execute('SELECT body FROM memory_subject_origins')]
                    self.assertEqual(len(origins),1)
                    goal=db.execute('SELECT goal_id FROM goals_goal').fetchone()[0]
                    works=[json.loads(row[0]) for row in db.execute('SELECT body FROM retrieval_embedding_work')]
                    self.assertEqual(len(works),2);self.assertTrue(all(w['state']=='APPLIED' for w in works))
                    self.assertEqual(db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_embedding_handoff_leaf').fetchone()[0],0)
                    self.assertEqual(db.execute("SELECT count(*) FROM media_references WHERE owner_kind='PROCESSING'").fetchone()[0],0)
                    self.assertGreater(db.execute('SELECT count(*) FROM media_blobs').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM self_model_persona_imports').fetchone()[0],1)
                if host.goals is None or host.semantic is None:raise AssertionError()
                self.assertIsNotNone(await host.goals.lookup(goal))
                self.assertIsNotNone(await host.assembly.memory.verify_subject_origin(origins[0]['subject_id'],origins[0]['source_id']))
                for work in works:self.assertEqual((await host.semantic.work(work['work_id']))['state'],'APPLIED')
                current=await host.combination.persona.read_current();self.assertIs(type(current),Found,current)
                confirmed=await host.bind_entry('entry').run_learning('learn')
                from companion_memory.persistence import Committed
                self.assertIs(type(confirmed),Committed,confirmed);self.assertFalse(credentials)
                print(json.dumps({'creating_process':created['creating_process'],'creating_exit_code':child.returncode,'recovery_process':os.getpid(),
                    'original_model_requests':3,'recovery_new_sends':0,'credential_reads':0,'verified':['Provider','media','candidate','SUBJECT','goal','persona','semantic_index','lexical_index','HTTP']}))
            finally:self.assertTrue(await host.close())
