"""Real Provider ledger evidence transformed into stable local memory proposals.

The controlled server supplies synthetic output; the configuration, original
request, completion audit and native result-owner attestation are actual.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from typing import cast
from companion_memory.persistence.schema import Value
import unittest
from companion_memory.cognition.text_candidates import TextCandidateInput
from companion_memory.cognition.text_context import freeze_context
from companion_memory.cognition.text_resources import render_learning_instructions, output_schema
from companion_memory.memory.formats import record
from companion_memory.memory.sources import source_digest, isolate_source
from companion_memory.provider import WorkGrant, Ready, Completed, ResultGrant
from companion_memory.provider.service import OPTIONALS, derived_id
from companion_memory.provider.terminal_evidence import TerminalVerified
from companion_memory.provider.chat_protocol import ChatBinding
from companion_memory.provider.values import freeze, as_record
from companion_memory.ingress.events import plain
from tests.cognition.test_text_context import inputs
from tests.provider.test_chat_transport import server
from tests.text_learning.provider_support import Fixture
from tests.text_learning.test_provider import response


def proposal():
    return {'action':'CREATE_MEMORY','category':'FACT','body':'An explicit quoted message was supplied.',
        'subject_ids':['speaker'],'speaker_subject_id':'speaker','stance':'ASSERTED',
        'world_scope':{'kind':'REAL','context_id':None},'occurred_range':None,'applicable_range':None,
        'belief':90,'belief_reason':'The complete target message is retained.',
        'target_anchors':[{'message_id':'message','part':'BODY','item_index':None,'start_utf8':0,'end_utf8':6}],
        'auxiliary_refs':[],'basis_refs':[]}


class TextCandidateTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_audited_output_builds_stable_zero_one_eight_and_rejects_invalid_entire_set(self):
        for count, invalid in ((0,False),(1,False),(8,False),(1,True)):
            memories=[proposal() for _ in range(count)]
            if invalid:memories[0]['target_anchors'][0]['message_id']='not-in-original-source'
            output={'schema_version':1,'memories':memories}
            with self.subTest(count=count,invalid=invalid),TemporaryDirectory() as directory,server(response(json.dumps(output))) as (port,requests,failures):
                grant=WorkGrant('cognition','instance',None,'LEARNING',('fixture_generation',),('GENERATION',),'cognition','scheduler',('run',),
                    entry_ids=('entry',),batch_ids=('batch',),prompt_revisions=('learning_prompt',))
                fixture=Fixture(Path(directory),port,grant)
                try:
                    self.assertIs(type(await fixture.initialize()),Ready)
                    stored=fixture.stored;assert stored is not None
                    args=inputs();context,identity=args[0],args[1]
                    selected=fixture.candidate.text.record('provider.generation')
                    context['system_text']=render_learning_instructions([{'subject_id':'speaker','revision':1,'kind':'PLATFORM_PERSON'}],[{'kind':'REAL','context_id':None}])
                    context['user']['identity'].update(database_id=stored.database_id,config_snapshot_id=stored.snapshot_id)
                    context['resources']={name:selected[name] for name in context['resources']}
                    context['model_binding'].update(profile_id='fixture_generation',config_snapshot_id=stored.snapshot_id,
                        profile_revision=derived_id('profile',stored.snapshot_id,'fixture_generation'),price_revision='fixture_price',
                        capability_evidence_ref=selected['capability_evidence_ref'],billing_evidence_ref=selected['billing_evidence_ref'])
                    identity.update(profile_id='fixture_generation',batch_id='batch')
                    binding=ChatBinding('ark-code-latest',('ark-code-latest','fixture_backend'),None,'learning_schema',str(selected['schema_digest']),'text_learning',output_schema('LEARNING'))
                    frozen=freeze_context(context,identity,args[2],args[3],args[4],args[5],binding)
                    result=await fixture.work.generate({**cast(dict,plain(frozen.request)),'deadline':time.monotonic()+3,'cancellation':fixture.cancellation.token})
                    self.assertIs(type(result),Completed,result);assert type(result) is Completed
                    self.assertEqual(result.record['outcome'],'SUCCEEDED')
                    original={name:frozen.request.get(name) for name in OPTIONALS}
                    original.update({name:frozen.request[name] for name in ('operation_key','run_id','profile_id','payload','entry_ids')})
                    owner=fixture.service.bind_result_owner(ResultGrant('cognition',(str(result.record['object_id']),)))
                    attested=await owner.verify_terminal(result.record['object_id'],as_record(freeze(original,131072,owned=True)))
                    self.assertIs(type(attested),TerminalVerified,attested);assert type(attested) is TerminalVerified
                    member=context['user']['members'][0]['member']
                    source={'source_version':1,'source_id':'source','batch_id':'batch','run_id':'run','entry_id':'entry','host_id':'host',
                        'platform_id':'sample_platform','config_snapshot_id':stored.snapshot_id,'domain_revisions':[{'domain_id':'runtime','revision':1}],
                        'material_contract_ref':'text_material','frozen_at_us':1,'ordered_members':[{'role':'T','message_id':'message','entry_seq':1,
                        'payload_digest':member['payload_digest'],'received_at_us':1,'transferred_at_us':None,'media':[]}],'digest':'pending'}
                    source['digest']=source_digest(record(cast(Value,freeze(source,8192,owned=True))))
                    original_source=isolate_source(source)
                    transform=TextCandidateInput(stored)
                    candidate=transform.build(original_source,1,attested.value,frozen)
                    from companion_memory.persistence.schema import InvalidValue
                    from dataclasses import replace
                    from types import MappingProxyType
                    altered=replace(frozen,context=MappingProxyType({**frozen.context,'system_text':render_learning_instructions(
                        [{'subject_id':'speaker','revision':1,'kind':'PLATFORM_PERSON'},{'subject_id':'unapproved','revision':1,'kind':'PLATFORM_PERSON'}],
                        [{'kind':'REAL','context_id':None}])}))
                    for invalid_context in (altered,replace(frozen,wire=frozen.wire+b' '),
                            replace(frozen,leaves=()),replace(frozen,request=MappingProxyType({**frozen.request,'operation_key':'different'}))):
                        with self.assertRaises(InvalidValue):
                            transform.build(original_source,1,attested.value,invalid_context)
                    changed_source={**original_source,'entry_id':'another-entry'}
                    changed_source['digest']=source_digest(record(cast(Value,freeze(changed_source,8192,owned=True))))
                    with self.assertRaises(InvalidValue):
                        transform.build(changed_source,1,attested.value,frozen)
                    self.assertEqual(candidate,transform.build(original_source,1,attested.value,frozen))
                    self.assertEqual(candidate.manifest['terminal_proposal'],'FAILED_DROPPED' if invalid else 'SUCCEEDED')
                    self.assertEqual(len(candidate.leaves),0 if invalid else count)
                    for leaf in candidate.leaves:
                        obj=record(leaf['proposed_value']);self.assertEqual(obj['object_version'],2)
                        self.assertEqual(record(obj['origin'])['model_origin'],'REMOTE_PROVIDER')
                    self.assertEqual(len(requests),1);self.assertEqual(failures,[])
                finally:await fixture.close()
