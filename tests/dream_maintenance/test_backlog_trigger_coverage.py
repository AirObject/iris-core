"""Legal ingress backlog retains original batch coverage within trigger limits."""
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest
from companion_memory.persistence import Committed
from companion_memory.runtime.results import NotCommitted
from tests.runtime.configuration_support import event
from .host_support import make_dream_host
from .exit_support import initialize


class BacklogTriggerCoverageTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_trigger_queue_keeps_oldest_batch_and_reports_capacity(self):
        with TemporaryDirectory() as directory:
            credentials=[];host=make_dream_host(Path(directory),9,credentials)
            try:
                await initialize(host);entry=host.bind_entry('entry')
                for number in range(131):
                    key='event-'+str(number);raw=event(key,'合成积压输入');raw['event_version']=2
                    self.assertIs(type(await entry.accept_event(key,raw)),Committed)
                result=await entry.request_learning('explicit-full')
                self.assertIs(type(result),NotCommitted,result)
                if type(result) is NotCommitted:
                    if result.error is None:raise AssertionError('Missing capacity reason')
                    self.assertEqual((result.error.code,result.error.reason),('RESOURCE_BUSY','CAPACITY_REACHED'))
                with sqlite3.connect('file:'+str(Path(directory)/'database'/'runtime.sqlite3')+'?mode=ro',uri=True) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM runtime_daily_learning_triggers').fetchone()[0],128)
                    self.assertEqual(db.execute("SELECT count(DISTINCT json_extract(body,'$.batch_id')) FROM runtime_daily_learning_triggers").fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT count(*) FROM buffers_content_positions').fetchone()[0],131)
                self.assertFalse(credentials)
            finally:self.assertTrue(await host.close())
