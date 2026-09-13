"""Zero-price responsibility, HTTP error evidence and first-write revocation.

All credentials and usage records are synthetic. Only local sockets and temporary
SQLite are used; no observation in these tests represents supplier billing.
"""
from pathlib import Path
import socket
import threading
import time
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from companion_memory.provider.credentials import CredentialLease,CredentialResolver,Available
from companion_memory.provider.chat_transport import ChatTransport
from companion_memory.provider.resources import CancellationSource
from companion_memory.provider.values import as_record,freeze
from companion_memory.provider.chat_protocol import observe_usage
from companion_memory.provider.text_accounting import liability,normalize
from companion_memory.provider.text_stored_schema import validate_usage
from tests.text_learning.configuration_support import inputs
from tests.text_learning.provider_support import Fixture
from tests.text_learning.test_provider import response
from tests.provider.test_chat_transport import server
from companion_memory.provider import Ready,Pending


class GenerationEdgeTests(unittest.TestCase):
    def test_zero_rates_have_zero_known_liability_without_inventing_usage(self):
        with TemporaryDirectory() as directory:
            values=inputs(Path(directory))[0]['explicit_values'];account=values['provider.accounts'][0];profile=as_record(freeze(values['provider.profiles'][0],2048))
            account['price'].update(input_atoms_per_million=0,cached_atoms_per_million=0,output_atoms_per_million=0)
            account=as_record(freeze(account,4096));self.assertEqual(liability(account,profile),(0,0))
            unknown=normalize(observe_usage(None),account,profile,0);validate_usage(unknown)
            self.assertFalse(unknown['cost_complete']);self.assertIsNone(unknown['known_cost_atoms'])
            observed=observe_usage(as_record(freeze({'prompt_tokens':8,'completion_tokens':4,'total_tokens':12,'prompt_tokens_details':{'cached_tokens':0}},2048)))
            usage=normalize(observed,account,profile,0);validate_usage(usage)
            self.assertTrue(usage['cost_complete']);self.assertEqual(usage['known_cost_atoms'],0)

    def test_revocation_before_actual_header_write_sends_no_bytes_and_releases_lease(self):
        with TemporaryDirectory() as directory:
            settings=as_record(freeze(inputs(Path(directory))[5]['explicit_values']['provider.transport'],4096))
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1);listener.settimeout(5)
        lease=CredentialLease(b'independent-revocation-fixture');resolver=CredentialResolver(lambda *_:Available(lease))
        transport=ChatTransport.controlled_loopback(settings,resolver,time.monotonic,listener.getsockname()[1])
        at_header,release=threading.Event(),threading.Event();original=CredentialLease._send_header
        def blocked(value,connection,prefix):
            at_header.set()
            if not release.wait(5):raise TimeoutError()
            return original(value,connection,prefix)
        observed=[];errors=[]
        def receive():
            try:
                connection,_=listener.accept()
                with connection:connection.settimeout(5);observed.append(connection.recv(1))
            except Exception as error:errors.append(type(error).__name__)
        result=[];receiver=threading.Thread(target=receive);worker=threading.Thread(target=lambda:result.append(transport.exchange(b'{}',time.monotonic()+5,CancellationSource().token)))
        try:
            with patch.object(CredentialLease,'_send_header',blocked):
                receiver.start();worker.start()
                self.assertTrue(at_header.wait(3));lease.revoke();release.set();worker.join(5);receiver.join(5)
            self.assertFalse(worker.is_alive());self.assertFalse(receiver.is_alive());self.assertEqual(errors,[])
            self.assertEqual(observed,[b'']);self.assertEqual(result[0].state,'NOT_SENT');self.assertTrue(lease.released)
        finally:
            release.set();listener.close()
            if worker.ident is not None:worker.join(5)
            if receiver.ident is not None:receiver.join(5)


class ErrorUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_response_retains_reported_usage_and_full_unknown_responsibility(self):
        wire=response().replace(b'HTTP/1.1 200 OK',b'HTTP/1.1 503 Unavailable',1)
        with TemporaryDirectory() as directory,server(wire) as (port,requests,failures):
            fixture=Fixture(Path(directory),port)
            try:
                self.assertIs(type(await fixture.initialize()),Ready)
                result=await fixture.work.generate(fixture.request());self.assertIs(type(result),Pending,result)
                import sqlite3,json
                with sqlite3.connect(Path(directory)/'database'/'runtime.sqlite3') as db:
                    attempt=json.loads(db.execute('SELECT body FROM provider_attempts').fetchone()[0]);usage=attempt['usage']
                    reservation=json.loads(db.execute('SELECT body FROM provider_reservations').fetchone()[0])
                self.assertEqual(usage['raw_usage']['prompt_tokens'],8);self.assertEqual(usage['raw_usage']['total_tokens'],12)
                self.assertFalse(usage['cost_complete']);self.assertEqual(usage['held_atoms'],reservation['reserved_atoms']);self.assertGreater(usage['held_atoms'],0)
                self.assertEqual(len(requests),1);self.assertEqual(failures,[])
            finally:await fixture.close()
