"""Persisted configuration, retained initial input and explicit human review.

No model request is issued. Review fixtures are synthetic successful candidates,
so these tests do not replace native Provider confirmation or publication tests.
"""
from pathlib import Path
import tempfile
from typing import cast
import unittest
from companion_memory.configuration.text_persistence import TextConfigurationAssembly
from companion_memory.configuration.text_persistent_results import ConfigurationCommitted
from companion_memory.persistence import PersistenceService,DatabaseResources,Ready,Value
from companion_memory.persistence.text_records import digest,stable_identity
from companion_memory.self_model.preparation import prepare_run,retained_material,associate_run,confirm_run,generation_key
from companion_memory.self_model.request_material import request_material
from companion_memory.self_model.transitions import review,next_generation
from companion_memory.self_model.formats import candidate_digest,isolate_candidate,isolate_run
from companion_memory.persistence.schema import InvalidValue
from tests.text_learning.configuration_support import candidate
from tests.text_learning.test_record_catalogs import records


class PersonaMaterialTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_material_three_generation_keys_and_review_preserve_text(self):
        with tempfile.TemporaryDirectory(prefix='persona-material-') as directory:
            root=Path(directory).resolve();configuration,supplied=candidate(root)
            assembly=TextConfigurationAssembly();storage=PersistenceService(assembly.repositories,assembly.commands,assembly_format='MODEL_TEXT_LEARNING_V1')
            publisher=None
            try:
                opened=await storage.initialize(configuration.foundation,DatabaseResources('persona-db',lambda identity,path:identity=='persona-db' and path==str(root/'database'/'runtime.sqlite3')),'CREATE_NEW')
                self.assertIs(type(opened),Ready,opened)
                publisher=assembly.bind(storage,'instance',configuration)
                committed=await publisher.persist_text_learning_configuration('initial-config',configuration,actor='operator',protected_directories=supplied[6])
                self.assertIs(type(committed),ConfigurationCommitted,committed)
                assert type(committed) is ConfigurationCommitted and committed.configuration is not None
                stored=committed.configuration
                fixtures=records()
                initial={**fixtures['memory_initial_self_inputs'],'database_id':stored.database_id,'config_snapshot_id':stored.snapshot_id}
                retained={**initial,'object_id':stable_identity('self-input',stored.database_id,'instance')}
                prepare_key={'owner_namespace':'self_model','operation_kind':'prepare_initial_persona','scope_id':'instance','operation_key':'prepare'}
                prepared=prepare_run(stored,retained,1,2,prepare_key,2)
                rid=cast(str,prepared['object_id'])
                self.assertEqual(prepared['provider_operation_key'],generation_key(stored.database_id,'instance',rid,1))
                self.assertEqual(len({generation_key(stored.database_id,'instance',rid,n) for n in (1,2,3)}),3)
                self.assertEqual(retained_material(stored,retained,prepared).request['operation_key'],prepared['provider_operation_key'])
                associated=associate_run(stored,retained,prepared,1,1,3,{**prepare_key,'operation_kind':'associate_initial_persona_request'},3)
                confirmed=confirm_run(associated,2,1,'actual-request-reference',{**prepare_key,'operation_kind':'confirm_initial_persona_request'},4)
                self.assertEqual(confirmed['revision'],3);self.assertEqual(confirmed['mode_epoch'],3)
                for altered in ({**prepared,'provider_operation_key':'arbitrary-key'},{**prepared,'generation':2},
                                {**prepared,'binding_digest':'f'*64},{**prepared,'schema_ref':'another-resource'},
                                {**prepared,'account_id':'another-account'},{**prepared,'window_id':'another-window'}):
                    with self.assertRaises(InvalidValue):retained_material(stored,retained,altered)
                with self.assertRaises(InvalidValue):prepare_run(stored,retained,2,2,prepare_key,2)
                with self.assertRaises(InvalidValue):associate_run(stored,retained,prepared,1,1,1,prepare_key,3)
                with self.assertRaises(InvalidValue):confirm_run(confirmed,3,1,'other-request',{**prepare_key,'operation_kind':'confirm_initial_persona_request'},5)
                materials=tuple(request_material(stored,initial,'run',generation,'original-'+str(generation)) for generation in (1,2,3))
                self.assertEqual(len({m.context_digest for m in materials}),3)
                self.assertEqual(len({cast(str,m.binding['request_digest']) for m in materials}),3)
                self.assertEqual(request_material(stored,initial,'run',1,'original-1'),materials[0])
                with self.assertRaises(InvalidValue):request_material(stored,initial,'run',4,'original-4')
                text='The operator explicitly selected no preset identity.'
                base={**fixtures['self_model_initial_persona_candidates'],'database_id':stored.database_id,'config_snapshot_id':stored.snapshot_id,
                    'run_id':'run','provider_operation_key':'original-1','provider_request_id':'request','handoff_id':'handoff','resolution':'SUCCEEDED','failure_reason':'NONE',
                    'text':text,'text_digest':digest(text),'review':'PENDING','binding_digest':digest(materials[0].binding)}
                proposal=isolate_candidate(base)
                run=isolate_run({**fixtures['self_model_initial_persona_runs'],'object_id':'run','database_id':stored.database_id,'config_snapshot_id':stored.snapshot_id,
                    'state':'WAITING_REVIEW','provider_operation_key':'original-1','provider_request_id':'request','resolution_id':proposal['object_id'],'binding_digest':digest(materials[0].binding)})
                operation={'owner_namespace':'self_model','operation_kind':'review_initial_persona','scope_id':'instance','operation_key':'review'}
                identity=candidate_digest(proposal)
                approved=review(run,proposal,1,1,identity,'APPROVE','operator',operation,2)
                rejected=review(run,proposal,1,1,identity,'REJECT','operator',operation,2)
                for result in (approved,rejected):
                    self.assertEqual(result.candidate['text'],text);self.assertEqual(candidate_digest(result.candidate),identity)
                    with self.assertRaises(InvalidValue):review(result.run,result.candidate,2,2,identity,'APPROVE','operator',operation,3)
                with self.assertRaises(InvalidValue):review(run,proposal,1,1,'f'*64,'APPROVE','operator',operation,2)
                retry_operation={**operation,'operation_kind':'retry_initial_persona','operation_key':'retry'}
                retried=next_generation(rejected.run,rejected.candidate,initial,2,1,cast(str,proposal['object_id']),3,retry_operation,3,materials[1])
                self.assertEqual(retried['generation'],2);self.assertEqual(retried['state'],'PREPARED')
                self.assertEqual(retried['provider_operation_key'],'original-2');self.assertIsNone(retried['resolution_id'])
                with self.assertRaises(InvalidValue):next_generation(approved.run,approved.candidate,initial,2,1,cast(str,proposal['object_id']),3,retry_operation,3,materials[1])
                with self.assertRaises(InvalidValue):next_generation(rejected.run,rejected.candidate,initial,2,1,cast(str,proposal['object_id']),3,retry_operation,3,materials[0])
                print({'source':'ACTUAL_PERSISTED_PERSONA_MATERIAL','generation_wire_bytes':[len(m.wire) for m in materials],'model_calls':0})
            finally:
                if publisher is not None:publisher.close()
                await storage.close()
                if publisher is not None:publisher.close()
