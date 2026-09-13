"""Actual same-transaction Provider checks without new model permission.

Explicit persona protocol fixtures exercise original material, current costs,
revocation and transaction rollback. The local fixture writer is deliberately
not an implementation of persona management or publication.
"""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from typing import cast
import unittest
from companion_memory.ingress.events import plain
from companion_memory.persistence import (PersistenceService, ResultBoundCommandDefinition, RecordSchema, Field,
    ResultBoundCommand, Committed)
from companion_memory.persistence.text_records import ID,stable_identity
from companion_memory.persistence.text_results import result_schema,audits,INTENT,result
from companion_memory.persistence.schema import InvalidValue
from companion_memory.provider import WorkGrant,ResultGrant,Ready,Completed,Found,Failed
from companion_memory.provider.completion_evidence import ConfirmedCompletion
from companion_memory.provider.terminal_evidence import TerminalVerified
from companion_memory.provider.service import OPTIONALS
from companion_memory.provider.values import as_record,freeze
from companion_memory.self_model.repository import persona_catalog
from companion_memory.self_model.storage import PersonaStorage
from companion_memory.self_model.formats import isolate_run
from companion_memory.self_model.request_material import request_material
from tests.text_learning.provider_support import Fixture
from tests.text_learning.test_record_catalogs import records
from tests.text_learning.test_provider import response
from tests.provider.test_chat_transport import server


class TransactionEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_completion_and_retry_recheck_real_rows_cost_and_permission(self):
        for branch in ('complete','unknown-cost','zero-attempt'):
            with self.subTest(branch=branch),TemporaryDirectory() as directory:
                root=Path(directory);database='text-provider-database'
                run_id=stable_identity('persona-run',database,'instance')
                input_id=stable_identity('self-input',database,'instance')
                payload=json.dumps({'schema_version':1,'text':'No preset background.','initial_input_ids':[input_id]})
                usage={'prompt_tokens':8} if branch=='unknown-cost' else None
                with server(response(payload,usage)) as (port,requests,failures):
                    grant=WorkGrant('self_model','instance',None,'PERSONA',('fixture_generation',),('GENERATION',),
                        'self_model','operator',(run_id,),prompt_revisions=('persona_prompt',),internal_dream=True)
                    fixture=Fixture(root,port,grant);catalog=persona_catalog()
                    owner=None;result_owner=None;proof=None;original=None;request_id='';retry=True
                    checked=[]
                    def handle(uow,values):
                        assert owner is not None and result_owner is not None and original is not None
                        matched=fixture.work.verify_request_in_transaction(uow,request_id,original)
                        checked.append(matched)
                        if type(matched) is not Found:raise InvalidValue()
                        confirmed=result_owner.verify_completion_in_transaction(uow,proof,retry_work=fixture.work if retry else None)
                        checked.append(confirmed)
                        if type(confirmed) is not Found:raise InvalidValue()
                        before=owner.read(uow,'run',run_id)
                        if before is None:
                            assert fixture.stored is not None
                            after=isolate_run({**records()['self_model_initial_persona_runs'],'object_id':run_id,'database_id':database,
                                'config_snapshot_id':fixture.stored.snapshot_id})
                            owner.stage_run(uow,after);previous=None
                        else:
                            previous=before.value['revision']
                            after=isolate_run({**before.value,'revision':cast(int,previous)+1})
                            owner.update_run(uow,before.value,after)
                        refs=({'kind':'RUN','object_id':run_id,'revision':after['revision']},)
                        return result(str(values['operation_id']),'VERIFIED',{'self_model':{'rows_changed':1,'references':refs}},refs,
                            ({'object_id':run_id,'previous_revision':previous,'revision':after['revision']},))
                    required,bindings=audits('fixture_provider_transaction',('self_model',))
                    definition=ResultBoundCommandDefinition('self_model','fixture_provider_transaction',1,RecordSchema((Field('operation_id',ID),)),1,
                        result_schema(('self_model',),('VERIFIED',)),(catalog.definition,fixture.assembly.repositories[0]),required,handle,INTENT,bindings)
                    fixture.storage=PersistenceService(fixture.configuration.repositories+fixture.assembly.repositories+(catalog.definition,),
                        fixture.configuration.commands+fixture.assembly.commands+(definition,),assembly_format='MODEL_TEXT_LEARNING_V1')
                    try:
                        self.assertIs(type(await fixture.initialize()),Ready)
                        stored=fixture.stored;assert stored is not None
                        owner=PersonaStorage(catalog,fixture.storage,stored,'instance')
                        source={**records()['memory_initial_self_inputs'],'object_id':input_id,'database_id':database,'config_snapshot_id':stored.snapshot_id}
                        material=request_material(stored,source,run_id,1,'original-persona')
                        if branch=='zero-attempt':fixture.gate.mode='FOCUSED'
                        generated=await fixture.work.generate({**cast(dict,plain(material.request)),
                            'deadline':time.monotonic()+3,'cancellation':fixture.cancellation.token})
                        self.assertIs(type(generated),Completed,generated);assert type(generated) is Completed
                        request_id=str(generated.record['object_id'])
                        result_owner=fixture.service.bind_result_owner(ResultGrant('self_model',(request_id,)))
                        original=as_record(freeze({**{name:material.request.get(name) for name in OPTIONALS},
                            **{name:material.request[name] for name in ('operation_key','run_id','profile_id','payload','entry_ids')}},131072,owned=True))
                        terminal=await result_owner.verify_terminal(request_id,original)
                        self.assertIs(type(terminal),TerminalVerified,terminal);assert type(terminal) is TerminalVerified
                        proof=await result_owner.confirm_completion(terminal.value)
                        self.assertIs(type(proof),ConfirmedCompletion,proof);assert type(proof) is ConfirmedCompletion
                        native_proof=proof
                        operation=fixture.storage.bind_operation(definition,'instance')
                        async def execute(key):
                            return await operation.execute(key,ResultBoundCommand(1,{'operation_id':key},{'self_model_text_learning':{'actor':'fixture'}}))
                        saved=await execute('verify')
                        if branch=='unknown-cost':
                            self.assertIsNot(type(saved),Committed,saved)
                            self.assertIsNone(await owner.read_original('run',run_id,time.monotonic()+3))
                            self.assertIs(type(checked[-1]),Failed)
                            self.assertFalse(checked[-1].error.cleanup_pending)
                            retry=False
                            saved=await execute('completion-only')
                        self.assertIs(type(saved),Committed,(saved,checked))
                        snapshot=await owner.read_original('run',run_id,time.monotonic()+3);assert snapshot is not None
                        another_scope=fixture.storage.bind_operation(definition,'another-instance')
                        self.assertIsNot(type(await another_scope.execute('cross-scope',ResultBoundCommand(1,{'operation_id':'cross-scope'},
                            {'self_model_text_learning':{'actor':'fixture'}}))),Committed)
                        # A native completion is not a bearer grant detached from
                        # its exact issuer, result owner and original material.
                        proof=object.__new__(ConfirmedCompletion)
                        self.assertIsNot(type(await execute('fabricated')),Committed)
                        proof=native_proof
                        actual_original=original;original=as_record(freeze({**original,'operation_key':'another-original'},131072,owned=True))
                        self.assertIsNot(type(await execute('wrong-material')),Committed)
                        original=actual_original
                        entered=threading.Event();release=threading.Event()
                        def block(sql):
                            if sql.startswith('INSERT INTO audit_records'):
                                entered.set()
                                if not release.wait(3):raise AssertionError('Test release was not signaled.')
                        fixture.hooks.before=block
                        job=asyncio.create_task(execute('revoke-before-commit'))
                        try:
                            self.assertTrue(await asyncio.to_thread(entered.wait,3))
                            fixture.service.revoke(result_owner)
                        finally:release.set()
                        self.assertIsNot(type(await job),Committed)
                        fixture.hooks.before=lambda sql:None
                        retained=await owner.read_original('run',run_id,time.monotonic()+3)
                        self.assertEqual(retained,snapshot)
                        self.assertEqual(len(requests),0 if branch=='zero-attempt' else 1)
                        self.assertEqual(failures,[])
                    finally:
                        if owner is not None:owner.close()
                        await fixture.close()
