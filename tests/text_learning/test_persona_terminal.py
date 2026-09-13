"""Public management regression for length, exact revision zero and absent proof.

These are deterministic protocol observations backed by real SQLite and native
owner capabilities. None is evidence about supplier eligibility or output quality.
"""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import threading
import time
from typing import cast
import unittest
from unittest.mock import patch
from companion_memory.persistence import Committed,NotCommitted,ResultBoundCommand
from companion_memory.persistence.schema import InvalidValue
from companion_memory.provider import WorkPort,NotFound as ProviderMissing
from companion_memory.provider.unsent_evidence import VerifiedUnsent
from companion_memory.self_model.management import SavedResolution,LocalUnconfirmed
from companion_memory.self_model.formats import isolate_candidate
from tests.persistence.support import Hooks
from tests.provider.test_chat_transport import server
from tests.text_learning.persona_terminal_support import PersonaScenario,length_response


class PersonaTerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_length_retains_known_failure_independent_of_usage_and_reopens(self):
        for usage in ('complete','missing','invalid'):
            with self.subTest(usage=usage),TemporaryDirectory() as directory,server(length_response(usage)) as (port,requests,failures):
                scenario=PersonaScenario(Path(directory),port)
                try:
                    await scenario.start();current=await scenario.run()
                    saved=await scenario.api.generate(scenario.run_id,1,cast(str,current['provider_operation_key']),time.monotonic()+5)
                    self.assertIs(type(saved),SavedResolution,saved);await scenario.join()
                    proposal=await scenario.candidate()
                    self.assertEqual((proposal['resolution'],proposal['failure_reason']),('KNOWN_FAILED','OUTPUT_LIMIT'))
                    request=scenario.bodies('provider_requests')[0];attempt=scenario.bodies('provider_attempts')[0]
                    self.assertEqual((request['phase'],request['outcome']),('TERMINAL','FAILED'))
                    self.assertEqual((attempt['state'],attempt['logical_outcome']),('COMPLETED','FAILED'))
                    self.assertEqual(request['first_error'],{'code':'ADAPTER_FAILED','field':'adapter','reason':'OUTPUT_LIMIT'})
                    self.assertEqual(request['first_error'],attempt['first_error']);self.assertIsNone(request['handoff_id'])
                    self.assertEqual(scenario.rows('provider_handoffs'),[])
                    self.assertEqual(attempt['usage']['cost_complete'],usage=='complete')
                    self.assertEqual(attempt['usage']['held_atoms']==0,usage=='complete')
                    self.assertIsNone(proposal['text']);self.assertEqual(len(requests),1);self.assertEqual(failures,[])
                    if usage!='complete':self.assertIsNot(type(await scenario.retry()),Committed)
                    before=scenario.bodies('provider_requests')
                finally:self.assertTrue(await scenario.host.close())
                restored=PersonaScenario(Path(directory),port)
                try:
                    await restored.start('OPEN_EXISTING')
                    self.assertEqual(await restored.candidate(),proposal)
                    self.assertEqual(restored.bodies('provider_requests'),before);self.assertEqual(len(requests),1)
                finally:self.assertTrue(await restored.host.close())

    async def test_absent_result_original_replay_three_generations_and_no_provider_rows(self):
        with TemporaryDirectory() as directory:
            scenario=PersonaScenario(Path(directory),1)
            try:
                await scenario.start();old_values=None;original=None
                for generation in (1,2,3):
                    current=await scenario.associate('associate-'+str(generation))
                    values=(scenario.run_id,cast(int,current['revision']),generation,cast(str,current['provider_operation_key']),0)
                    saved=await scenario.api.record_initial_persona_resolution('resolve-'+str(generation),*values,time.monotonic()+5)
                    self.assertIs(type(saved),Committed,saved);await scenario.join();proposal=await scenario.candidate()
                    self.assertEqual((proposal['resolution'],proposal['failure_reason']),('NOT_SENT','NONE'))
                    for name in ('provider_request_id','handoff_id','text','text_digest'):self.assertIsNone(proposal[name])
                    self.assertEqual(proposal['review'],'NOT_APPLICABLE')
                    self.assertEqual(proposal['terminal_receipt'],{'owner_namespace':'self_model','operation_kind':'record_initial_persona_resolution',
                        'scope_id':'instance','operation_key':'resolve-'+str(generation)})
                    if generation==1:old_values=values;original=saved
                    self.assertEqual(scenario.rows('provider_requests'),[]);self.assertEqual(scenario.rows('provider_attempts'),[])
                    self.assertEqual(scenario.rows('provider_handoffs'),[])
                    if generation<3:self.assertIs(type(await scenario.retry('retry-'+str(generation))),Committed)
                    else:self.assertIsNot(type(await scenario.retry('fourth-generation')),Committed)
                assert old_values is not None and type(original) is Committed
                audits=scenario.rows('audit_records')
                replay=await scenario.api.record_initial_persona_resolution('resolve-1',*old_values,time.monotonic()+5)
                await scenario.join();self.assertIs(type(replay),Committed,replay);assert type(replay) is Committed
                self.assertEqual(original.receipt,replay.receipt);self.assertEqual(scenario.rows('audit_records'),audits)
            finally:self.assertTrue(await scenario.host.close())
            restored=PersonaScenario(Path(directory),1)
            try:
                await restored.start('OPEN_EXISTING');self.assertEqual((await restored.run())['generation'],3)
                self.assertEqual(len(restored.bodies('self_model_initial_persona_candidates')),3)
                self.assertEqual(restored.rows('provider_requests'),[])
            finally:self.assertTrue(await restored.host.close())

    async def test_absent_orchestration_and_lookup_miss_without_native_proof_cannot_resolve(self):
        with TemporaryDirectory() as directory:
            scenario=PersonaScenario(Path(directory),1)
            try:
                await scenario.start();current=await scenario.associate();key=cast(str,current['provider_operation_key'])
                async def miss(*args):return ProviderMissing()
                with patch.object(WorkPort,'verify_unsent',miss):
                    unresolved=await scenario.api.generate(scenario.run_id,1,key,time.monotonic()+5)
                self.assertIs(type(unresolved),LocalUnconfirmed,unresolved);await scenario.join()
                self.assertEqual(scenario.rows('self_model_initial_persona_candidates'),[])
                for run,generation,reference,revision in ((scenario.run_id,1,key,1),(scenario.run_id,1,'wrong-key',0),
                        (scenario.run_id,2,key,0),('wrong-run',1,key,0)):
                    rejected=await scenario.api.record_initial_persona_resolution('invalid',run,cast(int,current['revision']),generation,reference,revision,time.monotonic()+5)
                    self.assertIsNot(type(rejected),Committed);await scenario.join()
                saved=await scenario.api.generate(scenario.run_id,1,key,time.monotonic()+5)
                self.assertIs(type(saved),SavedResolution,saved);assert type(saved) is SavedResolution
                self.assertIsNone(saved.request_id);await scenario.join()
                self.assertEqual(scenario.rows('provider_requests'),[])
                proposal=await scenario.candidate()
                for changed in ({'resolution':'KNOWN_FAILED'},{'provider_request_id':'fabricated'},{'failure_reason':'CANCELLED'},
                                {'terminal_receipt':{**cast(dict,proposal['terminal_receipt']),'owner_namespace':'provider'}}):
                    with self.assertRaises(InvalidValue):isolate_candidate({**proposal,**changed})
            finally:self.assertTrue(await scenario.host.close())

    async def test_registered_zero_and_positive_revision_require_exact_actual_completion(self):
        from companion_memory.self_model.transactions import PersonaTransactions
        for zero in (True,False):
            with self.subTest(zero=zero),TemporaryDirectory() as directory,server(length_response()) as (port,requests,failures):
                scenario=PersonaScenario(Path(directory),port)
                try:
                    await scenario.start();current=await scenario.run();original_generate=WorkPort.generate
                    async def blocked(work,request):
                        if zero:scenario.host.gate.close_ordinary()
                        try:return await original_generate(work,request)
                        finally:scenario.host.gate.resolve_cutoff('DREAM_FOCUSED',scenario.host.gate.epoch)
                    original_handle=PersonaTransactions.handle
                    def fail_resolution(owner,kind,uow,values):
                        if kind=='record_initial_persona_resolution':raise InvalidValue()
                        return original_handle(owner,kind,uow,values)
                    with patch.object(WorkPort,'generate',blocked),patch.object(PersonaTransactions,'handle',fail_resolution):
                        value=await scenario.api.generate(scenario.run_id,1,cast(str,current['provider_operation_key']),time.monotonic()+5)
                    self.assertIsNot(type(value),SavedResolution);await scenario.join()
                    current=await scenario.run();actual=scenario.bodies('provider_requests')[0];revision=actual['revision']
                    self.assertEqual(revision==0,zero);self.assertEqual(actual['attempt_count'],0 if zero else 1)
                    self.assertEqual(actual['outcome'],'MODE_BLOCKED' if zero else 'FAILED')
                    for wrong in (-1,True,revision+1,1 if zero else 0):
                        rejected=await scenario.api.record_initial_persona_resolution('wrong-revision',scenario.run_id,
                            cast(int,current['revision']),1,actual['object_id'],wrong,time.monotonic()+5)
                        self.assertIsNot(type(rejected),Committed);await scenario.join()
                    self.assertEqual(scenario.rows('self_model_initial_persona_candidates'),[])
                    values=(scenario.run_id,cast(int,current['revision']),1,actual['object_id'],revision)
                    saved=await scenario.api.record_initial_persona_resolution('exact-revision',*values,time.monotonic()+5)
                    self.assertIs(type(saved),Committed,saved);assert type(saved) is Committed
                    await scenario.join();proposal=await scenario.candidate()
                    self.assertEqual(proposal['provider_request_id'],actual['object_id'])
                    self.assertEqual(proposal['failure_reason'],'MODE_BLOCKED' if zero else 'OUTPUT_LIMIT')
                    self.assertEqual(cast(dict,proposal['terminal_receipt'])['owner_namespace'],'provider')
                    self.assertTrue(str(cast(dict,proposal['terminal_receipt'])['operation_key']).startswith('register-' if zero else 'settle-'))
                    audits=scenario.rows('audit_records')
                    repeated=await scenario.api.record_initial_persona_resolution('exact-revision',*values,time.monotonic()+5)
                    self.assertEqual(repeated,Committed(saved.receipt,'EXISTING'));await scenario.join()
                    self.assertEqual(scenario.rows('audit_records'),audits);self.assertEqual(len(requests),0 if zero else 1)
                    self.assertEqual(failures,[])
                    self.assertIs(type(await scenario.retry()),Committed)
                finally:self.assertTrue(await scenario.host.close())

    async def test_absent_rollback_unconfirmed_commit_and_cleanup_keep_original_seal(self):
        from companion_memory.provider import CancellationSource,Rejected as ProviderRejected
        from companion_memory.ingress.events import plain
        for edge in ('rollback','before-commit','after-commit','cleanup'):
            with self.subTest(edge=edge),TemporaryDirectory() as directory:
                hooks=Hooks();scenario=PersonaScenario(Path(directory),1,hooks);entered=threading.Event();release=threading.Event()
                touched=False;committed=False
                try:
                    await scenario.start();current=await scenario.associate()
                    audit_before=scenario.rows('audit_records');rows_before=scenario.rows('self_model_initial_persona_runs')
                    values=(scenario.run_id,cast(int,current['revision']),1,cast(str,current['provider_operation_key']),0)
                    def wait():
                        entered.set()
                        if not release.wait(10):raise AssertionError('Original transaction release was not signaled.')
                    def before(sql):
                        nonlocal touched
                        if sql.startswith('INSERT INTO self_model_initial_persona_candidates'):touched=True
                        if touched and sql.startswith('INSERT INTO audit_records') and edge=='rollback':raise sqlite3.OperationalError('Controlled audit rollback.')
                        if touched and sql=='COMMIT' and edge=='before-commit':wait()
                    def after(sql):
                        nonlocal committed
                        if touched and sql=='COMMIT':
                            committed=True
                            if edge=='after-commit':wait()
                    def closing():
                        if committed and edge=='cleanup':wait()
                    hooks.before=before;hooks.after=after;hooks.before_close=closing
                    # Allow real material/proof preparation before expiring
                    # the public wait at the observed SQL/cleanup barrier.
                    # Production storage deadlines and resource limits stay fixed.
                    job=asyncio.create_task(scenario.api.record_initial_persona_resolution('resolve',*values,time.monotonic()+5))
                    if edge!='rollback':
                        self.assertTrue(await asyncio.to_thread(entered.wait,5),repr(job.result()) if job.done() else 'Original operation still preparing.')
                        manager=scenario.host.persona;assert manager is not None and manager._permit is not None
                        self.assertIsNotNone(manager._unsent);self.assertIsNotNone(scenario.host.combination.persona._scope)
                        permit=manager._permit
                        old=await permit.work.generate({**cast(dict,plain(permit.material.request)),
                            'deadline':time.monotonic()+2,'cancellation':CancellationSource().token})
                        self.assertIs(type(old),ProviderRejected,old);assert type(old) is ProviderRejected
                        self.assertEqual(old.error.reason,'ORIGINAL_ADMISSION_CLOSED')
                        public=await job
                        self.assertIsNot(type(public),Committed,public)
                        self.assertIsNotNone(manager._unsent);self.assertIsNotNone(manager._task)
                        # Inspect actual committed visibility while the original
                        # owner still retains its lower cleanup and native seal.
                        self.assertEqual(len(scenario.rows('self_model_initial_persona_candidates')),1 if committed else 0)
                        release.set()
                    else:
                        public=await job;self.assertTrue(touched);self.assertIs(type(public),NotCommitted,public)
                    await scenario.join();hooks.before=lambda sql:None;hooks.after=lambda sql:None;hooks.before_close=lambda:None
                    if not committed:
                        self.assertEqual(scenario.rows('self_model_initial_persona_candidates'),[])
                        self.assertEqual(scenario.rows('self_model_initial_persona_runs'),rows_before)
                        self.assertEqual(scenario.rows('audit_records'),audit_before)
                    original=await scenario.api.record_initial_persona_resolution('resolve',*values,time.monotonic()+5)
                    self.assertIs(type(original),Committed,original);assert type(original) is Committed
                    self.assertEqual(original.source,'EXISTING' if committed else 'NEW');await scenario.join()
                    proposal=scenario.bodies('self_model_initial_persona_candidates')[0]
                    self.assertEqual(proposal['terminal_receipt']['operation_key'],'resolve')
                    self.assertEqual(len(scenario.rows('audit_records')),len(audit_before)+1)
                    self.assertEqual(scenario.rows('provider_requests'),[])
                    # A storage fault permits original confirmation, not new
                    # business work; readiness requires the real owner reopen.
                    if scenario.host.storage.get_health().lifecycle=='FAULTED':
                        current_raw=scenario.bodies('self_model_initial_persona_runs')[0]
                        denied=await scenario.api.retry_initial_persona('retry',scenario.run_id,current_raw['revision'],1,
                            proposal['object_id'],scenario.host.gate.epoch,time.monotonic()+5)
                        self.assertIsNot(type(denied),Committed);await scenario.join()
                finally:
                    release.set();hooks.before=lambda sql:None;hooks.after=lambda sql:None;hooks.before_close=lambda:None
                    await scenario.join();self.assertTrue(await scenario.host.close())
                restored=PersonaScenario(Path(directory),1)
                try:
                    await restored.start('OPEN_EXISTING')
                    self.assertEqual((await restored.candidate())['terminal_receipt'],proposal['terminal_receipt'])
                    self.assertIs(type(await restored.retry()),Committed)
                finally:self.assertTrue(await restored.host.close())

    async def test_ordinary_command_fake_native_proof_and_revocation_cannot_publish_absence(self):
        from companion_memory.provider import CancellationSource
        from companion_memory.provider.unsent_evidence import UnsentVerified
        from companion_memory.ingress.events import plain
        with TemporaryDirectory() as directory:
            scenario=PersonaScenario(Path(directory),1)
            try:
                await scenario.start();current=await scenario.associate()
                t=scenario.host.combination.persona;manager=scenario.host.persona;assert manager is not None
                kind='record_initial_persona_resolution';definition=t.definitions[kind]
                values={'operation_id':'native-check','run_id':scenario.run_id,'expected_revision':current['revision'],'generation':1,
                    'provider_reference':current['provider_operation_key'],'evidence_revision':0}
                command=ResultBoundCommand(1,values,{r.event_slot:{'actor':t.actor} for r in definition.required_audits})
                operation=scenario.host.storage.bind_operation(definition,'instance')
                self.assertIsNot(type(await operation.execute('native-check',command)),Committed)
                permit=await manager.work.issue(scenario.run_id,1,cast(str,current['provider_operation_key']),time.monotonic()+5,recovery=True)
                raw={**cast(dict,plain(permit.material.request)),'deadline':time.monotonic()+5,'cancellation':CancellationSource().token}
                try:
                    for invalid in (None,object.__new__(VerifiedUnsent)):
                        invocation=t.retain(kind,values,work=permit.work,unsent=invalid)
                        try:self.assertIsNot(type(await operation.execute('native-check',command)),Committed)
                        finally:t.release(invocation)
                    verified=await permit.work.verify_unsent('generate',raw)
                    self.assertIs(type(verified),UnsentVerified,verified);assert type(verified) is UnsentVerified
                    invocation=t.retain(kind,values,work=permit.work,unsent=verified.value)
                    # Revoke after proof issuance: the private invocation cannot
                    # turn a stale evidence object into present authority.
                    scenario.host.provider.revoke(permit.work)
                    try:self.assertIsNot(type(await operation.execute('native-check',command)),Committed)
                    finally:t.release(invocation)
                    self.assertEqual(scenario.rows('self_model_initial_persona_candidates'),[])
                finally:
                    manager.work.release(permit)
            finally:self.assertTrue(await scenario.host.close())
