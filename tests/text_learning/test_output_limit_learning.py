"""Complete length responses terminate ordinary frozen learning as known failure."""
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from typing import cast
import unittest
from companion_memory.persistence import Committed,Found
from companion_memory.memory.formats import record,sequence
from companion_memory.self_model.management import SavedResolution
from companion_memory.self_model.formats import candidate_digest
from tests.text_learning.persona_terminal_support import PersonaScenario,length_response
from tests.text_learning.test_provider import response
from tests.text_learning.host_driver import confirm_local,complete_learning
from tests.runtime.configuration_support import event


class OutputLimitLearningTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_learning_known_failure_consumes_original_batch_without_handoff_or_objects(self):
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size=int(self.headers['Content-Length']);raw=self.rfile.read(size);requests.append(raw)
                user=json.loads(json.loads(raw)['messages'][1]['content'])
                if 'initial_input' in user:
                    encoded=response(json.dumps({'schema_version':1,'text':'No external preset was supplied.',
                        'initial_input_ids':[user['initial_input']['object_id']]}))
                else:encoded=length_response()
                body=encoded.split(b'\r\n\r\n',1)[1]
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close')
                self.end_headers();self.wfile.write(body)
            def log_message(self,format,*args):pass
        listener=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=lambda:listener.serve_forever(poll_interval=.01));thread.start()
        try:
            with TemporaryDirectory() as directory:
                scenario=PersonaScenario(Path(directory),listener.server_port)
                try:
                    await scenario.start();current=await scenario.run()
                    generated=await scenario.api.generate(scenario.run_id,1,cast(str,current['provider_operation_key']),time.monotonic()+5)
                    self.assertIs(type(generated),SavedResolution,generated);await scenario.join()
                    candidate=await scenario.candidate();current=await scenario.run();digest=candidate_digest(candidate)
                    reviewed=await scenario.api.review_initial_persona('review',scenario.run_id,cast(int,current['revision']),
                        cast(str,candidate['object_id']),1,digest,'APPROVE',time.monotonic()+5)
                    self.assertIs(type(reviewed),Committed,reviewed);await scenario.join();current=await scenario.run()
                    published=await scenario.api.publish_initial_persona('publish',scenario.run_id,cast(int,current['revision']),
                        cast(str,candidate['object_id']),2,digest,scenario.host.gate.epoch,time.monotonic()+5)
                    self.assertIs(type(published),Committed,published);await scenario.join()
                    host=scenario.host;runtime=host.runtime;assert runtime is not None and runtime.text_contexts is not None
                    registered=await confirm_local(lambda:host.register_entry('entry','entry','host','sample_platform','conversation'))
                    self.assertIs(type(registered),Committed,registered)
                    entry=runtime.bind_entry('entry');read=runtime.memory.bind_read(('self',),('read_subject','get_current'))
                    runtime.text_contexts.bind_entry(entry,read,('self',),(),({'kind':'REAL','context_id':None},))
                    for ordinal in range(3):
                        value=event('event-'+str(ordinal),'A controlled factual claim.');value['event_version']=2
                        accepted=await confirm_local(lambda:entry.accept_event('accept-'+str(ordinal),value))
                        self.assertIs(type(accepted),Committed,accepted)
                    learned=await complete_learning(entry,'learn')
                    self.assertIs(type(learned),Committed,learned);assert type(learned) is Committed
                    self.assertEqual(record(learned.receipt.result)['terminal'],'FAILED_DROPPED')
                    self.assertEqual(sequence(record(learned.receipt.result)['object_refs']),())
                    learning=next(row for row in scenario.bodies('provider_requests') if row['task_role']=='LEARNING')
                    self.assertEqual((learning['phase'],learning['outcome']),('TERMINAL','FAILED'))
                    self.assertEqual(learning['first_error']['reason'],'OUTPUT_LIMIT');self.assertIsNone(learning['handoff_id'])
                    self.assertEqual(len(scenario.rows('provider_handoffs')),1);self.assertEqual(len(requests),2)
                    identity=learned.receipt.identity
                    def terminal_audits():
                        # The automatic index may publish its own legitimate
                        # audits concurrently. Compare every finalization key
                        # of this command, not unrelated background operations.
                        return tuple(row for row in scenario.rows('audit_records')
                            if (audit:=json.loads(row[3]))['operation_identity']['owner_namespace']==identity.owner_namespace
                            and audit['operation_identity']['operation_kind']==identity.operation_kind)
                    before=terminal_audits();self.assertEqual(len(before),4)
                    ledger={table:scenario.bodies(table) for table in ('provider_requests','provider_attempts','provider_reservations')}
                    await entry.run_learning('learn')
                    self.assertEqual(len(requests),2);self.assertEqual(terminal_audits(),before)
                    self.assertEqual({table:scenario.bodies(table) for table in ledger},ledger)
                    operation=host.storage.bind_operation(host.assembly.command_definition('commit_content_without_objects'),'instance')
                    confirmed=await operation.read_receipt(identity.operation_key)
                    self.assertIs(type(confirmed),Found,confirmed);assert type(confirmed) is Found
                    self.assertEqual(confirmed.value,learned.receipt)
                finally:self.assertTrue(await scenario.host.close())
                restored=PersonaScenario(Path(directory),listener.server_port)
                try:
                    self.assertIs(type(await restored.start('OPEN_EXISTING')),Found)
                    self.assertEqual(len(requests),2)
                finally:self.assertTrue(await restored.host.close())
        finally:
            listener.shutdown();listener.server_close();thread.join(3);self.assertFalse(thread.is_alive())
