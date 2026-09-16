"""Purpose mismatch and real journal fsync failure cannot trigger model HTTP."""
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from companion_memory.persistence import Found,Committed
from companion_memory.persistence.owned_statements import OwnerFailure
from tests.runtime.configuration_support import event
from .test_host import make_host
from .trial_support import controlled_activation


class TrialAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_purpose_and_unconfirmed_journal_never_send_or_refund_original_slot(self):
        for wrong_purpose in (True,False):
            with self.subTest(wrong_purpose=wrong_purpose),TemporaryDirectory() as directory:
                root=Path(directory);credentials=[];host=make_host(root,9,credentials)
                try:
                    self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                    grant=controlled_activation(host)
                    if wrong_purpose:grant.authority.select=lambda request:'media:0'
                    authorization=host.bind_trial_activation(grant);await authorization.resume()
                    self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                    entry=host.bind_entry('entry')
                    for ordinal in range(3):
                        value=event('event-'+str(ordinal),'合成用途与清理验证。');value['event_version']=2
                        self.assertIs(type(await entry.accept_event('accept-'+str(ordinal),value)),Committed)
                    real_fsync=os.fsync;calls=[]
                    def fail_registered(fd):
                        if fd==authorization.fd:
                            calls.append(fd)
                            if not wrong_purpose and len(calls)==2:raise OSError('Injected original journal sync failure')
                        return real_fsync(fd)
                    with patch('companion_memory.runtime.daily_trial_authorization.os.fsync',side_effect=fail_registered):
                        self.assertIs(type(await host.resume_learning('resume')),Committed)
                        observed=await entry.run_learning('learn')
                    self.assertFalse(credentials)
                    with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],0 if wrong_purpose else 1)
                        if not wrong_purpose:
                            self.assertEqual(db.execute("SELECT json_extract(body,'$.state'),json_extract(body,'$.confirmed_started') FROM provider_attempts").fetchall(),[('NOT_SENT',0)],observed)
                            self.assertEqual(db.execute("SELECT json_extract(body,'$.terminal_error.reason') FROM provider_attempts").fetchone()[0],'COMMIT_UNCONFIRMED')
                    if not wrong_purpose:
                        self.assertTrue(authorization.faulted);self.assertFalse(authorization.active)
                        self.assertEqual(len(authorization.entries),1)
                        with self.assertRaises(OwnerFailure):await authorization.resume()
                    self.assertEqual(len(authorization.entries),int(not wrong_purpose))
                finally:
                    if host.dispatch is not None:await host.dispatch.wait_actual()
                    self.assertTrue(await host.close())
                host=make_host(root,9,credentials)
                try:
                    opened=await host.initialize('OPEN_EXISTING');self.assertIs(type(opened),Found,opened)
                    restored=host.bind_trial_activation(grant)
                    self.assertFalse(restored.active);self.assertEqual(len(restored.entries),int(not wrong_purpose));self.assertFalse(credentials)
                    self.assertTrue(all(e['state']=='REGISTERED' for e in restored.entries.values()))
                finally:self.assertTrue(await host.close())
