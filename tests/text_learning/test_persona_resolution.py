"""Native audited Provider results matched to retained persona generation data.

Input records are explicit synthetic fixtures. The actual local Provider ledger
and completion receipt establish model provenance; these tests do not stand in
for initial-input registration, mode management or user acceptance.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from typing import cast
import unittest

from companion_memory.ingress.events import plain
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.text_records import stable_identity, digest
from companion_memory.provider import Ready, Completed, ResultGrant, WorkGrant
from companion_memory.provider.completion_evidence import ConfirmedCompletion
from companion_memory.provider.service import OPTIONALS
from companion_memory.provider.terminal_evidence import TerminalVerified
from companion_memory.provider.values import as_record, freeze
from companion_memory.self_model.formats import isolate_run,isolate_candidate,candidate_digest
from companion_memory.self_model.publication import publish_approved
from companion_memory.self_model.transitions import review
from companion_memory.self_model.request_material import request_material
from companion_memory.self_model.resolution import resolve_known
from tests.provider.test_chat_transport import server
from tests.text_learning.provider_support import Fixture
from tests.text_learning.test_provider import response
from tests.text_learning.test_record_catalogs import records


class PersonaResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_original_input_success_and_invalid_output_keep_actual_completion(self):
        database = 'text-provider-database'
        run_id = stable_identity('persona-run', database, 'instance')
        input_id = stable_identity('self-input', database, 'instance')
        for accepted in (True, False):
            payload = {'schema_version':1,'text':'No preset identity was supplied.',
                       'initial_input_ids':[input_id if accepted else 'another-input']}
            with self.subTest(accepted=accepted), TemporaryDirectory() as directory, server(response(json.dumps(payload))) as (port, requests, failures):
                grant = WorkGrant('self_model','instance',None,'PERSONA',('fixture_generation',),('GENERATION',),
                    'self_model','operator',(run_id,),prompt_revisions=('persona_prompt',),internal_dream=True)
                fixture = Fixture(Path(directory), port, grant)
                try:
                    self.assertIs(type(await fixture.initialize()), Ready)
                    stored = fixture.stored; assert stored is not None
                    source = {**records()['memory_initial_self_inputs'],'object_id':input_id,'database_id':database,'config_snapshot_id':stored.snapshot_id}
                    material = request_material(stored, source, run_id, 1, 'original-persona')
                    result = await fixture.work.generate({**cast(dict,plain(material.request)),
                        'deadline':time.monotonic()+3,'cancellation':fixture.cancellation.token})
                    self.assertIs(type(result), Completed, result); assert type(result) is Completed
                    self.assertEqual(result.record['outcome'], 'SUCCEEDED')
                    owner = fixture.service.bind_result_owner(ResultGrant('self_model',(str(result.record['object_id']),)))
                    original = {name:material.request.get(name) for name in OPTIONALS}
                    original.update({name:material.request[name] for name in ('operation_key','run_id','profile_id','payload','entry_ids')})
                    terminal = await owner.verify_terminal(result.record['object_id'], as_record(freeze(original,131072,owned=True)))
                    self.assertIs(type(terminal), TerminalVerified, terminal); assert type(terminal) is TerminalVerified
                    completion = await owner.confirm_completion(terminal.value)
                    self.assertIs(type(completion), ConfirmedCompletion, completion); assert type(completion) is ConfirmedCompletion
                    operation = {'owner_namespace':'self_model','operation_kind':'record_initial_persona_resolution','scope_id':'instance','operation_key':'resolve'}
                    run = isolate_run({**records()['self_model_initial_persona_runs'],'object_id':run_id,'database_id':database,
                        'config_snapshot_id':stored.snapshot_id,'state':'REQUEST_ASSOCIATED','input_id':input_id,'provider_operation_key':'original-persona',
                        'provider_request_id':result.record['object_id'],'binding_digest':digest(material.binding),
                        'account_id':'fixture_account','window_id':'fixture_window',
                        'prompt_ref':'persona_prompt','schema_ref':'persona_schema','transform_ref':'persona_transform'})
                    resolved = resolve_known(stored, source, run, completion, operation, 2)
                    self.assertEqual(resolved, resolve_known(stored, source, run, completion, operation, 2))
                    self.assertEqual(resolved.run['state'], 'WAITING_REVIEW' if accepted else 'KNOWN_FAILED')
                    self.assertEqual(resolved.candidate['resolution'], 'SUCCEEDED' if accepted else 'KNOWN_FAILED')
                    self.assertEqual(resolved.candidate['terminal_receipt'], completion.operation)
                    self.assertEqual(resolved.candidate['text'], payload['text'] if accepted else None)
                    self.assertEqual(resolved.candidate['review'], 'PENDING' if accepted else 'NOT_APPLICABLE')
                    for altered in ({**run,'generation':2},{**run,'provider_operation_key':'another-key'},{**run,'binding_digest':'f'*64},
                                    {**run,'account_id':'another-account'},{**run,'window_id':'another-window'}):
                        with self.assertRaises(InvalidValue):
                            resolve_known(stored, source, altered, completion, operation, 2)
                    with self.assertRaises(InvalidValue):
                        resolve_known(stored, source, run, object.__new__(ConfirmedCompletion), operation, 2)
                    if accepted:
                        now=time.time_ns()//1000
                        review_key={**operation,'operation_kind':'review_initial_persona','operation_key':'review'}
                        reviewed=review(resolved.run,resolved.candidate,cast(int,resolved.run['revision']),1,
                            candidate_digest(resolved.candidate),'APPROVE','operator',review_key,now)
                        publish_key={**operation,'operation_kind':'publish_initial_persona','operation_key':'publish'}
                        published=publish_approved(stored,source,reviewed.run,reviewed.candidate,cast(int,reviewed.run['revision']),2,
                            candidate_digest(reviewed.candidate),completion,publish_key,now)
                        self.assertEqual(published.run['state'],'PUBLISHED')
                        self.assertEqual(published.publication['text'],payload['text'])
                        self.assertEqual(published.publication['requested_model_id'],'ark-code-latest')
                        self.assertEqual(published.publication['reported_model_id'],'fixture_backend')
                        self.assertEqual(published.publication['review_operation'],review_key)
                        self.assertLessEqual(cast(int,published.publication['generated_at_us']),now)
                        for altered in ({**reviewed.candidate,'text':'Edited after review','text_digest':digest('Edited after review')},
                                        {**reviewed.candidate,'terminal_receipt':{**completion.operation,'operation_key':'another-completion'}}):
                            with self.assertRaises(InvalidValue):
                                publish_approved(stored,source,reviewed.run,altered,cast(int,reviewed.run['revision']),2,
                                    candidate_digest(isolate_candidate(altered)),completion,publish_key,now)
                        with self.assertRaises(InvalidValue):
                            publish_approved(stored,source,resolved.run,resolved.candidate,cast(int,resolved.run['revision']),1,
                                candidate_digest(resolved.candidate),completion,publish_key,now)
                    self.assertEqual(len(requests), 1); self.assertEqual(failures, [])
                finally:
                    await fixture.close()
