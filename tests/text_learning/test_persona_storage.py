"""Actual persona owner CAS and atomic review with complete synthetic records.

Only owner storage effects are exercised: the synthetic resolution is explicitly
not a Provider attestation or proof that public persona generation is complete.
"""
from pathlib import Path
import sqlite3
import tempfile
from typing import cast
from types import MappingProxyType
import time
import unittest
from companion_memory.configuration.text_persistence import TextConfigurationAssembly
from companion_memory.configuration.text_persistent_results import ConfigurationCommitted
from companion_memory.persistence import PersistenceService,DatabaseResources,Ready,Committed,ResultBoundCommand,ResultBoundCommandDefinition,RecordSchema,Field
from companion_memory.persistence.text_results import result_schema,audits,INTENT,result
from companion_memory.persistence.text_records import ID,stable_identity,digest
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.schema import InvalidValue
from companion_memory.self_model.repository import persona_catalog
from companion_memory.self_model.storage import PersonaStorage
from companion_memory.self_model.formats import isolate_run,isolate_candidate,candidate_digest
from companion_memory.self_model.transitions import review
from tests.text_learning.configuration_support import candidate
from tests.text_learning.test_record_catalogs import records
from tests.persistence.support import Hooks,sqlite_fault


class PersonaStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_cas_preserves_original_text_and_rolls_back_both_rows(self):
        with tempfile.TemporaryDirectory(prefix='persona-owner-') as directory:
            root=Path(directory).resolve();configuration,supplied=candidate(root);config=TextConfigurationAssembly();catalog=persona_catalog()
            owner: PersonaStorage | None=None;publisher=None;run=None;candidate_value=None
            hooks=Hooks()
            def handle(uow,values):
                assert owner is not None and run is not None and candidate_value is not None
                if values['action']=='prepare':
                    owner.stage_run(uow,run);refs=({'kind':'RUN','object_id':run['object_id'],'revision':1},);previous=None;count=1
                elif values['action']=='associate':
                    associated=isolate_run({**run,'revision':2,'state':'REQUEST_ASSOCIATED','provider_request_id':'fixture-request'})
                    owner.update_run(uow,run,associated)
                    refs=({'kind':'RUN','object_id':run['object_id'],'revision':2},);previous=1;count=1
                elif values['action']=='resolve':
                    associated=isolate_run({**run,'revision':2,'state':'REQUEST_ASSOCIATED','provider_request_id':'fixture-request'})
                    resolved=isolate_run({**associated,'revision':3,'state':'WAITING_REVIEW','resolution_id':candidate_value['object_id']})
                    owner.stage_resolution(uow,associated,resolved,candidate_value)
                    refs=({'kind':'RUN','object_id':run['object_id'],'revision':3},);previous=2;count=2
                elif values['action']=='review':
                    current=owner.read(uow,'run',str(run['object_id']));proposal=owner.read(uow,'candidate',str(candidate_value['object_id']))
                    assert current is not None and proposal is not None
                    operation={'owner_namespace':'self_model','operation_kind':'review_initial_persona','scope_id':'instance','operation_key':'review'}
                    reviewed=review(current.value,proposal.value,3,1,candidate_digest(proposal.value),'APPROVE','operator',operation,2)
                    owner.stage_review(uow,current.value,proposal.value,reviewed)
                    refs=({'kind':'RUN','object_id':run['object_id'],'revision':4},);previous=3;count=2
                else:
                    current=owner.read(uow,'run',str(run['object_id']));proposal=owner.read(uow,'candidate',str(candidate_value['object_id']))
                    assert current is not None and proposal is not None
                    operation={'owner_namespace':'self_model','operation_kind':'fixture_persona_effect','scope_id':'instance','operation_key':'publish'}
                    publication={**fixtures['self_model_persona_publications'],
                        **{name:current.value[name] for name in ('database_id','instance_id','config_snapshot_id','self_subject_id','self_revision','prompt_ref','schema_ref','transform_ref')},
                        **{name:proposal.value[name] for name in ('generation','input_id','input_digest','provider_request_id','handoff_id','text','reviewed_by','review_operation')},
                        'object_id':stable_identity('persona-publication',stored.database_id,'instance'),'run_id':run['object_id'],
                        'candidate_id':proposal.value['object_id'],'candidate_revision':proposal.value['revision'],'candidate_digest':candidate_digest(proposal.value),
                        'created_at_us':3,'generated_at_us':1,'publication_operation':operation}
                    after=isolate_run({**current.value,'revision':5,'state':'PUBLISHED','publication_id':publication['object_id'],
                        'last_operation':operation,'updated_at_us':3})
                    owner.stage_publication(uow,current.value,after,publication)
                    refs=({'kind':'RUN','object_id':run['object_id'],'revision':5},);previous=4;count=2
                targets=({'object_id':run['object_id'],'previous_revision':previous,'revision':refs[0]['revision']},)
                if values['action'] in ('resolve','review'):
                    reviewed=values['action']=='review'
                    refs+=({'kind':'RESOLUTION','object_id':candidate_value['object_id'],'revision':2 if reviewed else 1},)
                    targets+=({'object_id':candidate_value['object_id'],'previous_revision':1 if reviewed else None,'revision':2 if reviewed else 1},)
                if values['action']=='publish':
                    publication_id=stable_identity('persona-publication',stored.database_id,'instance')
                    refs+=({'kind':'PUBLICATION','object_id':publication_id,'revision':1},)
                    targets+=({'object_id':publication_id,'previous_revision':None,'revision':1},)
                return result(str(values['operation_id']),'SAVED',{'self_model':{'rows_changed':count,'references':refs}},refs,
                    targets)
            requirements,bindings=audits('fixture_persona_effect',('self_model',))
            definition=ResultBoundCommandDefinition('self_model','fixture_persona_effect',1,RecordSchema((Field('operation_id',ID),Field('action',ID))),1,
                result_schema(('self_model',),('SAVED',)),(catalog.definition,),requirements,handle,INTENT,bindings)
            storage=PersistenceService(config.repositories+(catalog.definition,),config.commands+(definition,),assembly_format='MODEL_TEXT_LEARNING_V1')
            try:
                self.assertIs(type(await storage.initialize(configuration.foundation,DatabaseResources('persona-owner-db',lambda identity,path:identity=='persona-owner-db' and path==str(root/'database'/'runtime.sqlite3'),connect=hooks.connect),'CREATE_NEW')),Ready)
                publisher=config.bind(storage,'instance',configuration)
                committed=await publisher.persist_text_learning_configuration('configuration',configuration,actor='fixture',protected_directories=supplied[6])
                assert type(committed) is ConfigurationCommitted and committed.configuration is not None
                stored=committed.configuration;owner=PersonaStorage(catalog,storage,stored,'instance');fixtures=records()
                rid=stable_identity('persona-run',stored.database_id,'instance');cid=stable_identity('persona-candidate',stored.database_id,'instance',rid,1)
                run=isolate_run({**fixtures['self_model_initial_persona_runs'],'object_id':rid,'database_id':stored.database_id,'config_snapshot_id':stored.snapshot_id})
                text='The explicitly selected identity has no preset background.'
                candidate_value=isolate_candidate({**fixtures['self_model_initial_persona_candidates'],'object_id':cid,'run_id':rid,'database_id':stored.database_id,
                    'config_snapshot_id':stored.snapshot_id,'provider_request_id':'fixture-request','handoff_id':'fixture-handoff','resolution':'SUCCEEDED','failure_reason':'NONE',
                    'text':text,'text_digest':digest(text),'review':'PENDING'})
                operation=storage.bind_operation(definition,'instance')
                def command(action):return ResultBoundCommand(1,{'operation_id':action,'action':action},{'self_model_text_learning':{'actor':'fixture'}})
                for action in ('prepare','associate','resolve'):
                    self.assertIs(type(await operation.execute(action,command(action))),Committed)
                for failing in ('UPDATE self_model_initial_persona_runs','INSERT INTO audit_records','INSERT INTO operation_receipts','COMMIT'):
                    seen=[]
                    def before(sql):
                        if sql.startswith(failing):seen.append(sql);raise sqlite_fault(sqlite3.SQLITE_CONSTRAINT)
                    hooks.before=before
                    failed=await operation.execute('review',command('review'));self.assertIsNot(type(failed),Committed,failed);self.assertTrue(seen)
                    hooks.before=lambda sql:None
                    actual=await owner.read_original('candidate',cid,time.monotonic()+3)
                    self.assertIsNotNone(actual);assert actual is not None
                    self.assertEqual(actual.value,candidate_value)
                reviewed=await operation.execute('review',command('review'));self.assertIs(type(reviewed),Committed,reviewed)
                again=await operation.execute('review',command('review'));assert type(reviewed) is Committed and type(again) is Committed
                self.assertEqual(reviewed.receipt,again.receipt)
                actual=await owner.read_original('candidate',cid,time.monotonic()+3);assert actual is not None
                self.assertEqual(actual.value['text'],text);self.assertEqual(actual.value['review'],'APPROVED')
                self.assertEqual(candidate_digest(actual.value),candidate_digest(candidate_value))
                self.assertIsNone(await owner.current_original(time.monotonic()+3))
                for failing in ('INSERT INTO self_model_persona_publications','UPDATE self_model_initial_persona_runs','INSERT INTO audit_records','INSERT INTO operation_receipts','COMMIT'):
                    seen=[]
                    def fail_publication(sql):
                        if sql.startswith(failing):seen.append(sql);raise sqlite_fault(sqlite3.SQLITE_CONSTRAINT)
                    hooks.before=fail_publication
                    failed=await operation.execute('publish',command('publish'))
                    self.assertIsNot(type(failed),Committed,failed);self.assertTrue(seen)
                    hooks.before=lambda sql:None
                    self.assertIsNone(await owner.current_original(time.monotonic()+3))
                    retained=await owner.read_original('run',rid,time.monotonic()+3);assert retained is not None
                    self.assertEqual(retained.value['state'],'APPROVED')
                published=await operation.execute('publish',command('publish'))
                self.assertIs(type(published),Committed,published)
                replay=await operation.execute('publish',command('publish'));assert type(replay) is Committed and type(published) is Committed
                self.assertEqual(replay.receipt,published.receipt)
                current=await owner.current_original(time.monotonic()+3);assert current is not None
                self.assertEqual(current.value['text'],text)
                # Review metadata is excluded from candidate identity, so the
                # publication must separately bind its actual retained review.
                damaged=MappingProxyType({**actual.value,'reviewed_by':'another-reviewer'})
                self.assertEqual(candidate_digest(damaged),candidate_digest(actual.value))
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    db.execute('UPDATE self_model_initial_persona_candidates SET body=? WHERE object_id=?',
                        (encode_content(damaged,4096).decode(),cid))
                with self.assertRaises(InvalidValue):await owner.current_original(time.monotonic()+3)
            finally:
                if owner is not None:owner.close()
                if publisher is not None:publisher.close()
                await storage.close()
                if publisher is not None:publisher.close()
