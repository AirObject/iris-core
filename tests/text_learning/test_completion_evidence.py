"""Actual completion keys, necessary audits and restricted result-owner reads.

Synthetic HTTP output drives real local ledger commits. Tests tamper retained
receipts and race capability revocation without issuing another model request.
"""
import asyncio
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest

from companion_memory.provider import Ready, Completed, Failed, ResultGrant
from companion_memory.provider.completion_evidence import ConfirmedCompletion, issued_completion
from companion_memory.provider.terminal_evidence import TerminalVerified, VerifiedTerminal
from tests.provider.test_chat_transport import server
from tests.text_learning.provider_support import Fixture
from tests.text_learning.test_provider import response


class CompletionEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_known_no_send_confirms_the_actual_registration_completion(self):
        with TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory), 1)
            try:
                self.assertIs(type(await fixture.initialize()), Ready)
                fixture.gate.mode = 'FOCUSED'
                result = await fixture.work.generate(fixture.request())
                self.assertIs(type(result), Completed, result); assert type(result) is Completed
                self.assertEqual(result.record['outcome'], 'MODE_BLOCKED')
                self.assertEqual(result.record['attempt_count'], 0)
                owner = fixture.service.bind_result_owner(ResultGrant('cognition', (str(result.record['object_id']),)))
                terminal = await owner.verify_terminal(result.record['object_id'])
                self.assertIs(type(terminal), TerminalVerified, terminal); assert type(terminal) is TerminalVerified
                completion = await owner.confirm_completion(terminal.value)
                self.assertIs(type(completion), ConfirmedCompletion, completion); assert type(completion) is ConfirmedCompletion
                self.assertEqual(completion.operation['operation_kind'], 'register')
                self.assertFalse(terminal.value.confirmed_sent)
                self.assertEqual(fixture.leases, [])
            finally:
                await fixture.close()

    async def test_original_completion_is_audited_and_recovered_without_new_model_work(self):
        with TemporaryDirectory() as directory, server(response()) as (port, requests, failures):
            root = Path(directory)
            fixture = Fixture(root, port)
            try:
                self.assertIs(type(await fixture.initialize()), Ready)
                result = await fixture.work.generate(fixture.request())
                self.assertIs(type(result), Completed); assert type(result) is Completed
                request_id = str(result.record['object_id'])
                owner = fixture.service.bind_result_owner(ResultGrant('cognition', (request_id,)))
                terminal = await owner.verify_terminal(request_id)
                self.assertIs(type(terminal), TerminalVerified); assert type(terminal) is TerminalVerified
                confirmation = await owner.confirm_completion(terminal.value)
                self.assertIs(type(confirmation), ConfirmedCompletion); assert type(confirmation) is ConfirmedCompletion
                self.assertTrue(issued_completion(confirmation))
                self.assertIs(confirmation.terminal, terminal.value)
                operation = dict(confirmation.operation)
                self.assertEqual(operation['operation_kind'], 'settle')
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    raw = db.execute('SELECT receipt FROM operation_receipts WHERE owner_namespace=? AND operation_kind=? AND scope_id=? AND operation_key=?',
                        tuple(operation[name] for name in ('owner_namespace','operation_kind','scope_id','operation_key'))).fetchone()
                    self.assertIsNotNone(raw)
                    self.assertEqual(json.loads(raw[0])['result'], {'object_id':request_id,'revision':result.record['revision']})
                denied = fixture.service.bind_result_owner(ResultGrant('other-owner', (request_id,)))
                self.assertIs(type(await denied.confirm_completion(terminal.value)), Failed)
                self.assertIs(type(await owner.confirm_completion(object.__new__(VerifiedTerminal))), Failed)
                self.assertFalse(issued_completion(object.__new__(ConfirmedCompletion)))
            finally:
                await fixture.close()
            reopened = Fixture(root, port)
            try:
                self.assertIs(type(await reopened.initialize('OPEN_EXISTING')), Ready)
                owner = reopened.service.bind_result_owner(ResultGrant('cognition', (request_id,)))
                terminal = await owner.verify_terminal(request_id)
                self.assertIs(type(terminal), TerminalVerified); assert type(terminal) is TerminalVerified
                confirmation = await owner.confirm_completion(terminal.value)
                self.assertIs(type(confirmation), ConfirmedCompletion); assert type(confirmation) is ConfirmedCompletion
                self.assertEqual(dict(confirmation.operation), operation)
                self.assertEqual(len(requests), 1)
                self.assertEqual(reopened.leases, [])
                self.assertEqual(failures, [])
            finally:
                await reopened.close()

    async def test_receipt_result_tampering_cannot_claim_a_different_audited_root(self):
        with TemporaryDirectory() as directory, server(response()) as (port, requests, failures):
            root = Path(directory); fixture = Fixture(root, port)
            try:
                self.assertIs(type(await fixture.initialize()), Ready)
                result = await fixture.work.generate(fixture.request())
                assert type(result) is Completed
                request_id = str(result.record['object_id'])
                owner = fixture.service.bind_result_owner(ResultGrant('cognition', (request_id,)))
                terminal = await owner.verify_terminal(request_id)
                assert type(terminal) is TerminalVerified
                # Corrupt only the completion result, preserving its necessary
                # audit. Reading the receipt must reject their disagreement.
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    commit_id, raw = db.execute("SELECT commit_id,receipt FROM operation_receipts WHERE owner_namespace='provider' AND operation_kind='settle'").fetchone()
                    body = json.loads(raw); body['result']['revision'] += 1
                    db.execute('UPDATE operation_receipts SET receipt=? WHERE commit_id=?',
                        (json.dumps(body,separators=(',',':'),sort_keys=True).encode(),commit_id))
                confirmed = await owner.confirm_completion(terminal.value)
                self.assertIs(type(confirmed), Failed, confirmed)
                self.assertEqual(len(requests), 1)
            finally:
                await fixture.close()

    async def test_revocation_before_delivery_and_reader_ownership_reject_late_confirmation(self):
        with TemporaryDirectory() as directory, server(response()) as (port, requests, failures):
            fixture = Fixture(Path(directory), port)
            entered, release = threading.Event(), threading.Event()
            try:
                self.assertIs(type(await fixture.initialize()), Ready)
                result = await fixture.work.generate(fixture.request()); assert type(result) is Completed
                owner = fixture.service.bind_result_owner(ResultGrant('cognition', (str(result.record['object_id']),)))
                terminal = await owner.verify_terminal(result.record['object_id']); assert type(terminal) is TerminalVerified
                def before(sql):
                    if sql.startswith('SELECT commit_id, length(receipt)'):
                        entered.set(); release.wait(5)
                fixture.hooks.before = before
                task = asyncio.create_task(owner.confirm_completion(terminal.value))
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                concurrent = await owner.confirm_completion(terminal.value)
                self.assertIs(type(concurrent), Failed); assert type(concurrent) is Failed
                self.assertEqual(concurrent.error.reason, 'ADMISSION_BUSY')
                fixture.service.revoke(owner)
                release.set()
                self.assertIs(type(await task), Failed)
                self.assertEqual(len(fixture.service._completion_evidence), 0)
                self.assertEqual(len(requests), 1)
            finally:
                release.set(); fixture.hooks.before = lambda sql: None
                await fixture.close()
